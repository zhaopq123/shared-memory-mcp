# shared-memory-mcp 优化设计（v2 · 重设计版）

生成时间：2026-10-13 00:10（00:40 增补机制层缺口：方案 9-13）
读者：dsh（开发机在本机快照之外，本文档基于只读快照 + vivo 生产实测）
证据等级：【实】读码/实测确认　【推】推断，未实测
本文档取代 `OPTIMIZATION-2026-10-12.md`（条目清单版，内容已全部吸收，可删）

---

## 0. 一页结论

服务已上线，核心链路（鉴权 / 多租户 / 单写者队列 / 时效四态 / 双向引用 / 视图）实调全部通过。没有"还差一大块"的功能，要做的分四类：

1. **读路径止血**——`all_notes()` 零缓存，每次读全量 `read_text` + `parse_note`。这是数据量上去后唯一会线性恶化的点。
2. **生命周期闭环**——时效四态只能手改 frontmatter、膨胀只标记不治理。"取代而非删除"目前只靠 instructions 软约束，工具层没抓手。（防冗余机制全景见 §3）
3. **机制层缺口**——写时才有的结构（claim 三分、时效四态）在检索接口上不暴露，agent 问不出"现行口径"；写入时无关联提示；会话启动无变更包。见方案 9-11。
4. **卫生与文档**——upsert 静默覆盖、死代码未清、v1 文档描述 v2 系统（照文档接必错）。

总原则一句话：**不动存储格式、不动冲突防御设计；共享库语义下一切治理 = 移出主检索范围但可找回**。

分三阶段：**性能止血 → 治理闭环 → 按需扩展**。每阶段有可验收的出口标准（§7）。

---

## 1. 方案总表

| # | 方案 | 改哪 | 阶段 | 工作量 | 风险 |
|---|---|---|---|---|---|
| 1 | `all_notes()` mtime 缓存 | vault.py:626 | 一 | S | 低 |
| 2 | stats 去 git 子进程 | tools_tenant.py（stats） | 一 | S | 低 |
| 3 | upsert 返回 action 标志 | vault.py:708 分支 | 一 | S | 低 |
| 4 | `memory_write` 时效参数 | tools_tenant.py:173-225 | 二 | S–M | 低 |
| 5a | 取代链视图折叠 | knowledge/view 层 | 二 | S | 低 |
| 5b | `.trash` TTL 清理 | vault 回收站 | 二 | S | 低 |
| 5c | 条数配额告警 | stats 返回 | 二 | S | 低 |
| 5d | `archive/` 归档层 | `_iter_files` + search/view | 三 | M | 中 |
| 6 | memory_view 目录 bug | tools_tenant.py:283-287 | 二 | S | 低 |
| 7 | 删 tools.py 死代码 | 先改 test_memory.py:1153 | 二 | S | 低 |
| 8 | 重写 README + design-rationale | v1 → v2 | 二 | M | 低 |
| 9 | search 补 claim/status 过滤 | vault.py:845 `search` | 二 | S | 低 |
| 10 | 写时关联提示 | memory_write 返回 | 二 | S–M | 低 |
| 11 | 会话启动变更包 | http_server.py:139-152 | 三 | M | 低 |
| 12 | 倒排索引（条件触发） | 读路径加一层 | 三 | M | 中 |
| 13 | memory_flag 质量反馈（候选） | 新工具 + expiring 视图 | 三 | S | 低 |
| 14 | `memory_export` / `memory_import`（按工作空间） | tools_tenant 新工具 + vault 打包 | 二 | M | 中 |

**明确不做**（见 §6 决策记录）：强制"先拉再推"、向量检索、agent 隔离。

---

## 2. 不要碰的部分（健康且被依赖）

优化前先明确红线——这些是已经实调验证过的设计，改动它们等于制造新问题：

- **单写者 FIFO 队列**（queue.py）：写串行、读不排队、future 落盘后才 resolve、满 256 明确拒绝、毒丸停机。并发写冲突已解决，任何缓存/索引不得给写路径开第二条并发通路。
- **时效四态**（knowledge.py:80 `expiry_state`）：superseded > expired > review > active，优先级正确。
- **权限模型**：身份由凭据解析，工具参数只能收窄不能扩大（tools_tenant.py 模块 docstring）——结构上不可能越权。缓存和归档都不能绕过服务端权限计算。
- **软删除 + git 全历史**：误删可恢复，历史不可篡改。
- **防抖提交**（service.py:137-163）：静默 8 秒批量提交，`committed=false + commit_pending=true` 防 agent 误判重试。
- **坏文件隔离**（vault.py:626-640）：`all_notes()` 跳过不可读文件——缓存不能让一条坏文件重新变成全库故障。

