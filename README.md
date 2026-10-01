# Redacted Records API

供机构公开发布脱敏记录的 HTTP 服务。维护人员凭服务密钥提交原文与敏感区间，发布时按 Unicode 码点区间合并遮蔽，生成**不可变快照**与**随机读取令牌**；公众仅凭有效令牌读取脱敏正文。

支持两种发布方式：

- **直接发布**（既有能力）：维护人员凭服务密钥直接发布指定版本；
- **受控发布**（申请-复核）：机构先把记录标记为受控，操作者用**各自的凭据与身份**提交指定历史版本的发布申请，由**另一名具复核资格者**批准或驳回（申请人不得复核）；批准时在同一事务生成不可变快照与新读取令牌。

已发布版本若不应继续公开，机构可凭服务密钥将对应**快照永久撤回**：撤回只作用于该快照，同记录其他版本继续可读；撤回后无论持有该快照的哪枚令牌，新开始的公开读取一律沿用模糊 `403 snapshot unavailable` 且不消耗读取次数；该版本再次直接发布、提交受控申请或批准撤回前已在审的申请均返回 `409`，待审申请仍可驳回。

签发快照读取令牌时可**可选**地设置正整数**读取次数上限**（`max_reads`）：直接发布在请求时确定，受控发布在提交申请时冻结、批准签发后生效。公开读取沿用既有脱敏正文响应，每次凭有效令牌**成功读取**消耗一次；无效、撤销、到期、快照已撤回、令牌与快照不匹配或已达上限的请求不消耗次数。

此外，机构可把**多个已发布快照**一次性交给同一外部审阅者：操作者提交**至少两个互不重复的快照 ID**、可选到期策略与**必填的正整数共用读取总额度**，由另一名 reviewer 批准或驳回（申请人不得自审）。提交时**冻结清单**，批准时**重新核对全部快照仍存在且未撤回**，任一项失效则整次批准失败、不签发凭证（申请仍可驳回）。批准成功后**一次性签发一支随机批量凭证**，绑定冻结清单并在清单内**共用读取额度**；凭证在既有公开快照读取接口上**只能读取清单内快照的脱敏正文**，响应与模糊 `403` 完全沿用。清单中某项后来撤回时**只拒绝该项且不扣额度**，其他项仍可读；凭证到期、被撤销或共用额度耗尽时清单内全部拒绝。维护人员凭服务密钥可查询凭证清单/额度/状态并撤销。

- 技术栈：Python 3.12 · FastAPI · SQLAlchemy 2 · PostgreSQL 16
- 服务地址：`http://localhost:8000`（交互式文档：`http://localhost:8000/docs`）

## 一键启动

```bash
docker compose up --build
```

Compose 会启动 PostgreSQL 与 API 两个容器，自动完成依赖安装与数据库初始化（启动时幂等建表，并重试等待数据库就绪）。数据持久化在命名卷 `pgdata` 中。

停止并清空数据：`docker compose down -v`

## 配置（环境变量）

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `SERVICE_KEY` | 维护人员服务密钥，**必填**，未配置服务拒绝启动 | 无（Compose 中为演示值 `dev-service-key-change-me`，生产务必修改） |
| `REVIEW_CREDENTIALS` | 受控发布操作者凭据，JSON 数组，见下 | `[]`（受控接口无人可用，不影响其他接口） |
| `DATABASE_URL` | PostgreSQL 连接串 | `postgresql+psycopg2://redact:redact@localhost:5432/redact` |
| `DB_CONNECT_RETRIES` | 启动时等待数据库的重试次数 | `30` |
| `DB_CONNECT_RETRY_DELAY_SECONDS` | 重试间隔（秒） | `1.0` |

维护接口（创建、修订、直接发布、撤销、状态、标记受控）通过请求头 `X-Service-Key` 鉴权；缺失或不符一律 `401` 整次拒绝。

受控发布接口（提交、批准、驳回、查询申请）使用 **HTTP Basic 认证**（`Authorization: Basic base64(用户名:口令)`），按 `REVIEW_CREDENTIALS` 校验**操作者个人凭据**，并区分角色：

- `operator`：可提交发布申请；
- `reviewer`：可批准/驳回申请（申请人不得复核自己的申请）；
- 一人可同时具备两种角色（如机构的备用复核人）。

`REVIEW_CREDENTIALS` 为 JSON 数组，每项形如：

```json
[
  {"username": "alice", "password": "强口令A", "roles": ["operator"]},
  {"username": "bob",   "password": "强口令B", "roles": ["reviewer"]},
  {"username": "carol", "password": "强口令C", "roles": ["operator", "reviewer"]}
]
```

服务启动时校验该配置：用户名非空且不重复、口令非空、角色只能是 `operator`/`reviewer` 且至少一个，非法配置会让服务**快速失败**（报错信息不含口令）。至少需配置一名 `operator` 与一名**不同于**常用申请人的 `reviewer`，否则申请无法被独立复核。生产环境请通过机密管理系统注入，不要把真实口令提交到仓库。

## API 一览

