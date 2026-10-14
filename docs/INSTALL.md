# 安装部署手册

从零把 shared-memory-mcp 跑起来，到长期稳定运行。

> **先读这段**：本服务**必须用 HTTP 传输，不能用 stdio**。
> stdio 模式下每个客户端各自起一个进程，第二个 agent 连不上第一个、
> 写队列也不共享 ——「多 agent 共享」在结构上就不成立。
> 所以它需要一个**常驻进程**，这也是本篇的主要内容。

---

## 目录

- [1. 前置要求](#1-前置要求)
- [2. 安装](#2-安装五分钟)
- [3. 配置](#3-配置)
- [4. 首次启动与管理员密码](#4-首次启动与管理员密码)
- [5. 长期运行（常驻）](#5-长期运行常驻)
- [6. 放到公网](#6-放到公网)
- [7. 升级](#7-升级)
- [8. 故障排查](#8-故障排查)
- [9. 卸载](#9-卸载)

---

## 1. 前置要求

| 项 | 要求 | 检查命令 |
|---|---|---|
| Python | **≥ 3.10** | `python3 --version` |
| git | 必须安装，记忆库的版本化依赖它 | `git --version` |
| pip | 随 Python 提供 | `python3 -m pip --version` |
| 端口 | 默认 `8200`，需未被占用 | `ss -tln \| grep 8200` |

**为什么依赖 git**：记忆库本身就是一个 git 仓库，每次写入都会提交。
这让记忆可以回滚、可以 diff、可以用任何 git 工具异地备份。
没有 git 服务起不来。

---

## 2. 安装（五分钟）

```bash
git clone https://github.com/zhaopq123/shared-memory-mcp.git
# 国内网络用镜像：
# git clone https://gitee.com/zhaopq/shared-memory-mcp.git

cd shared-memory-mcp

python3 -m venv venv
./venv/bin/pip install -e .
```

Windows 用 `venv\Scripts\pip install -e .`。

> ⚠️ **必须用 `pip install -e .`，不要直接 `python -m shared_memory.http_server`。**
> 面板的静态资源（`static/panel.css`、`static/panel.js`）靠 `package-data` 声明随包安装，
> 不装包就直接跑源码，面板会 500 ——找不到 css/js。

验证安装：

```bash
./venv/bin/python -c "import shared_memory, fastmcp; print('OK')"
```

装好后建议跑一遍**全量自检**（17 个工具 + 全部用户旅程：工作空间生命周期 / 回收站 / 导出导入 / 只读凭据 / 越权防护）：

```bash
./venv/bin/python scripts/e2e.py
```

自检会写入少量标记记录（个人库与公司库各 1 条、沙盘库 `ws-e2e自检` 2 条固定样例），可重复跑、不增长。

---

## 3. 配置

### 3.1 三种凭据方式，选一种

| 方式 | 适用 | 安全性 |
|---|---|---|
| **token 文件**（推荐） | 单机、多用户 | ⭐⭐⭐ 多条 token，可按人发放 |
| 环境变量单 token | 快速试跑 | ⭐⭐ 一条令牌走天下 |
| 不设（开放） | 仅本机且信任网络 | ⚠️ 任何人扫到端口可读写全部记忆 |

#### A. token 文件（推荐）

```bash
head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n'
```

写入 `tokens.txt`，每行一条：

```
alice:笔记本:<上面生成的随机串>
bob:台式机:<另一串>
collab:合作方只读:read:<再一串>
```

格式说明（**生产环境实测混用两种，别踩坑**）：

- 3 段 `user:label:token` → 默认**可读写**
- 4 段 `user:label:scope:token` → `scope` 为 `read` 或 `readwrite`

> ⚠️ **只读权限一定要显式写 `read`**。3 段格式缺 scope 会走默认值 `readwrite`，
> 于是"只读凭据"在服务重启后变成可写 —— 这是**权限提升**，踩过。

```bash
export SHARED_MEMORY_TOKEN_FILE=$PWD/tokens.txt
```

#### B. 环境变量单 token

```bash
export SHARED_MEMORY_TOKEN=$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')
```

> 环境变量会出现在 `ps e`、容器 inspect 里，所以正式部署更推荐 token 文件。

### 3.2 其他环境变量

复制 `.env.example` 为 `.env` 按需修改。**都有合理默认值，只设你需要的**：

| 变量 | 默认 | 说明 |
|---|---|---|
| `HOST` | `0.0.0.0` | 跨机器共享的前提；`127.0.0.1` 则只允许本机 |
| `PORT` | `8200` | |
| `SHARED_MEMORY_ROOT` | — | 记忆库位置（一个 git 仓库） |
| `SHARED_MEMORY_TOKEN` | 空 | 为空则不校验鉴权 |
| `SHARED_MEMORY_TOKEN_FILE` | — | 优先于上面的单 token |
| `SHARED_MEMORY_COMMIT_DEBOUNCE` | `8` | 写入静默多少秒后自动 commit。设 `0` = 每次写都提交，写入量大时 git 会成瓶颈 |
| `SHARED_MEMORY_AUTOCOMMIT` | `1` | 设 `0` 关闭 git 版本化（不推荐：失去回滚与异地备份能力） |
| `LOG_LEVEL` | `INFO` | |

---

## 4. 首次启动与管理员密码

```bash
export SHARED_MEMORY_HOME=$PWD/home
export SHARED_MEMORY_TOKEN_FILE=$PWD/tokens.txt
./venv/bin/python -m shared_memory.http_server
```

**首次启动会创建管理员账号，密码只打印一次**，同时自动发一把 token，也**只在启动日志里出现一次**。

⚠️ **现在就把这两样复制走**。关掉终端就再也拿不回来，只能删库重来。

之后：

- 打开 `http://localhost:8200/` 走面板管理用户与库
- 或直接改 `tenants.json`

---

## 5. 长期运行（常驻）

三选一。按推荐顺序排列。

### 5.1 systemd（Linux 服务器，首选）

`/etc/systemd/system/shared-memory.service`：

```ini
[Unit]
Description=shared-memory-mcp
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/shared-memory-mcp
EnvironmentFile=/etc/shared-memory-mcp/env
Environment=PYTHONPATH=/opt/shared-memory-mcp/src
ExecStart=/opt/shared-memory-mcp/venv/bin/python -m shared_memory.http_server
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now shared-memory
sudo systemctl status shared-memory
```

把凭据写进 `/etc/shared-memory-mcp/env`，别写在 unit 文件里。

### 5.2 runit / runsv（Termux、proot、容器 —— 推荐）

**不要在这类环境用 tmux 守护循环**，它有三个实测踩过的坑：

1. tmux 会话会连同 Termux 一起被系统回收，守护蒸发而无人察觉（典型现象："服务昨天还在，今天没了"）
2. 日志无上限增长：某客户端每分钟用失效 token 轮询，每次 401 打两条日志，4 小时 48KB，长期跑会吃满手机存储
3. 与 runsv 并存时会**抢启动**：两个 supervisor 各拉一份进程，端口持有者反复易主，服务时通时断

用仓库自带的生成器：

```bash
# 在 Termux 宿主层执行（不是在 proot 容器里）
bash scripts/ops/install-runsv.sh                # 安装全部服务
bash scripts/ops/install-runsv.sh shared-memory     # 只装本服务
```

它会做三件事：生成 run 脚本（自带日志裁剪与重启间隔）、由 runsvdir 接管、校验启动脚本就位。

验证守护真的有效（会强杀一次进程看它能不能自愈）：

```bash
bash scripts/ops/test-recovery.sh
```

日常运维：

```bash
sv status /data/data/com.termux/files/usr/var/service/shared-memory
tail -f ~/logs/shared-memory.log
```

> ⚠️ **非 git 部署（scp / rsync / 解压 tar）会丢可执行位。**
> Windows 没有 `+x` 概念，容器内的 `start-mcp.sh` 被覆盖成 644 之后，
> runsv 每 5 秒重拉全是 `Permission denied`，**服务静默死亡**
> （实测最长静默 13 小时，因为日志写到了别处，很难察觉）。
>
> 生成器已经对这一类免疫（run 脚本用 `bash <start.sh>` 而不是直接 exec），
> 但每次部署后还是建议看一眼：`ls -l start-mcp.sh` 必须是 **755**。

### 5.3 Docker

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . .
RUN pip install -e .
ENV SHARED_MEMORY_HOME=/data/home
VOLUME /data
EXPOSE 8200
CMD ["python", "-m", "shared_memory.http_server"]
```

```bash
docker build -t shared-memory-mcp .
docker run -d --name sm -p 8200:8200 \
  -v sm-data:/data \
  -e SHARED_MEMORY_TOKEN=<你的token> \
  shared-memory-mcp
```

**`-v sm-data:/data` 必须挂载**，否则容器删了记忆全没 ——而记忆正是这个服务唯一有价值的东西。

---

## 6. 放到公网

⚠️ **必须先设 `SHARED_MEMORY_TOKEN`（或 token 文件）**。留空的话任何人扫到端口就能读写你全部记忆。

建议放在反向代理后面，用 HTTPS：

```nginx
server {
    listen 443 ssl;
    server_name memory.example.com;

    ssl_certificate     /path/fullchain.pem;
    ssl_certificate_key /path/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8200;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        # streamable-http 需要长连接支持
        proxy_http_version 1.1;
        proxy_set_header Connection "";
    }
}
```

配套建议：

- 用防火墙只放行必要来源，或走 VPN / Tailscale
- 给不同人**发不同 token**（token 文件天然支持），不要共用一把
- 只读场景**显式写 `scope=read`**

---

## 7. 升级

```bash
cd /opt/shared-memory-mcp
git pull
./venv/bin/pip install -e .

# systemd
sudo systemctl restart shared-memory

# runsv（注意：sv restart 在 proot 里可能无效，用下面这条）
# TERM 直达容器内的 python，runsv 会自动重拉
```

> **proot 环境 `sv restart` 常常不生效** —— proot 收不到 / 不转发 TERM，服务照跑。
> 可靠做法是把 TERM 直接发给容器里的 python 进程，让 runsv 重新拉起：
> `bash scripts/ops/test-recovery.sh` 里用的就是这一招。

**升级前后各看一次**：`git log -1` 记录 commit，出问题好回滚。

---

## 8. 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| 面板 500，找不到 css/js | 没用 `pip install -e .` 装包。见[第 2 节](#2-安装五分钟) |
| 端口起不来，`address already in use` | 旧进程还占着。`ss -tlnp \| grep 8200` 找出并杀掉。**常驻服务要先停 supervisor 再手动启，否则两边抢** |
| 所有请求 401 | 不一定是凭据错了。服务端有**失败限速**，打满之后所有请求都会 401。换一条新连接重试，别反复重试把自己锁死 |
| 只读 token 也能写 | token 文件用的是 3 段格式（`user:label:token`），走了默认的可读写。改成 4 段显式写 `read` |
| 记忆的 commit 把代码仓库搞乱了 | 记忆库放在代码仓库里面了。把 `SHARED_MEMORY_ROOT` 指到一个独立目录 |
| 服务"昨天还在，今天没了" | 用了 tmux 守护且会话被回收。换 runsv，见 [5.2](#52-runit--runsvtermuxxproot容器--推荐) |
| 日志里有海量 401 | 某个客户端在用失效凭据轮询。修客户端，别提高日志级别硬扛 |
| 写入很慢 | `SHARED_MEMORY_COMMIT_DEBOUNCE=0` 导致每条都 commit git。改回默认 `8` |

查健康：

```bash
curl http://127.0.0.1:8200/health
# {"ok":true,"problems":[],"notes":42,"queue":{...},"uptime_seconds":3600}
```

绑 loopback（`HOST=127.0.0.1`）时 `/health` 免凭据，适合做容器探针。

---

## 9. 卸载

```bash
# systemd
sudo systemctl disable --now shared-memory
sudo rm /etc/systemd/system/shared-memory.service

# runsv
rm -rf /data/data/com.termux/files/usr/var/service/shared-memory

# 最后删数据
rm -rf /opt/shared-memory-mcp
```

**删之前确认记忆库备份过** ——记忆全在里面（`vault/`），删了就真没了。
如果配过 `git remote`，可以推到远端留一份。