---

## 3. 防冗余机制全景

冗余分两种，机制完全不同，别混：**写时重复**（同一事实被写成两条）靠写路径去重；**存量膨胀**（过期/废旧条目只增不减）靠治理。前者已有五层且健康，后者是本次要补的。

### 3.1 写时防重（已存在，优化时不要碰）

| 机制 | 位置 | 行为 |
|---|---|---|
| upsert 按标题去重 | vault.py:653 `put` 起 | 同标题 = 同一条，有则更新（不传 tags 保留原标签）。**核心防线** |
| create 模式硬检查 | 同上 | 同标题直接报 `CONFLICT`——agent 的"推前检查"替代品，按需用 |
| append 模式 | 同上 | 追加保留原文，不覆盖 |
| 防抖提交防重试 | service.py:137-163 | 防抖期间返回 `committed=false + commit_pending=true`。原因写在注释里：agent 若以为失败就重试，会写出重复内容，且每次重试重置防抖计时导致**提交永远不发生** |
| 文件名撞名三态 | vault.py `_unique_path` | 不存在→直接用；存在且 frontmatter 标题相同→同一条走更新；存在但是另一条→加 `--<id>` 后缀。**任何情况下不静默覆盖** |

连带效应【实】：软删后重建会让同标题存在多条，`resolve()` 按标题查找时取**最近更新的那条**——要精确定位用 id，别信标题唯一。

### 3.2 返回信号（本次补：方案 3）

upsert 静默更新是写时防重唯一的盲区——agent 以为在新建、实际覆盖了旧记忆。方案 3 加 `action` 标志补上：只在覆盖真的发生时给信号，不是每次写都付查询成本。这也是决策 1（不强制"先拉再推"）的替代落地。

### 3.3 存量防膨胀（方案 5，本次补）

| 膨胀源 | 现状 | 对策 |
|---|---|---|
| superseded 链只增不减，且全留在主检索 | A→B→C→D 旧条目无限累积，agent 捞到过期口径的概率随链长上升 | 5a 视图层折叠，同链只显示最新一代 |
| `.trash` 只增不减 | 无 TTL 无上限；git 全历史才是最终兜底，trash 只需覆盖近期误删 | 5b 超 30 天/超条数懒清理 |
| 无归档无告警 | backlinks 算了却不用于治理，0 引用老记忆与热记忆同权 | 5c 配额告警（不硬拒）+ 5d `archive/` 归档层（可显式找回） |

### 3.4 防不住的（诚实边界）

- **语义重复**：不同标题、同一事实——写时去重按标题匹配，发现不了。召回抓手是 `memory_search` + instructions 软约束（"写前先搜"已拍板不强制，见决策 1）；缓解是方案 10（写入响应带 related 提示，服务端把"拉"的时机还给 agent）。
- **跨库重复**：库间物理隔离，同一事实在 company 库和 user 库各写一条，工具层无感知。这是共享池 + 物理隔离的固有代价，不做跨库查重（与决策 3 一致）。
- **适用范围无约束**：`workspace` 字段就是现成的 scope 维度（vault.py:880-881，连搜索都算工作区名命中），但填写无纪律、无默认值——同一事实在多个工作区被各写一遍的防不住。这是 instructions 的活，**不建新字段**。

---

## 4. 设计约束

1. **单写者串行不可破坏**：缓存失效必须在写操作内部同步完成，不依赖异步刷新。
2. **ack 在落盘之后**：缓存只加速读，不改变"未落盘不返回成功"的语义。
3. **共享库语义**：任何清理动作影响所有 agent，必须"移出主检索范围但可找回"。禁止物理删除 `notes/` 内容（`.trash` TTL 除外，git 兜底）。
4. **权限模型不变**：归档内容对无权库不可见。
5. **纯文本人可读**：不把数据库/二进制索引写进 vault 主目录（索引可放 vault 外或 gitignore）。
6. **坏文件隔离保留**：见 §2。

---

## 5. 方案详设（按阶段）

### 阶段一：读性能止血

#### 1. `all_notes()` mtime 缓存【S｜低】

**问题**【实】：vault.py:626 `all_notes()` 每次调用全量 `read_text` + `parse_note`，全仓 grep 无任何 mtime/内存缓存（config.py 明说"读每次重新读文件"）。search / view / stats / get 全部建在它之上。实测 4 条记忆时单次读调用 ~0.1s（含手机 IO + 网络）——这个数字证明"小量下不慢"，证明不了扩展性。

**设计**：

```
缓存结构：dict[path] = (mtime_ns, size, Note)
读路径：_iter_files() → 逐文件比对 (mtime_ns, size) → 命中直接用 Note，未命中才 read+parse
失效：put / delete / restore 内部同步删/更新对应 path 的缓存项
```