| 方法 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| `POST` | `/records` | 服务密钥 | 创建记录（版本 1），提交原文与敏感区间 |
| `PUT` | `/records/{record_id}` | 服务密钥 | 修订：携带 `base_version`，过期返回 `409` 与当前版本 |
| `PUT` | `/records/{record_id}/controlled` | 服务密钥 | 将记录设为受控发布（幂等） |
| `POST` | `/records/{record_id}/publish` | 服务密钥 | 直接发布指定版本（可带 `max_reads` 上限）；**受控记录一律 `409` 拒绝** |
| `POST` | `/records/{record_id}/publish-requests` | operator | 提交受控发布申请（冻结指定历史版本内容与 `max_reads`） |
| `POST` | `/publish-requests/{request_id}/approve` | reviewer（非申请人） | 批准：同事务生成快照 + 新读取令牌（冻结的上限生效） |
| `POST` | `/publish-requests/{request_id}/reject` | reviewer（非申请人） | 驳回（可带 `reason`），驳回为终态，不生成令牌或计数 |
| `GET` | `/publish-requests/{request_id}` | 任一已认证操作者 | 查询单个申请状态（含冻结的 `token_max_reads`，不含原文） |
| `GET` | `/records/{record_id}/publish-requests` | 任一已认证操作者 | 列出某记录的全部申请（不含原文） |
| `POST` | `/bundle-requests` | operator | 提交多快照批量授权申请（≥2 个互不重复快照、可选到期、必填正整数共用额度） |
| `POST` | `/bundle-requests/{request_id}/approve` | reviewer（非申请人） | 批准：重新核对清单全部存在且未撤回，一次性签发绑定清单的批量凭证 |
| `POST` | `/bundle-requests/{request_id}/reject` | reviewer（非申请人） | 驳回批量申请（可带 `reason`），终态，不签发凭证 |
| `GET` | `/bundle-requests/{request_id}` | 任一已认证操作者 | 查询批量申请与冻结清单（不含正文/凭证） |
| `GET` | `/bundle-tokens/{token}` | 服务密钥 | 查询批量凭证的清单、共用额度（`max_reads`/`reads_used`/`remaining_reads`）与状态 |
| `POST` | `/bundle-tokens/{token}/revoke` | 服务密钥 | 撤销批量凭证（撤销后清单内全部拒绝；重复撤销幂等） |
| `GET` | `/tokens/{token}` | 服务密钥 | 查询**单快照令牌**状态：上限 `max_reads`、已用 `reads_used`、剩余 `remaining_reads`（不含原文） |
| `POST` | `/tokens/{token}/revoke` | 服务密钥 | 撤销**单快照令牌**（含受控发布签发的令牌） |
| `POST` | `/snapshots/{snapshot_id}/withdraw` | 服务密钥 | **永久撤回快照**（首次需带 `reason`）；重复撤回返回首次撤回时间与状态 |
| `GET` | `/records/{record_id}` | 服务密钥 | 查看当前版本、受控标记与快照列表（含撤回时间、原因与状态） |
| `GET` | `/public/snapshots/{snapshot_id}?token=...` | 无（凭单快照令牌或批量凭证） | 公开读取脱敏正文（批量凭证仅限其冻结清单内快照，共用额度） |
| `GET` | `/healthz` | 无 | 健康检查 |

### 区间语义

- 左闭右开 `[start, end)`，位置按 **Unicode 码点**计（与 Python 字符串下标一致，BMP 之外的字符算 1 个码点）。
- 越界、为空或倒置的区间 → `422`，**整次拒绝**，不落库任何内容。
- 发布时合并**相交或相邻**的区间，每段替换为 `[已遮蔽]`，其余原文保留。

### 读取次数上限（`max_reads`）

签发读取令牌时可**可选**地携带正整数 `max_reads`（不传或为 `null` 表示**不限次**，既有及新令牌默认如此）：

- **确定/冻结时点**：直接发布在发布请求中确定；受控发布在 `POST .../publish-requests` 提交申请时**冻结**（申请详情以 `token_max_reads` 返回），复核耗时与后续修订不改变它，**批准签发后**才落到新令牌上；申请被驳回不生成令牌或计数。
- **计数口径**：每次凭**有效**令牌成功读取脱敏正文消耗一次；`reads_used` 只增不减。同一快照的**不同令牌各自独立计数**（同一版本重复发布会复用快照、签出新令牌）。
- **达上限后**：读取沿用既有模糊拒绝 `403 snapshot unavailable`，公开响应结构不变，不额外暴露“已用尽”这一原因。
- **不消耗次数的请求**：令牌无效、已撤销、已到期，或令牌与快照不匹配（含快照不存在）。
- **撤销/到期优先**：即使尚有剩余次数，撤销或到期后读取一律 `403`，不能因尚有次数而放行。
- **并发安全**：次数判定与递增由公开读取接口内的**同一条条件 `UPDATE`** 原子完成（`reads_used < max_reads` 命中行锁串行化），并发读取的成功次数绝不超过上限；撤销是独立的原子 `UPDATE`，提交后优先生效。
- **维护侧查询**：`GET /tokens/{token}`（服务密钥）返回 `max_reads` / `reads_used` / `remaining_reads`（不限次时后两者中的上限与剩余为 `null`）及 `active` 等元数据；该接口**不返回快照原文或脱敏正文**，未知令牌 `404`，缺密钥 `401`。

