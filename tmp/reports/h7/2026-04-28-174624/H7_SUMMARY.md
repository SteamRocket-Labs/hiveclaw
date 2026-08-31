# H7 Evidence Loop Summary

Status: FAIL
Reason: Missing production platform admin bearer token.

No production endpoint was called because none of these token sources were available:

- HIVE_PLATFORM_ADMIN_TOKEN
- HIVE_ADMIN_TOKEN
- HIVE_TOKEN
- .h7.env in the repo root

Required setup:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
export BASE_URL="https://backend-production-326d.up.railway.app"
export ADMIN_USER="你的平台管理员用户名或邮箱"
export ADMIN_PASSWORD="你的密码"

export HIVE_PLATFORM_ADMIN_TOKEN=$(
  curl -fsS -X POST "$BASE_URL/api/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASSWORD\"}" \
  | jq -r '.access_token'
)

cat > .h7.env <<ENV
HIVE_BASE_URL=https://backend-production-326d.up.railway.app
HIVE_PLATFORM_ADMIN_TOKEN=$HIVE_PLATFORM_ADMIN_TOKEN
ENV
```

After setup, rerun the H7 one-shot check.
