#!/usr/bin/env bash
# 多快照批量凭证端到端示例：
#   发布多个快照 -> operator 提交批量申请（冻结清单+共用额度，至少两个不重复快照）->
#   申请人不得自审 -> 待审期间撤回一项导致整次批准失败（仍可驳回）->
#   第二次申请批准成功，一次性签发随机凭证 ->
#   公开接口凭凭证只读清单内脱敏正文，共享额度合计扣减 ->
#   清单内某项撤回只拒绝该项且不扣额度，其他项照读 ->
#   到期/撤销/额度耗尽全部拒绝 -> 维护侧查询清单、额度与状态
# 依赖：curl、jq
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
SERVICE_KEY="${SERVICE_KEY:-dev-service-key-change-me}"
ALICE_PW="${ALICE_PW:-alice-pw}"   # operator（申请人）
BOB_PW="${BOB_PW:-bob-pw}"         # reviewer（复核人）
CAROL_PW="${CAROL_PW:-carol-pw}"   # operator + reviewer
AUTH=(-H "X-Service-Key: ${SERVICE_KEY}" -H "Content-Type: application/json")

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
code() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }

step "0. 健康检查"
curl -sS "${BASE_URL}/healthz" | jq .

step "1. 维护端创建两条记录并各发布一个已发布快照"
CREATE_A=$(curl -sS -X POST "${BASE_URL}/records" "${AUTH[@]}" -d '{
  "text": "患者张三，身份证号 1101010119900307777，联系电话 13800001111。",
  "intervals": [{"start": 2, "end": 4}, {"start": 10, "end": 28}, {"start": 34, "end": 45}]
}')
RID_A=$(echo "${CREATE_A}" | jq -r .record_id)
SID1=$(curl -sS -X POST "${BASE_URL}/records/${RID_A}/publish" "${AUTH[@]}" \
  -d '{"version": 1}' | jq -r .snapshot_id)
CREATE_B=$(curl -sS -X POST "${BASE_URL}/records" "${AUTH[@]}" -d '{
  "text": "患者王五，卡号 6222000000000000。",
  "intervals": [{"start": 5, "end": 21}]
}')
RID_B=$(echo "${CREATE_B}" | jq -r .record_id)
SID2=$(curl -sS -X POST "${BASE_URL}/records/${RID_B}/publish" "${AUTH[@]}" \
  -d '{"version": 1}' | jq -r .snapshot_id)
# 记录 A 修订后再发布版本 2，作为第三个快照
curl -sS -X PUT "${BASE_URL}/records/${RID_A}" "${AUTH[@]}" -d '{
  "base_version": 1,
  "text": "完全不同的新原文，敏感词ABC。",
  "intervals": [{"start": 9, "end": 15}]
}' | jq .
SID3=$(curl -sS -X POST "${BASE_URL}/records/${RID_A}/publish" "${AUTH[@]}" \
  -d '{"version": 2}' | jq -r .snapshot_id)
echo "快照清单: SID1=${SID1} SID2=${SID2} SID3=${SID3}"

step "2. 提交校验：不足两个 / 重复 ID / max_reads 非正整数 -> 422"
printf '单个快照: HTTP %s\n' "$(code -X POST "${BASE_URL}/bundle-requests" \
  -u "alice:${ALICE_PW}" -H 'Content-Type: application/json' \
  -d "{\"snapshot_ids\":[\"${SID1}\"],\"max_reads\":3}")"
printf '重复快照: HTTP %s\n' "$(code -X POST "${BASE_URL}/bundle-requests" \
  -u "alice:${ALICE_PW}" -H 'Content-Type: application/json' \
  -d "{\"snapshot_ids\":[\"${SID1}\",\"${SID1}\"],\"max_reads\":3}")"
printf 'max_reads=0: HTTP %s\n' "$(code -X POST "${BASE_URL}/bundle-requests" \
  -u "alice:${ALICE_PW}" -H 'Content-Type: application/json' \
  -d "{\"snapshot_ids\":[\"${SID1}\",\"${SID2}\"],\"max_reads\":0}")"

step "3. alice(operator) 提交批量申请：冻结清单（按提交顺序）+ 共用总额度 4"
SUBMIT=$(curl -sS -X POST "${BASE_URL}/bundle-requests" \
  -u "alice:${ALICE_PW}" -H "Content-Type: application/json" \
  -d "{\"snapshot_ids\":[\"${SID1}\",\"${SID2}\"],\"max_reads\":4}")
echo "${SUBMIT}" | jq '{request_id, status, requested_by, token_max_reads, snapshots}'
REQ1=$(echo "${SUBMIT}" | jq -r .request_id)

step "4. 申请人不得自审；operator 不能复核"
printf 'alice 自批: HTTP %s\n' "$(code -X POST "${BASE_URL}/bundle-requests/${REQ1}/approve" -u "alice:${ALICE_PW}")"

step "5. 待审期间撤回 SID2：bob 批准时重新核对，整次失败 409，不签发凭证；申请仍可驳回"
curl -sS -X POST "${BASE_URL}/snapshots/${SID2}/withdraw" "${AUTH[@]}" \
  -d '{"reason":"外部审阅前发现该快照需撤回"}' | jq .