### 快照永久撤回

机构发现某个已发布版本不应继续公开时，凭服务密钥调用
`POST /snapshots/{snapshot_id}/withdraw`，请求体携带撤回原因 `{"reason": "..."}`：

- **响应**：`{"snapshot_id", "withdrawn_at", "status": "withdrawn"}`，其中 `withdrawn_at` 为**首次**撤回时间。
- **幂等**：重复撤回（即使原因不同或不带原因）返回**首次**撤回时间与状态，首次原因保留不变；并发撤回由条件 `UPDATE` 串行化，恰好一个首次结果。
- **作用域**：撤回只作用于该快照。同记录其他版本的快照与令牌继续可读；快照中的**历史脱敏正文、已批准的发布申请记录均继续留存**（维护侧可查）。
- **公开侧效果**：撤回提交后才开始的公开读取，无论持有该快照的**哪枚令牌**（或无效令牌），一律沿用既有模糊拒绝 `403 snapshot unavailable`；这些请求**不消耗**任何令牌的读取次数，公开响应也**不透露撤回原因**。
- **并发界限**：公开读取先对快照行加锁再判定，读取与撤回由数据库行锁串行化——撤回提交前已在读的请求按既有规则处理，撤回提交后开始的读取必定 403 且不计数。
- **发布冲突**：该版本
  - 再次**直接发布** → `409`（不会复用撤回快照签出新令牌）；
  - **提交受控申请** → `409`；
  - 撤回前已存在的**待审申请**，批准时 → `409`（不签发令牌），但**仍可驳回**；已批准申请记录继续留存。
- **状态码**：未知快照 `404`；缺失或错误服务密钥沿用 `401`；首次撤回缺少非空白 `reason` → `422`。
- **维护侧可见**：`GET /records/{record_id}` 的每个快照条目含 `status`（`published` / `withdrawn`）、`withdrawn_at`、`withdraw_reason`。

### 调用示例

```bash
# 1. 创建记录（版本 1）
curl -sS -X POST http://localhost:8000/records \
  -H "X-Service-Key: dev-service-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{
        "text": "患者张三，身份证号 110101199003077777，联系电话 13800001111。",
        "intervals": [{"start": 2, "end": 4}, {"start": 10, "end": 28}, {"start": 34, "end": 45}]
      }'
# => {"record_id":"<RID>","version":1}

# 2. 修订（base_version 过期时返回 409 与当前版本）
curl -sS -X PUT http://localhost:8000/records/<RID> \
  -H "X-Service-Key: dev-service-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"base_version": 1, "text": "……新原文……", "intervals": [{"start": 0, "end": 3}]}'
# => {"record_id":"<RID>","version":2}
# 过期时 => 409 {"detail":{"message":"base_version is stale","current_version":2}}

# 3. 发布版本 2（可另加 "expires_in_seconds": 3600、"expires_at": "2026-10-01T00:00:00Z"、"max_reads": 5）
curl -sS -X POST http://localhost:8000/records/<RID>/publish \
  -H "X-Service-Key: dev-service-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"version": 2, "max_reads": 5}'
# => {"snapshot_id":"<SID>","version":2,"status":"published","token":"<TOKEN>","expires_at":null,"max_reads":5}

# 3b. 维护侧查询已用/剩余次数（不限次令牌 max_reads 与 remaining_reads 为 null）
curl -sS http://localhost:8000/tokens/<TOKEN> -H "X-Service-Key: dev-service-key-change-me"
# => {"token":"<TOKEN>","snapshot_id":"<SID>","record_id":"<RID>","version":2,
#     "max_reads":5,"reads_used":0,"remaining_reads":5,"active":true, ...}

# 4. 公开读取（无需密钥，只返回脱敏正文）；前 5 次 200，第 6 次起 403 snapshot unavailable
curl -sS "http://localhost:8000/public/snapshots/<SID>?token=<TOKEN>"
# => {"record_id":"<RID>","version":2,"status":"published","redacted_text":"患者[已遮蔽]，……"}

# 5. 撤销令牌（撤销完成后，即使尚有剩余次数，新开始的读取一律 403）
curl -sS -X POST http://localhost:8000/tokens/<TOKEN>/revoke \
  -H "X-Service-Key: dev-service-key-change-me"

# 6. 永久撤回快照（首次需带原因；重复撤回返回首次 withdrawn_at 与 status）
curl -sS -X POST http://localhost:8000/snapshots/<SID>/withdraw \
  -H "X-Service-Key: dev-service-key-change-me" -H "Content-Type: application/json" \
  -d '{"reason":"发现该版本不应继续公开"}'
# => {"snapshot_id":"<SID>","withdrawn_at":"2026-09-30T12:00:00Z","status":"withdrawn"}
# 撤回后：公开读取（任意令牌）一律 403 snapshot unavailable 且不消耗次数；
#        该版本再次直接发布/提交受控申请/批准在审申请均 409，待审申请仍可驳回。
curl -sS "http://localhost:8000/public/snapshots/<SID>?token=<TOKEN>"
# => 403 {"detail":"snapshot unavailable"}
# 维护侧记录状态可见撤回时间与原因（公开侧不透露原因）
curl -sS http://localhost:8000/records/<RID> -H "X-Service-Key: dev-service-key-change-me"
# => {"snapshots":[{"version":2,"status":"withdrawn",
#                   "withdrawn_at":"...","withdraw_reason":"发现该版本不应继续公开", ...}, ...]}
```

