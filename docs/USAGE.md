# 使用教程

从「刚装好，不知道干什么」到「把它用成团队的第二大脑」。

> 还没装？先看 [安装部署手册](INSTALL.md)。
>
> **一句话心智模型**：把它当成一个**所有人共享的 Git 仓库**，
> 只不过里面装的不是代码，而是会给 AI 读的记忆。每条记忆一个 Markdown 文件。

---

## 目录

- [1. 五分钟上手](#1-五分钟上手)
- [2. 日常工作流](#2-日常工作流)
- [3. 记忆的三种口径：claim](#3-记忆的三种口径claim)
- [4. 给记忆加保质期](#4-给记忆加保质期)
- [5. 视图：让 AI 看全局而不是一条条翻](#5-视图让-ai-看全局而不是一条条翻)
- [6. 删错了怎么办](#6-删错了怎么办)
- [7. 备份与迁移](#7-备份与迁移)
- [8. 工作空间：隔离给谁用](#8-工作空间隔离给谁用)
- [9. 接入各种客户端](#9-接入各种客户端)
- [10. 网页面板](#10-网页面板)
- [11. FAQ](#11-faq)

---

## 1. 五分钟上手

连上之后，**先调这两个**：

```
memory_whoami     我是谁？我能看哪些库？
memory_search     已有什么？别重复造轮子
```

`memory_whoami` 返回你的身份和可访问的库。这是**开工第一步**——
因为权限是服务端从你的 token 解析的，**你在客户端指定不了范围**，
所以先搞清楚自己站在哪。

`memory_search` 是**写之前必调**。避免把已经存在的结论再写一遍——
重复记忆会稀释检索质量，是这个服务最主要的污染源。

然后写第一条：

```
memory_write
  title: 部署约定：一律用 runsv 不用 tmux
  body: 原因见 INSTALL.md 第 5.2 节，tmux 守护会被 Termux 回收...
  tags: [部署, 运维]
  workspace: infra
  lib: company
  claim: fact
```

⚠️ **`body` 不是 `content`**。第一次写最容易栽在这。

---

## 2. 日常工作流

```
开工：whoami → search（有没有既有结论）
干活：get（读全文）→ 干活 → write（沉淀结论）
收工：view kind=lessons（今天踩了什么）
```

| 场景 | 用什么 |
|---|---|
| 我记得有这个东西，找一下 | `memory_search` |
| 找到了，看全文 | `memory_get ident=<标题或id>` |
| 某项目下的都看看 | `memory_list workspace=<项目>` |
| 沉淀一条结论 | `memory_write mode=upsert` |
| 追加到已有记忆 | `memory_write mode=append` |
| 我不想覆盖已有的 | `memory_write mode=create`（存在则报错） |
| 谁引用了这条 | `memory_backlinks` |
| 库多大了 / 队列堵没堵 | `memory_stats` |

### mode 三选一（别无脑 upsert）

| mode | 行为 | 什么时候用 |
|---|---|---|
| `upsert` | 有则更新，无则创建 | 默认。结论在演进 |
| `create` | 存在则**报错** | 你不希望悄悄改掉别人写的东西 |
| `append` | 追加到末尾 | 日志型：这次又发生了什么 |

---

## 3. 记忆的三种口径：claim

这是它跟普通笔记工具最大的区别。**每条记忆必须标注自己是事实还是猜测**：

| claim | 含义 | 例子 |
|---|---|---|
| `fact` | 验证过的事实 | "runsv 每 5 秒重拉失败会静默死亡" |
| `inference` | 推断，未证实 | "看起来像是 proot 转发信号的问题，还没查证" |
| `proposal` | 提案，还没做 | "建议改成 Vault 层缓存" |

**为什么要强制标注**：AI 检索时默认可以只取 `fact`，
避免把同事的一个猜测当成既定事实去执行。

用 `memory_view kind=inferences` 可以专门列出所有还没证实的推断——
**这是一份"待验证清单"**，定期过一遍很有价值。

> ⚠️ **空列表参数为坑**：声称必填但传空列表会被静默替换成默认值。
> 需要区分"没有"和"默认"时，显式传 `None` 而不是 `[]`。

---

## 4. 给记忆加保质期

有些结论会过期。三个字段：

| 字段 | 作用 |
|---|---|
| `valid_until` | 到期后不再作为事实生效 |
| `review_after` | 到期提醒复审 |
| `superseded_by` | 指向替代它的新记忆 |

```jsonc
{
  "title": "部署方式：tmux 守护",
  "valid_until": "2026-10-13",
  "superseded_by": "部署方式：runsv 常驻"
}
```

过期记忆**不会被删**，只是在检索和视图里被降级/标记。
用 `memory_view kind=expiring` 看哪些该复审了。

> 这个设计解决的是**「AI 记住了一个已经不成立的结论，然后一直照做」**——
> 这在长期运行的 agent 里是灾难性的，而且极难发现。

---

## 5. 视图：让 AI 看全局而不是一条条翻

`memory_view` 一次性给出聚合结果，**跨库逐库算再合并**：

| kind | 看什么 |
|---|---|
| `overview` | 概览 |
| `decisions` | 所有决策（为什么这么定的）|
| `lessons` | 所有踩坑记录 |
| `todos` | 待办 |
| `expiring` | 即将过期 / 需要复审的 |
| `inferences` | 所有未证实的推断 |

典型用法：让 AI 开工前先 `memory_view kind=lessons`，
等于把团队踩过的坑全部预加载进上下文。

---

## 6. 删错了怎么办

`memory_delete` 是**软删除**，进回收站，30 天后才物理清除。

```
memory_delete ident=<标题或id> confirm=true
```

⚠️ **不传 `confirm=true` 只会返回一个预览**，不会真删——这是故意的，防止 AI 误操作。

恢复（**两个参数都必填**）：

```
memory_restore filename=<文件名> lib=<库名>
```

`memory_restore` **不会覆盖同名记忆**，避免把后来的更新冲掉。

---

## 7. 备份与迁移

以**工作空间**为单位导出 bundle（字节级 + manifest sha256）：

```
memory_export workspace=infra lib=company out_dir=/path/to/backup
```

导入（三种冲突策略）：

```
memory_import bundle=<bundle路径> target_lib=company on_conflict=rename
```

| on_conflict | 行为 |
|---|---|
| `skip` | 跳过已存在的 |
| `update` | 覆盖更新 |
| `rename` | 保留两份，新来的改名 |

> ⚠️ `memory_export` 的 `workspace` 是**条目的项目标签，不是库名**。
> 想按库导出用 `lib`。

另外：记忆库本身**就是一个 git 仓库**，配个 remote 就能直接推到远端，
这比 bundle 更轻量，也能享受 diff / 回滚。

---

## 8. 工作空间：隔离给谁用

"工作空间"= 一个项目/一个场景标签。默认**只有自己可见**：

```
memory_workspace_create workspace=新项目 label="新项目"   # share 留空 = 仅自己
```

逐步放开：

```
memory_workspace_share lib=<库名> share=小组    # 仅自己 → 小组 → 公司
memory_workspace_share lib=<库名> share=仅自己   # 随时收回，立即生效
```

`memory_workspaces` 看列表 + 共享范围 + 你能不能管理。

### 多台设备/多台电脑怎么用

你自己的多台设备（台式机、笔记本、服务器、手机）**不需要多个账号**——
所有设备用同一个 token，互相天然可读；**工作空间就是区分单位**，
A 空间的记忆 A 归、B 空间 B 归，不会混：

```
# 每台设备/每个项目建自己的工作空间库（share 留空 = 仅自己）
memory_workspace_create workspace=家里台式机
memory_workspace_create workspace=公司笔记本

# 各设备写入自己的库
memory_write title=部署笔记 lib=ws-家里台式机 workspace=家里台式机
memory_write title=客户备忘 lib=ws-公司笔记本 workspace=公司笔记本

# 跨设备查看：默认搜你有权限的全部库；也可以只看某一个
memory_search query=部署
memory_search query=部署 lib=ws-公司笔记本
```

要分享给**别人**时，才需要动 `share`（仅自己 → 小组 → 公司），
或者给对方发一把只读 scope 的 token。

**隔离模型记住三条**：

1. 一 token = 一 user = 一库面。同 token 就是同一个身份，没有第二重校验
2. **个人库连管理员都读不了**（刻意设计，不是 bug）
3. 你**没办法在客户端指定权限** ——想要什么权限，就去找管理员要对应 scope 的 token

---

## 9. 接入各种客户端

MCP 端点统一是 `http://<host>:8200/mcp`（streamable-http）。

### Claude Code

```jsonc
// ~/.claude.json 或项目 .mcp.json
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

### Cursor

`Settings → MCP Servers → Add new server`，选 `streamable-http` 类型，填同上两项。

### WorkBuddy / 其他

配置在 `~/.workbuddy/mcp.json`，格式同上。**加完需要在连接器页面点「Trust」激活**。

### 调试（探活的正确姿势）

```bash
curl -s http://127.0.0.1:8200/health
# {"ok":true,"problems":[],"notes":42,"queue":{...},"uptime_seconds":3600}
```

直接 curl `/mcp` 会拿到 401 ——**401 不一定是凭据错了**，
也可能是服务端的失败限速被打满了。换一条新连接重试，别反复重试把自己锁死。

---

## 10. 网页面板

打开 `http://localhost:8200/`，用管理员账号或一次性票据登录。

面板能做的事：
- 管理用户、组、库
- 直观浏览/编辑记忆
- 看 git 提交历史
- 中英文切换

> 面板静态资源随包安装（`pip install -e .`），
> 直接跑源码会遇到 500 —— 详见 [安装手册](INSTALL.md) 第 2 节。

---

## 11. FAQ

**Q：能让 AI 自己决定写哪个库吗？**
不能也不该。库由 token 决定 AI 能写哪些；在允许的库里 AI 再按 `lib` 参数选。

**Q：为什么必须用 HTTP，不能 stdio？**
stdio 下每个客户端各起一个进程，进程间不共享写队列，
"多 agent 共享"这个核心目标直接不成立。

**Q：写入之后要多久能搜到？**
写完即刻可搜 —— 检索读文件，不读 git。v2 起普通写入**根本不等提交**：
`ok=true` 就是已落盘，`committed=true` 只意味着这条标了 `versioned` 且进了版本库。
想立刻落 commit 用 `memory_commit`。

**Q：怎么让记忆不再可读时对 AI 隐藏？**
用 `valid_until`。到期后在检索与视图里降级/标记，但不物理删除——留着给人复核。

**Q：同事要只读权限，怎么给？**
发一条显式 `scope=read` 的 4 段 token。
**别发 3 段的**（`user:label:token`），那种默认是读写的。

**Q：能不能只让我自己的 AI 用，不进团队库？**
用工作空间，`share` 留空即可，默认仅自己。见第 8 节。