- 失效点收敛在 vault 写方法内（vault.py:653 `put`、:776 `delete`、:813 `restore`），与队列同模型——同一事件循环内访问，无需锁。
- `_read_note` 现有 meta 深拷贝语义（vault.py:622-623）保留，防调用方改坏缓存的 Note。
- 外部手改文件（人在编辑器里改 vault）靠 mtime 变化自然生效，不需要额外机制。
- LRU 上限防呆（几千条以内不会触发，写上防未来）。

**验收**：造 500 条测试记忆（测试 vault，不污染生产）跑 benchmark，对比 search/view 延迟；外部 touch 一个文件后读取立即反映新内容。

**依赖**：无。5d 归档层、10 关联提示和 12 索引都建立在它之上，必须最先做。

#### 2. stats 去掉无条件 git 子进程【S｜低】

**问题**【实】：实测 stats 0.33–0.36s，是其他调用（~0.1s）3 倍以上。原因是每次调用都起 git 子进程跑 log——固定开销，与数据量无关，是当前最慢的工具。

**设计**：git 历史改按需取。

```diff
- memory_stats()
+ memory_stats(with_commits: bool = false)
```

默认字段（队列积压、条数、dirty 状态）不变——这些有消费方（我今天的探测就用了）；`with_commits=true` 时数据与现状一致。

**验收**：stats 延迟降到 ~0.1s 量级；`with_commits=true` 与现状数据一致。

#### 3. upsert 返回 action 标志【S｜低】

**问题**【实】：vault.py:708-724 走更新路径时返回不说明发生了什么——agent 以为在新建，实际静默覆盖了旧记忆。

**设计**：`put` 的 existing 分支已有判断，返回加一个字段：

```diff
  { "ok": true, ... }
+ { "ok": true, "action": "created" | "updated" | "appended", ... }
```

一行级改动。这是"先拉再推"讨论的结论落地：**不强制写前检索，用这个精准信号替代**——只在覆盖真的发生时给信号，不是每次写都付查询成本。配套方案 10（写时关联提示）把"拉"的时机放进写入响应，两头一夹，写入反馈才完整。

### 阶段二：治理闭环

#### 4. `memory_write` 补时效参数【S–M｜低】

**问题**【实】：tools_tenant.py:173-225 签名无时效参数，valid-until / review-after / superseded-by 只能手改 frontmatter（dsh 自己 e2e 就这么干的）。"取代而非删除"的纪律（config.py:70,78）没有工具抓手——这是冲突防御第 5 层只闭合一半的根因。

**设计**：三个可选参数，经现有 `extra` 通道写入 frontmatter（机制已存在于 vault.py:653 `put`）：

```diff
  memory_write(lib, title, content, mode="upsert", tags=None, ...)
+ memory_write(..., valid_until: str | None = None,
+                    review_after: str | None = None,
+                    superseded_by: str | None = None)
```

- 日期复用 `knowledge.parse_date` 校验，不合法返回 `INVALID_ARGUMENT`。
- `expiring` 视图和时效四态自动生效，不需要改 knowledge.py。
- upsert 更新已有记忆时，三个字段**显式传才覆盖**，不传保留原值——避免只想改正文却把时效信息清掉。

**验收**：写一条带 valid_until 的记忆 → 日期过后 expiring 视图出现它；写 superseded_by → 旧条状态变 superseded 且全文仍可读。

#### 5. 记忆膨胀治理（四个动作）【S+S+S+M｜中】

**现状**【实】（全景见 §3）：三条防线只有一半——upsert 标题去重（有效）、时效标记（只读不清理）、instructions 纪律（软约束）。三个膨胀源：

- `.trash` 只增不减：软删为了"误删可恢复"刻意不物理删，但回收站无 TTL、无条数上限。git 全历史才是最终兜底，trash 真正需要的只覆盖"近期误删"。
- superseded 链只增不减：A→B→C→D 链式取代下旧条目无限累积，且**全部留在主检索范围**，agent 捞到过期口径的概率随链长上升。
- 无归档、无配额告警：引用关系算了（`build_backlinks`）却没用于治理——0 引用的老记忆是最典型的归档候选，现在和热记忆同权。直接 hit 性能：读路径零缓存下条数线性放大延迟。

**设计**（全部遵循约束 3：移出主检索范围但可找回）：