完整端到端演示（含 401/409/422、快照不可变、`max_reads` 次数上限与独立计数、维护侧计数查询、撤销与到期）：

```bash
./examples/demo.sh        # 依赖 curl 与 jq；可用 BASE_URL / SERVICE_KEY 覆盖默认配置
```

完整快照撤回演示（含 401/404/422、重复撤回幂等、撤回后任意令牌 403 不计数、同记录其他版本可读、三类发布冲突 409、在审申请仍可驳回）：

```bash
./examples/demo_withdraw.sh   # 可用 BASE_URL / SERVICE_KEY / ALICE_PW / BOB_PW 覆盖
```

多快照批量凭证演示（清单冻结与批准复核、一次性凭证、跨快照共用额度、单项撤回隔离、到期/撤销、维护侧查询）：

```bash
./examples/demo_bundle.sh     # 可用 BASE_URL / SERVICE_KEY / ALICE_PW / BOB_PW / CAROL_PW 覆盖
```

## 受控发布（申请-复核）

受控记录**不能**直接发布，必须走申请-复核流程。

### 语义与规则

1. **标记受控**：机构凭服务密钥 `PUT /records/{id}/controlled`（幂等）。受控记录的直接发布请求返回 `409`；非受控记录的受控申请同样返回 `409`（引导使用正确入口）。
2. **提交申请**：具备 `operator` 角色者用**本人凭据** `POST /records/{id}/publish-requests`，指定一个**历史版本**（`version`，可带 `expires_in_seconds`/`expires_at` 与可选正整数 `max_reads`，规则与直接发布一致）。申请在提交时**冻结该版本的原文与敏感区间，以及读取次数上限**，后续修订只产生新版本，不改变待审内容与上限。
3. **复核**：具备 `reviewer` 角色的**另一人**批准或驳回：
   - **申请人不得复核**自己的申请（即使其同时拥有 reviewer 角色）→ `403`；
   - 批准与驳回互斥：驳回后不能批准、批准后不能再驳回；对已终结申请再次操作 → `409`；
   - 并发复核由数据库行锁串行化，**同一申请只会产生一个终态**，落选方收到 `409`。
4. **批准事务**：沿用既有脱敏（合并相交/相邻区间→`[已遮蔽]`）与令牌（`secrets.token_urlsafe(32)`、可选到期）规则，在**同一事务**内生成不可变快照、写入新读取令牌（携带申请时冻结的 `max_reads`）并终结申请；任何一步失败整体回滚，**不会留下可读令牌或计数**。
5. **驳回**：只终结申请（可记录 `reason`），不生成快照、令牌或读取计数。
6. **拒绝不泄露原文**：无凭据/凭据错误 → `401`，无角色 → `403`，未知记录/版本/申请 → `404`，已终结/入口错误 → `409`；申请查询只返回元数据（含 `token_max_reads`），冻结原文绝不离开服务端。
7. 批准签发的令牌与直接发布令牌完全一致：遵守 `max_reads` 次数上限与到期规则，可用 `POST /tokens/{token}/revoke` 撤销，可用 `GET /tokens/{token}` 查询已用/剩余次数。

### 调用示例

