#!/bin/bash
# 轮换已泄露的 token。
#
# ━━━ 为什么必须轮换 ━━━
# 旧 token 被明文写进了对话记录（会话存档、备份、可能同步），
# 等于公开。**已泄露的凭据必须先作废**，而不是"以后注意"。
#
# 本脚本**不打印新 token** —— 打印就等于又泄露一次。
# 只输出指纹（前4后4）供核对。
#
# 用法：bash rotate-token.sh

set -e

VAULT_ROOT="${VAULT_ROOT:-/opt/shared-memory-mcp}"
TOKEN_FILE="$VAULT_ROOT/.token"
ENV_FILE="${ENV_FILE:-/etc/shared-memory-mcp/env}"

echo "═══ 轮换 token ═══"
echo

# ── 旧 token 指纹 ──
if [ -f "$TOKEN_FILE" ]; then
  OLD=$(cat "$TOKEN_FILE")
  echo "旧 token 指纹: ${OLD:0:4}…${OLD: -4}  （长度 ${#OLD}）"
else
  echo "旧 token 文件不存在（首次部署？）"
fi

# ── 生成新的（32 字节 = 64 hex）──
# 用 /dev/urandom 而不是密码学库：这是服务端本地生成，
# 不需要可复现，urandom 的熵足够了。
NEW=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
echo "$NEW" > "$TOKEN_FILE"
chmod 600 "$TOKEN_FILE"
echo "新 token 指纹: ${NEW:0:4}…${NEW: -4}  （长度 ${#NEW}）"
echo "  → 已写入 $TOKEN_FILE（权限 600）"

# ── 同步到 env（服务启动时读它）──
if [ -f "$ENV_FILE" ]; then
  # 用 sed 原地替换，避免重写整个文件时丢注释
  if grep -q '^SHARED_MEMORY_TOKEN=' "$ENV_FILE"; then
    sed -i "s|^SHARED_MEMORY_TOKEN=.*|SHARED_MEMORY_TOKEN=$NEW|" "$ENV_FILE"
  else
    echo "SHARED_MEMORY_TOKEN=$NEW" >> "$ENV_FILE"
  fi
  chmod 600 "$ENV_FILE"
  echo "  → 已同步到 $ENV_FILE"
else
  echo "  ⚠ $ENV_FILE 不存在，跳过（服务可能读不到新 token）"
fi

echo
echo "下一步：重启服务，然后用**旧 token** 验证它已失效。"
echo "提示：新 token 只在容器内，查看方式："
echo "  proot-distro login ubuntu -- cat $TOKEN_FILE"
