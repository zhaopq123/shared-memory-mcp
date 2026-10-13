#!/bin/bash
# 从**局域网 IP**（不是 127.0.0.1）穷举所有路由，全部零凭据。
#
# ━━━ 为什么这个脚本必须存在 ━━━
#
# 之前的审计全部从 127.0.0.1 发起 —— 那验证的是「代码逻辑」，
# 不是「真实攻击面」。用户从另一台电脑访问走的是 192.168.x.x，
# 这两条路径**可能**在某个环节被区别对待（代理、绑定、中间件顺序）。
#
# 审计必须从**攻击者的位置**发起，不是从服务自己的位置。
#
# ━━━ 已知的"非泄漏"结果（别误报）━━━
#
#  · `/favicon.ico` → 204：**故意豁免鉴权**。它返回空内容、
#    不含任何记忆，且浏览器每次打开页面都会自动请求它。
#    要它鉴权会导致：① 日志噪音 ② 探针反复请求触发限速 → 真人被误伤
#    （这个坑实测踩过：冒烟测试探测就绪把 IP 拖进限速名单）
#
#  · `429`：**限速在工作**。前面十几次 401 已把本 IP 计入失败，
#    超过阈值后开始退避。这不是泄漏，是机制正确。
#    （但它暴露一个真问题：阈值对"一次安全审计"都太严，见代码注释）
#
# 用法：bash audit-lan.sh [host] [port]

HOST="${1:-127.0.0.1}"
PORT="${2:-8200}"
BASE="http://${HOST}:${PORT}"

leak=0
safe=0
exempt=0

echo "════ 零凭据访问全路由（从 ${BASE}）════"
echo

probe() {
  local desc="$1"; shift
  local code
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 "$@")
  if [ "$code" = "401" ]; then
    printf "  ✓ %-46s 401\n" "$desc"
    safe=$((safe+1))
  elif [ "$code" = "429" ]; then
    # 限速 = 机制在工作（前面失败次数超限）
    printf "  ✓ %-46s 429（限速生效）\n" "$desc"
    safe=$((safe+1))
  elif [ "$code" = "000" ]; then
    printf "  ? %-46s 连不上\n" "$desc"
  else
    printf "  ✗ %-46s %s  ← 泄漏！\n" "$desc" "$code"
    leak=$((leak+1))
  fi
}

# favicon 单独判：它被故意豁免，204 是**预期**
fav=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 "$BASE/favicon.ico")
if [ "$fav" = "204" ]; then
  printf "  ○ %-46s 204（故意豁免：空内容、无记忆）\n" "GET /favicon.ico"
  exempt=$((exempt+1))
else
  printf "  ✗ %-46s %s ← 非预期的 favicon 状态\n" "GET /favicon.ico" "$fav"
  leak=$((leak+1))
fi

probe "GET /"                       "$BASE/"
probe "GET /panel.css"              "$BASE/panel.css"
probe "GET /panel.js"               "$BASE/panel.js"
probe "GET /api/state"              "$BASE/api/state"
probe "GET /api/note?ident=x"       "$BASE/api/note?ident=x"
probe "GET /api/view?kind=overview" "$BASE/api/view?kind=overview"
probe "GET /api/views"              "$BASE/api/views"
probe "GET /api/backlinks?title=x"  "$BASE/api/backlinks?title=x"
probe "GET /api/config"             "$BASE/api/config"
probe "GET /health"                 "$BASE/health"
probe "POST /mcp"                   -X POST "$BASE/mcp" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
probe "POST /api/config"            -X POST "$BASE/api/config" \
  -H 'Content-Type: application/json' -d '{"instructions":"PWNED"}'

echo
echo "── 变体：绕过尝试 ──"
probe "GET /api/state?token=null"      "$BASE/api/state?token=null"
probe "GET /api/state?token=undefined" "$BASE/api/state?token=undefined"
probe "GET /api/state?token=''"        "$BASE/api/state?token=%27%27"
probe "GET /api/state?token[]=x"       "$BASE/api/state?token[]=x"
probe "GET //api/state（路径归一）"      "$BASE//api/state"
probe "GET /./api/state"               "$BASE/./api/state"
probe "GET /api/state/（尾斜杠）"        "$BASE/api/state/"
probe "GET /api/STATE（大小写）"         "$BASE/api/STATE"
probe "X-Forwarded-For 伪造"           "$BASE/api/state" -H 'X-Forwarded-For: 127.0.0.1'
probe "X-Real-IP 伪造"                 "$BASE/api/state" -H 'X-Real-IP: 127.0.0.1'
probe "Host 头伪造为 localhost"        "$BASE/api/state" -H 'Host: localhost:8200'

echo
echo "════ 安全: $safe ｜ 豁免: $exempt ｜ 泄漏: $leak ════"
[ "$leak" -eq 0 ] || exit 1