```bash
# 0. 机构把记录设为受控（服务密钥；幂等）
curl -sS -X PUT http://localhost:8000/records/<RID>/controlled \
  -H "X-Service-Key: dev-service-key-change-me"
# => {"record_id":"<RID>","controlled":true}

# 0b. 受控记录的直接发布请求被明确拒绝（不生成快照）
curl -sS -X POST http://localhost:8000/records/<RID>/publish \
  -H "X-Service-Key: dev-service-key-change-me" -H "Content-Type: application/json" \
  -d '{"version": 1}'
# => 409 {"detail":"record is controlled; submit a publish request for review instead"}

# 1. 申请人 alice（operator）用本人凭据提交历史版本 1 的发布申请
#    max_reads 在申请时冻结，批准签发后生效（驳回则不生成令牌/计数）
curl -sS -X POST http://localhost:8000/records/<RID>/publish-requests \
  -u alice:alice-pw -H "Content-Type: application/json" \
  -d '{"version": 1, "max_reads": 3}'
# => {"request_id":"<PRID>","record_id":"<RID>","version":1,"status":"pending",
#     "requested_by":"alice","created_at":"..."}

# 1b. 申请详情可见冻结的上限（只含元数据，不含原文）
curl -sS http://localhost:8000/publish-requests/<PRID> -u alice:alice-pw
# => {...,"token_expires_in_seconds":null,"token_expires_at":null,"token_max_reads":3,...}

# 2. 申请人不能复核自己的申请
curl -sS -X POST http://localhost:8000/publish-requests/<PRID>/approve -u alice:alice-pw
# => 403 {"detail":"the requester may not review their own publish request"}

# 3. 另一人 bob（reviewer）批准：同事务生成不可变快照 + 新读取令牌
curl -sS -X POST http://localhost:8000/publish-requests/<PRID>/approve -u bob:bob-pw
# => {"request_id":"<PRID>","record_id":"<RID>","version":1,"status":"approved",
#     "reviewed_by":"bob","snapshot_id":"<SID>","token":"<TOKEN>","expires_at":null,"max_reads":3}

# 4. 公众凭令牌读取冻结版本的脱敏正文：恰好 3 次 200，第 4 次起 403 snapshot unavailable
curl -sS "http://localhost:8000/public/snapshots/<SID>?token=<TOKEN>"
# => {"record_id":"<RID>","version":1,"status":"published","redacted_text":"患者[已遮蔽]，……"}
curl -sS http://localhost:8000/tokens/<TOKEN> -H "X-Service-Key: dev-service-key-change-me"
# => {"max_reads":3,"reads_used":1,"remaining_reads":2,"active":true,...}（不含原文）

# —— 驳回路径 ——
curl -sS -X POST http://localhost:8000/records/<RID>/publish-requests \
  -u alice:alice-pw -H "Content-Type: application/json" -d '{"version": 2}'
# => {"request_id":"<PRID2>",...}
curl -sS -X POST http://localhost:8000/publish-requests/<PRID2>/reject \
  -u bob:bob-pw -H "Content-Type: application/json" -d '{"reason":"材料不全"}'
# => {"request_id":"<PRID2>","status":"rejected","reviewed_by":"bob","reason":"材料不全",...}
curl -sS -X POST http://localhost:8000/publish-requests/<PRID2>/approve -u bob:bob-pw
# => 409 {"detail":{"message":"publish request already decided","status":"rejected"}}

# 查询（任一已认证操作者；只返回元数据，不返回原文）
curl -sS http://localhost:8000/publish-requests/<PRID> -u alice:alice-pw
curl -sS http://localhost:8000/records/<RID>/publish-requests -u alice:alice-pw
```

完整受控流程演示（含 401/403/404/409、冻结内容与 `max_reads`、申请人回避、并发复核终态唯一、次数用尽与撤销）：

```bash
./examples/demo_controlled.sh   # 可用 BASE_URL / SERVICE_KEY / ALICE_PW / BOB_PW / CAROL_PW 覆盖
```

### 状态码约定（受控接口）

| 场景 | 状态码 |
| --- | --- |
| 缺少/错误 Basic 凭据 | `401`（带 `WWW-Authenticate: Basic`） |
| 已认证但缺少所需角色（operator/reviewer） | `403` |
| 申请人复核自己的申请 | `403` |
| 未知记录 / 版本 / 申请 | `404` |
| 受控记录直接发布、非受控记录提交申请、对已终结申请再操作、提交/批准命中已撤回版本、令牌到期时间非法 | `409` / `422` |

## 多快照批量凭证（同一外部审阅者）

机构需要把**多个已发布快照**交给同一外部审阅者时，可走批量授权流程：一次申请冻结一个快照清单与一笔**共用读取总额度**，批准后只签发**一支随机凭证**，该凭证在既有公开读取接口上访问清单内全部快照的脱敏正文，合计扣减同一额度。

### 语义与规则

1. **提交申请**：具备 `operator` 角色者用本人凭据 `POST /bundle-requests`，请求体：
   - `snapshot_ids`：**至少两个、互不重复**的已发布快照 ID（顺序即清单冻结顺序；不足两个或有重复 → `422`）；
   - `max_reads`：**必填正整数**，清单内全部快照的成功读取合计共用这笔总额度（批量凭证不提供“不限次”）；
   - `expires_in_seconds` / `expires_at`：可选到期策略，二选一，规则与单快照令牌一致（相对时长在批准签发时换算）。

   提交时即核对清单内快照**全部存在**（任一不存在 → `404`）且**未撤回**（任一已撤回 → `409`）；快照可以来自不同记录。通过后清单与额度在此**冻结**。
2. **复核**：具备 `reviewer` 角色的**另一人**批准或驳回，规则与受控发布一致：
   - 申请人不得复核自己的申请（即使兼具 reviewer）→ `403`；
   - 批准/驳回互斥且为终态，对已终结申请再操作 → `409`，并发行锁保证同一申请只有一个终态。
