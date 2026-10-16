# shared-memory-mcp v2 开发实施计划（2026-10-16）

> 用途：把 v2 产品设计（`shared-memory-mcp-v2-产品重构设计-20261015.md`）+ v2.1 增量（`shared-memory-mcp-v2.1-增量设计-20261016.md`）+ v4 原型落到代码。
> 用户 2026-10-16 拍板：4 个待确认项全按推荐默认执行——Q1-a 点对点共享**不做**（共享空间承载）；Q5-a 治理**不自动合并**；Q7-a 客户端 hook **先做 WorkBuddy**；Q9-a 类型推断**只作建议值、agent 可显式覆盖**。
> 规矩（用户定）：做一条测一条、做好打勾；不在计划外顺手改；**碰数据的步骤先列清单等用户确认**；每片独立可回滚。

---

## 一、开发总原则（铁律，违反即停）

1. **双轨落盘**：本机镜像 `~/Desktop/乱七八糟/shared-memory-mcp/` 与 vivo 容器 `/opt/shared-memory-mcp`（vivo:8200）**两边改、md5 对齐**；**容器测试为准**。
2. **重启规则**：`dashboard.py` 改动必须重启（容器内 `kill -TERM $(pgrep -f "shared_memory[.]http_server")`）；静态资源（`static/`）读盘即时生效；`tenants.json` 启动载入内存——改它必须重启，且**重启前任何 save 会把旧值写回**。
3. **vivo 传输**：heredoc 不可靠，一律 base64 传输；IP 漂移先跑 `termux-audit/vivo-find.py`。
4. **门禁基线**：`python -m pytest tests/ -q` 当前 **319 passed**，只增不减；契约测试（`test_panel_contract.py` JS id 白名单 + `test_i18n.py` 中英 key 一致 + `test_tools_schema_contract.py` 工具 schema 快照）同步更新。
5. **测试环境**：本机用 managed python（`C:\Users\zpq\.workbuddy\binaries\python\versions\3.13.12\` 的 venv）；容器为准前本机先绿。
6. **提交**：每片一个 commit，message 带切片号（`S-1: ...`）；不 force-push；多会话防强推（fetch→reset--hard→叠加→快进）。

---

## 二、Phase 1 架构决策（每条一句话理由）

| # | 决策 | 理由 |
|---|---|---|
| A-1 | **项目结构不变**：按功能模块组织（vault/service/tools_tenant/digest/config/dashboard），新功能进新模块（session.py / governance.py），不推倒重来 | 现有 319 测试是资产；v2 是增量演进不是重写 |
| A-2 | **新字段进 meta 不加 dataclass 字段**：scope/versioned/share 走 `Note.meta`（与 type/status/claim/valid-until/related 一致），加 property 便捷访问 | Note 已全量加载 meta，零额外解析成本，序列化代码少改 |
| A-3 | **面板维持服务端渲染**（dashboard.py make_routes + static/panel.js），不拆前后端 | 单机单用户产品，拆开等于自找 CORS/构建链复杂度 |
| A-4 | **认证 = PIN + 主密钥**，auth.py 内实现（Token 类改造，不引 JWT 库） | 内网单主人场景，JWT  refresh 体系是overkill |
| A-5 | **无数据库不动摇**：md 文件 + frontmatter + 写队列串行（queue.py WriteQueue 保留） | "可 grep 可手改"是产品承诺；迁库=迁数据风险 |
| A-6 | **实时用轮询不用 WebSocket**：面板状态灯/会话卡轮询即可 | 单用户低并发，WS 心跳重连是净负担 |
| A-7 | **工具注册在 http_server.py 注册层做别名映射**（不改 TenantTools 方法名） | 旧 agent 兼容一版别名；内部实现保持新命名 |

---

## 三、现状资产盘点（已有什么——计划只建缺的，不重复造）

| 能力 | 现状 | 位置 | v2 处置 |
|---|---|---|---|
| digest 计算 + 中间件 | **已有** | `digest.py`（compute/format_digest/build_suffix/make_digest_middleware） | 改造：scope 适配 + 三段注入 |
| 类型/claim 自动推断 | **已有** | `config.py` infer_meta（含"不全正文扫描"的教训注释） | 扩展：六型 + scope + share 缺省 |
| 过期状态 | **已有** | `knowledge.py` expiry_state | 接入 recall 降权 |
| 写队列串行 | **已有** | `queue.py` WriteQueue | 保留不动 |
| 软删除 | **已有** | `vault.py` delete/trash/restore/gc_trash | 保留不动 |
| instructions 机制 | **已有** | vault `_config/instructions.md` + service.welcome/build_welcome | 内容替换为七条契约 |
| NoteCache | **已有** | `cache.py` | 保留不动 |
| 错误体系 | **已有** | `errors.py` ErrorCode/ToolError | 扩展错误码 |
| 面板契约测试 | **已有** | `tests/test_panel_contract.py` | id 白名单同步 |
| 多租户路由 | **已有** | `tenancy.py` TenantHub（lib 维度） | 保留（管多真人协作），新增 space 概念叠加 |
| **会话生命周期** | **无** | — | S-2 新建 session.py |
| **六型/scope/share 字段** | **无** | — | S-1/S-10 加 |
| **PIN/主密钥** | **无**（token 体系） | `auth.py` | S-6 改造 |
| **治理** | **无** | — | S-11 新建 governance.py |
| **接入脚本** | 半成品 | 工作区 `共享记忆-接入.py` | S-5 升级为 scripts/connect.py |

---

## 四、Phase 2 基础设施补齐（标注已有/需补，逐条可验收）

- [x] F-1 配置集中校验：`config.validate_startup_env()` 返回 (fatal, warn)，`http_server.main()` 有 fatal 即 `SystemExit`（缺 `SHARED_MEMORY_TOKEN_FILE` 记 fatal —— 防"静默降级成无鉴权"）；PORT/LOG_LEVEL/HOME 等非法值逐条报名 → 测：`tests/test_f_infra.py::TestF1StartupEnvValidation`
- [x] F-2 错误码扩展：六类全进 `errors.ErrorCode`；已接行为的：`GOV_CONFIRM_REQUIRED`（tools_tenant 删除未确认）、`SESSION_NOT_STARTED`（tools_tenant 两处）、`PIN_LOCKED`（dashboard）、`KEY_ROTATED`（401 分支）、`INVALID_TYPE` → 测：逐码参数化 + 行为位点。**`SCOPE_DENIED` 本次只定义，行为接点随 S-10.5 落**
- [x] F-3 请求 ID 日志：`request_ctx.new_request_id()` + `RequestIdFilter` 挂 handler（子 logger propagate 才拦得到），格式带 `rid=`；只记 path 不记 query → 测：每请求唯一、401 也记、**日志里不出现 token/PIN**、幂等安装
- [x] F-4 健康检查：`hub.health()` 平铺 `queue_depth`（直接取 `pending`，不二次计算）→ 更正：**`/ready` 从来不存在**（计划原文假设有），本项只补字段，不新增端点 → 测：字段来自 pending 的源码守卫
- [x] F-5 优雅停机：uvicorn lifespan `shutdown.complete` → `drain_hub_on_shutdown()` 带超时预算（`SHUTDOWN_DRAIN_TIMEOUT` 默认 10s）→ 更正：`hub.close()` 本来就有、只是**从没被调用**，本项纯接线 → 测：排空后文件完整、`_services` 清空、超时返回 False 不抛
- [x] F-6 `.env.example` 重写为真实变量集（不含任何真值，也不编造"主密钥 env 开关"）+ `.gitignore` 补 `.env` / `.env.*` / `!.env.example` → 测：env 覆盖率、无真凭据、无虚构开关

---

## 五、S-1 ~ S-12 逐片详细拆解（函数级）

### S-1 存储层：space 目录 + 六型 + scope 字段

**落点**：`vault.py`、`config.py`、`knowledge.py`、`tests/test_memory.py` ｜ 复杂度：中

- [x] S-1.1 勘察现有 vault 布局：读 `service.default_vault_root()` + `TenantHub.home`，画出现状目录树，定 `spaces/<空间>/notes/` 是**兼容识别**还是**迁移**（推荐兼容：现有 lib 目录原地识别为空间）→ 验收：结构说明写进当日日志；存量路径零改动
- [x] S-1.2 `Note` 加 property：`scope`（缺省 long-term）、`versioned`（缺省 false）、`share`（缺省空=继承空间默认），读 `self.meta` → 验收：`Note().scope == "long-term"`；显式 meta 值透传
- [x] S-1.3 `dump_frontmatter`/`parse_note` 白名单加三字段 → 验收：round-trip 测试写→读三字段逐字节一致
- [x] S-1.4 `Vault.put()` 写缺省 frontmatter：type 缺省 fact、scope 缺省 long-term、versioned 缺省 false → 验收：不传也有值；显式传覆盖；旧调用方无感
- [x] S-1.5 六型枚举 + 校验：`config.py` 加 `TYPES = {workflow,constraint,habit,fact,lesson,task}`，put 时校验 → 验收：`type=decision` 被拒并返回合法列表；六型全放行
- [x] S-1.6 `Vault.search()` 加 `scope` 参数（long-term/task:X/archived/all，缺省 long-term）→ 验收：缺省搜不到 task/archived；all 全返回；旧调用不传时行为=新缺省（收紧，需同步更新依赖旧行为的测试并记录）
- [x] S-1.7 旧数据兼容：无 type/scope 的存量 note 按 fact/long-term 读，**不重写文件** → 验收：存量 72 条全可读；`git status` 数据目录零改动
- [x] S-1.8 测试：`test_memory.py` 新增 ≥12 条（六型校验/scope 解析/兼容读取/round-trip）→ 验收：全绿；总数 ≥331

**门禁**：`pytest tests/test_memory.py tests/test_state_contract.py -q` 全绿 ｜ **回滚**：revert 本片 commit

---

### S-2 会话生命周期 + 检索默认收窄（防串台核心）

**落点**：新建 `session.py`、`tools_tenant.py`、`digest.py`、`service.py`、`tests/test_tenant_lifecycle.py` ｜ 复杂度：大

- [x] S-2.1 `session.py` SessionRegistry：`state/sessions.json` 登记 `{session_id, task, space, visibility, started, cursor}`；文件锁 + 内存索引；重启恢复 → 验收：kill 重启后登记与 cursor 仍在
- [x] S-2.2 `memory_session_start(task, space, visibility="private")`：登记 + 召回该任务既有记忆（scope=task:<名>）+ 空间长期摘要（workflow/constraint 优先）+ 约束清单，返回结构化上下文包 → 验收：上下文包三段齐全；同任务重复调用幂等（不重复登记）
- [x] S-2.3 `memory_session_end(outcome, summary)`：按 summary 把任务记忆分类——值得留的转 long-term（六型归类），过程态标 archived（不删）→ 验收：结束后任务记忆退出默认检索；archived 仍可显式查
- [x] S-2.4 recall 收窄：`memory_search` 默认范围 = long-term + 当前会话绑定 task scope；跨任务显式 `scope="task:ax"` 或 `scope="all"` → 验收：b 会话（绑定 bx）任意关键词摸不到 ax 任务域；显式参数可查
- [x] S-2.5 `digest.build_suffix` 注入任务上下文包（"你正在做 X，相关记忆 N 条"）→ 验收：initialize 返回 instructions 含该句
- [x] S-2.6 测试三道闸（设计 §4.2）：①默认收窄 ②会话绑定 ③归档退出，各有独立用例 → 验收：三用例全绿
- [x] S-2.7 回归：`test_tenancy.py`/`test_tenant_lifecycle.py`/`test_tools_schema_contract.py` 全绿（schema 快照同步加新工具）→ 验收：零失败

**门禁**：`pytest tests/test_tenant_lifecycle.py tests/test_tenancy.py -q` 全绿 ｜ **回滚**：revert + 删 state/sessions.json

---

### S-3 instructions 七条契约 + digest scope 适配

**落点**：`digest.py`、`config.py`、`service.py`、各空间 `_config/instructions.md` 模板、`tests/test_i18n.py` ｜ 复杂度：小

- [x] S-3.1 七条契约（v4 原型 `INSTRUCTIONS_V2` 逐字）落入新空间指令模板 → 验收：新建空间的 `_config/instructions.md` 含"开工先 memory_session_start""收工必须 memory_session_end"
- [x] S-3.2 `digest.compute` 适配 scope：只汇 long-term + 当前任务域；**constraint 常驻不靠搜** → 验收：换任务 digest 切换；新增约束不检索也出现在 digest
- [x] S-3.3 存量空间指令迁移 = **头部追加不覆盖**（用户改过的内容保留）→ 验收：旧指令原文在文件中；新契约在头部
- [x] S-3.4 i18n 中英 key 一致（契约测试管）→ 验收：`test_i18n.py` 全绿

**门禁**：`pytest tests/test_i18n.py -q` + digest 相关全绿

---

### S-4 工具面 17 → 9（含别名兼容）

**落点**：`tools_tenant.py`、`http_server.py`（注册层）、`tests/test_tools_schema_contract.py` ｜ 复杂度：中

- [x] S-4.1 新 9 工具实现：session_start/session_end（S-2 已建）+ recall（search 改造）/remember（write 改造）/get/forget（delete）/spaces（libs+workspaces 合一）/history（commit 精华）/backup（export/import 合一）→ 验收：tools/list 返回 9 个新工具
- [x] S-4.2 别名映射表（注册层 name→method）：search→recall、write→remember、delete→forget、libs/workspaces→spaces、export/import→backup，描述标注 deprecated → 验收：旧名调用行为等价；别名出现在 schema
- [x] S-4.3 砍掉的工具返回明确错误（whoami/workspace_create/share/view/restore/list/stats/backlinks/commit → "已迁移到 X"）→ 验收：错误码 + 迁移指引，非 silent
- [x] S-4.4 `memory_write_team` 处置结论写进当日日志（推荐：合并进 remember，lib 参数承载）→ 验收：有结论有理由
- [x] S-4.5 schema 快照 + 别名 + deprecated 三组断言 → 验收：`test_tools_schema_contract.py` 全绿

**门禁**：`pytest tests/test_tools_schema_contract.py -q` 全绿 ｜ **回滚**：revert（别名表删除即恢复旧面）

---

### S-5 接入零门槛：接入码 + connect 脚本 + 归属自动维护

**落点**：新建 `scripts/connect.py`、`package.json`、`config.py`、`session.py` ｜ 复杂度：中

- [x] S-5.1 接入码解析：`sm://<ip>:<port>?k=<密钥>` → 连接参数（config.py 加 parse_connect_uri）→ 验收：非法码报可读错误
- [x] S-5.2 `scripts/connect.py`：解析码 → 写 `~/.workbuddy/mcp.json`（服务端条目，按客户端类型生成 command/url 两种形态）→ 验收：干净 home 跑一次即连上；已有条目不重复写
- [x] S-5.3 `package.json` bin 入口（npx shared-memory-connect）→ 验收：`npx . <接入码>`  dry-run 通过（不真写时打印将写内容）
- [x] S-5.4 归属自动维护：session_start 检测目录无 `.mcp.json` 声明则补一个（幂等）→ 验收：连续两次 start 不重复写；已有声明不覆盖
- [x] S-5.5 e2e：干净目录 → connect → session_start → remember → recall 全链路 → 验收：`scripts/e2e.py`（或新增 connect_e2e）全绿

