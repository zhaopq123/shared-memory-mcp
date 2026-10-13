#!/bin/bash
# 崩溃恢复测试：强杀 shared-memory 服务进程，验证 runsv 守护能否自动拉起
#
# ⚠️ 本脚本跑在 **Termux 层**（8022），不是容器层。
#    原因：proot-distro 是 Termux 命令，在容器内跑会报
#    "attempted to run proot-distro in a proot session"。
#
# 与 social-insight 的 tmux 版不同：shared-memory 由 runsv 监管，
# 本脚本验证的是 runsv 重启链（杀 python → proot 退出 → runsv 重拉），
# 不是 tmux 守护循环。
#
# 用法（Termux 层）：
#   bash scripts/ops/test-recovery.sh
set -u

SVDIR=/data/data/com.termux/files/usr/var/service
PORT=8200

svc_up() {   # runsv 视角：处于 run 状态
    sv status "$SVDIR/shared-memory" 2>/dev/null | grep -q '^run:'
}

pid_in_container() {
    proot-distro login ubuntu -- bash -c \
        "ps aux | grep '[s]hared_memory.http_server' | awk '{print \$2}' | head -1" \
        2>/dev/null | tr -d '[:space:]'
}

http_code() {
    curl -s -o /dev/null -w '%{http_code}' --max-time 8 "http://127.0.0.1:$PORT/mcp" 2>/dev/null
}

echo "===== 1. 测试前状态 ====="
if svc_up; then
    echo "  OK runsv 监管中"
else
    echo "  !! runsv 未监管 shared-memory——先跑 scripts/ops/install-runsv.sh"
    exit 1
fi
echo "  杀之前 HTTP: $(http_code)"
PID_BEFORE=$(pid_in_container)
echo "  服务 PID: ${PID_BEFORE:-无}"
[ -n "$PID_BEFORE" ] || { echo "  !! 进程本就不在——守护可能已失效"; exit 1; }

echo ""
echo "===== 2. 强杀服务进程（SIGKILL，模拟崩溃）====="
proot-distro login ubuntu -- bash -c "kill -9 $PID_BEFORE" 2>/dev/null
echo "  OK 已 kill -9 $PID_BEFORE"

echo ""
echo "===== 3. 等待自动恢复（runsv 应在 ~15s 内重拉）====="
OK=0
for i in $(seq 1 60); do
    sleep 1
    CODE=$(http_code)
    # 401 = 服务已起来且鉴权开启（正常态）；200/400 = 亦活
    if [ "$CODE" = "401" ] || [ "$CODE" = "400" ] || [ "$CODE" = "200" ]; then
        echo "  OK ${i}s 内恢复（HTTP $CODE）"
        OK=1
        break
    fi
    [ $((i % 15)) -eq 0 ] && echo "  ...已等 ${i}s"
done
[ "$OK" = "1" ] || { echo "  !! 60s 内未恢复——守护链可能已断"; exit 1; }

echo ""
echo "===== 4. 恢复后的服务是否真能用（真实 MCP 调用）====="
NEW_PID=$(pid_in_container)
echo "  新 PID: ${NEW_PID:-无}（旧的是 $PID_BEFORE）"
if [ "$NEW_PID" = "$PID_BEFORE" ]; then
    echo "  !! PID 没变——可能根本没被杀掉"
fi

# 从 .tokens 取一把凭据做真实调用。
# 格式有两种（生产文件里混用）：3 段 user:label:token（默认 readwrite）
# 和 4 段 user:label:scope:token——统一取最后一段，两种都对。
TOKEN=$(proot-distro login ubuntu -- bash -c \
    "grep -v '^#' /opt/shared-memory-mcp/.tokens 2>/dev/null | grep -v '^[[:space:]]*$' | head -1 | awk -F: '{print \$NF}'" \
    2>/dev/null | tr -d '[:space:]')
if [ -z "$TOKEN" ]; then
    echo "  （取不到 token，跳过工具调用——端口恢复已验证）"
    exit 0
fi

# 用 curl 走真实 HTTP 链路（Termux 层直连 127.0.0.1，不进容器）：
# initialize 拿 Mcp-Session-Id → 带 session + Bearer 调 memory_whoami
MCP="http://127.0.0.1:$PORT/mcp"
SID=$(curl -s -D - -o /dev/null --max-time 10 -X POST "$MCP" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "Authorization: Bearer $TOKEN" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"recovery-test","version":"1"}}}' \
    2>/dev/null | grep -i '^mcp-session-id:' | tr -d '\r' | awk '{print $2}')

if [ -z "$SID" ]; then
    echo "  !! initialize 没拿到 Mcp-Session-Id——服务可能没真恢复"
    exit 1
fi

RESP=$(curl -s --max-time 10 -X POST "$MCP" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Mcp-Session-Id: $SID" \
    -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"memory_whoami","arguments":{}}}' \
    2>/dev/null)

if echo "$RESP" | grep -q '"ok":true'; then
    echo "  OK MCP 全链路通（initialize + tools/call memory_whoami，session ${SID:0:8}…）"
else
    echo "  !! 工具调用异常：$(echo "$RESP" | head -c 200)"
    exit 1
fi