- **5a 取代链折叠**【S】：view 层沿 superseded-by 建链，同链只显示最新一代（或最新+上一代）。纯展示过滤，存储不动。
- **5b `.trash` TTL**【S】：超 N 天（默认 30，可配）或超 M 条的回收站文件物理清除。触发时机放懒清理（list_trash / stats 时检查），不占写路径。
- **5c 配额告警**【S】：stats 返回增加"接近上限"提示（单库条数阈值可配，默认如 500），到量提示归档，**不硬拒**——共享库拒写会挡住别人。
- **5d 归档层**【M，放阶段三】：低引用（backlinks=0）+ 超 N 天未更新的记忆移入 `archive/` 子目录。`_iter_files` 默认跳过；search/view 加 `include_archive` 参数显式找回。由 `memory_gc` 工具或定时触发，**不自动搬**——共享库里自动移动别人的记忆要先有共识。

**验收**：造 superseded 链 A→B→C，视图只显示 C；trash 放超期文件，下次 stats 后消失且 git 历史可 recover；条数过阈值 stats 出现告警字段。

**依赖**：5a/5b/5c 独立；5d 动 `_iter_files`（读路径公共入口），必须与方案 1 的缓存一起测，故排阶段三。

#### 6. memory_view 目录 bug【S｜低】

tools_tenant.py:283-287：`readable()` 被调两次，且不传 `kind` 时只取 `readable()[0]` 一个库的 `.views()` 当目录，其余库的视图目录不体现。改法：`readable()` 取一次复用，逐库 `views()` 合并成完整目录。

#### 7. 清理 tools.py 死代码【S｜低】

删 `src/shared_memory/tools.py`（386 行旧单池 `MemoryTools`，生产无入口引用）。**注意 tests/test_memory.py:1153 有旧测试引用它，且那个测试测的 `memory_export` 已由方案 14 接进 v2**——旧测试改写为测新工具（v2 多租户 + 工作空间维度），**先改测试再删文件**，别留下挂红引用。

#### 8. 重写 README + design-rationale 为 v2【M｜低】

README 工具表直接以 tools_tenant.py 的 12 个工具为准（whoami/libs/search/get/list/write/delete/restore/view/backlinks/stats/commit——生产 tools/list 已核实就是这 12 个）。design-rationale 补多租户章节：为什么身份由服务端算、为什么库间物理隔离、为什么默认写落个人库、为什么不上向量（vault.py:857 的论证值得写进去）。**这份代码最大的坑不是 bug 是文档**——README 讲"无分桶 8 工具"、design-rationale 零处提及租户，照文档接必错。

**验收**：grep 两份文档的"工具数/分桶"描述与代码一致；新人只读文档能接对。

### 机制层：召回与反馈（并入阶段二）

先说清 search **已经有什么**，避免重复造：计分健全（标题开头 100 > 标题包含 60 > 工作区名 30 > 标签 40 > 正文 8×最多 5 处）、多词 AND、同分按更新时间倒序、tags/workspace/since/until 过滤、relevance/updated 排序（vault.py:926-955）。机制层的问题是**写时才有的结构在读的接口上不暴露**，不是检索弱。

#### 9. search 补 claim/status 过滤【S｜低】

**问题**【实】：memory_search 签名（tools_tenant.py:105-116）有 tags/workspace/since/until/sort，**没有 claim 和 status**；search 主循环（vault.py:900-952）不过任何时效状态。后果：agent 问"部署的现行口径"，superseded 旧条、过期条、未核实推断和 active 事实混在一起返回。写时的反幻觉结构（claim 三分、时效四态）在检索层完全不生效——`inferences`/`expiring` 只有全局视图，search 问不出"只要 active 的 fact"。

**设计**：

```diff
  memory_search(query, tags, workspace, lib, limit, offset, since, until, sort)
+ memory_search(..., claim: str = "", status: str = "active")
```

- `claim`：`fact / inference / proposal`，空 = 不限（复用 frontmatter 现有字段，knowledge.py 已解析）。
- `status`：`active`（默认，排除 superseded/expired）/ `all`（考古模式，含被取代的）。过滤逻辑直接调 `knowledge.expiry_state`，view 层已有同款判断，不重写。
- **默认 active 是行为变更**——见决策 7。`expiring` 视图和 `memory_get` 不受影响，考古走 `status=all`。

**验收**：造 A→B superseded 链 + 一条 inference，search "A 的关键词" 默认只回 B（active），`status=all` 才回 A；`claim=fact` 过滤掉 inference。

**依赖**：方案 4（时效参数）让"可标记"成立，本方案让"可检索"成立——4 之后做。

#### 10. 写时关联提示【S–M｜低】

**问题**【实】：memory_write 返回只有 ok +（方案 3 后的）action。agent 写完才知道撞了标题，之前没有任何"可能相关"的信号——这是"先拉再推"讨论的真正缺口：不强制写前检索（决策 1），但**服务端可以在写入响应里把"拉"的时机还给 agent**。