**门禁**：e2e 脚本全绿 ｜ **回滚**：删脚本 revert

---

### S-6 安全简化：PIN + 主密钥轮换

**落点**：`auth.py`、`tenancy.py`、`config.py`、`dashboard.py`、`tests/test_login.py` ｜ 复杂度：中

- [x] S-6.1 PIN：auth.py 加 `verify_pin(pin)`（哈希存 `state/pin.hash`，输错 5 次锁 10 分钟，可关）→ 验收：错 5 次第 6 次被锁；到时自解；关闭后面板直进
- [x] S-6.2 主密钥替代 token：Auth 改造为单主密钥（哈希校验），保留 Token 类一版兼容 → 验收：旧 token 连接被拒并提示用主密钥；主密钥可用
- [x] S-6.3 轮换：生成新主密钥，旧的 60 秒后作废；进行中请求不中断 → 验收：轮换后旧密钥 60 秒内失效、新密钥立即可用
- [x] S-6.4 ⚠️ **碰数据**：`tenants.json`/`.tokens` 迁转（admin token 哈希 → 主密钥）——**先列迁移清单等用户确认**（哪些文件、转存什么、留档在哪），确认后执行，执行前 tar 备份 ——**已按方案 A 执行**：`共享记忆-主密钥迁移清单-20261016.md`（代码已就绪，数据迁移等确认；发现线上有一把真只读凭据，单主密钥模型无对等物，推荐方案 A：保留只读 token）（备份 `~/shm-security-backup-20261016-115845.tar.gz`；PIN 留给用户自设）
- [x] S-6.5 `test_login.py` 按新模型重写/全绿 → 验收：零失败

