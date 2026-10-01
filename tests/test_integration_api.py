"""受控发布端到端集成测试：需要真实 PostgreSQL。

默认尝试用 pgserver（pip 包，内置 Postgres 16 二进制）自动拉起临时库；
也可通过环境变量 TEST_DATABASE_URL 指定已有数据库。

运行：
    python3 -m unittest tests.test_integration_api -v
"""

import json
import os
import threading
import unittest
from datetime import datetime, timezone

# 环境变量必须在导入 app.config 之前设置
os.environ.setdefault("SERVICE_KEY", "dev-service-key-change-me")
os.environ.setdefault(
    "REVIEW_CREDENTIALS",
    json.dumps(
        [
            {"username": "alice", "password": "alice-pw", "roles": ["operator"]},
            {"username": "bob", "password": "bob-pw", "roles": ["reviewer"]},
            {"username": "carol", "password": "carol-pw", "roles": ["operator", "reviewer"]},
        ]
    ),
)

_PG_SERVER = None
_SKIP_REASON = ""
if "TEST_DATABASE_URL" not in os.environ:
    try:
        import pgserver

        _PG_SERVER = pgserver.get_server("/tmp/pgdata-integration-test")
        # unix socket 形式：postgresql+psycopg2://postgres@/postgres?host=<socketdir>
        os.environ["DATABASE_URL"] = (
            "postgresql+psycopg2://postgres@/postgres?host=/tmp/pgdata-integration-test"
        )
    except Exception as exc:  # pragma: no cover - 环境不可用时跳过
        _PG_SERVER = None
        _SKIP_REASON = f"pgserver unavailable: {exc}"
else:
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]

from fastapi.testclient import TestClient  # noqa: E402

from app import main as app_main  # noqa: E402
from app.models import PublishRequest as PublishRequestModel  # noqa: E402

SKIP_INTEGRATION = _PG_SERVER is None and "TEST_DATABASE_URL" not in os.environ

SERVICE_HEADERS = {"X-Service-Key": "dev-service-key-change-me"}
BASIC_ALICE = ("alice", "alice-pw")
BASIC_BOB = ("bob", "bob-pw")
BASIC_CAROL = ("carol", "carol-pw")

TEXT_V1 = "患者张三，身份证号 110101199003077777，联系电话 13800001111。"
TEXT_V2 = "完全不同的新原文，敏感词ABC。"
INTERVALS_V1 = [{"start": 2, "end": 4}, {"start": 10, "end": 28}, {"start": 34, "end": 45}]


