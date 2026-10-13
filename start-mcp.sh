#!/bin/bash
# 生产启动脚本（多租户版）
#
# ━━━ 与旧版的区别 ━━━
# 旧版：单个 vault + 一个 token
# 新版：多库 + 用户/权限 + 强制鉴权
#
# 关键变化：**不再把 token 写进脚本**。改为从文件读 ——
# 环境变量会出现在 `ps e`、容器 inspect、崩溃转储里，
# 而文件可以设 600 权限，且不进版本库。
set -e

APP=/opt/shared-memory-mcp
cd "$APP"

# ── 凭据文件（不存在则生成一把 admin token）──
# 为什么单独一个文件：token 是长期凭据，放在这里可以：
#   · chmod 600（只有属主能读）
#   · 不进 git（.gitignore 里有）
#   · 轮换时只改这个文件
TOKEN_FILE="${SHARED_MEMORY_TOKEN_FILE:-$APP/.tokens}"
if [ ! -f "$TOKEN_FILE" ]; then
  umask 077
  NEW=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
  echo "# 共享记忆 MCP 的访问 token（每行一把）" > "$TOKEN_FILE"
  echo "# 格式：用户名:标签:token   或   用户名:token   或   token" >> "$TOKEN_FILE"
  echo "admin:主账号:$NEW" >> "$TOKEN_FILE"
  echo ""
  echo "════════════════════════════════════════════"
  echo "  已生成初始 token 文件：$TOKEN_FILE"
  echo "  admin 的 token：$NEW"
  echo "  ⚠️ 只显示这一次，请立刻记下"
  echo "════════════════════════════════════════════"
  echo ""
fi
chmod 600 "$TOKEN_FILE" 2>/dev/null || true

# ── 环境 ──
export SHARED_MEMORY_HOME="${SHARED_MEMORY_HOME:-$APP/tenant-home}"
export SHARED_MEMORY_TOKEN_FILE="$TOKEN_FILE"
export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-8200}"
export LOG_LEVEL="${LOG_LEVEL:-INFO}"

# ⚠️ 不要把 SHARED_MEMORY_TOKEN 也设了 —— 两处都设会重复注册同一把，
# 虽然不会出错，但列表里会出现重复项，不好排查。

# ── 启动 ──
# 用 exec 让 python 替换 shell 进程 —— 这样守护脚本发的 SIGTERM
# 能直接到 python（否则到 bash，python 收不到，优雅停机就不生效）。
#
# ⚠️ PYTHONPATH 必须显式给：源码在 src/ 下，没有安装成 site-packages。
# 少了它，`-m shared_memory.http_server` 会 ModuleNotFoundError
# （实测踩过：包解到 /opt 但找不到模块）。
export PYTHONPATH="$APP/src${PYTHONPATH:+:$PYTHONPATH}"
exec ./venv/bin/python -m shared_memory.http_server
