#!/bin/bash
# 确认服务到底在跑哪个版本的代码
echo "════ 1. 有几个 python 进程（重复进程 = 端口被旧进程占着）════"
ps aux 2>/dev/null | grep "shared_memory.http_server" | grep -v grep | awk '{print "  PID", $2, "启动", $9}'
echo "  进程数: $(ps aux 2>/dev/null | grep 'shared_memory.http_server' | grep -v grep | wc -l)"

echo
echo "════ 2. 谁占着 8200 ════"
ss -tlnp 2>/dev/null | grep ":8200" || echo "  没人占"

echo
echo "════ 3. 服务实际发出的 HTML 里有没有新元素 ════"
RF=/data/data/com.termux/files/usr/var/lib/proot-distro/containers/ubuntu/rootfs
T=$(grep -v '^#' "$RF/opt/shared-memory-mcp/.tokens" | grep -v '^$' | head -1 | awk -F: '{print $NF}')
HTML=$(curl -s --max-time 8 "http://127.0.0.1:8200/?login=invalid" -H "Authorization: Bearer $T")
for k in 's-libs' 's-who' 'libchips' 'admin-users'; do
  n=$(echo "$HTML" | grep -c "$k")
  echo "  $k : $n 处"
done

echo
echo "════ 4. 服务实际发出的 panel.js 里有没有新函数 ════"
JS=$(curl -s --max-time 8 "http://127.0.0.1:8200/panel.js" -H "Authorization: Bearer $T")
for k in 'renderLibChips' 'loadAdminUsers' 'activeLib'; do
  n=$(echo "$JS" | grep -c "$k")
  echo "  $k : $n 处"
done
echo "  panel.js 字节数: ${#JS}"

echo
echo "════ 5. 磁盘上的文件是什么版本 ════"
for f in dashboard.py; do
  echo "  $f: $(grep -c 's-who' $RF/opt/shared-memory-mcp/src/shared_memory/$f) 处 s-who"
done
echo "  panel.js: $(grep -c 'renderLibChips' $RF/opt/shared-memory-mcp/src/shared_memory/static/panel.js) 处 renderLibChips"
