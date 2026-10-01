from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class Interval(BaseModel):
    """左闭右开 [start, end)，按 Unicode 码点计。"""

    start: int = Field(ge=0)
    end: int = Field(ge=1)


class RecordCreateRequest(BaseModel):
    text: str
    intervals: list[Interval] = Field(default_factory=list)


class RecordReviseRequest(BaseModel):
    base_version: int = Field(ge=1)
    text: str
    intervals: list[Interval] = Field(default_factory=list)


class PublishRequest(BaseModel):
    version: int = Field(ge=1)
    # 两种到期方式二选一，均不填则令牌永不到期
    expires_in_seconds: int | None = Field(default=None, gt=0)
    expires_at: datetime | None = None
    # 可选的成功读取次数上限（正整数）；不填表示不限次。
    # 直接发布时确定；受控发布在提交申请时冻结，批准签发后生效。
    max_reads: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _only_one_expiry(self):
        if self.expires_in_seconds is not None and self.expires_at is not None:
            raise ValueError("use only one of expires_in_seconds and expires_at")
        return self


class ControlledPublishRequest(PublishRequest):
    """受控发布申请：复用既有发布的版本与令牌到期字段。"""


class ReviewRejectRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


class SnapshotWithdrawRequest(BaseModel):
    # 撤回原因：首次撤回必填（非空白），重复撤回时沿用首次结果、忽略本次内容
    reason: str | None = Field(default=None, max_length=2000)


class SnapshotWithdrawResponse(BaseModel):
    """撤回结果：重复撤回也返回首次撤回的时间与状态。"""

    snapshot_id: UUID
    withdrawn_at: datetime
    status: str


class RecordResponse(BaseModel):
    record_id: UUID
    version: int


class ControlledFlagResponse(BaseModel):
    record_id: UUID
    controlled: bool


class SnapshotInfo(BaseModel):
    snapshot_id: UUID
    version: int
    status: str
    published_at: datetime
    # 维护侧状态：撤回时间与原因；未撤回时均为 null（原因不向公开侧透露）
    withdrawn_at: datetime | None = None
    withdraw_reason: str | None = None


class RecordStatusResponse(BaseModel):
    record_id: UUID
    current_version: int
    controlled: bool
    snapshots: list[SnapshotInfo]


class PublishResponse(BaseModel):
    snapshot_id: UUID
    record_id: UUID
    version: int
    status: str
    token: str
    expires_at: datetime | None
    # 该令牌的成功读取次数上限；null 表示不限次
    max_reads: int | None = None


class TokenStatusResponse(BaseModel):
    """维护侧令牌状态：只含计数与元数据，绝不返回快照原文。"""

    token: str
    snapshot_id: UUID
    record_id: UUID
    version: int
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    # null 表示不限次；此时 remaining_reads 也为 null
    max_reads: int | None
    reads_used: int
    remaining_reads: int | None
    # 便于维护侧排查：invalid/expired 等状态仅用于维护接口，不影响公开侧的模糊拒绝
    active: bool


class RevokeResponse(BaseModel):
    token: str
    revoked: bool
    already_revoked: bool


class PublicSnapshotResponse(BaseModel):
    record_id: UUID
    version: int
    status: str
    redacted_text: str


class PublishRequestInfo(BaseModel):
    """申请详情：只含元数据，绝不返回冻结原文。"""

    request_id: UUID
    record_id: UUID
    version: int
    status: str
    requested_by: str
    reviewed_by: str | None
    reason: str | None
    # 提交时选择的令牌到期策略；批准后 token_expires_at 为实际绝对到期时刻
    token_expires_in_seconds: int | None
    token_expires_at: datetime | None
    # 申请时冻结的读取次数上限；null 表示不限次
    token_max_reads: int | None
    snapshot_id: UUID | None
    created_at: datetime
    decided_at: datetime | None


class PublishRequestListResponse(BaseModel):
    record_id: UUID
    requests: list[PublishRequestInfo]


class ControlledSubmitResponse(BaseModel):
    request_id: UUID
    record_id: UUID
    version: int
    status: str
    requested_by: str
    created_at: datetime


class ApproveResponse(BaseModel):
    request_id: UUID
    record_id: UUID
    version: int
    status: str
    reviewed_by: str
    snapshot_id: UUID
    token: str
    expires_at: datetime | None
    # 批准签发令牌的成功读取次数上限；null 表示不限次
    max_reads: int | None = None


# ---- 多快照批量凭证 ----


class BundleSubmitRequest(BaseModel):
    """批量授权申请：至少两个互不重复的已发布快照 ID，加共用读取总额度。"""

    snapshot_ids: list[UUID] = Field(min_length=2)
    # 两种到期方式二选一，均不填则凭证永不到期
    expires_in_seconds: int | None = Field(default=None, gt=0)
    expires_at: datetime | None = None
    # 清单共用的成功读取总额度：必填正整数
    max_reads: int = Field(gt=0)

    @model_validator(mode="after")
    def _only_one_expiry_and_distinct_snapshots(self):
        if self.expires_in_seconds is not None and self.expires_at is not None:
            raise ValueError("use only one of expires_in_seconds and expires_at")
        if len(set(self.snapshot_ids)) != len(self.snapshot_ids):
            raise ValueError("snapshot_ids must not contain duplicates")
        return self


class BundleRequestSnapshotInfo(BaseModel):
    """批量申请/凭证清单内的单个快照条目（只含元数据，不含正文）。"""

    position: int
    snapshot_id: UUID
    record_id: UUID | None = None
    version: int | None = None
    # 实时维护状态：published / withdrawn；快照行缺失（理论上不会）时为 null
    status: str | None = None
    withdrawn_at: datetime | None = None


class BundleRequestInfo(BaseModel):
    """批量申请详情：只含元数据与清单，绝不返回快照正文或凭证。"""

    request_id: UUID
    status: str
    requested_by: str
    reviewed_by: str | None
    reason: str | None
    token_expires_in_seconds: int | None
    token_expires_at: datetime | None
    # 提交时冻结的共用读取总额度（正整数）
    token_max_reads: int
    snapshots: list[BundleRequestSnapshotInfo]
    created_at: datetime
    decided_at: datetime | None = None


class BundleApproveResponse(BaseModel):
    request_id: UUID
    status: str
    reviewed_by: str
    snapshots: list[BundleRequestSnapshotInfo]
    # 批准成功后一次性签发的随机批量凭证
    token: str
    expires_at: datetime | None
    # 清单共用的成功读取总额度
    max_reads: int


class BundleTokenSnapshotStatus(BundleRequestSnapshotInfo):
    """维护侧凭证清单条目：额外给出快照当前维护状态（不返回正文）。"""


class BundleTokenStatusResponse(BaseModel):
    """维护侧批量凭证状态：清单、共用额度与有效性（绝不返回快照正文）。"""

    token: str
    request_id: UUID
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    max_reads: int
    reads_used: int
    remaining_reads: int
    active: bool
    snapshots: list[BundleTokenSnapshotStatus]