**设计**：write 返回增加 `related` 字段——按标题相似度 + 共享标签 + backlinks 重合取 top 3 的既有记忆（只带标题/id/_lib，不带正文），纯提示不改写入行为：

```diff
  { "ok": true, "action": "updated", ... }
+ { "ok": true, "action": "updated", "related": [{"id": "...", "title": "...", "_lib": "..."}], ... }
```

agent 看到 related 自行决定：忽略 / 改用 create / 补 superseded_by（方案 4 的参数）/ 合并。成本：每次写多一次 `all_notes()` 扫描——方案 1 的缓存就位后近似零成本，且写是低频操作。

**验收**：写一条与既有记忆标题高度相似的记忆，返回的 related 里出现它；无关写入 related 为空数组。

**依赖**：方案 1（缓存摊薄扫描成本）、方案 3（action 与 related 一起构成完整的写入反馈）。

### 阶段三：按需

#### 11. 会话启动变更包【M｜低】

**问题**：instructions 是构造时静态文本（http_server.py:139-152），agent 每次会话要主动 whoami 才知道身份和可读库——多一个往返（实测 ~0.1s）。更深的缺口：**每个 agent 每次会话都是盲启动**——不知道上次离开后别人写了什么。search 有 `since` 参数（vault.py:884）但这个原语没有 agent 会主动用。

**设计**：initialize 响应挂中间件，把当前 token 对应的启动包渲染进 instructions：

- 身份 + 可读/可写库清单（省一次 whoami 往返）
- **自上次访问以来的变更**：服务端按 token 记 last-seen 游标，启动包带"新记忆 / 被取代 / 时效告警"摘要（recent + expiring 的裁剪版，不带全文）
- 静态 instructions 保持兼容，动态部分挂在 initialize 后的上下文里

这是 workbuddy"画像预注入"的本地化。收益随接入方数量上升：今天只有 WorkBuddy 一个 agent 时是边际改善，第二个 agent 接入即为刚需——共享记忆的核心交互就是"我写的别人看得到"。

#### 12. 倒排索引（条件触发）【M｜中】

**触发条件**：单库过千条，且方案 1 缓存做完后读延迟仍不可接受。到时在读路径加一层索引，不换存储层。README 自己这个判断是对的——别提前优化。

#### 13. memory_flag 质量反馈（候选，需求未验证）【S｜低】

**问题**：现在唯一的纠正路径是 superseded-by（方案 4）——要求纠正者**知道正确答案**。但常见状态是"知道这条不对，不知道什么才对"：推断被事实推翻、口径已变但没人写新的。这类信号现在无处安放，只能烂在库里或被静默覆盖。

**设计**：`memory_flag(ident, reason: wrong | outdated | duplicate, note="")`——不删不改原记忆，标记写进 frontmatter + 计数进类 expiring 视图（如 `disputed`）；search 结果带标记，配合方案 9 的 status 过滤可排除。

**为什么是候选**：多 agent 真实使用前，无法验证 flag 会被用还是被滥用（乱 flag 本身就是污染源）。触发条件：至少两个 agent 实际接入，且出现过一次"想标记却发现没工具"的场景。

### 数据可迁移（并入阶段二）

#### 14. memory_export / memory_import（以工作空间为单位）【M｜中】

**背景**：v1 承诺过 `memory_export`（整库导出 md）但从未接进 v2（连面板 UI 都没有）——这是用户 2026-10-13 明确要补的需求，且粒度定为**项目/工作空间**：workspace 字段（vault.py:880-881）就是现成的项目维度，导出导入都以它为单位，整库导出降级为不传 workspace 的特例。

**设计——导出**：

```diff
+ memory_export(workspace: str = "", lib: str = "", out_dir: str = "") -> {
+     "ok", "bundle": "<path>", "workspace", "lib", "count", "bytes", "manifest"
+ }
```

- 产物 = 一个自包含 bundle 目录（或 zip）：每条记忆原样 `.md`（frontmatter + 正文**字节级保留**，不重新序列化——导出再导入必须无损往返）+ `manifest.json`（schema 版本、workspace、lib、条数、导出时间、每条 id/title/mtime 校验和）。
- **权限**：只能导你有权读的库；不传 lib 时按可读库逐个导出并标注来源库。
- 落点：默认 `<vault 外>/exports/<workspace>-<timestamp>/`（vault 主目录保持纯净，约束 5）；也可指定 out_dir。返回路径供 agent/人取走。

**设计——导入**：

```diff
+ memory_import(bundle: str, target_lib: str = "", workspace: str = "",
+               on_conflict: str = "skip") -> {"ok", "imported", "skipped", "updated", "errors"}
```

