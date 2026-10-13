#!/bin/bash
# 鉴权绕过测试 —— 全部打真实运行的服务，只看 HTTP 状态码。
#
# 判定：受保护的端点在没有/错误凭据时必须返回 401。
# 任何 2xx/3xx 都是漏洞。
#
# 用法：bash audit-auth.sh [port]

PORT="${1:-8200}"
BASE="http://127.0.0.1:${PORT}"
TOKEN_FILE="$(dirname "$0")/.token"
T=$(cat "$TOKEN_FILE" 2>/dev/null || cat /opt/shared-memory-mcp/.token)

pass=0
fail=0

# check <期望码> <描述> <curl 参数...>
check() {
  local want="$1"; shift
  local desc="$1"; shift
  local code
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 "$@")
  if [ "$code" = "$want" ]; then
    printf "  ✓ %-44s %s\n" "$desc" "$code"
    pass=$((pass+1))
  else
    printf "  ✗ %-44s %s（期望 %s）\n" "$desc" "$code" "$want"
    fail=$((fail+1))
  fi
}

echo "════ 鉴权边界（端口 $PORT）════"
echo
echo "── 应当被拒绝（401）──"
check 401 "GET /            无凭据" "$BASE/"
check 401 "GET /api/state   无凭据" "$BASE/api/state"
check 401 "GET /api/config  无凭据" "$BASE/api/config"
check 401 "POST /api/config 无凭据（改配置）" -X POST "$BASE/api/config" \
  -H 'Content-Type: application/json' -d '{"instructions":"HACKED"}'
check 401 "POST /mcp        无凭据（调工具）" -X POST "$BASE/mcp" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"memory_write","arguments":{"title":"HACK","body":"x"}}}'
check 401 "GET 错误 token" "$BASE/api/state?token=wrongtoken123"
check 401 "GET 空 token" "$BASE/api/state?token="
check 401 "GET Bearer 错 token" "$BASE/api/state" -H 'Authorization: Bearer nope'
check 401 "GET token 大写变换" "$BASE/api/state?token=$(echo "$T" | tr 'a-z' 'A-Z')"
check 401 "GET token 加前缀" "$BASE/api/state?token=x$T"
check 401 "GET token 加后缀" "$BASE/api/state?token=${T}x"
check 401 "POST 只带 query token（写操作必须走 header）" -X POST "$BASE/api/config?token=$T" \
  -H 'Content-Type: application/json' -d '{"instructions":"HACK-VIA-QUERY"}'

echo
echo "── 应当放行（200）──"
check 200 "GET /            query token" "$BASE/?token=$T"
check 200 "GET /api/state   query token" "$BASE/api/state?token=$T"
check 200 "GET /api/state   bearer token" "$BASE/api/state" -H "Authorization: Bearer $T"
check 200 "GET /health      query token" "$BASE/health?token=$T"

echo
echo "── 静态资源（面板需要，但要确认不是无凭据开放）──"
check 401 "GET /panel.css   无凭据（应 401）" "$BASE/panel.css"
check 200 "GET /panel.css   query token" "$BASE/panel.css?token=$T"
check 401 "GET /panel.js    无凭据（应 401）" "$BASE/panel.js"
check 200 "GET /panel.js    query token" "$BASE/panel.js?token=$T"

echo
echo "════ 结果: $pass 通过 / $fail 失败 ════"
[ "$fail" -eq 0 ] || exit 1
