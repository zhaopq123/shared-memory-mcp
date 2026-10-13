# shared-memory-mcp

![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)
![MCP](https://img.shields.io/badge/MCP-streamable--http-purple.svg)
![Tests](https://img.shields.io/badge/tests-318%20passed-brightgreen.svg)

> **国内镜像（Gitee，同步更新）**：https://gitee.com/zhaopq/shared-memory-mcp

**多租户的 agent 共享长期记忆服务。** 一个常驻 MCP 服务，所有 AI agent 连同一个地址；公司/小组/个人三级库，身份由服务端从凭据解析，**调用方无法指定权限范围**。

> 解决的是这件事：*「我在 Cursor 里让 AI 记住的东西，Claude Code 不知道；换台机器、换个工具就全忘光。」*
> 以及它的团队版：*「全公司一个池子，谁的结论都能被找到，但谁也不能改别人的东西。」*

### 📚 文档导航

| 文档 | 看这个当你想…… |
|---|---|
| **[安装部署手册](docs/INSTALL.md)** | **把它跑起来** —— 各平台安装、常驻配置、公网暴露、升级、故障排查 |
| **[使用教程](docs/USAGE.md)** | **学会怎么用** —— 从零建库、读写记忆、多客户端接入、面板管理、常见问题 |
| [架构设计](ARCHITECTURE.md) | 理解它为什么这么设计、每层职责、关键技术选型 |
| [贡献指南](CONTRIBUTING.md) | 改代码、加功能、提 PR |
| [设计原理](docs/design-rationale.md) | 深入每个设计决策的取舍过程 |
| [新用户走查](docs/product-walkthrough-20261013.md) | 看一个真实用户从安装到踩坑的全过程 |

---

## 为什么不用现成的

现成的记忆方案**几乎都是隔离设计** —— 每个 agent / user / session 一个命名空间（`user_id`、`agent_id` 分桶）。那正好是这里的反面：

| | 隔离型（mem0 / OpenMemory 等） | 本项目 |
|---|---|---|
| 数据模型 | 每 agent 一个桶 | **公司 / 小组 / 个人三级库** |
| 谁写的 | 各自私有 | **同库内所有人可见** |
| 权限 | 隐式（靠 key 分桶） | **显式（角色 + 归属，服务端强制）** |
| 存取形式 | 向量库（黑盒） | **Markdown 文件（人可读可手改）** |
| 版本化 | 无 | **git，可 diff 可回滚** |
| 依赖 | 向量库 / LLM / API key | **零 LLM、零 key、零外部服务** |

如果你要的是「让 agent 有私有记忆」，别用这个。如果你要的是**一个团队/一个人所有 agent 共用一本笔记，且权限有边界**，这就是。

---

## 四个核心设计

### 1. 一条记忆 = 一个 Markdown 文件

```
<home>/
├── libs/
│   ├── company/          # 公司库（member 只读，admin 可写）
│   │   └── notes/
│   ├── group-研发/        # 小组库（组员读写）
│   │   └── notes/
│   └── user-alice/       # 个人库（仅本人）
│       └── notes/
├── tenants.json          # 用户 / 角色 / 组 / 库 / 归属（一个文件，权限 600）
├── state/                # 非 git 的运行态（不进任何库的提交）
│   ├── tokens.json       # 面板发的 token（重启后仍有效）
│   └── digest-cursors.json
└── exports/              # bundle 产物（不进 vault，不被 git 提交）
```

> `state/tokens.json` 只保存**服务面板发放的长期 token**及吊销状态；环境变量/
> `SHARED_MEMORY_TOKEN_FILE` 提供的 token 仍由原配置管理，服务不会复制它们的明文。
> 吊销外部 token 时仅保留吊销墓碑，避免它从原配置重新注入后复活。文件损坏或字段
> 不符合 schema 时服务**拒绝启动**，不会跳过坏记录后部分加载或静默覆盖。
> POSIX 上文件权限收紧到 0600；Windows 请限制 `state/` 目录的 NTFS ACL。

文件是人类可读的，**故障时用文本工具就能抢救**，不依赖任何程序。
每条记忆带 YAML frontmatter（标题 / 标签 / 时间 / 作者 / claim / 时效字段）。

### 2. 写入串行：单写者 FIFO 队列

不隔离就意味着**并发写同一份存储**，而并发写会静默互相覆盖。

这里不加锁，而是**排队**：所有写请求进一个 FIFO 队列，由一个写者循环串行执行。

```
agent A ─┐
agent B ─┼→ HTTP → 常驻进程 → FIFO 队列 → 单写者 → 磁盘
agent C ─┘                        ↑ 串行在这，所以不会互相覆盖
```

因此获得：

- **不互相覆盖** —— 严格串行，后到的等前一个落盘
- **不死锁** —— 队列本身无锁
- **不无限等待** —— 有容量上限，满了立刻返回 `QUEUE_FULL`
- **读不排队** —— 读没有竞争，走线程池并发路径，一个 agent 写不会堵住别人查

**关键约定：ack 在落盘之后。** `submit()` 返回的 future 在写入真正完成之后才 resolve —— 绝不允许「入队即返回成功」，否则进程一崩 agent 会基于假事实继续工作。

### 3. git 就是那个「无副作用的锁」

并发控制靠队列，**防丢失靠 git**：写入静默 8 秒后自动提交（防抖，避免一个 agent 连写 5 条产生 5 个提交）。

好处是**后验的**：冲突不会被静默吞掉、误删能回滚、整个库能 push 到任意远端做异地备份。

### 4. 身份来自凭据，不来自参数

工具签名里**没有 username 参数**（有的话客户端就能自己填）。身份由鉴权中间件写进 contextvar，工具层用 `require_user()` 取。这样"伪造身份越权"在结构上不可能。

同理，**没有任何工具参数能扩大可见范围**：`lib` 参数只用来**收窄**（在你有权限的集合里选一个），传一个你没权限的库会返回 403，而不是拿到内容。

---

## 快速开始

```bash
git clone <this-repo> shared-memory-mcp
cd shared-memory-mcp

python3 -m venv venv
./venv/bin/pip install -e .

# token 放文件（推荐）—— 环境变量会出现在 ps e、容器 inspect 里
echo "alice:$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')" > tokens.txt
export SHARED_MEMORY_TOKEN_FILE=$PWD/tokens.txt
export SHARED_MEMORY_HOME=$PWD/home
./venv/bin/python -m shared_memory.http_server
```

首次启动会创建管理员账号，**密码只打印一次**（同时自动发一把 token，也只在启动日志里出现一次）。之后用户与库由管理员在面板里管理（也可以直接改 `tenants.json`）。

打开 `http://localhost:8200/` 走面板登录（或一次性票据）。

### 接到你的 agent

MCP 端点：`http://<host>:8200/mcp`（streamable-http）

```jsonc
// Claude Code / Cursor / 任何 MCP 客户端
{
  "mcpServers": {
    "shared-memory": {
      "type": "streamable-http",
      "url": "http://192.168.1.10:8200/mcp",
      "headers": { "Authorization": "Bearer <你的token>" }
    }
  }
}
```

> **必须是 HTTP 传输，不能是 stdio。** stdio 模式下每个客户端各起一个进程，
> 第二个 agent 连不上第一个、队列也不共享 —— 「多 agent 共享」结构上就不成立。

---

## 工具（17 个）

| 工具 | 用途 |
|---|---|
| `memory_whoami` | 我是谁、能看哪些库（**开工先调它**）|
| `memory_libs` | 库清单与规模/队列状况 |
| `memory_search` | 检索（**开工前先调这个**）。`claim`/`status` 口径过滤、分页、时间过滤、排序 |
| `memory_get` | 按标题或 id 读全文 |
| `memory_list` | 列举，可按标签/工作区过滤 |
| `memory_write` | 写入（`upsert`/`create`/`append`）；`claim` 三值、`valid_until`/`review_after`/`superseded_by` 时效 |
| `memory_delete` | 软删除（进回收站），需 `confirm=true` |
| `memory_restore` | 从回收站恢复（不会覆盖同名记忆）|
| `memory_view` | 视图（决策/踩坑/待办/时效告警/推断清单），跨库逐库算再合并 |
| `memory_backlinks` | 引用关系（谁引用了它、它引用了谁）|
| `memory_stats` | 库况 / 队列 / git 记录；`with_commits` 按需取 git 历史 |
| `memory_commit` | 立刻提交（不用等防抖）|
| `memory_export` | 以**工作空间**为单位导出 bundle（字节级 + manifest sha256）|
| `memory_import` | 导入 bundle（冲突三策略：skip/update/rename）|
| `memory_workspaces` | 工作空间列表 + **共享范围** + 可管理标记 |
| `memory_workspace_create` | 为工作空间建独立库（默认**仅自己**）|
| `memory_workspace_share` | 改共享范围（仅自己 → 小组 → 公司；可随时收回）|

### 参数速查（最容易写错的几个）

工具 docstring 里有完整说明；这里是**第一次调用最容易踩的几个参数名**：

| 工具 | 关键参数（别写错） |
|---|---|
| `memory_write` | `title` / **`body`**（不是 content）/ `tags` / `workspace`（项目标签）/ `lib`（写到哪个库）/ `mode`(upsert·create·append) / `claim`(fact·inference·proposal) |
| `memory_get` | **`ident`**（不是 title —— 标题或 id 都行）/ `lib` |
| `memory_search` | `query` / `tags` / `workspace` / `lib` / `limit` / `offset` / `since` / `until` / `sort` / `claim` / `status` |
| `memory_list` | `limit` / **`tag`**（单数，不是 tags）/ `workspace` / `lib` |
| `memory_delete` | `ident` / **`confirm=true`**（不传只返回预览）/ `lib` |
| `memory_restore` | `filename` / `lib`（两个都必填） |
| `memory_view` | `kind`(overview·lessons·decisions·todos·expiring·inferences) / `lib` / `tag` |
| `memory_backlinks` | `title` / `lib` |
| `memory_export` | `workspace`（**条目项目标签，不是库名**）/ `lib`（按库导）/ `out_dir` |
| `memory_import` | `bundle` / `target_lib` / `workspace`（过滤器）/ `on_conflict`(skip·update·rename) |
| `memory_workspace_create` | `workspace` / `share`(留空=仅自己) / `label` |
| `memory_workspace_share` | `lib` / `share` |

> ⚠️ **两个 "workspace" 不是一回事**：`memory_write(workspace=...)` 是写在
> 记忆条目上的**项目标签**；`memory_workspace_create(workspace=...)` 建的是一个
> **独立的库**（`ws-<名>`）。导出按前者过滤，跨库导出用 `lib=`。

### 为什么控制在 17 个

有实测结论指出：**工具数量每增加一个，agent 选对工具的概率都会下降。**
所以按「一个动词一个工具」划分；新能力优先**加可选参数**，不做
`memory_write_team` 这种组合爆炸。多租户时代也只新增了 2 个
（whoami / libs），优化时代再新增 2 个（export / import）。

### 搜索为什么不用向量

引入 embedding = 引入 API key、网络依赖、额度限制、模型行为**四个额外失败点**。
子串匹配 + 标签过滤在当前量级够用，而且**结果可复现**（同样的库和查询永远同样排序）。
真需要语义搜索时，正确做法是加一层**可选**索引，而不是把 embedding 塞进存储路径。

计分：标题开头 100 / 标题包含 60 / 标签 40 / 正文每处 8（最多 5 处）。
多关键词是 AND。

---

## 记忆机制：防污染的四道闸

共享记忆最大的风险不是丢数据，是**脏数据被当成事实传下去**。对应四道闸：

**① claim 三分（反幻觉的结构性落点）**
每条记忆标注 `fact`（事实）/ `inference`（推断）/ `proposal`（建议）。
检索可按 claim 过滤 —— 问"现行口径"时只要 `fact`。写入不填则服务端按规则推断。

**② 时效四态（取代而非删除）**
`superseded-by` / `valid-until` / `review-after` 三个 frontmatter 字段。
旧口径被取代时**不删除**，标记后退出现行口径（search 默认 `status=active` 排除它），
但原文仍在 —— 后人知道"这条路走过、不通"。

**③ 写时防重五层**
upsert 按标题去重 / create 遇已存在报 CONFLICT / append 保留原文 /
防抖期 `committed=false` 防 agent 重试写出重复 / 撞名自动加 id 后缀。

**④ 启动变更摘要（不漏报、不刷屏）**
会话 initialize 时，中间件把「你离开期间的变更」追加进 instructions：
新写的、改过的、刚进入告警态的。零变更不播报；播报过的进 seen 记账，
**精确一次**。查当前全部告警用 `memory_view(kind="expiring")`。

---

## 运维

```bash
# 健康检查（给守护脚本 / 监控 / 容器探针用）
curl -s -H "Authorization: Bearer $TOKEN" "http://localhost:8200/health"
# {"ok":true,"problems":[],"notes":42,"queue":{...},"uptime_seconds":3600}

# ⚠️ 绑 loopback（HOST=127.0.0.1）时 /health 免凭据，探针直接 curl 即可：
curl -s "http://localhost:8200/health"

# 或走 MCP
memory_stats(with_commits=true)
```

> ⚠️ **不要用 `?token=`** —— 服务**刻意不接受** URL 里的凭据
> （URL 会进浏览器历史、代理日志、截图）。一律走 `Authorization: Bearer`。

`/health` **只返回判定所需的最小信息**，有明确的 `ok` 布尔与 `problems` 列表。

判定为不健康的情形：
- 队列积压触顶（写者消费不过来）
- 写者循环**在用过之后**停止了（那之后的写入会永远挂着）
- notes 目录不可写（**用真实写入探测**，不是 `os.access`）

> 注意「用过之后」这个限定：写者循环是惰性创建的，所以刚启动、
> 还没有任何写入的服务里 `worker_alive` 本来就是 false —— 那不是故障。
> 早期实现没做这个区分，导致每个正常启动的服务都被误报。

### 可调参数（`<库>/config/settings.json`，改完即生效不用重启）

| 键 | 默认 | 作用 |
|---|---|---|
| `search_default_status` | `active` | search 不显式传 status 时的默认口径 |
| `trash_ttl_days` | `30` | 回收站懒清理年龄阈值（stats 时顺带执行）|
| `trash_max_files` | `200` | 回收站条数上限 |
| `quota_warn_notes` | `500` | 单库条数告警阈值（只提醒，不拦截）|

---

## 数据安全

| 机制 | 说明 |
|---|---|
| **自动提交** | 写入静默 8 秒后 git commit（防抖，避免噪音提交）|
| **优雅停机** | 收到 SIGTERM 时先把未提交的改动落盘，再退出 |
| **软删除 + 懒清理** | 删除进 `.trash/` 可恢复；超期/超量由 stats 顺带物理清除（git 全历史是兜底）|
| **不覆盖** | 标题 slug 撞名时自动加 id 后缀；恢复时若已同名另行落盘 |
| **坏文件隔离** | 读不出来的单个文件被跳过并计数，不让一条烂数据拖垮整库 |
| **bundle 防穿越** | 导入路径白名单（与 `_slug` 同口径）+ resolve 双重校验 + sha256 逐条验 |
| **配额告警** | 单库条数过阈值只提醒不拦截（共享库的膨胀靠人决策，不该静默拦）|
| **凭据持久化** | 面板发的 token 落 `state/tokens.json`（非 git 目录；POSIX 0600，Windows 继承目录 ACL，需限制 NTFS 权限）——**重启后仍有效**；吊销也持久（重启不复活）|

---

## 可视化面板

`http://<host>:8200/`

- **时间脊柱** —— 按日分组，看得见记忆在累积
- **作者色标** —— 每个 agent 一个确定性颜色，扫一眼知道谁写的
- **全文搜索** —— 标题 / 标签 / 正文，命中高亮
- **Markdown 阅读** —— 目录跳转、表格、代码块、阅读时长
- **键盘操作** —— `/` 搜索、`j`/`k` 或 `↑`/`↓` 切换、`Esc` 清空
- **明暗双主题** —— 跟随系统

**面板是只读的。** 写入只能走 MCP —— 那条路径有队列串行 + git 提交 + 软删除保护，在面板开写入口等于绕过这些保护。

---

## 部署

<details>
<summary>长期运行（systemd）</summary>

```ini
# /etc/systemd/system/shared-memory.service
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
</details>

<details>
<summary><b>推荐：runit（runsv）</b> —— proot / 容器 / Termux / 任何没 systemd 的环境</summary>

不要在这类环境用 tmux 守护循环 —— 它有三个实测踩过的坑：

1. **tmux 会话会连同 Termux 一起被系统回收**，守护随之蒸发，而没人察觉（典型现象："服务昨天还在，今天没了"）
2. **日志增长无上限**：实测某客户端每分钟用失效 token 轮询，每次 401 打两条日志，4 小时 48KB，长期运行会吃满手机存储
3. **与 runsv 并存会抢启动**：两个 supervisor 各拉一份进程，端口持有者反复易主，服务时通时断

用仓库自带的生成器一键注册（自带日志裁剪与重启间隔控制）：

```bash
# Termux 宿主层执行
bash scripts/ops/install-runsv.sh              # 安装全部服务
bash scripts/ops/install-runsv.sh shared-memory   # 只装本服务

# 验证守护是否真的有效（会强杀一次进程再看自愈）
bash scripts/ops/test-recovery.sh
```

查看状态与日志：

```bash
sv status /data/data/com.termux/files/usr/var/service/shared-memory
tail -f ~/logs/shared-memory.log
```

⚠️ **非 git 部署（scp / rsync / 解压 tar）会丢可执行位** —— Windows 没有 `+x` 概念，
容器内的 `start-mcp.sh` 被覆盖成 644 后，runsv 每 5 秒重拉全是 `Permission denied`，
**服务静默死亡**（实测最长静默 13 小时，且很难察觉）。

生成器已经免疫了这一类问题（run 脚本用 `bash <start.sh>` 而非直接 exec），
但部署后仍建议看一眼：

```bash
ls -l start-mcp.sh    # 必须是 755
```

完整步骤见 **[安装部署手册](docs/INSTALL.md)**。
</details>

---

## 实测数据

| 项 | 结果 |
|---|---|
| 单元测试 | **全绿**（见 CI/本机运行；含越权测试优先于功能测试）|
| 并发压测 | 50 个并发写 → 0 异常、0 丢失 |
| 读并发 | 40 并发读卸载线程池后长尾消失（未卸载 p95=2194ms / soak p99=19665ms）|
| 崩溃恢复 | `kill -9` 后守护循环拉起 |
| 跨进程 | 另开连接能读到本连接写的内容 |

### 开发过程中实测挖出的 bug（都已修，且都补了回归测试）

**队列层：**

1. **容量只数 `qsize()` 不数在办任务** → 满员时不是拒绝而是**无限等待**。
   症状：第三个并发写永远不返回。这是最危险的一个 —— 承诺的
   「队列满明确拒绝」被悄悄变成了「静默挂起」。
2. **计数只在成功路径减** → 一次取消会永久少一个名额，队列容量随调用次数
   "缩水"，最终永久拒绝写入。
3. **写者无条件 `await fn()`** → 传同步函数时抛 `TypeError`，且错误发生在
   写者循环里，极难定位。
4. **`start()` 在无事件循环处 `create_task`** → 服务直接起不来。改为惰性创建写者。

**存储层：**

5. **`subprocess` 用系统编码解码 git 输出** → 中文提交信息崩。
6. **回收站时间戳只到秒** → 同一秒删两次同名会 `FileExistsError`。
7. **恢复时复用已存在的文件路径** → 覆盖别人的内容。
8. **`resolve` 按标题返回不确定的那条** → 同名多条时结果随文件重命名摇摆。

**ASGI / 协议层：**

9. **`Mount('/mcp')` 会 307 重定向** → MCP 端点 404（面板却正常）。
10. **把 ASGI 应用包一层，lifespan 没转发** → 面板 200、探活正常，
    但**所有 MCP 工具调用 500**。

**健康检查：**

11. **探测根目录而非 notes 目录** → 给出假的"健康"。
12. **把"写者还没创建"判成故障** → 每个正常启动的服务都被误报。

**2026-10-13 优化期新增（都补了回归测试）：**

13. **bundle 白名单正则不含中文标点** → 标题带 `·`/`（）` 的条目被静默跳过，
    导出结果少一半没人发现。改为与 `_slug` 同口径的结构化校验。
14. **导入用 `write_text`** → Windows 上 `\r\n` 被二次翻译成 `\r\r\n`，
    字节级还原失效。改 `write_bytes`。
15. **`gc_trash` 对 Python 3.13 惰性 `glob()` 用 `len()`** → TypeError。
16. **digest 秒级时间精度** → 同秒写入的"漏报/重复报"二选一。
    解法：`>=` 比较保不丢 + seen 记账保不重。

> 教训汇总：**HTTP 层全部正常不代表功能正常。**
> 第 9、10 条的 symptom 都是「面板 200 + 端口监听正常 + 工具全挂」，
> 只有真调一次工具才暴露。第 13 类只有真渲染/真导出才暴露。

**2026-10-13 产品走查期新增（新用户视角，都补了回归测试）：**

17. **17 个工具全带 `outputSchema`** → 严格模型（GLM-5.3-Flash / Deepseek-V4.1-Flash /
    Step-5-Preview）逐字校验工具 schema，直接拒**整个服务**（11129）。
    后果是"整个服务对一整类模型不可用"，客户端只显示通用错误码。
    修法：`@mcp.tool(output_schema=None)`。回归见 `tests/test_tools_schema_contract.py`。
18. **面板发的 token 只存内存** → 服务一重启全部失效，症状是"昨天还好用、
    今天所有 agent 都 401"，而新 token 只打印在启动日志里没人看见。
    修法：落 `state/tokens.json`，重启加载、增删写回、吊销持久。
19. **`/health` 在未启用鉴权时仍 401** → 日志说"未强制鉴权"，行为却处处强制，
    监控探针拿不到 token 就探不了活。修法：loopback 绑定时 /health 免凭据。
20. **`/health` 返回的是 stub**（永远 `ok:true`、`problems:[]`）→ 与 README 承诺的
    "队列积压/写者死/目录不可写"判定完全不符。修法：接真实的
    `SharedMemoryHub.health()`，判定逻辑只有一份。
21. **绝对形式请求目标 404**（RFC 7230 §5.3.2 要求必须接受）→ 代理/网关转发、
    或 httpx 带 `trust_env` 走代理时，MCP 端点 404 而面板正常。
    修法：最外层把绝对形式 target 归一化成路径。
22. **`e2e.py` 钉死「工具齐全（12 个）」** → 实际 17 个，生产验证脚本永远误报失败。

> 这一轮的方法论：**以新用户身份真的走一遍**（装包→启动→登录→连 agent→读写→
> 重启）。上面 6 条里有 4 条（17/18/19/21）是"单测全绿 + curl 探活正常"时
> 完全看不出来的 —— 只有真重启、真换模型、真在网络代理下跑才暴露。

---

## 工作空间与共享

一个工作空间一个库。共享范围 = 库的 `owner`，**没权限的人物理上打不开那个库**（不是"搜完再过滤"）。

```
user:<你>   → 仅自己（不共享）
group:<组>  → 组内可读
company     → 全员可读（默认只读，防污染）
```

```bash
# 建（默认仅自己）
memory_workspace_create(workspace="客户A")

# 共享给组
memory_workspace_share(lib="ws-客户A", share="group:研发")

# 收回（立即生效）
memory_workspace_share(lib="ws-客户A", share="user:admin")
```

要点：

- **改共享范围 = 改一个字段**，不用搬数据；`user:` → `group:` → `company` 就是逐步放开
- **只有库所有者或管理员能改** —— 改归属等于决定泄露，别人不能动你的
- **`created_by` 与 `owner` 分开**：前者回答"库是谁的"（不变），后者回答"现在谁能读"（会变）。
  否则一旦共享出去就收不回来（实测踩过的缺陷）
- 面板「设置 → 工作空间与共享」可以下拉直接改，也能建新库

---

## 面板国际化

**默认中文**，顶栏右侧切换，选择记进 localStorage。

```
优先级：用户选过的 > URL 参数（?lang=en）> 浏览器是英文 > 默认中文
```

- 语言包：`src/shared_memory/static/i18n.js`（扁平 key + `t('key', {占位符})`）
- 文案两种来源：HTML 里的 `data-i18n` 属性（静态）、JS 里的 `t()`（动态）
- 缺 key 时回落到中文，再回落到 key 本身 —— **绝不返回空串**（空串会让界面留一片空白）
- 一致性测试 `tests/test_i18n.py` 钉住：两种语言 key 集合一致、占位符一致、
  中文包不是复制粘贴、JS 用到的 key 都存在、英文包不能不伦不类
- **MCP 工具描述不翻译** —— 那是给 agent（LLM）看的，翻译反而降低准确率

---

## 不适合什么

诚实说明边界：

- **不是私有记忆** —— 同库内所有人可见（个人库仅本人，管理员也不行）
- **单机** —— 队列在进程内，多实例部署不会共享队列（要横向扩展得换成外部锁）
- **全量加载** —— 检索在内存里过滤所有笔记，适合几千条量级；十万条以上需要建索引
- **语义重复检测** —— 写时关联提示是标题相似度 + 标签，防不住"说法不同
  但意思相同"的重复。那条边界靠制度和 expiring 视图兜
- **界面只说两种语言** —— 中文/英文。要加第三种，在 `i18n.js` 里加一份语言包即可，
  测试会自动要求它与现有语言包 key 对齐

---

## License

MIT