- 读 manifest 校验 schema 版本，不认的版本直接拒绝（防止导出的结构变了还硬灌）。
- 走**正常写路径**（队列 + 权限检查 + 坏文件隔离），不搞旁路写入——导入不过是批量 memory_write。
- 冲突策略（按 title 判重，与写时防重同一把尺子）：
  - `skip`（默认）：已存在同标题 → 跳过并计数。**共享库里"不覆盖"是默认道德**，与约束 3 一致；
  - `update`：按 upsert 语义合并（方案 4 的时效字段显式传才覆盖的规则同样适用）；
  - `rename`：加 `--<id>` 后缀另存（复用 `_unique_path` 三态逻辑）。
- workspace 参数缺省用 bundle manifest 里的值；target_lib 缺省落**个人库**（写入默认最小污染面，与现有 write 默认一致）。

**验收**：导出 20 条（含 superseded 链、时效字段、特殊字符标题）→ 删库重导 → 逐条 diff 原文一致；同 bundle 二次导入默认全 skip；`on_conflict=update` 只更新指定字段；跨库导入落个人库且 `_lib` 标注正确。

**工作量**：M。**风险**：中——导入是唯一"一次写一堆"的入口，必须走队列；bundle 路径要防目录穿越（不接受绝对路径逃逸 out_dir 的 manifest 条目）。

---

## 6. 决策记录（已拍板）

1. **"先拉再推"不做成强制机制**（2026-10-12 拍板）：create 模式报 CONFLICT 已是"推前检查"的硬版本，按需用即可；强制写前检索让每次写都多一次 HTTP 往返，成本收益不匹配；真实风险（静默覆盖）用方案 3 的 action 标志精准防。存储层无需担心——单写者队列已把写串行化，天然无 read-modify-write 竞态。不加强制的是"写前检索"，不是"写本身的安全网"——方案 3 和 4 照做。
2. **不上向量检索**：引入 API key / 网络 / 额度 / 模型行为四个失败点，语义召回在当前量级不划算，结果不可复现（vault.py:857 论证成立）。
3. **不做 agent 隔离**：共享池是产品定位不是疏忽。要隔离的场景不该用这个服务。
4. **workbuddy 式"删除语义"不照搬**：workbuddy 删自己的旧日志只影响自己；共享库清理影响所有 agent。可借鉴的已转化——预注入→方案 11（启动变更包）、渐进披露→views 已有（加缓存即可，方案 1）、硬上限→配额告警（5c）、日志蒸馏→归档层（5d）；每用户独立库明确不借鉴（产品定位的反面）。
5. **倒排索引条件触发**：单库过千条且方案 1 就位后仍不够快再做。
6. **search 机制不重写，只补口径过滤**：计分 / AND / since/until / tags/workspace 已健全（vault.py:926-955），问题是写时的结构（claim 三分、时效四态）在读的接口不暴露。补参数，不换检索机制，不上 embedding（决策 2）。
7. **search 默认只返回 active**（行为变更，需在 README 和工具描述里显式标注）：stale-by-default 是缺陷不是特性——agent 问"现行口径"时 superseded/expired 混在结果里是最典型的误用来源；考古场景用 `status=all`，`expiring` 视图和 `memory_get` 不受影响。
8. **导入导出以工作空间为单位，字节级往返，冲突默认 skip**（2026-10-13 用户需求）：workspace 是现成项目维度，不另造"项目"概念；导出不重新序列化 md（frontmatter 原样），保证导出→导入无损；导入走正常写路径（队列+权限），冲突默认跳过——共享库里"不覆盖"是默认道德，`update`/`rename` 显式选。

---

## 7. 实施路线图

**阶段一：性能止血**（1 → 2 → 3）
- 顺序：方案 1 先行（其他都受益于它），然后 2，3 是一行级顺手做
- 验收：`scripts/` 加 bench.py，造 500 条测试记忆测 search/view/stats 三调用做前后对比；外部改文件后读取立即生效
- **出口标准**：search/view ~常数时间（有缓存），stats < 0.15s

**阶段二：治理闭环 + 机制层**（4 → 5a/5b/5c → 9 → 10 → 6 → 7 → 8 → 14）
- 顺序：4 时效参数（5a 折叠和 9 过滤都消费它，先行）→ 5a/5b/5c → 9 claim/status 过滤 → 10 写时关联提示 → 6 小 bug → 7 死代码 → 14 导入导出 → 8 文档
- 验收：时效写入 → expiring → superseded 全链路可走；取代链视图折叠正确；search 默认只回 active、`status=all` 可考古；写入返回 related 提示；按工作空间导出→删→重导逐条 diff 无损、二次导入默认全 skip；文档 grep 校验与代码一致；**开发机 `pip install -e .` 后全量 pytest 绿灯**（tests 三个文件合计 161 个测试函数：92+55+14——现在这只是 dsh 自述，实跑截图贴进 README，把"声称"变"证据"）
- **出口标准**：工具面能自洽完成"写-标效-取代-告警-按口径检索-按工作空间迁移"闭环，不需要手改 vault