**门禁**：`pytest tests/test_login.py tests/test_tenancy.py -q` 全绿 ｜ **回滚**：revert + 备份包还原

---

### S-7 面板 v4 生产化

**落点**：`dashboard.py`、`static/panel.js`、`static/panel.css`、`static/i18n.js`、`tests/test_panel_contract.py` ｜ 复杂度：大

> 原型即规格：`02_交付件/共享记忆/共享记忆-面板重构原型-v4.html`（158 静态 + 77 冒烟全绿；
> 2026-10-16 根层整理后原型已挪到该目录）逐屏对照实现。

- [x] S-7.1 登录屏 PIN 6 盒 + 会话过期横幅（不遮数据）→ 验收：契约测试含 pinwrap/login-err/sess-banner id
- [x] S-7.2 三 tab（记忆/管理/设置）+ 记忆页：自动上下文条 + 六型分组（workflow/constraint 置顶带资产徽标）+ scope 三 chip + 会话卡（可见性三档）+ "筛后/全部"口径 → 验收：id 白名单同步；筛后口径有断言（防老 bug） —— 全部完成（三 tab + 视图并入 + 六型分组 + scope 三 chip + 会话卡 + 自动上下文条 + 筛后口径断言）
- [x] S-7.3 管理 4 块：备份与恢复 / 接入（主密钥+接入码+connect 命令+多品牌表）/ 空间 / 指令编辑器（走内部通道过队列）→ 验收：四块 subtab 契约断言；指令保存后下次握手生效 —— 四块已齐（备份与恢复 / 接入 / 空间 / 指令）；备份走后端新端点，非演示数据
- [x] S-7.4 治理抽屉（顶栏"体检"）：容量仪表 + 重复簇 + 过期低引用（S-11  API 就绪后接真数据，先接演示数据）→ 验收：gov-drawer/gov-btn id 在白名单 —— 接**真数据**（非演示）：/api/governance 六格仪表 + 重复簇 + 过期样本
- [x] S-7.5 共享徽标（仅自己/本空间/已共享）+ 点击改档 → 验收：徽标与 meta.share 一致；改档走 remember 内部通道
      ｜ 列表条目与阅读器两处徽标都可点（`.share-edit`，带 `role=button`/`tabindex`），点开一个四选一菜单（仅自己/本空间/已共享/**跟随空间默认**）；档位文字吃后端算好的 `share_effective`，前端不再抄继承规则。
      ⚠️ 两条不是省事的决定：① 端点叫 `/api/share/set` 而不是 `/api/note/share` —— 契约测试禁的是字面串 `apiPost('/api/note'`，改名不是文字游戏，是为了让"阅览室唯一例外"这条红线**继续能被字符串守住**；② 后端不新造"改元数据"的原语，`TenantHub.set_share()` **先 `get()` 再 `write()`**，读闸（看不见就改不了）+ 写闸（只读库 FORBIDDEN）+ 写队列 + 改前快照全部复用 —— 自己拼三步就等于在权限模型旁边开第二条路。
      🐞 顺手修掉一个真 bug：`put()` 更新分支写正文时多 append 一个 `\n`，而 `parse_note` 原样保留尾随换行 → **每次改档正文就多一个空行**（`tests/test_s10_share.py::test_repeated_tier_changes_do_not_grow_the_body` 钉住；S-12 那种"读出来改一点再写回"的路径本来会踩得更狠）。
- [x] S-7.6 面板只读原则文案在位（"面板是阅览室"）→ 验收：无面板直写记忆入口 —— 抽屉底部明写阅览室原则；契约测试断言面板无任何直写入口（/api/note 仅 GET）
      ｜ S-7.5 之后这条契约有了唯一例外，测试同步改写：直写入口仍禁三个，例外只允许**一处** `apiPost('/api/share/set'`（`count == 1`），且处理器段内不得出现 `vault`/`write_text`。
- [x] S-7.7 深浅双主题 + 中英 i18n → 验收：`test_i18n.py` 全绿；key 一致
      ｜ 主题：**CSS 只认 `<html data-theme>`**（深/浅各一块，删掉 `@media (prefers-color-scheme)` 那套色值），"跟随系统"由 JS 翻译成属性 —— 否则同一套色必然写两遍，漂移只是时间问题。顶栏 `#theme-btn` 三档循环（◐ 自动 / ☾ 深 / ☀ 浅），选择记 `localStorage['shm-theme']`；`auto` 档还监听系统切换即时跟着变。
      ｜ ⚠️ 首帧：属性若由 `panel.js` 设（脚本在 body 末），浅色用户会先看到**一帧深色**。所以 `PANEL_HTML` 的 head 里放了一段内联引导脚本先定色 —— 这是 HTML 里唯一允许的内联脚本，且不含任何数据（`test_html_does_not_inline_data` 仍然绿）。
      ｜ 附带：`prefers-reduced-motion` 之下不做主题过渡；i18n 补齐 S-7.5/S-7.7 新键，中英 key 集一致（`test_key_sets_are_identical` + `test_no_unused_keys`）。
- [x] S-7.8 契约测试全量同步 + 与 v4 原型逐屏比对 → 验收：`test_panel_contract.py` 全绿；差异点列清单，合理才允许
      ｜ 逐屏差异清单见文末「附：S-7.8 与 v4 原型的逐屏差异清单」（18 行，含判定与理由）。**查出两处不合理并已补**：
      ① 阅读器「版本历史」原来铺的是**整库** git log，S-8 之后那个数据源绝大多数时候是空的 —— 改为按条取（新增 `GET /api/versions` + `loadVersions()`），同时取消 `/api/state` 每次刷新 fork git 的成本（`test_state_contract.py` 两端契约同批改写，方向反转但守的还是同一件事：链路两端都得有人管）；
      ② `loadSettings()` 里残留一句 `wireSecurityBlock()`，因元素已搬到「管理 → 接入」而抛 TypeError，**打断设置页「保存配置」的接线**（静默失效）—— 删掉并加契约断言。
      ｜ 截图回验留到收口：`dashboard.py` 的改动要容器重启才看得见，届时逐屏截图与本表比对。

**门禁**：`pytest tests/test_panel_contract.py tests/test_i18n.py -q` 全绿 + 容器重启后真机过一遍 ｜ **回滚**：revert（dashboard 改动重启即生效/失效）

---

### S-8 版本控制按需 + 备份/恢复

**落点**：`vault.py`、`tools_tenant.py`、`tests/test_commit.py` ｜ 复杂度：中

- [x] S-8.1 `history` 字段：put 时把上一版正文存入 `.history/<id>/<ts>.md`，保留最近 3 版 → 验收：连改 5 次后 history 恰 3 版；非 versioned 也存（防手滑）—— 落地：微秒时间戳且 `:`→`-`（**Windows 文件名不允许冒号**，只按 Linux 想会在开发机直接炸）；份数走 `history_keep`（默认 3，`0`=关，可按库配 `_config/settings.json`）；目录名取自 frontmatter `id`，那是**用户可手改**字段 → 消毒 + 越界回退到文件名，防"存快照"变"往库外写文件"；快照失败只 warning 不冒泡（冒泡=agent 以为写失败而重试→重复内容）；⚠️ **快照不进 git**：往 `.history/` 里塞一份 `*` 的 `.gitignore`（不改库根那份——库根只在 `init_repo` 写一次，**老库拿不到**，而删除的审计提交是 `add -A` 会顺手把快照提走）
- [x] S-8.2 `_versioned.git`：仅 versioned=true 的记忆提交进共享版本库 → 验收：未标 versioned 的写入零 git 操作 —— 落地：`<home>/_versioned` 是**跨库一个**仓库（两库共用已被断言：仓库数=1），路径 `files/<lib-id>/<file>`；普通写入零 git 操作由 `_mirror_version()` 把关（没标 versioned 直接 return None）。两条如实记录：① 把 `versioned` 关掉时**版本库留最后一份**不删（删除要多一次 git 操作，而历史反正还在）；② **独立模式（非多租户）没有 `_versioned`**，只有 `.history` 快照 —— 版本库要 home 级目录才放得下
- [x] S-8.3 `memory_history(title)`：返回 versioned 记忆的 diff → 验收：非 versioned 返回"未启用版本控制" —— 落地：`memory_history(title=…)` 转 `memory_versions()`；⚠️ 读闸**复用 `hub.get()`** 而不是重拼 `_find_lib_of`+可见性判断 —— 两条路径的 NOT_FOUND 措辞不同，措辞不同就是"这个标题存在吗"的 oracle（S-10.5 同一个坑）。非 versioned 除了提示语，还回 `previous_body`（`.history` 最新一版正文），因为它才是"刚改坏了想退回去"的实际入口
- [x] S-8.4 `memory_backup`：tar 打包 `spaces/`+`state/`；恢复明示覆盖 + 恢复前自动快照 → 验收：备份→恢复 round-trip；快照自动生成 —— 落地：打包对象按磁盘实际结构改成 `libs/`+`_versioned/`+`tenants.json`；⚠️ `state/`（令牌/主密钥/PIN）**默认排除**，`with_state=True` 才带并明确警告"按机密对待"，包 chmod 600，写完回读校验成员数（防写出一个解不开的包当成功）；恢复限 **admin** + `confirm=True`（不 confirm 只给预览），恢复前**自动**对当前状态打一份快照并在返回里给路径；绝对路径/`..` **整包拒绝**（不是跳过坏条目）；解包先落 `.restore-staging/` 再**整目录替换**——git 对象是 444 只读，Windows 不 chmod 就 unlink 不掉，逐个覆盖还会留下"半新半旧"。⚠️ **恢复后必须重启**：`tenants.json` 启动载入内存，否则"文件已恢复但身份还是旧的"
- [x] S-8.5 `test_commit.py` 按新模型重写 + 备份 round-trip 新测 → 验收：全绿 —— `test_commit.py` 重写 **15 绿**、新增 `test_s8_versioning.py` **25 绿**（门禁 `pytest tests/test_commit.py -q` 全绿）；`committed` 语义改为"进了版本库"，**`commit_pending` 字段删除**；破坏性动作走 `_flush_commit(force=True)`（`_dirty` 在新模型下不再是"有东西可提"的判据，不 force 会漏提）。全量 `pytest tests -q` **536 passed**（S-8 前基线 511）

**门禁**：`pytest tests/test_commit.py -q` 全绿

---

### S-9 数据迁移（★ 全程碰数据，每步等确认）

**落点**：新建 `scripts/migrate_v2.py`（参照 `scripts/migrate_cleanup.py`）｜ 复杂度：大 ｜ 依赖：S-1~S-8 全部完成

- [x] M-1 全量备份：tar 数据目录 + tenants.json + .tokens → 验收：包可解包校验；**清单（文件数+字节）先给用户看** —— 已执行：`~/shm-v2-full-backup-20261016-140323.tar.gz`，772,587 字节，包内 1292 / 应有 1292 **✓ 一致**
- [x] M-2 类型推断**预览**（不执行写）：存量 72 条逐条出"推断六型+scope"对照表 → **等用户确认**后才进 M-3 —— **预览已出**（`scripts/migrate_v2.py preview --with-body`）：91 条中 **24 条会变**、65 条无信号词（保持原样）。**M-3 及以后仍等确认**
- [ ] M-3 双写并行（约一周）：旧路径照常写 + 新字段镜像；每日比对 → 验收：日报 0 差异
- [ ] M-4 切换：新存储为主、旧路径只读；git 历史整体留档 `tenant-home-git-archive-20261015.tar.gz` → **切换窗口+回滚条件写明等确认**
- [ ] M-5 测试库归档：ws-e2e自检/ws-D盘test/ws-MEMSYNC-TEST-1014 归档；user-admin 144 压测残留进归档区（不删）→ **逐个列文件清单等确认**；用移动不用 rm
- [ ] M-6 终验：id 集合比对一条不少；三端抽验 20 条全文一致 → 验收：差集为空

---

### S-10 共享粒度：空间默认档 + 条目 share + 会话可见性

**落点**：`vault.py`、`config.py`、`service.py`、`library.py`、`session.py`、`tools_tenant.py`、`tenancy.py`、`http_server.py`、`dashboard.py`、`static/panel.js|css`、`scripts/migrate_v2.py`、`tests/test_s10_share.py` ｜ 复杂度：中

- [x] S-10.1 空间 `default_share`：`_config/space.json`（mine/space/shared），新建默认 space → 验收：字段读写；存量空间按"单人→mine、协作→space"出预览
      ｜ 落 `config.py`：`DEFAULT_SPACE` + `Config.space()/default_share()/accept_shared_from()/validate_space()`，读侧**每次读盘**（改完即时生效、不用重启）、坏值退回 `space` 不抛；预览落在 `scripts/migrate_v2.py space-preview`（**只读**：走 `Config(libs.path_of(lib))`，不碰 `service_for()`，否则会在租户目录里 mkdir + git init）
- [x] S-10.2 条目 `share` 落 meta；`memory_remember` 支持显式传，缺省继承空间 → 验收：写入带 share；recall 只返回有权档位（mine 仅作者、space 同空间、shared 已授权空间）
      ｜ 落 `vault.py`（`effective_share`/`share_visible` 两个纯函数是唯一口径源，检索过滤在打分与计数**之前**）、`service.py`（search/get/list_notes/view 全部经 `visible_notes(viewer)`）、`library.py`（`shared_incoming` 授权集合只从**我可读的库**算）、`tenancy.py`、`tools_tenant.py`（`memory_write(share=)`，`mine` 用鉴权身份填 author）、`http_server.py`（`memory_remember` + 旧别名同步）。
      ⚠️ 两条**与直觉相反**的口径，都有测试钉住：① 无作者的存量条目在 `mine` 下 **fail-open**（否则改一次默认档就把整库老数据对所有人隐藏）；② `share=mine` + 别人的 `author` 直接 `INVALID_ARGUMENT`（否则写出一条写者自己也读不到的条目 —— 成功的黑洞）
- [x] S-10.3 会话 visibility 落 SessionRegistry（S-2.1 已留字段）：切 space/shared 历史过程态按新档位开放；切 private 立即收回 → 验收：三档切换用例
      ｜ 落 `session.py`（`open_tasks` 只收 active、`space` 档**没登记空间就不开放**；新增 `set_visibility()`）+ `tools_tenant.py`（默认生效域并入别人开放的任务域；读越权→剔除后返回空，写越权→`SCOPE_DENIED`，即 F-2 预留的那颗钉子）。
      🐞 顺手修掉一个潜在面板缺陷：`/api/session/visibility` 原来复用 `session.start()` 改档位，副作用是**把已收工的会话偷偷重开**、给不存在的会话凭空新建一条
- [x] S-10.4 面板：共享徽标 + 会话卡三档（S-7.2/S-7.5 已含 UI，本片接真数据）→ 验收：徽标与字段一致
      ｜ `to_dict(share_default=…)` 输出 `share_effective`（**继承只在后端解析一次**，前端不再抄一遍规则），`panel.js` 徽标 + `panel.css` 三档样式吃这个字段；会话卡三档 S-7.2 已有 UI，本片让它打到真 `set_visibility` 上。
      ⚠️ **本片没做 `default_share` 的面板编辑器** —— 读写走 `/api/config`（`space` 字段，带校验）或直接改 `_config/space.json`。理由：默认档一改就作用于整库未标注条目，属于治理动作，不该做成侧栏一键
- [x] S-10.5 单测：条目×会话可见性矩阵 + 越权读返回空不报错（不泄露存在性）→ 验收：矩阵全用例
      ｜ 新建 `tests/test_s10_share.py`（**64 例**，含 13 行档位×身份×跨空间矩阵）。写测试时抓出并修掉两个真洞：
      ① `memory_view` 是第三个读入口，原来直接拿 `all_notes()` 算视图与统计 → 别人的 `mine` 条目连着计数一起端出去；已在 `service.view(kind, viewer=)` 收口；
      ② 越权读虽然返回 `NOT_FOUND`，但文案与"库里真没有这条"**不一样**，等于把错误体变成存在性探针 → 抽出 `tenancy._not_found()` 与 vault 同源构造

**门禁**：`pytest tests/test_tenancy.py tests/test_memory.py -q` 全绿 → ✅ 171 passed；全量 `pytest tests/ -q` → ✅ **511 passed**（基线 447 + 本片 64，零回退）

---

### S-11 治理四件套

**落点**：新建 `governance.py`、`knowledge.py`、`dashboard.py` ｜ 复杂度：中

- [x] S-11.1 重复簇检测：标题+标签相似度聚簇（阈值可配），输出"建议走取代链" → 验收：存量样本簇与人工抽查一致率 ≥80%（误报可漏报不可）；**不自动执行**
- [x] S-11.2 容量仪表：空间条数/近 30 天增长率/重复率/低引用候选（口径写注释）→ 验收：指标可复现
- [x] S-11.3 过期降权：`knowledge.expiry_state` 接入 recall——valid_until 到期默认不返回，显式 all 可查 → 验收：到期条显式可查（考古不丢）
- [x] S-11.4 引用计数落 `state/`（recall 命中/关联次数），低引用进候选 → 验收：重启不丢
- [x] S-11.5 面板体检 API + 抽屉接真数据 → 验收：gov 端点返回四件套；红线=无静默删除路径（code review 确认）

**门禁**：治理单测 + `pytest tests/ -q` 全绿

---

### S-12 自动存取：digest 三段注入 + 类型推断扩展 + 客户端 hook 规范

**落点**：`digest.py`、`config.py`、新建 `docs/client-hooks.md` ｜ 复杂度：中

- [x] S-12.1 digest 三段注入：initialize 下发「长期摘要 + 约束清单（常驻）+ 当前任务上下文包」→ 验收：任意 MCP 客户端握手后 instructions 含三段；constraint 每次必达
- [x] S-12.2 `infer_meta` 扩展：六型 + scope 推断（task 过程态落当前会话绑定域）；workflow/constraint 信号词优先（必须/一律/禁止/流程/步骤）→ 验收：存量样本推断准确率 ≥80%；**推断只作建议值，agent 显式传可覆盖**（Q9-a）
- [x] S-12.3 `docs/client-hooks.md`：事件、负载、重试约定 + WorkBuddy hook 样例（Q7-a 先做本家）→ 验收：文档含可复制样例
- [x] S-12.4 诚实边界入文档：MCP client-pull，写无法服务端强制 → 验收：设计文档+设置页均有（不承诺推送）
- [x] S-12.5 两段式读取 token 对比记录（recall 摘要 vs 全文直进）→ 验收：记录显示 <50%
      ｜ 落地：`scripts/measure_two_stage.py` 跑一次落一份 `docs/two-stage-read-cost.md`（**41.0% < 50% PASS**）；两条测试钉死——`test_two_stage_read_cost_under_half` 直接调脚本 `measure()`（口径漂移即红）、`test_two_stage_report_is_persisted` 断言报告在盘且结论一致（派生视图≠手写）。S-12.1~12.4 于 `04f7c58` 落地（digest 三段常驻 / infer_meta 六型+scope / client-hooks.md 232 行 / 诚实边界进 i18n 设置页），勾选滞后于代码，本轮一并补上。

**门禁**：digest 测试 + 文档 review

---

## 六、测试与门禁策略

| 层 | 手段 | 命令/位置 |
|---|---|---|
| 单测 | 临时目录建真 vault（不 mock 服务层） | `pytest tests/test_memory.py tests/test_state_contract.py -q` |
| 集成 | 真文件系统 round-trip（写→读→导出→导入） | `pytest tests/test_optimization.py -q` |
| 契约 | 面板 id 白名单 / i18n key / 工具 schema 快照 | `pytest tests/test_panel_contract.py tests/test_i18n.py tests/test_tools_schema_contract.py -q` |
| 全量门禁 | 每片出口必跑 | `pytest tests/ -q`（基线 319，只增不减） |
| 容器验收 | 双轨 md5 对齐后，容器内全量 + 真机面板过一遍 | 见总原则 1/2 |
| 冒烟 | 服务起来 curl 关键端点 | `/health`、tools/list、PIN 登录流 |

**新测试命名规范**：`test_<切片号>_<场景>`（如 `test_s2_session_end_archives_task_scope`），失败时名字即定位。

---

## 七、里程碑与依赖

```
S-1（存储层）
 └→ S-2（会话+收窄）──→ S-3（契约+digest）──→ S-4（工具面）
      └→ S-10（共享粒度）  S-11（治理）  S-12（自动存取）
S-1 ─→ S-6（安全）──→ S-7（面板，依赖 S-1+S-6+S-10 UI）
S-1 ─→ S-5（接入，依赖 S-4）  S-8（版本+备份）
全部就绪 ──→ S-9（迁移，全程等确认）
```

建议推进顺序：S-1 → S-2 → S-3 → S-4 → S-5/S-6/S-8（可并行）→ S-7 → S-10/S-11/S-12 → S-9。
每片完成即打勾即沉淀当日日志；不带着上片的失败进下片。

---

## 八、风险登记册

| # | 风险 | 等级 | 应对 |
|---|---|---|---|
| RK-1 | search 缺省收紧（S-1.6）打破依赖旧行为的存量 agent | 中 | 收紧前跑一遍存量 agent 常用检索词回归；别名期宽松、正式版收紧 |
| RK-2 | S-6 数据迁转移错 token/主密钥 | 高 | M-4 式清单+备份+双写期；回滚=还原备份 |
| RK-3 | dashboard.py 改动忘重启，测试绿但线上旧 | 中 | 门禁含"容器重启后真机验证"步骤 |
| RK-4 | 双轨 md5 漂移（只改一边） | 中 | 每片出口 `md5sum` 两边对；不一致先对齐再测 |
| RK-5 | 重复簇误报导致错误合并建议 | 中 | 阈值可配+只建议不自动；一致率 ≥80% 才上线 |
| RK-6 | 治理/迁移误删数据 | 高 | 红线：无静默删除路径；删除类一律清单+确认+备份先行 |
| RK-7 | 跨品牌客户端配置格式差异 | 低 | connect 按客户端类型生成；文档列明支持矩阵 |
| RK-8 | 319 基线里 flaky 用例（已知 test_service_concurrent_writes_all_land） | 低 | 勿瞎修；重跑确认 |

---

## 九、不做清单（防范围蔓延）

- 不做点对点指定人共享（Q1-a 拍板：共享空间承载）
- 不做治理自动合并/自动删除（Q5-a 拍板：只建议）
- 不做账号/密码/票据体系复活（D-1）
- 不做数据库迁移（A-5）
- 不做微服务拆分/前后端分离（A-1/A-3）
- 不做 WebSocket 实时（A-6）
- 不做除 WorkBuddy 外的客户端自动写 hook（Q7-a：先本家，其他家出模板）
- 不在 S-9 之前动任何存量数据文件

---

## 附：S-7.8 与 v4 原型的逐屏差异清单

> 原型即规格（`02_交付件/共享记忆/共享记忆-面板重构原型-v4.html`）。逐屏比对后，**每条差异都要写明判定**：
> ✅ 已按原型实现 ／ ⚠️ 合理偏离（给理由）／ ❌ 不合理（本片补）。
> 判据只有一条：**用户在这一屏能问出的问题，界面答不答得出**。id 名字不算差异（原型是演示件，
> 生产的 id 契约由 `test_panel_contract.py` 白名单钉住）。

| 屏 | 原型 | 生产 | 判定 | 说明 |
|---|---|---|---|---|
| 登录 | PIN 六盒 + 锁定倒计时 + 降级入口 + 会话过期横幅 | `pinwrap`/`pin-wait`/`login-pw-link`/`sess-banner` | ✅ | S-7.1 已做，契约测试在位 |
| 记忆页 左栏 | 库下拉、分组分段、scope chip、自动上下文条、筛选条 + 全部清除、统计仪表 | `lib-select`/`groupby`/`scopechips`/`ctxbar`/`fclear`/`s-*` | ✅ | 名字不同、内容等价（分组多一档「类型」是 S-7.2 加的） |
| 阅读器 相关条目 | 「相关条目」列表 | 共享标签相关记忆 + 谁引用了它/它引用了谁（backlinks） | ✅ | 生产比原型多给了反向引用 |
| **阅读器 版本历史** | 按条列 sha/时间/说明；未 versioned 明说没有 | **本片补**：`/api/versions` + `loadVersions()` | ❌→✅ | 生产原来铺的是**整库** git log，还自带一句"这不是这一条的改动"——那句话本身就没回答用户点开这条时的问题；S-8 之后普通写入不进 git，整库日志更是绝大多数时候空着。现改为按条取：git 序列（有则列）+ `.history/` 改前快照（人人有） |
| 阅读器 版本历史 | — | 不放"恢复到此版"按钮 | ⚠️ | 退回某版是**写动作**，走 agent 的 `memory_restore`；面板是阅览室。区块末尾把这句话明写在界面上 |
| 管理 备份 | 立即备份 + 备份表 + 复制路径 / **删除备份** + 恢复 | 导出 + 列表（名/时间/条数/KB）+ 恢复到… | ⚠️ | **不在面板删备份包**：面板里点掉的就是唯一那份后备，误删成本远大于省一次 SSH。导出返回里已给路径 |
| 管理 接入 | 主密钥哈希 + 接入码 + connect 命令 + **四行客户端品牌表** | `mk-*`（状态/生成/轮换/明文一次）+ `conn-*` + `access-snippet`（可复制 mcp.json） | ⚠️ | 品牌表是静态说明文案，复制进面板＝第二处维护（README「接入客户端：一条命令」+ `docs/USAGE.md` 第 9 节已按客户端逐个写，两处必然漂移）。面板只给**能真跑的那一行** |
| 管理 空间 | 空间表：条数 / 类型分布 / 状态 | 空间表：档位 / 可读 / 可写 / 归属 | ⚠️ | 条数在左栏树里（跟着当前筛选走，两处显示必然打架）；空间表管的是**权限归属**，那一屏该答的是"谁能读谁能写" |
| 管理 空间 | — | 已有空间的 `default_share` 不能在这里改 | ⚠️ | S-10.4 已记录的决策：默认档一改就作用于整库未标注条目，属治理动作，不做成侧栏一键（读写走 `/api/config` 或直接改 `_config/space.json`） |
| 管理 指令 | 单个 textarea + 保存 | `cfg-instructions` + 保存（过内部写通道） | ✅ | S-7.3 |
| 设置 | **只剩外观**（主题下拉 + 语言下拉） | 顶栏 `theme-btn` 三档循环 + `lang-select`；设置页放的是**真实配置项**（防抖/上限/策略/类型推断…） | ⚠️ | 主题放顶栏离作用对象更近、少一次展开；设置页空着只放两个下拉反而让真配置无处放。契约测试 `test_theme_is_single_source_and_set_before_first_paint` 钉住"CSS 只认 data-theme + 首帧前定色" |
| 设置 | 自动存取（`auto-cs`/`auto-lt`/`auto-wf` + 上下文条预览） | **没有** | ⚠️ | 属 **S-12** 范围，本片不做（S-12 落点里含设置页这一段） |
| 设置 | **没有**用户/令牌管理（v4 走单主密钥，这一屏只有外观+自动存取+关于） | 仍有「用户管理 / 令牌管理 / 改密码」 | ⚠️ | S-6 的单主密钥是**默认形态**，不是唯一形态：多账号（每人一把凭据、只读库按人分）仍然靠这一屏。砍掉等于砍功能，不在前端片里做。**待办**：真要收敛，把它并入「管理 → 接入」，别两处都有 |
| 设置 | — | 🐞 **本片查出并修掉的真 bug**：`loadSettings()` 里残留一句 `wireSecurityBlock()` | ❌→✅ | 安全块 S-7.3 已搬到「管理 → 接入」，设置页没有 `mk-gen` 这个元素 → 那句调用抛 TypeError，**把紧随其后的 `cfg-save` 接线一起打断**：设置页照常渲染、「保存配置」点了没反应。删掉残留调用，并在契约测试钉住"接线调用只能一处、且不在设置页"（这正是静态检查看不见、只有真点才发现的那类失效） |
| 设置 | 关于（版本号 build 标记） | 顶栏 `· build <PANEL_VER>` | ✅ | 位置不同，信息在 |
| 治理抽屉 | 容量仪表 + 重复簇 + 过期低引用 | `/api/governance` 真数据六格 + 重复簇 + 过期样本 | ✅ | S-7.4，接的是真数据不是演示 |
| 全局 | 自绘 `modal` / `toast` | 原生 `alert` / `confirm` / `prompt` | ⚠️ | 不为两个确认框引入第二套 UI 框架；原生对话框的可访问性与焦点由浏览器保证。**已知代价**：样式不吃主题、`prompt` 输入无校验。若后续要收敛，统一换成一个轻量的 dialog 组件，别一处一套 |
| 全局 | `demo-bar` / `demo-toggle`（演示数据开关） | **没有** | ✅ | 生产不能有演示数据开关——真实数据与演示数据同屏时，用户无法判断"这个 0 是真没有还是没接上" |

**本片因此补的代码**（都在 S-7.8 名下，不是计划外改动）：
`dashboard.py` 新增 `GET /api/versions`（纯读，转 `hub.versions()`，错误体走 `_HTTP_BY_CODE`）；
`/api/state` **不再** fork git log 取整库 commits（`state_contract` 两端契约同批改写）；
`panel.js` 新增 `loadVersions()`（含串条护栏 `STATE.cur !== ident`），删掉 `groupCommits()` 与 `STATE.commits`；
`panel.css` 加 `.versub`/`.vershint`/`.c .n`；`i18n.js` 换 `doc.history`/`doc.noHistory` 为 8 个按条版本的键（中英齐）；
`panel.js` 删掉 `loadSettings()` 里那句残留的 `wireSecurityBlock()`（设置页「保存配置」失灵的根因）。

**截图回验**：留到收口（`dashboard.py` 改动要容器重启才看得见），届时逐屏截图与本表比对。

---

## 十、每片出口自检（复制到当日日志）

```
- [ ] 本片 commit 已打（S-x: ...）
- [ ] pytest tests/ -q 全绿（passed 数 ≥ 上片 + 新增）
- [ ] 契约测试三件同步（如涉及面板/工具/i18n）
- [ ] 本机 + 容器 md5 对齐；容器内全量跑过
- [ ] dashboard 改动已重启验证（如涉及）
- [ ] 当日日志已更新（改了哪些文件、新增哪些测试、数字前后对比）
- [ ] 无计划外改动（git diff 只含本片文件）
```

---

*本计划为工程实施参考；涉及数据迁移的步骤以用户确认为前置。执行中每完成一条打勾一条。*
