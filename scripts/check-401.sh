#!/bin/bash
# 确认 401 只是登录前探测 + 没有任何 500/异常
cd /opt/shared-memory-mcp

echo "══ 1. 401 的路径分布（应只有 /api/state 这类"未登录就探测"的）══"
tmux capture-pane -t shared-memory -p -S -300 2>/dev/null | grep '^INFO:' | \
  grep ' 401 ' | awk '{print $5}' | sort | uniq -c | sort -rn | head -10

echo
echo "══ 2. 有没有 500 / Traceback（那才是真问题）══"
N=$(tmux capture-pane -t shared-memory -p -S -500 2>/dev/null | grep -cE ' 500 |Traceback|ERROR')
echo "  500/异常 行数: $N"

echo
echo "══ 3. 有没有"未知用户"（删用户没吊销 token 的症状）══"
tmux capture-pane -t shared-memory -p -S -500 2>/dev/null | grep -c '未知用户' | \
  xargs echo "  未知用户次数:"

echo
echo "══ 4. 正常业务在跑吗（POST /mcp 200）══"
tmux capture-pane -t shared-memory -p -S -200 2>/dev/null | grep -c 'POST /mcp.* 200' | \
  xargs echo "  MCP 200 次数:"
