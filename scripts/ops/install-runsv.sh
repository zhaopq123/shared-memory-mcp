#!/bin/bash
# 把服务注册为 Termux runsv 服务（**推荐方式**）
#
# ━━━ 为什么用 runsv 而不是 tmux ━━━
#
# 早先的做法是 tmux 会话里跑 `while true; do start-mcp.sh; sleep 10; done`。
# 三个问题（都是实测踩过的）：
#
#   1. **tmux 会话会死**。Termux 被系统回收时 tmux 一起消失，
#      守护就没了 —— 而 runsvdir 是 Termux 的**标准守护机制**
#      （sshd / git-daemon 就是它管的），跟系统生命周期绑定。
#   2. **logs 增长无上限**。实测某客户端每分钟用失效 token 轮询，
#      每次 401 打两条日志，4 小时就 48KB；长期运行吃满手机存储。
#   3. **tmux 会话是临时的**。实际部署时经常发现「服务昨天还在，今天没了」，
#      就是因为 tmux 会话被清掉了而没人察觉。
#
# runsv 的 run 脚本自带日志裁剪与重启间隔控制，且能被 runsvdir 持续监管。
#
# ━━━ 2026-10-13 两个实测坑（已修进本脚本，别改回去）━━━
#
# 1. run 脚本必须用 `bash <start.sh>` 启动，不能直接 exec 脚本本身。
#    Windows 侧 scp/rsync/解压类部署没有 +x 概念，会把容器内 start.sh
#    冲成 644 → runsv 每 5s 重拉全是 Permission denied → 服务静默死亡
#    （2026-10-13 连发两次，最长静默 13h，且日志写到幻影路径极难排查）。
#    bash 调用不依赖执行位，这一类回归彻底免疫。
#
# 2. LOG_DIR 曾写成 $PREFIX_DIR/../files/home/logs，多一层 files，
#    实际落到幻影路径 /data/data/com.termux/files/files/home/logs/。
#    排障时读 ~/logs 看到的是陈旧假线索（20:22 误判「健康检查秒杀」
#    就是读了幻影路径之外的旧文件）。正确值见下方定义。
#
# ━━━ 用法（Termux 层，8022）━━━
#
#   bash scripts/ops/install-runsv.sh            # 安装两个服务的守护
#   bash scripts/ops/install-runsv.sh shared-memory   # 只装一个
#
# ━━━ 验证守护是否真的有效 ━━━
#
#   bash scripts/ops/test-recovery.sh

set -u

PREFIX_DIR=/data/data/com.termux/files/usr
SERVICE_DIR="$PREFIX_DIR/var/service"
# 注意：$PREFIX_DIR/.. 已经是 /data/data/com.termux/files，这里只能再跟 home，
# 写成 ../files/home 会多一层 files 落到幻影路径（2026-10-13 踩过，见头部注释）
LOG_DIR="$PREFIX_DIR/../home/logs"

# 服务名 → 容器内启动脚本
SERVICES="shared-memory:/opt/shared-memory-mcp/start-mcp.sh
social-insight:/opt/social-insight-mcp/start-mcp.sh"

make_run() {
    local name="$1" start="$2"
    cat > "$SERVICE_DIR/$name/run" <<RUNEOF
#!/data/data/com.termux/files/usr/bin/sh
# $name · 常驻服务（由 runsvdir 监管）
#
# 本文件由 scripts/ops/install-runsv.sh 生成，请勿手改。
# 设计说明见该脚本头部注释。

export PATH=$PREFIX_DIR/bin:\$HOME/bin:\$PATH
export PREFIX=$PREFIX_DIR
export HOME=/data/data/com.termux/files/home

# shared-memory 需要这些；social-insight 自己从 /etc/*/env 读，故只条件设置
if [ "$name" = "shared-memory" ]; then
    export SHARED_MEMORY_HOME=/opt/shared-memory-mcp/tenant-home
    export SHARED_MEMORY_TOKEN_FILE=/opt/shared-memory-mcp/.tokens
    export PYTHONPATH=/opt/shared-memory-mcp/src
    export PORT=8200
    export HOST=0.0.0.0
    export LOG_LEVEL=info
fi

# ── 日志裁剪 ──
# 超过 5MB 就裁到 2MB（保留尾部）。手机存储有限，长期运行必须设上限。
LOGDIR=$LOG_DIR
mkdir -p "\$LOGDIR"
LOG="\$LOGDIR/$name.log"
MAX_BYTES=\$((5 * 1024 * 1024))
KEEP_BYTES=\$((2 * 1024 * 1024))
if [ -f "\$LOG" ]; then
    SZ=\$(wc -c < "\$LOG")
    if [ "\$SZ" -gt "\$MAX_BYTES" ]; then
        tail -c "\$KEEP_BYTES" "\$LOG" > "\$LOG.trimmed" 2>/dev/null && mv "\$LOG.trimmed" "\$LOG"
        echo "[runsv] 日志裁剪到 \$(wc -c < "\$LOG") 字节" >&2
    fi
fi

# 崩溃后等 3 秒再重启 —— 配置错误时避免无限快速重启（刷屏 + 耗电）
sleep 3

exec >>"\$LOG" 2>&1
echo "[runsv] \$(date '+%F %T') 启动 $name"

exec proot-distro login ubuntu -- bash $start
RUNEOF
    chmod 755 "$SERVICE_DIR/$name/run"
}

echo "=== 安装 Termux runsv 守护 ==="
mkdir -p "$SERVICE_DIR" "$LOG_DIR"

want="${1:-}"
for entry in $SERVICES; do
    name="${entry%%:*}"
    start="${entry#*:}"
    if [ -n "$want" ] && [ "$want" != "$name" ]; then
        continue
    fi
    mkdir -p "$SERVICE_DIR/$name"
    make_run "$name" "$start"
    echo "  已写入 $SERVICE_DIR/$name/run"
done

echo ""
echo "=== 校验启动脚本（执行位丢失 = 服务静默死亡，2026-10-13 踩过两次）==="
if command -v proot-distro >/dev/null 2>&1; then
    for entry in $SERVICES; do
        name="${entry%%:*}"
        start="${entry#*:}"
        if [ -n "$want" ] && [ "$want" != "$name" ]; then
            continue
        fi
        if proot-distro login ubuntu -- bash -c "[ -f $start ]" 2>/dev/null; then
            proot-distro login ubuntu -- bash -c "chmod 755 $start" 2>/dev/null
            echo "   OK $name: $start 就位（已确保 755）"
        else
            echo "   !! $name: $start 不存在——run 脚本会无限重启失败，先部署代码再装守护"
        fi
    done
else
    echo "   （检测到在容器内执行，proot-distro 不可用，跳过执行位校验；"
    echo "     建议在 Termux 层重跑一次以完成校验）"
fi

echo ""
echo "=== runsvdir 会在 5 秒内自动扫描并拉起新服务 ==="
echo "  查看状态："
echo "    ps aux | grep 'runsv \\(shared-memory\\|social-insight\\)'"
echo "    ss -tln | grep -E ':8200|:8100'"
echo ""
echo "  看日志："
echo "    tail -f $LOG_DIR/shared-memory.log"
echo ""
echo "  验证自愈（杀掉服务，应在 25 秒内自动恢复）："
echo "    bash scripts/ops/test-recovery.sh"