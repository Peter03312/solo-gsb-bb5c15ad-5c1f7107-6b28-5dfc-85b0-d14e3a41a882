import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Record(Base):
    """一条脱敏记录的当前状态；历史原文存放在 record_versions。"""

    __tablename__ = "records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 受控记录：发布必须走“申请-复核”流程，直接发布请求一律拒绝
    controlled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class RecordVersion(Base):
    """某次修订提交的原文与敏感区间，按 (record_id, version) 唯一。"""

    __tablename__ = "record_versions"

    record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("records.id", ondelete="CASCADE"), primary_key=True
    )
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    original_text: Mapped[str] = mapped_column(Text, nullable=False)
    # JSONB 数组，元素为 {"start": int, "end": int}，左闭右开、按 Unicode 码点计
    intervals: Mapped[list] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Snapshot(Base):
    """发布产生的不可变快照：只保存脱敏后的正文，后续修订不影响它。"""

    __tablename__ = "snapshots"
    __table_args__ = (
        UniqueConstraint("record_id", "version", name="uq_snapshots_record_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("records.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    redacted_text: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # 永久撤回：withdrawn_at 为空表示仍公开；非空即永久不可公开读取。
    # 撤回只作用于本快照：同记录其他版本、历史脱敏正文与已批准申请均继续留存。
    withdrawn_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 撤回原因仅维护侧可见，公开侧的模糊 403 绝不透露它
    withdraw_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class ReadToken(Base):
    """快照的随机读取令牌，可设到期时间、可撤销，并可设成功读取次数上限。

    - max_reads 为空表示不限次数（既有及新令牌默认如此）；为正整数时，
      每次凭本令牌成功读取消耗一次，达到上限后的读取沿用既有模糊拒绝。
    - reads_used 记录已消耗次数；不同令牌各自计数，互不影响。
    """

    __tablename__ = "read_tokens"
    __table_args__ = (
        CheckConstraint("max_reads IS NULL OR max_reads > 0", name="ck_read_tokens_max_reads_positive"),
        CheckConstraint("reads_used >= 0", name="ck_read_tokens_reads_used_non_negative"),
        CheckConstraint(
            "max_reads IS NULL OR reads_used <= max_reads",
            name="ck_read_tokens_reads_used_within_limit",
        ),
    )

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("snapshots.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 成功读取次数上限：NULL 表示不限次；冻结后不随申请变化
    max_reads: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 已成功读取次数；由条件 UPDATE 原子递增，保证并发读取不超过上限
    reads_used: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


# 受控发布申请的终态
REQUEST_PENDING = "pending"
REQUEST_APPROVED = "approved"
REQUEST_REJECTED = "rejected"
REQUEST_STATUSES = (REQUEST_PENDING, REQUEST_APPROVED, REQUEST_REJECTED)


class PublishRequest(Base):
    """受控发布申请：绑定提交时刻的历史版本内容（原文与区间在此冻结）。

    - pending：待复核，可被批准或驳回
    - approved / rejected：终态，终态唯一、不可变更
    """

    __tablename__ = "publish_requests"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'approved', 'rejected')", name="ck_publish_requests_status"),
        Index("ix_publish_requests_record", "record_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("records.id", ondelete="CASCADE"), nullable=False
    )
    # 申请目标版本；待审内容冻结在 frozen_* 字段，后续修订不改变它
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    frozen_text: Mapped[str] = mapped_column(Text, nullable=False)
    frozen_intervals: Mapped[list] = mapped_column(JSONB, nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=REQUEST_PENDING, default=REQUEST_PENDING
    )
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 令牌到期策略（提交时确定）：相对时长在批准签发时才换算成绝对时刻
    token_expires_in_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 读取次数上限（申请时冻结）：NULL 表示不限次，批准后原样落到新令牌
    token_max_reads: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 批准后同事务生成的快照（驳回时保持为空；令牌经 snapshot_id 关联，不在此冗余）
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("snapshots.id", ondelete="CASCADE"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---- 多快照批量凭证（同一外部审阅者） ----


class BundleRequest(Base):
    """多快照批量授权申请：提交时冻结快照清单，另一名 reviewer 复核。

    - pending：待复核，可被批准或驳回
    - approved / rejected：终态，终态唯一、不可变更
    快照清单冻结在 bundle_request_items 中；读取总额度（正整数）在提交时冻结，
    批准后一次性签发绑定清单的批量随机凭证。
    """

    __tablename__ = "bundle_requests"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'approved', 'rejected')", name="ck_bundle_requests_status"),
        # 总额度为必填的正整数（批量凭证不允许不限次）
        CheckConstraint("token_max_reads IS NOT NULL AND token_max_reads > 0", name="ck_bundle_requests_max_reads_positive"),
        Index("ix_bundle_requests_requested_by", "requested_by", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=REQUEST_PENDING, default=REQUEST_PENDING
    )
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 凭证到期策略（提交时确定）：相对时长在批准签发时才换算成绝对时刻
    token_expires_in_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 清单内全部快照共用的成功读取总额度（正整数，提交时冻结）
    token_max_reads: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BundleRequestItem(Base):
    """批量申请冻结的快照清单条目：按提交顺序保存，批准时原样核对。"""

    __tablename__ = "bundle_request_items"
    __table_args__ = (
        # 同一申请内快照不重复；position 唯一决定清单顺序
        UniqueConstraint("request_id", "snapshot_id", name="uq_bundle_request_items_snapshot"),
        UniqueConstraint("request_id", "position", name="uq_bundle_request_items_position"),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bundle_requests.id", ondelete="CASCADE"),
        primary_key=True,
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("snapshots.id", ondelete="CASCADE"),
        nullable=False,
    )


class BundleToken(Base):
    """批量读取凭证：一次批准一次性随机签发，绑定冻结快照清单，共用读取额度。

    - max_reads 为必填正整数：清单内所有快照的成功读取合计消耗同一额度；
    - reads_used 由公开读取接口内的条件 UPDATE 原子递增，并发读取绝不越限；
    - 到期、撤销或额度耗尽后，清单内全部快照一律拒绝；
    - 清单中某项快照被撤回，只拒绝该项（公开读取在快照行检查阶段即 403，
      不触碰本凭证计数），其余项仍可正常读取并共享额度。
    """

    __tablename__ = "bundle_tokens"
    __table_args__ = (
        CheckConstraint("max_reads > 0", name="ck_bundle_tokens_max_reads_positive"),
        CheckConstraint("reads_used >= 0", name="ck_bundle_tokens_reads_used_non_negative"),
        CheckConstraint("reads_used <= max_reads", name="ck_bundle_tokens_reads_used_within_limit"),
    )

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 清单共用的成功读取总额度（正整数，必填）
    max_reads: Mapped[int] = mapped_column(Integer, nullable=False)
    reads_used: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # 来源申请（一枚凭证只来自一次批准；申请批准后不允许再变更）
    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bundle_requests.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )


class BundleTokenSnapshot(Base):
    """批量凭证绑定的快照清单：凭证只能读取清单内的脱敏正文。"""

    __tablename__ = "bundle_token_snapshots"

    token: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("bundle_tokens.token", ondelete="CASCADE"),
        primary_key=True,
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("snapshots.id", ondelete="CASCADE"),
        primary_key=True,
    )
