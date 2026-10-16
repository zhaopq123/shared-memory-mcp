# shared-memory-mcp · 架构设计

> 一句话：**一条记忆 = 一个 Markdown 文件，整个库 = 一个 git 仓库，身份 = Bearer token。**
>
> 想了解每个决策背后的取舍过程，看 [docs/design-rationale.md](docs/design-rationale.md)。

---

## 1. 为什么不用「隔离」架构

主流记忆方案（mem0、OpenMemory 等）几乎都是**隔离设计**：每个 agent / user / session 一个命名空间。

本项目**正好是反面**：

| | 隔离型 | 本项目 |
|---|---|---|
| 目标 | 我的 AI 记住我的事 | 一群人的 AI 共享同一批结论 |
| 隔离手段 | `user_id` / `agent_id` 分桶 | 服务端按凭据解析，调用方**无法指定** |
| 跨工具共享 | 不支持 | 这是核心目标 |
| 个人私密 | 靠同一个分桶 | 个人库连 admin 都读不了 |

**关键在于权限在哪一层**：隔离型把 `user_id` 当参数传给服务端，
于是谁都能传别人的 id 试试运气；本项目里 **token 是认身份的唯一依据**，
服务端没有 clientInfo、没有 session 概念，你传什么都改变不了自己是谁。

---

## 2. 四个核心设计

### 2.1 一条记忆 = 一个 Markdown 文件

不用数据库，不用向量库。

好处直接：**人可以直接用任何编辑器读写记忆库**。
AI 写坏了，你打开文件夹改一行就行，不需要学任何 API。

代价是格式要自洽：YAML frontmatter 存元数据（`claim`、`tags`、`valid_until`），
正文存内容。好处是格式足够简单，AI 也能稳定地产出合规文件。

### 2.2 写入串行：单写者 FIFO 队列

所有写操作进一个队列，串行执行。

这解决的是并发写覆盖问题 —— 但更重要的是**它天然带来了顺序保证**：
后写的覆盖先写的，语义明确，不需要做冲突合并。
分布式方案的"谁赢了"在这里是个伪问题，因为压根不并行。

### 2.3 git 是兜底设施，不是每次写入的流水账

v2（S-8）把「防丢失」拆成三层，git 只负责需要长期追溯的那一层：

- **防手滑**：每次覆盖写前，把上一版正文存进库内 `.history/<id>/<ts>.md`，默认留 3 版 —— 不进 git，纯粹给"刚改坏了"用。
- **版本控制**：只有 frontmatter 标了 `versioned: true` 的记忆才提交进一个**跨库共享**的版本库（`<home>/_versioned`）。普通写入**零 git 操作**。
- **审计兜底**：删除/恢复这类破坏性动作仍然立刻提交所在库 —— 留痕不能等。

git 在这里不是"版本控制功能"，而是**基础设施**：

- **可回滚**：AI 写坏了，checkout 回去
- **可 diff**：两个人谁改了什么一目了然
- **异地备份**：配个 remote 就能推走，这是免费的灾备
- **人可读**：可以直接 `git log` 看团队知识是怎么演进的

> 为什么改成按需：v1 的"每次写都防抖提交"实测暴露三个问题 ——
> `committed=false` 被 agent 误读成失败而重试（写出重复内容，且重试重置计时，
> 提交反而永不发生）；连写 5 条 = 5 个噪音提交，真想 diff 的那条长文被淹没；
> 十几个人提交长得一样，追溯只能靠文件内容。改成按需后，
> 「写了没写」看 `ok`，「入库没入库」看 `committed`，两件事不再挤在同一个字段里。

> ⚠️ 必须设置边界：`vault._git()` 固定了 `GIT_CEILING_DIRECTORIES`，
> 防止记忆库放在代码仓库内时，git 向上遍历把代码 commit 卷进记忆历史。


### 2.4 身份来自凭据，不来自参数

```
POST /mcp  Authorization: Bearer <token>
              ↓
        auth.py 解析 → user / role / groups
              ↓
   vault_for(user, lib, need_write)  ← 唯一取库入口，顺带鉴权
```

`vault_for()` 是**结构性硬隔离**：它是拿库的唯一通道，顺带就把鉴权做了。
绕不过去 —— 没有第二条路能拿到 vault 对象。

---

## 3. 分层

```
┌─────────────────────────────────────────────┐
│  MCP 工具面 (17 个)                          │
│  tools_tenant · workspace · request_ctx      │
└──────────────────┬──────────────────────────┘
                   │ require_write_scope(...)
┌──────────────────▼──────────────────────────┐
│  鉴权与租户                                   │
│  auth (token→user) · tenancy · tenant        │
│  vault_for(user, lib, need_write) — 唯一入口  │
└──────────────────┬──────────────────────────┘
                   │
┌──────────────────▼──────────────────────────┐
│  写入队列 (queue)  — 单写者 FIFO，串行执行     │
└──────────────────┬──────────────────────────┘
                   │
┌──────────────────▼──────────────────────────┐
│  存储层 (vault)                              │
│  Markdown 文件 + git 版本化 + trash 回收站    │
└─────────────────────────────────────────────┘

旁路：library(库元数据) · knowledge(视图聚合) · digest · cache(读缓存)
面板：dashboard + static/panel.{css,js} + static/i18n.js
```

### request_ctx 与 scope 判定

**这是一个必须记住的设计约束**：MCP 协议里读和写**全都走 POST**，
所以**按 HTTP method 判断读写必然误杀** —— 只读凭据连 `initialize`、`whoami`、`search`
都会被拒 403。

正确做法：在工具层按语义判。

```python
request_ctx.WRITE_TOOLS      # {memory_write, memory_delete, ...}
require_write_scope()        # 写工具自己调
```

> **新增写工具必须登记进 `WRITE_TOOLS`**，否则这个工具会漏过权限检查。

---

## 4. 工具面为什么是 17 个

不是功能少，是**刻意收敛**。每加一个工具，AI 的选择空间就大一分，
选错的概率也大一分。17 个是"能完成全部工作流"的最小集。

| 类别 | 工具 |
|---|---|
| 身份与库 | `whoami` `libs` `workspaces` `workspace_create` `workspace_share` |
| 读 | `search` `get` `list` `view` `backlinks` `stats` |
| 写 | `write` `delete` `restore` `commit` |
| 迁移 | `export` `import` |

`view` 值得单独说：它一次返回决策/踩坑/待办/过期/推断五种聚合视图，
让 AI 不用循环调用十七次来拼全局图景。

---

## 5. 部署形态

| 形态 | 适用 | 参考 |
|---|---|---|
| systemd | Linux 服务器 | [安装手册](docs/INSTALL.md) §5.1 |
| runit / runsv | Termux、proot、无 systemd 容器 | 安装手册 §5.2（`scripts/ops/install-runsv.sh`） |
| Docker | 隔离部署 | 安装手册 §5.3 |

**常驻是硬性要求** —— 必须 HTTP 传输，stdio 模式下每个客户端各起进程，
多 agent 共享在结构上就不成立。

---

## 6. 已知的边界

诚实说明它**不适合**什么（详见 README）：

- 不适合高吞吐写场景（串行队列是瓶颈）
- 不适合需要毫秒级语义检索规模（当前非向量检索）
- 不适合把记忆当数据库用（它是给人看的文件，不是给机器查询的表）