**阶段三：按需**（5d / 11 / 12 / 13）
- 触发条件：5d 和 11 在第二个 agent 实际接入后启；12 在单库条数过千且阶段一出口标准失守时上；13 在出现"想标记却没工具"的真实场景后做

---

## 8. 附录

### 实测数据（2026-10-12，vivo 生产，4 条记忆，2 库，含网络开销）

| 调用 | 耗时 | 说明 |
|---|---|---|
| memory_whoami | 0.07–0.10s | |
| memory_search | 0.09–0.10s | 跨库全量扫描 |
| memory_view(overview) | 0.08s | 实时计算 |
| memory_stats | 0.33–0.36s | git 子进程固定开销 |

注：这是数据量≈0 的下限，证明"小量下不慢"，不证明扩展性。扩展性取决于方案 1。

### 证据索引（读码定位用）

| 事实 | 位置 |
|---|---|
| all_notes 无缓存 | vault.py:626；全仓 grep 无 mtime/缓存 |
| put/delete/restore | vault.py:653 / 776 / 813 |
| upsert 静默更新分支 | vault.py:708-724 |
| upsert/create/append 三模式 | vault.py:653 `put` 起 |
| 文件名撞名三态 | vault.py `_unique_path` |
| meta 深拷贝 | vault.py:622-623 |
| 向量检索论证 | vault.py:857 |
| memory_write 签名 | tools_tenant.py:173-225 |
| view 目录 bug | tools_tenant.py:283-287 |
| 防抖提交 | service.py:137-163 |
| 时效四态 | knowledge.py:80 |
| instructions 纪律 | config.py:70,78 |
| 静态 instructions | http_server.py:139-152 |
| 死代码 | tools.py:50；tests/test_memory.py:1153 |
| search 计分与过滤实现 | vault.py:845-955（标题>工作区名>标签>正文，AND，**无 claim/status 过滤**） |
| memory_search 签名 | tools_tenant.py:105-116 |
| memory_view 八类视图 | tools_tenant.py:262-280 |
| workspace 适用范围维度 | vault.py:880-881, 940-943（已存在，缺填写纪律不是缺字段） |

### 仍未坐实（dsh 自述，本机无 fastmcp/pytest 复跑不了）

- 161 单测全过、生产 e2e 全过、局域网 23 项 401 零泄漏审计——仓库脚本都在，阶段二实跑 pytest 后可转为证据。

---

## 9. 实施记录（2026-10-13，全自动实施）

### 9.1 十四项方案终态

| # | 方案 | 终态 | 说明 |
|---|---|---|---|
| 1 | all_notes mtime 缓存 | ✅ **dsh 同步实现**（sync-import 9b4e53e）| NoteCache 逐文件 (mtime_ns,size) + stamp 缓存 + 5s revalidate，本机复核保留 |
| 2 | stats 去 git 子进程 | ✅ | `with_commits` 按需（tenancy/service 两层）+ dsh 的 log 纯 TTL 缓存叠加 → **稳态 ~6ms**（原 350ms）|
| 3 | upsert action 标志 | ✅（部分既有）| `vault.put` 早已返回 `action`；本方案实质新增是 **related 提示（方案 10）**，文档口径据此修正 |
| 4 | write 时效参数 | ✅ | service 进队列前校验坏日期；工具层三参数 + **claim**（计划外增补，见 9.2）|
| 5a | 取代链视图折叠 | ✅ | `build_view` 默认滤除 superseded-by 非空；expiring 恒含 |
| 5b | trash TTL | ✅ | `gc_trash(max_age_days, max_files)`，stats 时顺带懒清理 |
| 5c | 配额告警 | ✅ | `quota_warn_notes`，stats 带 `quota_warning`，只提醒不拦截 |
| 5d | 归档层 | ⏭ **跳过** | 触发条件未到（第二个 agent 未接入）；5b/5c 已覆盖主要膨胀源。条件：单库超千条且主检索范围仍显拥挤 |
| 6 | view 目录 bug | ✅ | 不填 kind 直出静态 `list_views()`，不再走 `readable()[0]` |
| 7 | 删 tools.py | ✅ | 386 行死代码已删；唯一引用它的测试迁移为 v2 版（信封契约不变）|
| 8 | 文档重写 | ✅ | README 全量重写为 v2（三级库/14 工具/四道闸）；design-rationale 新增 §7-12 |
| 9 | search claim/status | ✅ | vault 打分前过滤；service 解析 `search_default_status`；工具层透传 |
| 10 | 写时关联提示 | ✅ | `_related_notes`：标题相似度×100 + 共享标签×10 + 同工作区 +5，top3 |
| 11 | 启动变更包 | ✅ | `digest.py`：on_initialize 中间件 + seen 记账精确一次 + 三层并发锁（见 9.2）|
| 12 | 倒排索引 | ⏭ **跳过** | 条件未触发：压测 400 条规模检索 p95=183ms，远未到瓶颈 |
| 13 | memory_flag | ⏭ **跳过** | 需求未验证（没有"想标记却没工具"的真实场景）|
| 14 | 导入导出 | ✅ | bundle 字节级 + manifest sha256 + 冲突三策略 + 防穿越 + schema 版本校验 |