3. **批准时重新核对**：批准事务内对清单内快照全部加行锁并重新核对**仍存在且未撤回**；任一项失效则**整次批准失败**（不存在 → `404`、已撤回/清单残缺 → `409`），**不签发凭证**，申请保持 `pending`、**仍可驳回**。
4. **一次性签发**：全部有效时，凭证（`secrets.token_urlsafe(32)`）、凭证-清单绑定与申请终态在**同一事务**提交；中途失败整体回滚，不留可读凭证。
5. **公开读取（沿用既有接口与响应）**：`GET /public/snapshots/{snapshot_id}?token=<批量凭证>`：
   - 仅当凭证未撤销、未到期、共用额度未耗尽，**且该快照在冻结清单内**时才返回脱敏正文；
   - 响应结构与单快照令牌完全一致；快照不存在/已撤回、凭证无效、清单外快照、到期、撤销、额度耗尽统一返回模糊 `403 snapshot unavailable`；
   - **每一次清单内成功读取扣减一笔共享额度**；清单外、无效凭证、到期、撤销等失败请求不扣额度；
   - 计数判定与递增是凭证行上的**同一条条件 `UPDATE`**（含清单绑定的 `EXISTS` 子查询），并发读取由行锁串行化，跨快照合计成功次数**绝不超过** `max_reads`。
6. **单项撤回隔离**：凭证签发后清单中某项快照被撤回，公开读取在快照行检查阶段即返回 `403`，**只拒绝该项且不消耗共享额度**；清单内其他未撤回快照继续可读、继续共用同一额度。
7. **整凭证失效**：凭证**到期、被维护侧撤销或共用额度耗尽**时，清单内**全部**快照的读取一律 `403`。
8. **维护侧（服务密钥）**：
   - `GET /bundle-tokens/{token}`：返回凭证的冻结清单（每项含实时 `published`/`withdrawn` 状态）、`max_reads` / `reads_used` / `remaining_reads`、`expires_at` / `revoked_at` 与 `active`；**不含任何正文**，未知凭证 `404`、缺密钥 `401`；
   - `POST /bundle-tokens/{token}/revoke`：撤销凭证（原子条件更新；重复撤销返回首次结果，幂等）。
9. **兼容性**：既有单快照令牌（直接发布/受控批准签发）与受控发布各接口的行为、路径与响应**完全不变**；单快照令牌走 `/tokens/...`，批量凭证走 `/bundle-tokens/...`，公开读取接口同时接受两者且对外不可区分。

### 调用示例

```bash
# 1. alice（operator）提交两个已发布快照的批量申请：共用总额度 4、3600s 后到期
curl -sS -X POST http://localhost:8000/bundle-requests \
  -u alice:alice-pw -H "Content-Type: application/json" \
  -d '{"snapshot_ids": ["<SID1>", "<SID2>"], "max_reads": 4, "expires_in_seconds": 3600}'
# => {"request_id":"<BREQ>","status":"pending","requested_by":"alice",
#     "token_max_reads":4,"snapshots":[{"position":0,"snapshot_id":"<SID1>",
#     "record_id":"...","version":1,"status":"published","withdrawn_at":null}, ...], ...}

# 不足两个/重复快照、max_reads 非正整数 -> 422；含未知快照 -> 404；含已撤回快照 -> 409

# 2. 申请人不得自审
curl -sS -X POST http://localhost:8000/bundle-requests/<BREQ>/approve -u alice:alice-pw
# => 403 {"detail":"the requester may not review their own bundle request"}

# 3. bob（reviewer，另一人）批准：重新核对全部快照后一次性签发凭证
curl -sS -X POST http://localhost:8000/bundle-requests/<BREQ>/approve -u bob:bob-pw
# => {"request_id":"<BREQ>","status":"approved","reviewed_by":"bob",
#     "snapshots":[...],"token":"<BTOKEN>","expires_at":"...","max_reads":4}
# 若待审期间某快照被撤回：=> 409，不签发凭证；申请仍可驳回：
curl -sS -X POST http://localhost:8000/bundle-requests/<BREQ>/reject \
  -u bob:bob-pw -H "Content-Type: application/json" -d '{"reason":"清单内快照已撤回"}'

# 4. 外部审阅者用既有公开接口读取清单内任一快照的脱敏正文
curl -sS "http://localhost:8000/public/snapshots/<SID1>?token=<BTOKEN>"
# => {"record_id":"...","version":1,"status":"published","redacted_text":"……"}
# 清单外快照（或无效/到期/撤销/额度耗尽）：
# => 403 {"detail":"snapshot unavailable"}

# 5. 维护侧查询清单、共用额度与状态（不含正文）
curl -sS http://localhost:8000/bundle-tokens/<BTOKEN> -H "X-Service-Key: dev-service-key-change-me"
# => {"token":"<BTOKEN>","request_id":"<BREQ>","max_reads":4,"reads_used":1,
#     "remaining_reads":3,"active":true,"revoked_at":null,
#     "snapshots":[{"position":0,"snapshot_id":"<SID1>","version":1,"status":"published",...},
#                  {"position":1,"snapshot_id":"<SID2>","version":1,"status":"published",...}]}

# 6. 清单内某单项撤回：只拒绝该项、不扣共享额度，其他项照读
curl -sS -X POST http://localhost:8000/snapshots/<SID2>/withdraw \
  -H "X-Service-Key: dev-service-key-change-me" -H "Content-Type: application/json" \
  -d '{"reason":"该版本不再对外提供"}'
curl -sS "http://localhost:8000/public/snapshots/<SID2>?token=<BTOKEN>"
# => 403 {"detail":"snapshot unavailable"}（reads_used 不增加）

# 7. 维护侧撤销凭证：撤销/到期/额度耗尽后清单内全部 403
curl -sS -X POST http://localhost:8000/bundle-tokens/<BTOKEN>/revoke \
  -H "X-Service-Key: dev-service-key-change-me"
# => {"token":"<BTOKEN>","revoked":true,"already_revoked":false}
```