@unittest.skipIf(SKIP_INTEGRATION, _SKIP_REASON)
class ControlledPublishIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 建表（幂等）并通过 TestClient 上下文触发 lifespan 之外的显式初始化
        app_main._init_db_with_retry()
        cls.client = TestClient(app_main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        # 每个用例使用全新记录，避免相互干扰
        r = self.client.post(
            "/records",
            headers=SERVICE_HEADERS,
            json={"text": TEXT_V1, "intervals": INTERVALS_V1},
        )
        self.assertEqual(r.status_code, 201, r.text)
        self.record_id = r.json()["record_id"]

    # ---- 工具方法 ----

    def _mark_controlled(self):
        r = self.client.put(f"/records/{self.record_id}/controlled", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["controlled"])

    def _submit(self, auth=BASIC_ALICE, version=1, expires=None, expected=201):
        body = {"version": version}
        if expires is not None:
            body.update(expires)
        r = self.client.post(
            f"/records/{self.record_id}/publish-requests", auth=auth, json=body
        )
        self.assertEqual(r.status_code, expected, r.text)
        return r

    # ---- 基础鉴权 ----

    def test_healthz(self):
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)

    def test_service_key_endpoints_still_require_key(self):
        r = self.client.post("/records", json={"text": "x", "intervals": []})
        self.assertEqual(r.status_code, 401)
        r = self.client.put(f"/records/{self.record_id}/controlled")
        self.assertEqual(r.status_code, 401)

    def test_controlled_endpoints_require_basic_auth(self):
        self._mark_controlled()
        r = self.client.post(
            f"/records/{self.record_id}/publish-requests", json={"version": 1}
        )
        self.assertEqual(r.status_code, 401)
        self.assertIn("Basic", r.headers["WWW-Authenticate"])
        # 错误口令
        r = self.client.post(
            f"/records/{self.record_id}/publish-requests",
            auth=("alice", "wrong"),
            json={"version": 1},
        )
        self.assertEqual(r.status_code, 401)
        # 未知用户
        r = self.client.post(
            f"/records/{self.record_id}/publish-requests",
            auth=("nobody", "x"),
            json={"version": 1},
        )
        self.assertEqual(r.status_code, 401)

    # ---- 受控标记与直接发布互斥 ----

    def test_mark_controlled_is_idempotent(self):
        self._mark_controlled()
        r = self.client.put(f"/records/{self.record_id}/controlled", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 200)
        r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
        self.assertTrue(r.json()["controlled"])

    def test_mark_controlled_unknown_record_404(self):
        import uuid

        r = self.client.put(f"/records/{uuid.uuid4()}/controlled", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 404)

    def test_direct_publish_rejected_for_controlled_record(self):
        self._mark_controlled()
        r = self.client.post(
            f"/records/{self.record_id}/publish",
            headers=SERVICE_HEADERS,
            json={"version": 1},
        )
        self.assertEqual(r.status_code, 409)
        self.assertNotIn(TEXT_V1, r.text)
        # 未留下任何快照或令牌
        r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
        self.assertEqual(r.json()["snapshots"], [])

    def test_submit_rejected_for_non_controlled_record(self):
        r = self._submit(expected=409)
        self.assertNotIn(TEXT_V1, r.text)

    # ---- 角色校验 ----

    def test_operator_role_required_to_submit(self):
        self._mark_controlled()
        # bob 只有 reviewer 角色，不能提交
        r = self._submit(auth=BASIC_BOB, expected=403)
        self.assertNotIn(TEXT_V1, r.text)

    def test_reviewer_role_required_to_approve(self):
        self._mark_controlled()
        self._submit()
        # alice 只有 operator 角色，不能复核
        r = self.client.post(
            f"/publish-requests/{self._latest_request_id()}/approve", auth=BASIC_ALICE
        )
        self.assertEqual(r.status_code, 403)

    def _latest_request_id(self):
        r = self.client.get(
            f"/records/{self.record_id}/publish-requests", auth=BASIC_ALICE
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["requests"][-1]["request_id"]

    # ---- 未知资源 ----

    def test_unknown_record_and_version(self):
        import uuid

        self._mark_controlled()
        r = self.client.post(
            f"/records/{uuid.uuid4()}/publish-requests", auth=BASIC_ALICE, json={"version": 1}
        )
        self.assertEqual(r.status_code, 404)
        r = self._submit(version=99, expected=404)
        self.assertNotIn(TEXT_V1, r.text)

    def test_unknown_request_approve_reject_get(self):
        import uuid

        rid = str(uuid.uuid4())
        self.assertEqual(
            self.client.post(f"/publish-requests/{rid}/approve", auth=BASIC_BOB).status_code, 404
        )
        self.assertEqual(
            self.client.post(f"/publish-requests/{rid}/reject", auth=BASIC_BOB, json={}).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(f"/publish-requests/{rid}", auth=BASIC_BOB).status_code, 404
        )

    # ---- 申请冻结版本内容 ----

    def test_request_freezes_version_content_at_submission(self):
        self._mark_controlled()
        self._submit(version=1)
        request_id = self._latest_request_id()
        # 提交后修订到版本 2
        r = self.client.put(
            f"/records/{self.record_id}",
            headers=SERVICE_HEADERS,
            json={"base_version": 1, "text": TEXT_V2, "intervals": [{"start": 9, "end": 15}]},
        )
        self.assertEqual(r.status_code, 200, r.text)
        # 批准沿用冻结的版本 1 内容
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["version"], 1)
        self.assertEqual(body["status"], "approved")
        token = body["token"]
        snapshot_id = body["snapshot_id"]
        # 公开读取得到版本 1 的脱敏正文，而非版本 2 原文
        r = self.client.get(f"/public/snapshots/{snapshot_id}?token={token}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            r.json()["redacted_text"],
            "患者[已遮蔽]，身份证号 [已遮蔽]，联系电话 [已遮蔽]。",
        )
        self.assertNotIn(TEXT_V2, r.text)
        # 申请详情不泄露冻结原文
        r = self.client.get(f"/publish-requests/{request_id}", auth=BASIC_ALICE)
        self.assertNotIn(TEXT_V1, r.text)
        self.assertNotIn("身份证", r.text)

    def test_approved_token_follows_existing_token_rules(self):
        self._mark_controlled()
        self._submit(version=1, expires={"expires_in_seconds": 3600})
        request_id = self._latest_request_id()
        # 提交后到批准前，申请保存的是相对时长策略（尚无绝对到期时刻）
        r = self.client.get(f"/publish-requests/{request_id}", auth=BASIC_ALICE)
        self.assertEqual(r.json()["token_expires_in_seconds"], 3600)
        self.assertIsNone(r.json()["token_expires_at"])
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 200, r.text)
        token = r.json()["token"]
        snapshot_id = r.json()["snapshot_id"]
        # 到期时刻在批准签发时换算，约为批准时刻 + 3600s
        expires_at = r.json()["expires_at"]
        self.assertIsNotNone(expires_at)
        self.assertGreater(
            datetime.fromisoformat(expires_at), datetime.now(timezone.utc)
        )
        # 批准后申请记录实际绝对到期时刻
        r = self.client.get(f"/publish-requests/{request_id}", auth=BASIC_ALICE)
        self.assertIsNotNone(r.json()["token_expires_at"])
        # 令牌可通过既有撤销接口撤销，撤销后公开读取 403
        r = self.client.post(f"/tokens/{token}/revoke", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 200)
        r = self.client.get(f"/public/snapshots/{snapshot_id}?token={token}")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["detail"], "snapshot unavailable")

    def test_submit_with_past_expires_at_is_422(self):
        self._mark_controlled()
        r = self._submit(
            expires={"expires_at": "2000-01-01T00:00:00Z"}, expected=422
        )
        self.assertNotIn(TEXT_V1, r.text)

    def test_approval_failure_rolls_back_snapshot_and_token(self):
        """批准事务中途失败时，不得留下快照或可读令牌。"""
        from unittest import mock

        from sqlalchemy import select

        from app import main as main_module
        from app.db import SessionLocal
        from app.models import ReadToken as ReadTokenModel
        from app.models import Snapshot as SnapshotModel

        self._mark_controlled()
        self._submit()
        request_id = self._latest_request_id()

        # 让令牌在 flush 时超长（read_tokens.token 为 varchar(64)）：
        # 此时快照 INSERT 已执行、令牌已加入事务，但尚未提交。
        with mock.patch.object(
            main_module.secrets, "token_urlsafe", return_value="x" * 65
        ):
            with self.assertRaises(Exception):
                self.client.post(
                    f"/publish-requests/{request_id}/approve", auth=BASIC_BOB
                )

        with SessionLocal() as session:
            # 申请仍为 pending（终态未写入），快照与令牌全部回滚
            pr = session.get(PublishRequestModel, request_id)
            self.assertEqual(pr.status, "pending")
            self.assertIsNone(pr.snapshot_id)
            snapshots = (
                session.query(SnapshotModel)
                .filter(SnapshotModel.record_id == self.record_id)
                .all()
            )
            self.assertEqual(snapshots, [])
            orphan_tokens = (
                session.query(ReadTokenModel)
                .filter(
                    ReadTokenModel.snapshot_id.in_(
                        select(SnapshotModel.id).where(
                            SnapshotModel.record_id == self.record_id
                        )
                    )
                )
                .count()
            )
            self.assertEqual(orphan_tokens, 0)
        # 申请仍可被正常批准（证明前一次没有遗留半完成状态）
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 200, r.text)

    def test_explicit_expires_at_is_kept_at_approval(self):
        from datetime import timedelta

        self._mark_controlled()
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        self._submit(expires={"expires_at": future})
        request_id = self._latest_request_id()
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNotNone(r.json()["expires_at"])

    # ---- 申请人不得复核 ----

    def test_requester_cannot_review_own_request(self):
        # carol 同时具备 operator/reviewer，仍不得复核自己的申请
        self._mark_controlled()
        r = self._submit(auth=BASIC_CAROL)
        request_id = r.json()["request_id"]
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_CAROL)
        self.assertEqual(r.status_code, 403)
        r = self.client.post(
            f"/publish-requests/{request_id}/reject", auth=BASIC_CAROL, json={}
        )
        self.assertEqual(r.status_code, 403)
        # 申请仍处于 pending，他人可正常批准
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 200, r.text)

    # ---- 终态语义 ----

    def test_rejected_request_cannot_be_approved(self):
        self._mark_controlled()
        self._submit()
        request_id = self._latest_request_id()
        r = self.client.post(
            f"/publish-requests/{request_id}/reject",
            auth=BASIC_BOB,
            json={"reason": "材料不全"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "rejected")
        # 驳回后不能批准
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 409)
        # 再次驳回也是 409
        r = self.client.post(
            f"/publish-requests/{request_id}/reject", auth=BASIC_BOB, json={}
        )
        self.assertEqual(r.status_code, 409)
        # 驳回未生成快照/令牌
        r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
        self.assertEqual(r.json()["snapshots"], [])

    def test_approved_request_cannot_be_decided_again(self):
        self._mark_controlled()
        self._submit()
        request_id = self._latest_request_id()
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            self.client.post(
                f"/publish-requests/{request_id}/approve", auth=BASIC_BOB
            ).status_code,
            409,
        )
        self.assertEqual(
            self.client.post(
                f"/publish-requests/{request_id}/reject", auth=BASIC_BOB, json={}
            ).status_code,
            409,
        )

    def test_concurrent_reviews_produce_single_terminal_state(self):
        """两个线程同时复核同一申请：恰好一个终态成功，另一个 409。"""
        self._mark_controlled()
        for _ in range(5):
            self._submit()
            request_id = self._latest_request_id()
            results: list[int] = []
            barrier = threading.Barrier(2)

            def approve():
                barrier.wait()
                client = TestClient(app_main.app)
                results.append(
                    client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB).status_code
                )

            def reject():
                barrier.wait()
                client = TestClient(app_main.app)
                results.append(
                    client.post(
                        f"/publish-requests/{request_id}/reject", auth=BASIC_CAROL, json={}
                    ).status_code
                )

            t1 = threading.Thread(target=approve)
            t2 = threading.Thread(target=reject)
            t1.start(); t2.start(); t1.join(); t2.join()
            self.assertEqual(len(results), 2)
            self.assertEqual(sorted(results), [200, 409], results)
            # 库里申请唯一终态，且至多一份快照
            from app.db import SessionLocal
            from app.models import Snapshot

            with SessionLocal() as session:
                pr = session.get(PublishRequestModel, request_id)
                self.assertIn(pr.status, ("approved", "rejected"))
                self.assertIsNotNone(pr.reviewed_by)
            r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
            self.assertLessEqual(len(r.json()["snapshots"]), 1)

    # ---- 既有接口兼容性（非受控记录） ----

    def test_legacy_flow_unchanged_on_non_controlled_record(self):
        # 直接发布非受控记录仍可用
        r = self.client.post(
            f"/records/{self.record_id}/publish",
            headers=SERVICE_HEADERS,
            json={"version": 1},
        )
        self.assertEqual(r.status_code, 201, r.text)
        token = r.json()["token"]
        snapshot_id = r.json()["snapshot_id"]
        r = self.client.get(f"/public/snapshots/{snapshot_id}?token={token}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            r.json()["redacted_text"],
            "患者[已遮蔽]，身份证号 [已遮蔽]，联系电话 [已遮蔽]。",
        )
        # 状态接口新增字段不破坏既有结构
        r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
        body = r.json()
        self.assertFalse(body["controlled"])
        self.assertEqual(body["current_version"], 1)
        self.assertEqual(len(body["snapshots"]), 1)

    def test_legacy_revision_409_and_422_still_work(self):
        r = self.client.put(
            f"/records/{self.record_id}",
            headers=SERVICE_HEADERS,
            json={"base_version": 99, "text": "x", "intervals": []},
        )
        self.assertEqual(r.status_code, 409)
        r = self.client.put(
            f"/records/{self.record_id}",
            headers=SERVICE_HEADERS,
            json={"base_version": 1, "text": "短", "intervals": [{"start": 0, "end": 99}]},
        )
        self.assertEqual(r.status_code, 422)

    def test_public_read_unified_403_does_not_leak(self):
        import uuid

        r = self.client.get(
            f"/public/snapshots/{uuid.uuid4()}?token=definitely-not-a-token"
        )
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["detail"], "snapshot unavailable")

    # ---- 读取次数上限（直接发布） ----

    def _direct_publish(self, version=1, body_extra=None):
        body = {"version": version}
        if body_extra:
            body.update(body_extra)
        r = self.client.post(
            f"/records/{self.record_id}/publish",
            headers=SERVICE_HEADERS,
            json=body,
        )
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def test_direct_publish_response_reports_max_reads(self):
        pub = self._direct_publish(body_extra={"max_reads": 2})
        self.assertEqual(pub["max_reads"], 2)
        # 不设上限时该字段为 None（再次发布复用同一快照，签出新令牌）
        pub_unlimited = self._direct_publish()
        self.assertIsNone(pub_unlimited["max_reads"])
        self.assertEqual(pub_unlimited["snapshot_id"], pub["snapshot_id"])

    def test_max_reads_must_be_positive_integer(self):
        for bad in (0, -1):
            r = self.client.post(
                f"/records/{self.record_id}/publish",
                headers=SERVICE_HEADERS,
                json={"version": 1, "max_reads": bad},
            )
            self.assertEqual(r.status_code, 422, r.text)
        # 非整数同样 422
        r = self.client.post(
            f"/records/{self.record_id}/publish",
            headers=SERVICE_HEADERS,
            json={"version": 1, "max_reads": 1.5},
        )
        self.assertEqual(r.status_code, 422, r.text)

    def test_unlimited_token_can_be_read_repeatedly(self):
        pub = self._direct_publish()
        self.assertIsNone(pub["max_reads"])
        sid, tok = pub["snapshot_id"], pub["token"]
        for _ in range(7):
            r = self.client.get(f"/public/snapshots/{sid}?token={tok}")
            self.assertEqual(r.status_code, 200, r.text)
        r = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 200, r.text)
        status_body = r.json()
        self.assertIsNone(status_body["max_reads"])
        self.assertIsNone(status_body["remaining_reads"])
        self.assertEqual(status_body["reads_used"], 7)
        self.assertTrue(status_body["active"])

    def test_capped_token_allows_exactly_max_reads_then_403(self):
        pub = self._direct_publish(body_extra={"max_reads": 2})
        sid, tok = pub["snapshot_id"], pub["token"]
        # 前两次成功
        for expected_used in (1, 2):
            r = self.client.get(f"/public/snapshots/{sid}?token={tok}")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("redacted_text", r.json())
            q = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS).json()
            self.assertEqual(q["reads_used"], expected_used)
            self.assertEqual(q["remaining_reads"], 2 - expected_used)
        # 第三次起沿用既有模糊拒绝
        for _ in range(3):
            r = self.client.get(f"/public/snapshots/{sid}?token={tok}")
            self.assertEqual(r.status_code, 403)
            self.assertEqual(r.json()["detail"], "snapshot unavailable")
        # 用尽后计数不再增长，维护侧显示已耗尽
        q = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q["reads_used"], 2)
        self.assertEqual(q["remaining_reads"], 0)
        self.assertFalse(q["active"])
        # 维护侧响应不含原文
        r = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS)
        self.assertNotIn(TEXT_V1, r.text)

    def test_invalid_revoked_and_mismatched_requests_do_not_consume(self):
        pub = self._direct_publish(body_extra={"max_reads": 2})
        sid, tok = pub["snapshot_id"], pub["token"]
        # 第一次成功
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok}").status_code, 200
        )
        # 无效令牌不消耗
        r = self.client.get(f"/public/snapshots/{sid}?token=wrong-token")
        self.assertEqual(r.status_code, 403)
        # 令牌与快照不匹配：给同一快照再签发一个令牌，交叉使用
        other = self._read_token_for_same_snapshot_via_republish(
            sid, body_extra={"max_reads": 2}
        )
        self.assertNotEqual(other, tok)
        r = self.client.get(f"/public/snapshots/{sid}?token={other}")
        self.assertEqual(r.status_code, 200, r.text)
        # 用 other 访问一个不存在的快照：403，不消耗 other
        import uuid

        r = self.client.get(
            f"/public/snapshots/{uuid.uuid4()}?token={other}"
        )
        self.assertEqual(r.status_code, 403)
        q = self.client.get(f"/tokens/{other}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q["reads_used"], 1)
        # 撤销后读取 403，不消耗剩余次数
        self.assertEqual(
            self.client.post(f"/tokens/{tok}/revoke", headers=SERVICE_HEADERS).status_code,
            200,
        )
        r = self.client.get(f"/public/snapshots/{sid}?token={tok}")
        self.assertEqual(r.status_code, 403)
        q = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q["reads_used"], 1)
        self.assertFalse(q["active"])
        self.assertIsNotNone(q["revoked_at"])

    def _read_token_for_same_snapshot_via_republish(self, existing_snapshot_id, body_extra=None):
        """同一快照可由再次直接发布签发出另一支令牌（ON CONFLICT 复用快照）。"""
        pub = self._direct_publish(body_extra=body_extra)
        self.assertEqual(pub["snapshot_id"], existing_snapshot_id)
        return pub["token"]

    def test_distinct_tokens_on_same_snapshot_count_independently(self):
        pub1 = self._direct_publish(body_extra={"max_reads": 1})
        sid = pub1["snapshot_id"]
        tok1 = pub1["token"]
        tok2 = self._read_token_for_same_snapshot_via_republish(sid)
        # 第二次直接发布未携带 max_reads -> 新令牌不限次
        q2 = self.client.get(f"/tokens/{tok2}", headers=SERVICE_HEADERS).json()
        self.assertIsNone(q2["max_reads"])
        # tok1 用尽
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok1}").status_code, 200
        )
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok1}").status_code, 403
        )
        # tok2 不受影响，继续可读
        for _ in range(4):
            self.assertEqual(
                self.client.get(f"/public/snapshots/{sid}?token={tok2}").status_code, 200
            )
        q1 = self.client.get(f"/tokens/{tok1}", headers=SERVICE_HEADERS).json()
        q2 = self.client.get(f"/tokens/{tok2}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q1["reads_used"], 1)
        self.assertEqual(q2["reads_used"], 4)

    def test_expiry_or_revocation_takes_precedence_over_remaining_reads(self):
        pub = self._direct_publish(body_extra={"max_reads": 5, "expires_in_seconds": 1})
        sid, tok = pub["snapshot_id"], pub["token"]
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok}").status_code, 200
        )
        import time as _time

        _time.sleep(1.2)
        # 仍有剩余次数，但已到期 -> 403，不消耗
        r = self.client.get(f"/public/snapshots/{sid}?token={tok}")
        self.assertEqual(r.status_code, 403)
        q = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q["reads_used"], 1)
        self.assertFalse(q["active"])
        self.assertEqual(q["remaining_reads"], 4)

    def test_concurrent_reads_never_exceed_max_reads(self):
        pub = self._direct_publish(body_extra={"max_reads": 10})
        sid, tok = pub["snapshot_id"], pub["token"]
        successes: list[int] = []
        failures: list[int] = []
        barrier = threading.Barrier(20)

        def read_once():
            barrier.wait()
            client = TestClient(app_main.app)
            code = client.get(f"/public/snapshots/{sid}?token={tok}").status_code
            (successes if code == 200 else failures).append(code)

        threads = [threading.Thread(target=read_once) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(successes), 10, successes + failures)
        self.assertEqual(len(failures), 10)
        self.assertTrue(all(code == 403 for code in failures))
        q = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q["reads_used"], 10)
        self.assertEqual(q["remaining_reads"], 0)

    def test_token_status_unknown_token_404_and_requires_service_key(self):
        r = self.client.get("/tokens/not-a-real-token", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 404)
        r = self.client.get("/tokens/not-a-real-token")
        self.assertEqual(r.status_code, 401)

    # ---- 读取次数上限（受控发布：申请冻结、批准生效） ----

    def test_controlled_max_reads_frozen_at_submission_and_effective_after_approval(self):
        self._mark_controlled()
        self._submit(version=1, expires={"max_reads": 2})
        request_id = self._latest_request_id()
        # 申请详情可见冻结的上限（不泄露原文）
        r = self.client.get(f"/publish-requests/{request_id}", auth=BASIC_ALICE)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["token_max_reads"], 2)
        self.assertNotIn(TEXT_V1, r.text)
        # 批准签发结果携带上限
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["max_reads"], 2)
        sid, tok = r.json()["snapshot_id"], r.json()["token"]
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok}").status_code, 200
        )
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok}").status_code, 200
        )
        r = self.client.get(f"/public/snapshots/{sid}?token={tok}")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["detail"], "snapshot unavailable")

    def test_controlled_reject_creates_no_token_or_counters(self):
        from sqlalchemy import select

        from app.db import SessionLocal
        from app.models import ReadToken as ReadTokenModel
        from app.models import Snapshot as SnapshotModel

        self._mark_controlled()
        self._submit(version=1, expires={"max_reads": 3})
        request_id = self._latest_request_id()
        r = self.client.post(
            f"/publish-requests/{request_id}/reject",
            auth=BASIC_BOB,
            json={"reason": "不予发布"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["token_max_reads"], 3)
        with SessionLocal() as session:
            # 驳回不产生快照/令牌（限定本记录，不受其他用例数据影响）
            token_count = (
                session.query(ReadTokenModel)
                .filter(
                    ReadTokenModel.snapshot_id.in_(
                        select(SnapshotModel.id).where(
                            SnapshotModel.record_id == self.record_id
                        )
                    )
                )
                .count()
            )
            self.assertEqual(token_count, 0)
            snapshot_count = (
                session.query(SnapshotModel)
                .filter(SnapshotModel.record_id == self.record_id)
                .count()
            )
            self.assertEqual(snapshot_count, 0)
        # 驳回后不能批准
        self.assertEqual(
            self.client.post(
                f"/publish-requests/{request_id}/approve", auth=BASIC_BOB
            ).status_code,
            409,
        )

    def test_controlled_max_reads_must_be_positive(self):
        self._mark_controlled()
        r = self._submit(version=1, expires={"max_reads": 0}, expected=422)
        self.assertNotIn(TEXT_V1, r.text)

    # ---- 数据库升级路径 ----

    def test_existing_db_gets_controlled_column(self):
        from sqlalchemy import inspect, text

        from app.db import engine

        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE records DROP COLUMN controlled"))
        app_main._init_db_with_retry()
        inspector = inspect(engine)
        columns = {c["name"] for c in inspector.get_columns("records")}
        self.assertIn("controlled", columns)
        # 旧行默认 false，数据未损坏
        r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["controlled"])

    def test_existing_db_gets_read_limit_columns(self):
        from sqlalchemy import inspect, text

        from app.db import engine

        # 先发布一支令牌，模拟既有部署的 read_tokens / publish_requests 数据
        pub = self._direct_publish()
        token = pub["token"]
        self._mark_controlled()
        self._submit(version=1)
        request_id = self._latest_request_id()

        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE read_tokens DROP COLUMN max_reads"))
            conn.execute(text("ALTER TABLE read_tokens DROP COLUMN reads_used"))
            conn.execute(text("ALTER TABLE publish_requests DROP COLUMN token_max_reads"))
        app_main._init_db_with_retry()
        inspector = inspect(engine)
        token_columns = {c["name"] for c in inspector.get_columns("read_tokens")}
        request_columns = {c["name"] for c in inspector.get_columns("publish_requests")}
        self.assertIn("max_reads", token_columns)
        self.assertIn("reads_used", token_columns)
        self.assertIn("token_max_reads", request_columns)

        from app.db import SessionLocal
        from app.models import PublishRequest as PRModel
        from app.models import ReadToken as RTModel

        with SessionLocal() as session:
            # 既有令牌迁移后继续不限次，已用次数为 0
            existing = session.get(RTModel, token)
            self.assertIsNone(existing.max_reads)
            self.assertEqual(existing.reads_used, 0)
            pr = session.get(PRModel, request_id)
            self.assertIsNone(pr.token_max_reads)

        # 服务升级后既有令牌仍可继续不限次读取
        r = self.client.get(
            f"/public/snapshots/{pub['snapshot_id']}?token={token}"
        )
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.get(
            f"/public/snapshots/{pub['snapshot_id']}?token={token}"
        )
        self.assertEqual(r.status_code, 200, r.text)
        q = self.client.get(f"/tokens/{token}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q["reads_used"], 2)
        self.assertIsNone(q["remaining_reads"])


    # ---- 快照永久撤回 ----

    def _withdraw(self, snapshot_id, reason="误发，撤回", expected=200):
        body = {} if reason is None else {"reason": reason}
        r = self.client.post(
            f"/snapshots/{snapshot_id}/withdraw", headers=SERVICE_HEADERS, json=body
        )
        self.assertEqual(r.status_code, expected, r.text)
        return r

    def test_withdraw_requires_service_key_and_unknown_is_404(self):
        import uuid

        # 缺失/错误服务密钥沿用 401（且在未知性判断之前整次拒绝）
        r = self.client.post(
            f"/snapshots/{uuid.uuid4()}/withdraw", json={"reason": "x"}
        )
        self.assertEqual(r.status_code, 401)
        r = self.client.post(
            f"/snapshots/{uuid.uuid4()}/withdraw",
            headers={"X-Service-Key": "wrong-key"},
            json={"reason": "x"},
        )
        self.assertEqual(r.status_code, 401)
        # 密钥正确但快照未知 -> 404
        r = self._withdraw(uuid.uuid4(), expected=404)

    def test_withdraw_first_time_requires_reason_422(self):
        pub = self._direct_publish()
        sid = pub["snapshot_id"]
        # 缺 reason / 空白 reason -> 422，且不产生撤回
        for body in ({}, {"reason": ""}, {"reason": "   "}):
            r = self.client.post(
                f"/snapshots/{sid}/withdraw", headers=SERVICE_HEADERS, json=body
            )
            self.assertEqual(r.status_code, 422, r.text)
        r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
        self.assertEqual(r.json()["snapshots"][0]["status"], "published")

    def test_withdrawn_snapshot_reads_403_for_any_token_without_consuming(self):
        # 两支令牌：一支带次数上限，一支不限次
        pub1 = self._direct_publish(body_extra={"max_reads": 3})
        sid, tok1 = pub1["snapshot_id"], pub1["token"]
        tok2 = self._read_token_for_same_snapshot_via_republish(sid)
        # 撤回前各成功读一次
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok1}").status_code, 200
        )
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok2}").status_code, 200
        )
        r = self._withdraw(sid, reason="发现不应公开的内容")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["snapshot_id"], sid)
        self.assertEqual(body["status"], "withdrawn")
        self.assertIsNotNone(body["withdrawn_at"])
        self.assertNotIn("reason", body)  # 撤回响应不向操作回执以外扩散原因
        # 无论持有哪枚令牌（甚至无效令牌），公开读取统一 403 snapshot unavailable
        for tok in (tok1, tok2, "not-a-token"):
            r = self.client.get(f"/public/snapshots/{sid}?token={tok}")
            self.assertEqual(r.status_code, 403)
            self.assertEqual(r.json()["detail"], "snapshot unavailable")
            self.assertNotIn("撤回", r.text)  # 公开侧不透露撤回原因
        # 撤回后的读取不消耗次数
        q1 = self.client.get(f"/tokens/{tok1}", headers=SERVICE_HEADERS).json()
        q2 = self.client.get(f"/tokens/{tok2}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q1["reads_used"], 1)
        self.assertEqual(q1["remaining_reads"], 2)
        self.assertEqual(q2["reads_used"], 1)

    def test_repeat_withdraw_returns_first_result_and_keeps_first_reason(self):
        pub = self._direct_publish(body_extra={"max_reads": 5})
        sid = pub["snapshot_id"]
        first = self._withdraw(sid, reason="首次撤回原因").json()
        first_at = first["withdrawn_at"]
        # 重复撤回（即便原因不同或为空）返回首次时间与状态
        for body in ({"reason": "另一个原因"}, {}):
            r = self.client.post(
                f"/snapshots/{sid}/withdraw", headers=SERVICE_HEADERS, json=body
            )
            self.assertEqual(r.status_code, 200, r.text)
            again = r.json()
            self.assertEqual(again["status"], "withdrawn")
            self.assertEqual(again["withdrawn_at"], first_at)
        # 维护侧记录保留首次撤回时间与原因
        r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
        info = r.json()["snapshots"][0]
        self.assertEqual(info["status"], "withdrawn")
        self.assertEqual(info["withdrawn_at"], first_at)
        self.assertEqual(info["withdraw_reason"], "首次撤回原因")

    def test_withdraw_is_scoped_to_single_snapshot(self):
        # 版本 1 与版本 2 都发布；撤回版本 1 不影响版本 2
        pub1 = self._direct_publish(version=1)
        sid1, tok1 = pub1["snapshot_id"], pub1["token"]
        r = self.client.put(
            f"/records/{self.record_id}",
            headers=SERVICE_HEADERS,
            json={"base_version": 1, "text": TEXT_V2, "intervals": [{"start": 9, "end": 15}]},
        )
        self.assertEqual(r.status_code, 200, r.text)
        pub2 = self._direct_publish(version=2)
        sid2, tok2 = pub2["snapshot_id"], pub2["token"]
        self._withdraw(sid1)
        # 版本 1 不可读；版本 2 继续可读
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid1}?token={tok1}").status_code, 403
        )
        r = self.client.get(f"/public/snapshots/{sid2}?token={tok2}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn(TEXT_V1, r.text)
        # 维护侧两个快照状态并存；撤回的历史脱敏正文仍留存可见于记录状态
        r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
        snapshots = r.json()["snapshots"]
        statuses = {s["version"]: s["status"] for s in snapshots}
        self.assertEqual(statuses, {1: "withdrawn", 2: "published"})
        self.assertEqual(snapshots[0]["withdraw_reason"], "误发，撤回")

    def test_direct_publish_of_withdrawn_version_conflicts(self):
        pub = self._direct_publish()
        sid = pub["snapshot_id"]
        self._withdraw(sid)
        # 再次直接发布同一版本 -> 409，且不签发新令牌
        r = self.client.post(
            f"/records/{self.record_id}/publish",
            headers=SERVICE_HEADERS,
            json={"version": 1, "max_reads": 9},
        )
        self.assertEqual(r.status_code, 409, r.text)
        # 修订出新版本后仍可正常发布（撤回只作用于该快照）
        r = self.client.put(
            f"/records/{self.record_id}",
            headers=SERVICE_HEADERS,
            json={"base_version": 1, "text": TEXT_V2, "intervals": [{"start": 9, "end": 15}]},
        )
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post(
            f"/records/{self.record_id}/publish",
            headers=SERVICE_HEADERS,
            json={"version": 2},
        )
        self.assertEqual(r.status_code, 201, r.text)
        r = self.client.get(
            f"/public/snapshots/{r.json()['snapshot_id']}?token={r.json()['token']}"
        )
        self.assertEqual(r.status_code, 200)

    def test_controlled_submit_for_withdrawn_version_conflicts(self):
        # 先发布版本 1（非受控直接发布）再撤回，随后把记录转为受控
        pub = self._direct_publish()
        self._withdraw(pub["snapshot_id"])
        self._mark_controlled()
        r = self._submit(expected=409)
        self.assertNotIn(TEXT_V1, r.text)

    def test_pending_request_can_be_rejected_but_approval_conflicts_after_withdraw(self):
        self._mark_controlled()
        self._submit(version=1, expires={"max_reads": 2})
        request_id = self._latest_request_id()
        # 期间该版本经另一申请批准产生快照并被撤回（模拟版本曾公开后撤回）
        other = self._submit()
        other_id = self._latest_request_id_from_list()
        r = self.client.post(f"/publish-requests/{other_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 200, r.text)
        sid = r.json()["snapshot_id"]
        self._withdraw(sid, reason="复核后发现问题")
        # 撤回不改动待审申请；批准命中已撤回快照 -> 409，申请仍可驳回
        r = self.client.get(f"/publish-requests/{request_id}", auth=BASIC_ALICE)
        self.assertEqual(r.json()["status"], "pending")
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 409, r.text)
        r = self.client.post(
            f"/publish-requests/{request_id}/reject",
            auth=BASIC_BOB,
            json={"reason": "版本已撤回，不予批准"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "rejected")
        # 已批准申请记录仍留存，并指向撤回快照
        r = self.client.get(f"/publish-requests/{other_id}", auth=BASIC_ALICE)
        self.assertEqual(r.json()["status"], "approved")
        self.assertEqual(r.json()["snapshot_id"], sid)
        # 撤回后公开读取仍 403
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token=x").status_code, 403
        )

    def _latest_request_id_from_list(self):
        r = self.client.get(
            f"/records/{self.record_id}/publish-requests", auth=BASIC_ALICE
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["requests"][-1]["request_id"]

    def test_concurrent_withdraw_and_reads_respect_boundary(self):
        pub = self._direct_publish(body_extra={"max_reads": 50})
        sid, tok = pub["snapshot_id"], pub["token"]
        results: list[int] = []
        barrier = threading.Barrier(9)

        def withdraw():
            barrier.wait()
            client = TestClient(app_main.app)
            r = client.post(
                f"/snapshots/{sid}/withdraw",
                headers=SERVICE_HEADERS,
                json={"reason": "并发撤回"},
            )
            results.append(0 if r.status_code == 200 else r.status_code)

        def read_once():
            barrier.wait()
            client = TestClient(app_main.app)
            # 略作退让，争取让撤回先提交；无论先后，撤回提交后的读取必须 403
            code = client.get(f"/public/snapshots/{sid}?token={tok}").status_code
            results.append(code)

        threads = [threading.Thread(target=withdraw)]
        threads += [threading.Thread(target=read_once) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertIn(0, results)  # 撤回成功
        concurrent_reads = [c for c in results if c != 0]
        # 撤回提交后开始的读取一律 403；在撤回提交前已获行锁的读取至多 8 个 200
        self.assertTrue(all(c in (200, 403) for c in concurrent_reads), results)
        # 撤回完成后的新读取稳定 403
        for _ in range(5):
            self.assertEqual(
                self.client.get(f"/public/snapshots/{sid}?token={tok}").status_code, 403
            )
        q = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS).json()
        # 消耗次数恰为并发阶段的 200 数，撤回后的 403 未消耗
        self.assertEqual(q["reads_used"], concurrent_reads.count(200))

    def test_existing_db_gets_snapshot_withdrawal_columns(self):
        from sqlalchemy import inspect, text

        from app.db import SessionLocal, engine
        from app.models import Snapshot as SnapshotModel

        pub = self._direct_publish()
        sid = pub["snapshot_id"]
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE snapshots DROP COLUMN withdraw_reason"))
            conn.execute(text("ALTER TABLE snapshots DROP COLUMN withdrawn_at"))
        app_main._init_db_with_retry()
        columns = {c["name"] for c in inspect(engine).get_columns("snapshots")}
        self.assertIn("withdrawn_at", columns)
        self.assertIn("withdraw_reason", columns)
        # 既有快照迁移后默认未撤回，继续可读
        r = self.client.get(
            f"/public/snapshots/{sid}?token={pub['token']}"
        )
        self.assertEqual(r.status_code, 200, r.text)
        # 补列后撤回功能正常
        r = self._withdraw(sid)
        self.assertEqual(r.json()["status"], "withdrawn")
        with SessionLocal() as session:
            row = session.get(SnapshotModel, sid)
            self.assertIsNotNone(row.withdrawn_at)
            self.assertEqual(row.withdraw_reason, "误发，撤回")


@unittest.skipIf(SKIP_INTEGRATION, _SKIP_REASON)
class BundleCredentialIntegrationTests(unittest.TestCase):
    """多快照批量凭证：申请-复核、清单冻结与批准复核、共用额度、撤回隔离。"""

    @classmethod
    def setUpClass(cls):
        app_main._init_db_with_retry()
        cls.client = TestClient(app_main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        # 两条记录、各发布版本 1；再修订记录 A 发布版本 2 —— 共三个已发布快照
        self.record_a = self._create_record(TEXT_V1, INTERVALS_V1)
        self.record_b = self._create_record(
            "患者王五，卡号 6222000000000000。",
            [{"start": 5, "end": 21}],
        )
        self.sid_a1 = self._direct_publish(self.record_a, 1)
        self.sid_b1 = self._direct_publish(self.record_b, 1)
        r = self.client.put(
            f"/records/{self.record_a}",
            headers=SERVICE_HEADERS,
            json={"base_version": 1, "text": TEXT_V2, "intervals": [{"start": 9, "end": 15}]},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.sid_a2 = self._direct_publish(self.record_a, 2)
        self.assertEqual(3, len({self.sid_a1, self.sid_a2, self.sid_b1}))

    # ---- 工具方法 ----

    def _create_record(self, text, intervals):
        r = self.client.post(
            "/records", headers=SERVICE_HEADERS, json={"text": text, "intervals": intervals}
        )
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["record_id"]

    def _direct_publish(self, record_id, version, body_extra=None):
        body = {"version": version}
        if body_extra:
            body.update(body_extra)
        r = self.client.post(
            f"/records/{record_id}/publish", headers=SERVICE_HEADERS, json=body
        )
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["snapshot_id"]

    def _publish_token(self, record_id, version, body_extra=None):
        body = {"version": version}
        if body_extra:
            body.update(body_extra)
        r = self.client.post(
            f"/records/{record_id}/publish", headers=SERVICE_HEADERS, json=body
        )
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def _submit_bundle(self, snapshot_ids, auth=BASIC_ALICE, extra=None, expected=201):
        body = {"snapshot_ids": snapshot_ids, "max_reads": 5}
        if extra:
            body.update(extra)
        r = self.client.post("/bundle-requests", auth=auth, json=body)
        self.assertEqual(r.status_code, expected, r.text)
        return r

    def _approve_bundle(self, request_id, auth=BASIC_BOB, expected=200):
        r = self.client.post(f"/bundle-requests/{request_id}/approve", auth=auth)
        self.assertEqual(r.status_code, expected, r.text)
        return r

    def _read(self, snapshot_id, token):
        return self.client.get(f"/public/snapshots/{snapshot_id}?token={token}")

    def _withdraw(self, snapshot_id, reason="批量流程中撤回"):
        r = self.client.post(
            f"/snapshots/{snapshot_id}/withdraw",
            headers=SERVICE_HEADERS,
            json={"reason": reason},
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r

    # ---- 提交校验 ----

    def test_submit_requires_at_least_two_distinct_snapshots(self):
        # 仅一个 -> 422
        r = self.client.post(
            "/bundle-requests", auth=BASIC_ALICE, json={"snapshot_ids": [self.sid_a1], "max_reads": 3}
        )
        self.assertEqual(r.status_code, 422)
        # 重复 ID -> 422
        r = self.client.post(
            "/bundle-requests",
            auth=BASIC_ALICE,
            json={"snapshot_ids": [self.sid_a1, self.sid_a1], "max_reads": 3},
        )
        self.assertEqual(r.status_code, 422)

    def test_submit_requires_positive_integer_max_reads(self):
        for bad in (None, 0, -1, 1.5):
            body = {"snapshot_ids": [self.sid_a1, self.sid_b1], "max_reads": bad}
            r = self.client.post("/bundle-requests", auth=BASIC_ALICE, json=body)
            self.assertEqual(r.status_code, 422, r.text)

    def test_submit_unknown_snapshot_is_404_and_withdrawn_is_409(self):
        import uuid

        r = self.client.post(
            "/bundle-requests",
            auth=BASIC_ALICE,
            json={"snapshot_ids": [self.sid_a1, str(uuid.uuid4())], "max_reads": 3},
        )
        self.assertEqual(r.status_code, 404, r.text)
        self._withdraw(self.sid_b1)
        r = self._submit_bundle([self.sid_a1, self.sid_b1], expected=409)
        self.assertNotIn("身份证", r.text)

    def test_submit_roles_and_auth(self):
        body = {"snapshot_ids": [self.sid_a1, self.sid_b1], "max_reads": 3}
        # 无凭据
        self.assertEqual(self.client.post("/bundle-requests", json=body).status_code, 401)
        # bob 仅 reviewer
        self.assertEqual(
            self.client.post("/bundle-requests", auth=BASIC_BOB, json=body).status_code, 403
        )

    def test_submit_freezes_manifest_in_order(self):
        ids = [self.sid_b1, self.sid_a1, self.sid_a2]
        r = self._submit_bundle(ids)
        body = r.json()
        self.assertEqual(body["status"], "pending")
        self.assertEqual(body["requested_by"], "alice")
        self.assertEqual(body["token_max_reads"], 5)
        self.assertEqual([s["snapshot_id"] for s in body["snapshots"]], ids)
        self.assertEqual(
            [s["status"] for s in body["snapshots"]], ["published"] * 3
        )
        # 详情不含正文
        self.assertNotIn(TEXT_V1, r.text)
        self.assertNotIn("卡号", r.text)

    # ---- 复核：角色、回避、终态 ----

    def test_review_rules(self):
        r = self._submit_bundle([self.sid_a1, self.sid_b1])
        request_id = r.json()["request_id"]
        # alice 只有 operator，不能批准
        self.assertEqual(
            self.client.post(f"/bundle-requests/{request_id}/approve", auth=BASIC_ALICE).status_code,
            403,
        )
        # 无凭据
        self.assertEqual(
            self.client.post(f"/bundle-requests/{request_id}/approve").status_code, 401
        )
        # carol 是申请人本人（兼具 reviewer）也不得自审
        r = self._submit_bundle([self.sid_a1, self.sid_b1], auth=BASIC_CAROL)
        own_id = r.json()["request_id"]
        self.assertEqual(
            self.client.post(f"/bundle-requests/{own_id}/approve", auth=BASIC_CAROL).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                f"/bundle-requests/{own_id}/reject", auth=BASIC_CAROL, json={}
            ).status_code,
            403,
        )
        # 未知申请
        import uuid

        self.assertEqual(
            self.client.post(f"/bundle-requests/{uuid.uuid4()}/approve", auth=BASIC_BOB).status_code,
            404,
        )

    def test_rejected_request_cannot_be_approved_and_creates_no_credential(self):
        r = self._submit_bundle([self.sid_a1, self.sid_b1])
        request_id = r.json()["request_id"]
        r = self.client.post(
            f"/bundle-requests/{request_id}/reject",
            auth=BASIC_BOB,
            json={"reason": "范围不符"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "rejected")
        self.assertEqual(r.json()["reason"], "范围不符")
        self.assertEqual(self._approve_bundle(request_id, expected=409).status_code, 409)
        from app.db import SessionLocal
        from app.models import BundleToken as BundleTokenModel

        with SessionLocal() as session:
            self.assertEqual(
                session.query(BundleTokenModel)
                .filter(BundleTokenModel.request_id == request_id)
                .count(),
                0,
            )
        # 未签发凭证：任何快照都不能凭猜测的凭证读取
        self.assertEqual(self._read(self.sid_a1, "anything").status_code, 403)

    def test_approved_request_cannot_be_decided_again(self):
        r = self._submit_bundle([self.sid_a1, self.sid_b1])
        request_id = r.json()["request_id"]
        self._approve_bundle(request_id)
        self.assertEqual(self._approve_bundle(request_id, expected=409).status_code, 409)
        self.assertEqual(
            self.client.post(
                f"/bundle-requests/{request_id}/reject", auth=BASIC_BOB, json={}
            ).status_code,
            409,
        )

    # ---- 批准时重新核对 ----

    def test_approval_rechecks_snapshots_failure_issues_no_credential(self):
        r = self._submit_bundle([self.sid_a1, self.sid_b1])
        request_id = r.json()["request_id"]
        # 等待期间撤回清单中的一项：整次批准失败（409），不签发凭证
        self._withdraw(self.sid_b1, reason="复核期间发现问题")
        r = self._approve_bundle(request_id, expected=409)
        # 申请仍 pending，仍可驳回
        r = self.client.get(f"/bundle-requests/{request_id}", auth=BASIC_ALICE)
        self.assertEqual(r.json()["status"], "pending")
        r = self.client.post(
            f"/bundle-requests/{request_id}/reject",
            auth=BASIC_BOB,
            json={"reason": "快照失效"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "rejected")
        from app.db import SessionLocal
        from app.models import BundleToken as BundleTokenModel

        with SessionLocal() as session:
            self.assertEqual(
                session.query(BundleTokenModel)
                .filter(BundleTokenModel.request_id == request_id)
                .count(),
                0,
            )

    def test_approval_rechecks_manifest_integrity(self):
        r = self._submit_bundle([self.sid_a1, self.sid_b1])
        request_id = r.json()["request_id"]
        # 模拟冻结清单在待审期间残缺（清单条目丢失、不足两项）：
        # 批准必须整次失败（409），不能缩水签发
        from sqlalchemy import text

        from app.db import engine

        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM bundle_request_items WHERE request_id = :rid AND position = 1"),
                {"rid": request_id},
            )
        self._approve_bundle(request_id, expected=409)
        # 申请仍可驳回
        r = self.client.post(
            f"/bundle-requests/{request_id}/reject",
            auth=BASIC_BOB,
            json={"reason": "清单不完整"},
        )
        self.assertEqual(r.status_code, 200, r.text)

    def test_successful_approval_issues_one_credential_bound_to_manifest(self):
        ids = [self.sid_a1, self.sid_b1]
        r = self._submit_bundle(ids, extra={"expires_in_seconds": 3600})
        request_id = r.json()["request_id"]
        r = self._approve_bundle(request_id)
        body = r.json()
        self.assertEqual(body["status"], "approved")
        self.assertEqual(body["reviewed_by"], "bob")
        self.assertEqual(body["max_reads"], 5)
        self.assertIsNotNone(body["expires_at"])
        token = body["token"]
        self.assertEqual([s["snapshot_id"] for s in body["snapshots"]], ids)
        # 凭证只能读清单内快照：清单外快照 -> 模糊 403
        self.assertEqual(self._read(self.sid_a2, token).status_code, 403)
        # 清单内两项都可读，返回各自脱敏正文，响应结构不变
        r = self._read(self.sid_a1, token)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            r.json()["redacted_text"],
            "患者[已遮蔽]，身份证号 [已遮蔽]，联系电话 [已遮蔽]。",
        )
        r = self._read(self.sid_b1, token)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("[已遮蔽]", r.json()["redacted_text"])

    # ---- 共用额度 ----

    def test_shared_quota_is_pooled_across_manifest_items(self):
        r = self._submit_bundle(
            [self.sid_a1, self.sid_b1, self.sid_a2], extra={"max_reads": 3}
        )
        request_id = r.json()["request_id"]
        token = self._approve_bundle(request_id).json()["token"]
        # 3 次成功（跨不同清单快照合计）
        for sid in (self.sid_a1, self.sid_b1, self.sid_a2):
            self.assertEqual(self._read(sid, token).status_code, 200)
        # 额度耗尽：清单内全部快照一律 403
        for sid in (self.sid_a1, self.sid_b1, self.sid_a2):
            r = self._read(sid, token)
            self.assertEqual(r.status_code, 403)
            self.assertEqual(r.json()["detail"], "snapshot unavailable")
        q = self.client.get(f"/bundle-tokens/{token}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q["max_reads"], 3)
        self.assertEqual(q["reads_used"], 3)
        self.assertEqual(q["remaining_reads"], 0)
        self.assertFalse(q["active"])
        self.assertEqual(len(q["snapshots"]), 3)

    def test_failed_reads_do_not_consume_shared_quota(self):
        r = self._submit_bundle([self.sid_a1, self.sid_b1], extra={"max_reads": 3})
        token = self._approve_bundle(r.json()["request_id"]).json()["token"]
        # 清单外读取不消耗
        self.assertEqual(self._read(self.sid_a2, token).status_code, 403)
        # 无效凭证不消耗
        self.assertEqual(self._read(self.sid_a1, "not-a-credential").status_code, 403)
        q = self.client.get(f"/bundle-tokens/{token}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q["reads_used"], 0)
        # 一次成功后，清单内某项撤回：只拒绝该项且不扣额度，其他项照读
        self.assertEqual(self._read(self.sid_a1, token).status_code, 200)
        self._withdraw(self.sid_b1)
        r = self._read(self.sid_b1, token)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["detail"], "snapshot unavailable")
        q = self.client.get(f"/bundle-tokens/{token}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q["reads_used"], 1)
        self.assertEqual(q["remaining_reads"], 2)
        # 其他项仍可读，占用剩余共享额度
        self.assertEqual(self._read(self.sid_a1, token).status_code, 200)
        self.assertEqual(self._read(self.sid_a1, token).status_code, 200)
        self.assertEqual(self._read(self.sid_a1, token).status_code, 403)
        # 撤回项维护侧清单状态为 withdrawn，凭证仍 active=False（额度已耗尽）
        q = self.client.get(f"/bundle-tokens/{token}", headers=SERVICE_HEADERS).json()
        statuses = {s["snapshot_id"]: s["status"] for s in q["snapshots"]}
        self.assertEqual(statuses[self.sid_b1], "withdrawn")
        self.assertEqual(statuses[self.sid_a1], "published")

    def test_concurrent_reads_never_exceed_shared_quota(self):
        r = self._submit_bundle(
            [self.sid_a1, self.sid_b1, self.sid_a2], extra={"max_reads": 12}
        )
        token = self._approve_bundle(r.json()["request_id"]).json()["token"]
        targets = [self.sid_a1, self.sid_b1, self.sid_a2]
        successes: list[int] = []
        failures: list[int] = []
        barrier = threading.Barrier(24)

        def read_once(index):
            barrier.wait()
            client = TestClient(app_main.app)
            sid = targets[index % 3]
            code = client.get(f"/public/snapshots/{sid}?token={token}").status_code
            (successes if code == 200 else failures).append(code)

        threads = [threading.Thread(target=read_once, args=(i,)) for i in range(24)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(successes), 12, successes + failures)
        self.assertEqual(len(failures), 12)
        self.assertTrue(all(code == 403 for code in failures))
        q = self.client.get(f"/bundle-tokens/{token}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q["reads_used"], 12)
        self.assertEqual(q["remaining_reads"], 0)

    # ---- 到期、撤销、维护侧 ----

    def test_expiry_denies_all_manifest_items_without_consuming(self):
        r = self._submit_bundle(
            [self.sid_a1, self.sid_b1],
            extra={"max_reads": 5, "expires_in_seconds": 1},
        )
        token = self._approve_bundle(r.json()["request_id"]).json()["token"]
        self.assertEqual(self._read(self.sid_a1, token).status_code, 200)
        import time as _time

        _time.sleep(1.2)
        for sid in (self.sid_a1, self.sid_b1):
            self.assertEqual(self._read(sid, token).status_code, 403)
        q = self.client.get(f"/bundle-tokens/{token}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q["reads_used"], 1)
        self.assertFalse(q["active"])
        self.assertEqual(q["remaining_reads"], 4)

    def test_past_expires_at_submission_is_422(self):
        r = self.client.post(
            "/bundle-requests",
            auth=BASIC_ALICE,
            json={
                "snapshot_ids": [self.sid_a1, self.sid_b1],
                "max_reads": 2,
                "expires_at": "2000-01-01T00:00:00Z",
            },
        )
        self.assertEqual(r.status_code, 422)

    def test_revocation_denies_all_and_maintenance_endpoints(self):
        r = self._submit_bundle([self.sid_a1, self.sid_b1])
        request_id = r.json()["request_id"]
        token = self._approve_bundle(request_id).json()["token"]
        # 缺服务密钥
        self.assertEqual(self.client.get(f"/bundle-tokens/{token}").status_code, 401)
        self.assertEqual(
            self.client.post(f"/bundle-tokens/{token}/revoke").status_code, 401
        )
        # 未知凭证
        self.assertEqual(
            self.client.get("/bundle-tokens/not-real", headers=SERVICE_HEADERS).status_code, 404
        )
        # 状态查询
        q = self.client.get(f"/bundle-tokens/{token}", headers=SERVICE_HEADERS)
        self.assertEqual(q.status_code, 200, q.text)
        body = q.json()
        self.assertEqual(body["request_id"], request_id)
        self.assertTrue(body["active"])
        self.assertNotIn(TEXT_V1, q.text)
        # 撤销
        r = self.client.post(f"/bundle-tokens/{token}/revoke", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["revoked"])
        # 重复撤销幂等
        r = self.client.post(f"/bundle-tokens/{token}/revoke", headers=SERVICE_HEADERS)
        self.assertTrue(r.json()["already_revoked"])
        # 撤销后清单内全部 403
        for sid in (self.sid_a1, self.sid_b1):
            self.assertEqual(self._read(sid, token).status_code, 403)
        q = self.client.get(f"/bundle-tokens/{token}", headers=SERVICE_HEADERS).json()
        self.assertFalse(q["active"])
        self.assertIsNotNone(q["revoked_at"])

    def test_legacy_single_snapshot_tokens_remain_compatible(self):
        # 既有单快照令牌继续在公开接口按既有规则工作
        pub = self._publish_token(self.record_a, 1, body_extra={"max_reads": 2})
        sid, tok = pub["snapshot_id"], pub["token"]
        for _ in range(2):
            self.assertEqual(self._read(sid, tok).status_code, 200)
        self.assertEqual(self._read(sid, tok).status_code, 403)
        # 单快照令牌只能读自己的快照
        self.assertEqual(self._read(self.sid_b1, tok).status_code, 403)
        # 既有令牌状态/撤销接口仍只服务单快照令牌（批量凭证不在该命名空间）
        q = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS)
        self.assertEqual(q.status_code, 200)
        self.assertEqual(q.json()["reads_used"], 2)

    def test_bundle_credential_does_not_work_on_legacy_token_status_endpoint(self):
        r = self._submit_bundle([self.sid_a1, self.sid_b1])
        token = self._approve_bundle(r.json()["request_id"]).json()["token"]
        self.assertEqual(
            self.client.get(f"/tokens/{token}", headers=SERVICE_HEADERS).status_code, 404
        )
        self.assertEqual(
            self.client.post(f"/tokens/{token}/revoke", headers=SERVICE_HEADERS).status_code,
            404,
        )

    def test_approval_failure_rolls_back_credential(self):
        """批准事务在凭证写入阶段失败时，不留凭证、申请仍 pending。"""
        from unittest import mock

        r = self._submit_bundle([self.sid_a1, self.sid_b1])
        request_id = r.json()["request_id"]
        with mock.patch.object(
            app_main.secrets, "token_urlsafe", return_value="x" * 65
        ):
            with self.assertRaises(Exception):
                self.client.post(f"/bundle-requests/{request_id}/approve", auth=BASIC_BOB)
        r = self.client.get(f"/bundle-requests/{request_id}", auth=BASIC_ALICE)
        self.assertEqual(r.json()["status"], "pending")
        # 之后仍可正常批准
        self.assertEqual(self._approve_bundle(request_id).status_code, 200)


if __name__ == "__main__":
    unittest.main()