printf '批准(含已撤回项): HTTP %s\n' "$(code -X POST "${BASE_URL}/bundle-requests/${REQ1}/approve" -u "bob:${BOB_PW}")"
curl -sS -X POST "${BASE_URL}/bundle-requests/${REQ1}/reject" \
  -u "bob:${BOB_PW}" -H "Content-Type: application/json" \
  -d '{"reason":"清单内快照已撤回"}' | jq '{request_id, status, reason}'

step "6. 重新提交不含撤回项的申请（SID1 + SID3，额度 4、3600s 后到期），bob 批准"
SUBMIT2=$(curl -sS -X POST "${BASE_URL}/bundle-requests" \
  -u "alice:${ALICE_PW}" -H "Content-Type: application/json" \
  -d "{\"snapshot_ids\":[\"${SID1}\",\"${SID3}\"],\"max_reads\":4,\"expires_in_seconds\":3600}")
REQ2=$(echo "${SUBMIT2}" | jq -r .request_id)
APPROVE=$(curl -sS -X POST "${BASE_URL}/bundle-requests/${REQ2}/approve" -u "bob:${BOB_PW}")
echo "${APPROVE}" | jq '{request_id, status, reviewed_by, max_reads, expires_at, snapshots}'
BTOKEN=$(echo "${APPROVE}" | jq -r .token)
echo "一次性签发的批量凭证: ${BTOKEN}"

step "7. 公开读取沿用既有接口与响应：只能读清单内快照，模糊 403 不变"
curl -sS "${BASE_URL}/public/snapshots/${SID1}?token=${BTOKEN}" | jq .
echo "读清单外的 SID2（且已撤回）:"
curl -sS "${BASE_URL}/public/snapshots/${SID2}?token=${BTOKEN}" | jq .

step "8. 共享额度：额度为 4，第 7 步已成功 1 次；再成功 3 次（跨快照）后全部 403"
for SID in "${SID3}" "${SID1}" "${SID3}"; do
  printf '读 %s: HTTP %s\n' "${SID}" "$(code "${BASE_URL}/public/snapshots/${SID}?token=${BTOKEN}")"
done
# 已成功 1（第7步）+3 = 4 次，共用额度耗尽
printf '额度耗尽后读 SID1: HTTP %s\n' "$(code "${BASE_URL}/public/snapshots/${SID1}?token=${BTOKEN}")"
printf '额度耗尽后读 SID3: HTTP %s（清单内任一项均拒绝）\n' "$(code "${BASE_URL}/public/snapshots/${SID3}?token=${BTOKEN}")"

step "9. 维护侧查询凭证清单、共用额度与状态（服务密钥；不含正文）"
curl -sS "${BASE_URL}/bundle-tokens/${BTOKEN}" "${AUTH[@]}" \
  | jq '{max_reads, reads_used, remaining_reads, active, revoked_at, snapshots: [.snapshots[] | {position, version, status}]}'

step "10. 撤回清单内某项只拒绝该项且不扣额度（用新凭证演示隔离语义）"
SUBMIT3=$(curl -sS -X POST "${BASE_URL}/bundle-requests" \
  -u "alice:${ALICE_PW}" -H "Content-Type: application/json" \
  -d "{\"snapshot_ids\":[\"${SID1}\",\"${SID3}\"],\"max_reads\":5}")
REQ3=$(echo "${SUBMIT3}" | jq -r .request_id)
BTOKEN2=$(curl -sS -X POST "${BASE_URL}/bundle-requests/${REQ3}/approve" \
  -u "bob:${BOB_PW}" | jq -r .token)
printf '读 SID1: HTTP %s（成功，扣一次）\n' "$(code "${BASE_URL}/public/snapshots/${SID1}?token=${BTOKEN2}")"
# SID3 此前未撤回；这里撤回它以演示“只拒绝该项”
curl -sS -X POST "${BASE_URL}/snapshots/${SID3}/withdraw" "${AUTH[@]}" \
  -d '{"reason":"演示单项撤回隔离"}' | jq .
printf '读已撤回项 SID3: HTTP %s（拒绝且不扣额度）\n' "$(code "${BASE_URL}/public/snapshots/${SID3}?token=${BTOKEN2}")"
printf '读未撤回项 SID1: HTTP %s（仍可读）\n' "$(code "${BASE_URL}/public/snapshots/${SID1}?token=${BTOKEN2}")"
curl -sS "${BASE_URL}/bundle-tokens/${BTOKEN2}" "${AUTH[@]}" \
  | jq '{reads_used, remaining_reads, active, snapshots: [.snapshots[] | {version, status}]}'

step "11. 维护侧撤销凭证：撤销后清单内全部 403；重复撤销幂等"
curl -sS -X POST "${BASE_URL}/bundle-tokens/${BTOKEN2}/revoke" "${AUTH[@]}" | jq .
printf '撤销后读 SID1: HTTP %s\n' "$(code "${BASE_URL}/public/snapshots/${SID1}?token=${BTOKEN2}")"
curl -sS -X POST "${BASE_URL}/bundle-tokens/${BTOKEN2}/revoke" "${AUTH[@]}" | jq .

step "12. 既有单快照令牌接口保持兼容（批量凭证不占用 /tokens 命名空间）"
printf '批量凭证访问 /tokens/{token} 状态接口: HTTP %s\n' \
  "$(code "${BASE_URL}/tokens/${BTOKEN}" -H "X-Service-Key: ${SERVICE_KEY}")"

printf '\n\033[1m多快照批量凭证演示完成。\033[0m\n'