完整批量凭证端到端演示（含 422/404/409/403、申请人回避、批准时重新核对失败仍可驳回、跨快照共用额度与并发不越限、单项撤回隔离不扣额度、到期/撤销全部拒绝、维护侧查询）：

```bash
./examples/demo_bundle.sh   # 可用 BASE_URL / SERVICE_KEY / ALICE_PW / BOB_PW / CAROL_PW 覆盖
```

### 状态码约定（批量接口）

| 场景 | 状态码 |
| --- | --- |
| 缺少/错误 Basic 凭据 | `401`（带 `WWW-Authenticate: Basic`） |
| 已认证但缺少所需角色、申请人复核自己的批量申请 | `403` |
| 未知批量申请 / 凭证 | `404` |
| 提交清单含未知快照 | `404` |
| 提交/批准清单含已撤回快照、清单残缺、对已终结申请再操作 | `409` |
| 快照少于两个、存在重复 ID、`max_reads` 非正整数、到期时间非法 | `422` |
| 维护接口缺少/错误服务密钥 | `401` |
| 公开读取：清单外/无效/已撤回/到期/撤销/额度耗尽 | 模糊 `403 snapshot unavailable` |

## 设计要点

- **不可变快照**：发布时把脱敏结果写入 `snapshots`（按 `(record_id, version)` 唯一），公开读取只访问快照；后续修订写入新版本，旧快照不变。
- **并发安全**：
  - 修订用条件更新 `UPDATE ... WHERE current_version = :base_version`，原子比较并递增，并发修订只有一个成功，其余收到 `409` 与当前版本；
  - 发布用 `INSERT ... ON CONFLICT DO NOTHING` 保证同一版本只有一份快照，快照与令牌同事务提交，不存在部分发布；
  - 受控复核用 `SELECT ... FOR UPDATE` 锁定申请行串行化并发批准/驳回，配合终态检查保证同一申请只有一个终态；
  - 公开读取的**有效性判定与次数消耗合并为同一条条件 `UPDATE ... WHERE 未撤销 AND 未到期 AND (不限次 OR reads_used < max_reads)`**，命中行即 `reads_used = reads_used + 1`：行锁串行化并发读取，成功次数绝不超过上限；撤销/到期不命中因而不消耗次数，且优先于“尚有剩余次数”；
  - 撤销是单条原子 `UPDATE`，提交后新开始的读取即被拒绝。
- **快照永久撤回**：
  - `snapshots` 增加 `withdrawn_at` / `withdraw_reason`：`withdrawn_at` 为空即仍公开，非空为撤回终态；撤回原因仅维护侧返回，公开侧永不透露；
  - 撤回是一条条件 `UPDATE ... WHERE withdrawn_at IS NULL`，并发撤回只有一个首次结果，落选方读回首次时间与状态，重复撤回完全幂等；
  - 公开读取先 `SELECT ... FOR UPDATE` 锁定快照行再判定，撤回与读取按快照行锁串行化：撤回提交后开始的读取在计数 `UPDATE` 之前就收到模糊 403，**不返回正文也不消耗次数**；
  - 直接发布/受控批准在写令牌前以 `FOR UPDATE` 取快照并检查撤回状态，提交受控申请在写申请前检查；已撤回版本的重新公开一律 `409`，但撤回前已在审的申请仍可驳回；
  - 撤回不删除任何数据：历史脱敏正文、同记录其他版本快照、已批准申请记录全部留存，仅公开读取被关闭。
- **不泄露原文**：公开接口只返回快照中的脱敏正文；令牌无效/撤销/到期/次数用尽/快照已撤回/与快照不匹配统一返回模糊的 `403 snapshot unavailable`；维护侧令牌查询与受控申请查询只返回元数据与计数，错误信息不含原文内容。
- **令牌**：`secrets.token_urlsafe(32)` 随机生成；支持 `expires_in_seconds` 或 `expires_at`（二选一）设置到期时间，并支持可选正整数 `max_reads` 读取次数上限；到期、撤销或次数用尽后读取一律拒绝。直接发布与受控批准共用同一套令牌规则、计数、查询与撤销接口。多快照批量凭证为另一支同强度随机凭证，绑定冻结清单并以凭证行为计数单位共用正整数总额度，公开读取接口与单快照令牌共用入口、维护侧使用独立的 `/bundle-tokens/...` 查询与撤销接口。
- **受控发布**：
  - 申请行保存**冻结副本** `frozen_text`/`frozen_intervals`，复核批准基于副本做脱敏，历史版本行即使变化也不影响待审内容；
  - 批准/驳回先 `SELECT ... FOR UPDATE` 锁定申请行，把并发复核串行化：先提交者写入终态，后提交者看到非 `pending` 收到 `409`，配合 `status` 检查约束，保证同一申请只有一个终态；
  - 批准时**快照、新令牌、申请终态在同一事务提交**，任一异常整体回滚，不会留下无主或可读令牌；驳回不产生快照/令牌；
  - 个人凭据（HTTP Basic）与机构服务密钥（`X-Service-Key`）分离，角色为 `operator`/`reviewer`，申请人与复核人身份按用户名强制不同；
  - 既有数据库升级时启动会幂等补列 `records.controlled`（旧记录默认非受控）、`read_tokens.max_reads`/`reads_used`（既有令牌 `max_reads` 为空即继续不限次、已用为 0）、`publish_requests.token_max_reads` 与 `snapshots.withdrawn_at`/`withdraw_reason`（既有快照默认未撤回、继续可读），新表随 `create_all` 幂等创建。
