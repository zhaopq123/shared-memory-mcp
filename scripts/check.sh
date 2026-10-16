#!/bin/bash
# 检查共享记忆服务状态
echo "=== tmux 会话 ==="
tmux ls 2>&1 | grep shared-memory || echo "无 shared-memory 会话"
echo
echo "=== 端口监听 ==="
ss -tln 2>/dev/null | grep -E ":8200|:8300" || echo "8200/8300 都未监听"
echo
echo "=== 启动日志（最后 15 行）==="
tmux capture-pane -t shared-memory -p -S -100 2>/dev/null | grep -v '^$' | tail -15
echo
echo "=== token 文件 ==="
ls -la /data/data/com.termux/files/usr/var/lib/proot-distro/containers/ubuntu/rootfs/opt/shared-memory-mcp/.tokens 2>/dev/null || echo "无 token 文件"
echo
echo "=== 从本机探测（无凭据，应 401）==="
# 默认探测本机；要查远端：`HOST=192.168.1.10 bash check.sh`
HOST="${HOST:-127.0.0.1}"
curl -s -o /dev/null -w "  ${HOST}:8200/     -> %{http_code}\n" --max-time 6 "http://${HOST}:8200/"
curl -s -o /dev/null -w "  ${HOST}:8200/mcp -> %{http_code}\n" --max-time 6 -X POST "http://${HOST}:8200/mcp"
