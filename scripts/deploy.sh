#!/bin/bash
# 部署 + 重启（Termux 层执行）
#
# ━━━ 2026-10-13 重写。三处曾致事故的写法，别改回去 ━━━
#
# 1. **不按端口杀进程。** 旧版 `ss|grep :8200 → kill → kill -9 兜底`：
#    TERM 穿不过 proot（实测 proot 收 TERM 不退出不转发），于是每次都走到
#    kill -9——2026-10-13 06:48 一次部署就把健康服务 SIGKILL 了。
#    现在 TERM 直达容器内 python（pkill -f，与 restart-social.sh 同款），
#    杀不掉就报错退出，**不拿 -9 兜底**（-9 跳过 uvicorn 优雅关闭，
#    可能丢队列里还没落盘的写）。
#
# 2. **不起 tmux 守护。** 旧版第 3 步 `tmux new-session -d -s shared-memory
#    guard.sh`——runsv 时代这就是双监督抢启动（端口持有者反复易主，
#    服务时通时断）。shared-memory 由 runsv 监管，python 一退出 runsv
#    自动重拉，无需任何守护循环。
#
# 3. **解压后必须校验执行位。** Windows 侧打包没有 +x 概念，解压出来是
#    644，runsv 每 5s 重拉全是 Permission denied → 服务静默死亡
#    （2026-10-13 07:18 事故，静默 13h，日志还写到幻影路径极难排查）。
#    现在 chmod 后立即验证，不是 755 就终止部署。
#
# 用法：bash scripts/deploy.sh [tarball]
set -e

APP=/opt/shared-memory-mcp
RF=/data/data/com.termux/files/usr/var/lib/proot-distro/containers/ubuntu/rootfs
TARBALL="${1:-/data/data/com.termux/files/home/shm40.tar.gz}"

alive() {   # 容器内服务进程是否还在
  proot-distro login ubuntu -- bash -c \
    "ps aux | grep -q '[s]hared_memory.http_server'" 2>/dev/null
}

echo "── 0. 部署新代码（先落盘，再重启）──"
[ -f "$TARBALL" ] || { echo "   ✗ tarball 不存在: $TARBALL"; exit 1; }
tar xzf "$TARBALL" -C "$RF/opt"
rm -rf "$RF/opt/shared-memory-mcp/src/shared_memory/__pycache__"
for f in e2e.py audit-lan.sh; do
  cp "/data/data/com.termux/files/home/$f" "$RF/opt/shared-memory-mcp/$f" 2>/dev/null || true
done

echo "── 1. 赋权并校验执行位 ──"
proot-distro login ubuntu -- bash -c "chmod 755 $APP/start-mcp.sh $APP/start-demo.sh"
MODE=$(proot-distro login ubuntu -- stat -c %a "$APP/start-mcp.sh" 2>/dev/null | tr -d '[:space:]')
if [ "$MODE" != "755" ]; then
  echo "   ✗ start-mcp.sh 权限是 ${MODE:-未知} 不是 755——终止（否则 runsv 无限重启失败）"
  exit 1
fi
echo "   ✓ start-mcp.sh 755"

echo "── 2. 重启（TERM 直达容器内 python；runsv 自动重拉新代码）──"
PID_BEFORE=$(proot-distro login ubuntu -- bash -c \
  "ps aux | grep '[s]hared_memory.http_server' | awk '{print \$2}' | head -1" \
  2>/dev/null | tr -d '[:space:]')
proot-distro login ubuntu -- bash -c 'pkill -f "shared_memory[.]http_server"' 2>/dev/null || true
for i in $(seq 1 10); do
  alive || break
  sleep 1
done
if alive; then
  echo "   ✗ 旧进程 10 秒未退出——proot TERM 转发异常，停止部署（勿用 kill -9 兜底）"
  exit 1
fi
echo "   ✓ 旧进程已退出（PID ${PID_BEFORE:-无}）"

echo "── 3. 等 runsv 重拉并就绪（确认是新进程）──"
NEWPID=""
for i in $(seq 1 60); do
  sleep 1
  NEWPID=$(ss -tlnp 2>/dev/null | grep ':8200' | grep -oP 'pid=\K[0-9]+' | head -1)
  if [ -n "$NEWPID" ]; then
    echo "   ✓ 8200 已监听（${i}s，PID $NEWPID）"
    break
  fi
done
[ -n "$NEWPID" ] || {
  echo "   ✗ 60 秒没起来。真日志在 ~/logs/shared-memory.log（不是 tmux 面板）"
  exit 1
}

if [ -n "$PID_BEFORE" ] && [ "$NEWPID" = "$PID_BEFORE" ]; then
  echo "   ✗ PID 没变——跑的還是旧进程。检查 runsv 是否在管："
  echo "     SVDIR=/data/data/com.termux/files/usr/var/service sv status shared-memory"
  exit 1
fi

echo "── 4. 确认跑的是新代码 ──"
# 用「panel.js 免鉴权」这个新行为当指纹（旧代码是 401）。
#
# ⚠️ 必须**轮询**而不是查一次：端口刚通时应用可能还在启动
# （MCP lifespan / Starlette 初始化），此时请求可能 500 或超时。
# 实测踩过：查一次正好撞上启动中，误判成"部署失败"而 exit 1，
# 其实服务是好的 —— 那是脚本的竞态，不是服务的问题。
OK=""
CODE=""
for i in $(seq 1 20); do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 6 http://127.0.0.1:8200/panel.js)
  if [ "$CODE" = "200" ]; then
    OK=1
    echo "   ✓ 新代码已生效（panel.js 免鉴权 → 200，第 ${i} 次探测）"
    break
  fi
  sleep 1
done
if [ -z "$OK" ]; then
  echo "   ✗ panel.js 返回 ${CODE:-无响应}（期望 200）—— 可能还是旧代码"
  exit 1
fi

echo ""
echo "部署完成。完整自愈验证：bash scripts/ops/test-recovery.sh"