- **多快照批量凭证**：
  - `bundle_requests`（申请：状态、申请人/复核人、到期策略、必填正整数 `token_max_reads`）与 `bundle_request_items`（冻结清单：`(request_id, position)` 保序、`(request_id, snapshot_id)` 防重复）共同冻结清单；批准签发 `bundle_tokens`（一支凭证一行，含共用 `max_reads`/`reads_used`、到期、撤销）并在 `bundle_token_snapshots` 绑定清单；新表随启动 `create_all` 幂等创建，不改动既有表；
  - 提交时与批准时**两次核对**：提交确认清单全部存在且未撤回后才冻结；批准在同一事务内对申请行与清单快照行加 `FOR UPDATE` 锁重新核对，任一项不存在/已撤回或清单残缺则整次失败回滚，**不签发凭证**，申请仍可驳回；
  - 凭证、清单绑定与申请终态在**同一事务**提交，任一异常整体回滚；一支凭证只对应一次批准（`bundle_tokens.request_id` 唯一）；
  - 公开读取在既有单快照令牌条件 `UPDATE` 未命中后，再对 `bundle_tokens` 执行一条条件 `UPDATE ... WHERE 未撤销 AND 未到期 AND reads_used < max_reads AND EXISTS(清单绑定)`：清单判定、额度判定与递增原子完成，计数行是凭证本身——清单内全部快照的并发读取串行化在同一行，跨快照合计成功次数绝不越限；失败请求不计数；
  - 清单内某项撤回时，公开读取先在快照行锁阶段看到 `withdrawn_at` 即模糊 403，**不触碰凭证计数**：只拒绝该项、不扣共享额度，其他项继续可读；凭证到期、撤销或额度耗尽则清单内全部拒绝；
  - 单快照令牌与批量凭证仅在公开读取接口共用入口，对外响应（含模糊 403）不可区分；维护侧命名空间分离（`/tokens/...` 与 `/bundle-tokens/...`），既有单快照令牌接口行为与响应保持兼容。

## 本地开发

```bash
pip install -r requirements.txt
export SERVICE_KEY=dev-service-key-change-me
export DATABASE_URL=postgresql+psycopg2://redact:redact@localhost:5432/redact
# 可选：配置受控发布操作者（不配置则受控接口一律 401，其他功能不受影响）
export REVIEW_CREDENTIALS='[{"username":"alice","password":"alice-pw","roles":["operator"]},{"username":"bob","password":"bob-pw","roles":["reviewer"]}]'
uvicorn app.main:app --reload --port 8000

# 脱敏核心逻辑单元测试（仅依赖标准库）
python3 -m unittest tests.test_redaction -v
# 凭据配置校验单元测试
python3 -m unittest tests.test_config -v
# 受控发布端到端集成测试：默认用 pgserver 内置 PostgreSQL 自动建临时库，
# 也可用 TEST_DATABASE_URL 指向已有数据库
python3 -m unittest tests.test_integration_api -v
```

## 目录结构

```
app/
  main.py       # 路由与事务逻辑（受控发布申请-复核、令牌读取次数计数与查询、快照永久撤回、多快照批量凭证）
  models.py     # records / record_versions / snapshots(含撤回字段) / read_tokens(含 max_reads/reads_used) / publish_requests / bundle_requests(+items) / bundle_tokens(+snapshots)
  redaction.py  # 区间校验、合并、替换（纯函数）
  schemas.py    # 请求/响应模型（含撤回请求/响应、max_reads 与令牌状态响应）
  security.py   # 服务密钥与操作者个人凭据（Basic）鉴权、角色依赖
  config.py     # 环境变量配置（含 REVIEW_CREDENTIALS 校验）
  db.py         # 引擎与会话
examples/
  demo.sh             # 直接发布端到端调用示例
  demo_controlled.sh  # 受控发布（申请-复核）端到端调用示例
  demo_withdraw.sh    # 快照永久撤回端到端调用示例
  demo_bundle.sh      # 多快照批量凭证（申请-复核、共用额度、单项撤回隔离、撤销）端到端调用示例
tests/
  test_redaction.py       # 脱敏逻辑单元测试
  test_config.py          # 操作者凭据配置校验单元测试
  test_integration_api.py # 受控/既有接口端到端集成测试（真实 PostgreSQL）
Dockerfile / docker-compose.yml
```