### 9.2 计划外增补（实施中实证出来的必要项）

1. **`memory_write` 加 `claim` 参数**：claim 三分是反幻觉的结构性落点，但原设计只能靠服务端推断——推断不了"我建议…"和"我断定…"的区别。显式标注 + service 三值校验（拼错一个字母就会变成永远过滤不掉的幽灵值）。
2. **digest seen 记账**：note 的 `updated` 是秒级精度，`>=` 比较无法区分"游标同秒的基线条目"和"同秒新写入"——漏报和重复报二选一。解法：`>=` 保不丢 + 播报过的 id 进 seen 保不重，**精确一次**。
3. **bundle 白名单从正则改结构化校验**：第一版 `[\w.-]` 正则① 不含中文 → `group-研发` 整库导不出去；② 更隐蔽——不含中文标点 → 标题带 `·`/`（）` 的条目被**静默跳过**（只记 warning）。改为与 `_slug` 同口径的负向校验 + Windows 保留名（CON.md 在 Windows 是控制台设备）。
4. **导入改 `write_bytes`**：`write_text` 在 Windows 把 `\r\n` 二次翻译成 `\r\r\n`，字节级还原失效——而字节级正是这条链路唯一可验证的正确性标准。
5. **digest 游标三层并发锁**：压测 10 用户并发 initialize 实锤三层 race（tmp 同名互踩 WinError 32 / save 撞别人 load WinError 5 / **跨用户整表保存互抹**——最深：u5 基于旧表保存把 u0 的基线整个抹掉）。逐层修：唯一 tmp 名 + 文件 IO 全局锁 + 锁内只合并自己条目。
6. **`gc_trash` 修 Python 3.13 惰性 glob**：`Path.glob()` 3.13 返回惰性 map，`len()` 直接 TypeError。
7. **`tenancy.export` 改同步**：内部无 await 点，包 async 会让工具层 `_io` 卸载拿到未 await 的协程。

### 9.3 测试与压测（本机实跑，非自述）

- **单测**：`188 → 189 passed`（test_optimization.py 29 例覆盖方案 2/3/4/5a/5b/5c/6/9/10/11/14 + digest 并发回归）
- **压测**（`scripts/stress.py`，14/14 通过，2026-10-13 最终轮）：
  - 多库并发写：5 库 × 8 写者 × 10 条 = **400 笔 / 7.2s（55 writes/s），0 丢失 0 覆盖**
  - 读写混合：1027 次并发检索 **p50=127ms / p95=180ms / p99=199ms**，0 异常
  - 多工作空间：8 区 × 25 条过滤全对；8 区导出导入往返 + 字节级抽验通过
  - 导入导出量级：300 条导出 2.2s、导入 7.9s（走队列串行，38 条/s），复导冲突全跳过
  - digest 并发：10 用户 × 3 连——首连静默、二连见 30 条变更、三连全静默（精确一次）
  - stats：默认 5.6ms / with_commits 6.2ms（TTL 缓存下都远低于 50ms）
- **已知限制（诚实记录）**：大库（~900 文件）防抖提交时 `git add -A` 会触 30s 超时警告。commit 是**尽力而为的后验备份**，写入早已落盘（ack 在落盘后），不影响 durability；后续优化方向是增量 add（按本次变更文件列表），未做——不在本次范围。

### 9.4 对原设计的偏离（口径修正）

- **方案 3 的实质**：`action` 标志在 `vault.put` 早已存在，本方案真正的增量是 related 提示（方案 10）。总表口径据此更新，不算"两项做了一项"。
- **方案 2 的收益被缓存部分抵消**：dsh 同步来的 log TTL 缓存让 with_commits 也快了（6.6ms vs 5.9ms）。flag 的价值从"省 60ms"变成"省冷缓存时的那一次 fork"——README/design-rationale 措辞已按实测改。
