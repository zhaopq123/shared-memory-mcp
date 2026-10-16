/**
 * 国际化（i18n）—— 面板 UI 文案的多语言支持。
 *
 * ━━━ 设计原则 ━━━
 *
 * **默认中文**，因为：
 *   · 这是自用/团队内部工具，使用者是中文母语
 *   · 浏览器语言检测只是"锦上添花"，不该覆盖默认
 *   · 手动切换后**记住选择**（localStorage），尊重用户决定
 *
 * ━━━ 为什么用扁平 key 而不是嵌套对象 ━━━
 *
 * 扁平 key（`'nav.library': '记忆库'`）的好处：
 *   · 查找快、不用递归
 *   · **缺失时容易发现**（对比两种语言的 key 集合即可）
 *   · 翻译文件可以按 key 排序，diff 友好
 *
 * ━━━ 什么不翻译（重要）━━━
 *
 *   · **MCP 工具描述** —— 那是给 agent（LLM）看的，翻译会降低准确率
 *   · **记忆内容** —— 那是用户数据
 *   · **错误码**（FORBIDDEN 等）—— 已经语言无关，前端按 code 查文案
 *   · **工作空间/库名** —— 用户起的名，不是 UI 文案
 */

/* ══════════════════════ 语言包 ══════════════════════ */

/** 中文（默认）。key 用点号分层，但结构是扁平的。 */
const ZH = {
  // 顶栏 / 导航
  'app.title': '共享记忆 · 档案库',
  'app.seal': '记',
  'app.brand': '共享记忆',
  'nav.library': '记忆',
  'nav.admin': '管理',
  'nav.settings': '设置',
  'hint.search': '搜索',
  'hint.nav': '切换',
  'hint.pages': '页',
  'hint.clear': '清空',

  // 统计
  'stat.notes': '条记忆',
  'stat.libs': '个库',
  'stat.workspaces': '个工作区',
  'stat.tags': '个标签',
  'stat.queue': '队列积压',
  'stat.readFail': '读取失败',
  'role.admin': '管理员',
  'role.member': '成员',

  // 左栏
  'search.placeholder': '搜索（跨你可读的全部库）…',
  'lib.all': '全部',
  'lib.label': '库',
  'group.label': '分组',
  'group.workspace': '工作区',
  'group.time': '时间',
  'group.ungrouped': '未分类',
  'list.empty': '从左侧选一条记忆开始阅读',
  'list.noMatch': '没有匹配的记忆',
  'list.emptyLib': '这个库还是空的',
  'list.loading': '载入中…',

  // 条目徽章
  'badge.expired': '已过期',
  'badge.review': '待复查',
  'badge.superseded': '已取代',
  'badge.inference': '推断',
  'badge.proposal': '建议',
  'badge.fact': '事实',
  'badge.readonly': '只读',
  'badge.workspace': '工作空间',
  'badge.readable': '可读',
  'badge.unreadable': '不可读',
  'badge.writable': '可写',
  'badge.notWritable': '只读',
  'badge.active': '有效',
  'badge.revoked': '已吊销',
  'badge.disabled': '已停用',
  'badge.normal': '正常',
  'claim.fact': '✓ 事实',
  'claim.inference': '? 推断',
  'claim.proposal': '◇ 建议',
  'hit.label': '命中',
  'hit.body': '正文',
  'hit.tag': '标签',
  'hit.workspace': '工作区',

  // 阅读器
  'doc.copyLink': '🔗 复制链接',
  'doc.copyRaw': '⧉ 复制原文',
  'doc.copied': '✓ 已复制',
  'doc.toc': '目录',
  'doc.related': '共享标签的相关记忆',
  'doc.backlinks': '谁引用了它',
  'doc.outgoing': '它引用了谁',
  'doc.history': '库提交历史（最近 {n} 条 · 整库，不是这一条）',
  'doc.minutes': '约 {n} 分钟',
  'doc.chars': '{n} 字',
  'doc.noHistory': '版本历史（最近 0 次提交）',
  'doc.supersededBy': '已被「{t}」取代',
  'doc.validUntil': '有效期至 {t}',
  'doc.reviewAfter': '复查：{t}',
  'doc.readFail': '读取失败',

  // 时间
  'time.justNow': '刚刚',
  'time.minutesAgo': '{n} 分钟前',
  'time.hoursAgo': '{n} 小时前',
  'time.daysAgo': '{n} 天前',
  'time.today': '今天',
  'time.yesterday': '昨天',

  // 体系
  'view.overview': '总览',
  'view.decisions': '决策记录',
  'view.lessons': '踩坑清单',
  'view.todos': '待办',
  'view.expiring': '时效告警',
  'view.inferences': '未核实的推断',
  'view.projects': '按项目',
  'view.recent': '最近更新',
  // 视图的一句话说明（后缀 .desc）—— 前端优先用它，后端 r.desc 作兜底
  'view.overview.desc': '库的整体状况：条数、类型分布、时效告警',
  'view.decisions.desc': '定过的口径与取舍（type=decision）',
  'view.lessons.desc': '踩过的坑，避免重复（type=lesson）',
  'view.todos.desc': '未完成的事（type=todo 或 status=todo）',
  'view.expiring.desc': '已过期 / 该复查 / 已被取代',
  'view.inferences.desc': 'claim=inference 的条目 —— 引用前需当心',
  'view.projects.desc': '每个工作区下的记忆',
  'view.recent.desc': '按时间倒序',
  'view.empty': '这个视图是空的。<br>写入相应类型的记忆后，它们会自动出现在这里 —— 视图是按规则实时算的，不需要维护。',
  'view.total': '记忆总数',
  'view.byWorkspace': '按工作区',
  'view.loadFail': '视图加载失败',

  // 设置
  'cfg.title': '服务设置',
  'cfg.subtitle': '配置存在库里（跟着 git 走、可回滚），改完立即生效。',
  'cfg.defaultInstructions': '当前用的是内置默认指令。它包含「不幻觉」与「记忆纪律」两条核心约定，可以直接用。改动后保存即生效（客户端需要重连才会重新拉取）。',
  'cfg.disabledInstructions': '指令已停用（文件内容为 _disabled）。接入的 agent 不会收到任何行为约定。',
  'cfg.instructions': '给 agent 的指令',
  'cfg.instructionsHint': '这段文字会随 MCP 握手发给所有接入的 agent，是服务端唯一能"对每个 agent 说话"的地方。<br>因为影响面最大，改动会进 git 留痕。留空 = 使用内置默认；填 <code>_disabled</code> = 完全停用。',
  'cfg.writeRules': '写入规则',
  'cfg.writeRulesHint': '记忆写入时按这些规则处理。默认策略是自动补全 + 提醒而不硬拒 —— 硬拒会让 agent 反复换写法重试，浪费轮次。',
  'cfg.policy': '缺字段时的策略',
  'cfg.policyHint': 'fill = 自动补默认值（推荐）；warn = 只提醒；reject = 拒绝写入',
  'cfg.maxTitle': '标题长度上限',
  'cfg.maxTitleHint': '防止 agent 把一整段当标题写。0 = 不限。',
  'cfg.requireWs': '是否要求每条记忆都带工作区',
  'cfg.requireWsHint': 'true 时缺工作区会提醒（默认策略下仍会写入）。',
  'cfg.inferType': '自动推断类型的关键词',
  'cfg.inferTypeHint': 'JSON 格式。标题或正文命中关键词时自动推断 type，视图（决策/踩坑/待办）就靠它。留空表示不启用推断规则。',
  'cfg.runParams': '运行参数',
  'cfg.runParamsHint': '这些改完立即生效。',
  'cfg.debounce': '自动提交间隔（秒）',
  'cfg.debounceHint': '写入静默这么久后 git 提交一次。0 = 每次写都提交。',
  'cfg.searchLimit': '检索默认返回条数',
  'cfg.refresh': '面板自动刷新间隔（秒）',
  'cfg.refreshHint': '0 = 不自动刷新。',
  'cfg.save': '保存配置',
  'cfg.saving': '保存中…',
  'cfg.reload': '放弃改动',
  'cfg.saved': '已保存 ✓ 立即生效',
  'cfg.saveFail': '保存失败：',
  'cfg.source': '配置来源',
  'cfg.sourceHint': '配置存在 vault 里，跟着 git 走、可回滚、可随库迁移：',
  'cfg.isDefault': '是',
  'cfg.changed': '否（你改过）',
  'cfg.inferJsonErr': '推断规则 JSON 有误：',

  // 用户管理
  'admin.users': '用户与库',
  'admin.usersHint': '加人 → 给他发一把 token → 他就能接入自己的 agent。<strong>个人库只有本人能读</strong>（管理员也不行 —— 那是刻意的设计）。',
  'admin.tokens': '访问凭据（token）',
  'admin.tokensHint': '每台设备/每个 agent 一把 token，绑定到人。<br>泄露时能<strong>单独吊销那一把</strong>，不必打断其他人；人离职时按用户名吊销，一次清掉他所有设备。',
  'admin.col.user': '用户',
  'admin.col.role': '角色',
  'admin.col.groups': '组',
  'admin.col.status': '状态',
  'admin.col.lib': '库',
  'admin.col.owner': '归属',
  'admin.col.memberWrite': '成员可写',
  'admin.col.fingerprint': '指纹',
  'admin.col.tokenUser': '归属',
  'admin.col.label': '标签',
  'admin.col.scope': '权限',
  'admin.col.lastUsed': '最后使用',
  // 操作列（"吊销"按钮那一列）。表头刻意留空 —— 但**不能是空串**，
  // 那会被 i18n 一致性测试判为"空文案"，而且空表头在某些
  // 无障碍场景下（屏幕阅读器）会读出奇怪的东西。
  // 用一个不可见的占位符（零宽空格）既不显示内容，又语义明确。
  'admin.col.actions': '\u200b',
  'admin.libs': '库（{n}）',
  'admin.addUser': '加用户',
  'admin.creating': '创建中…',
  'admin.userName': '用户名（英文，如 zhangsan）',
  'admin.displayName': '显示名（可选）',
  'admin.groups': '组（逗号分隔，可选）',
  'admin.role': '角色',
  'admin.nameRequired': '用户名不能空',
  'admin.createFail': '创建失败',
  'admin.userCreated': '已创建 {u}',
  'admin.pwOnce': '初始密码（<strong>只显示这一次</strong>，请立刻复制）：',
  'admin.issueToken': '发 token',
  'admin.issuing': '签发中…',
  'admin.tokenUser': '给哪个用户（用户名）',
  'admin.tokenLabel': '标签（如：办公室笔记本）',
  'admin.scopeRW': '读写',
  'admin.scopeRO': '只读（给新人看）',
  'admin.needUser': '要指定用户',
  'admin.tokenIssued': '已为 {u} 签发（{s}）',
  'admin.tokenOnce': '这把 token（<strong>只显示这一次</strong>，请立刻复制）：',
  'admin.revoke': '吊销',
  'admin.revokeConfirm': '吊销 {t} ？用这把 token 的客户端会立刻断开。',
  'admin.revokeFail': '吊销失败',
  'admin.readFail': '读取失败',
  'admin.onlyAdmin': '只有管理员能管理用户与凭据。',

  // 工作空间
  'ws.title': '工作空间与共享',
  'ws.hint': '一个工作空间一个库。共享范围就是库的归属 —— <strong>没权限的人物理上打不开那个库</strong>（不是"搜完再过滤"）。改共享范围即时生效，不用搬数据。',
  'ws.mine': '你能管理的（{n}）—— 改下拉即生效',
  'ws.others': '其他人的 / 系统的（{n}）—— 你只能看',
  'ws.col.space': '工作空间',
  'ws.col.share': '共享给',
  'ws.col.read': '你能读',
  'ws.col.write': '你能写',
  'ws.ownerYou': '你是所有者',
  'ws.new': '新建工作空间',
  'ws.namePlaceholder': '工作空间名（如：乱七八糟、客户A）',
  'ws.create': '建库',
  'ws.creating': '建库中…',
  'ws.nameRequired': '名字不能空',
  'ws.shareConfirm': '把「{lib}」的共享范围改成「{to}」？\n\n这会立刻改变谁能看到里面的内容。',
  'ws.shareFail': '改失败',
  'ws.shareChanged': '已改：{from} → {to}',
  'ws.created': '已建「{l}」',
  'ws.writeHint': '写入时传 <code>lib="{lib}"</code> 即落进这个工作空间。',
  'ws.footHint': '默认<strong>仅自己</strong>（最安全）。共享可以随时改，也可以随时收回 —— 收回后别人立刻读不到。',

  // 共享范围
  'share.self': '仅自己',
  'share.selfDesc': '不共享。只有你能读，管理员也读不到。',
  'share.group': '小组：{g}',
  'share.groupDesc': '{g} 组的成员都能读。',
  'share.company': '公司',
  'share.companyDesc': '全员可读。注意：公司库默认只读，成员不能写。',

  // 登录（账号密码 —— 给人用的正经登录）
  'login.title': '需要登录',
  'login.hint': '用你的账号密码登录。',
  'login.username': '用户名',
  'login.usernamePh': '输入用户名',
  'login.password': '密码',
  'login.passwordPh': '输入密码',
  'login.button': '登录',
  'login.submitting': '登录中…',
  'login.invalid': '用户名或密码不正确',

  // 我的账号
  'account.title': '我的账号',
  'account.hint': '改密码后当前会话仍有效，但旧密码立即失效。',
  'account.curPw': '当前密码',
  'account.curPwPh': '输入当前密码',
  'account.newPw': '新密码',
  'account.newPwPh': '至少 8 位',
  'account.newPw2': '确认新密码',
  'account.newPw2Ph': '再输一次',
  'account.change': '修改密码',
  'account.changing': '提交中…',
  'account.changed': '密码已修改',
  'account.mismatch': '两次输入的新密码不一致',
  'account.tooShort': '新密码至少 8 位',
  'account.needCur': '要先填当前密码',
  'account.logout': '退出登录',
  'account.logoutConfirm': '退出登录？之后要重新登录。',
  'account.loggedOut': '已退出登录',

  // 通用
  'common.loading': '载入中…',
  'common.confirm': '确定',
  'common.cancel': '取消',
  'common.yes': '是',
  'common.no': '否',
  'common.close': '关闭',
  'lang.switch': '语言',
};

/** 英文。结构与中文**完全一致** —— 缺 key 会被检测出来。 */
const EN = {
  'app.title': 'Shared Memory · Archive',
  'app.seal': 'M',
  'app.brand': 'Shared Memory',
  'nav.library': 'Memory',
  'nav.admin': 'Admin',
  'nav.settings': 'Settings',
  'hint.search': 'Search',
  'hint.nav': 'Navigate',
  'hint.pages': 'Pages',
  'hint.clear': 'Clear',

  'stat.notes': 'notes',
  'stat.libs': 'libraries',
  'stat.workspaces': 'workspaces',
  'stat.tags': 'tags',
  'stat.queue': 'queued',
  'stat.readFail': 'read errors',
  'role.admin': 'Admin',
  'role.member': 'Member',

  'search.placeholder': 'Search all libraries you can read…',
  'lib.all': 'All',
  'lib.label': 'Libraries',
  'group.label': 'Group by',
  'group.workspace': 'Workspace',
  'group.time': 'Time',
  'group.ungrouped': 'Ungrouped',
  'list.empty': 'Pick a note on the left to start reading',
  'list.noMatch': 'No matching notes',
  'list.emptyLib': 'This library is empty',
  'list.loading': 'Loading…',

  'badge.expired': 'Expired',
  'badge.review': 'Review',
  'badge.superseded': 'Superseded',
  'badge.inference': 'Inference',
  'badge.proposal': 'Proposal',
  'badge.fact': 'Fact',
  'badge.readonly': 'Read-only',
  'badge.workspace': 'Workspace',
  'badge.readable': 'Readable',
  'badge.unreadable': 'No access',
  'badge.writable': 'Writable',
  'badge.notWritable': 'Read-only',
  'badge.active': 'Active',
  'badge.revoked': 'Revoked',
  'badge.disabled': 'Disabled',
  'badge.normal': 'OK',
  'claim.fact': '✓ Fact',
  'claim.inference': '? Inference',
  'claim.proposal': '◇ Proposal',
  'hit.label': 'Match',
  'hit.body': 'body',
  'hit.tag': 'tag',
  'hit.workspace': 'workspace',

  'doc.copyLink': '🔗 Copy link',
  'doc.copyRaw': '⧉ Copy raw',
  'doc.copied': '✓ Copied',
  'doc.toc': 'Contents',
  'doc.related': 'Related by shared tags',
  'doc.backlinks': 'Referenced by',
  'doc.outgoing': 'References',
  'doc.history': 'Library log (last {n} · whole library)',
  'doc.minutes': '~{n} min',
  'doc.chars': '{n} chars',
  'doc.noHistory': 'History (last 0 commits)',
  'doc.supersededBy': 'Superseded by “{t}”',
  'doc.validUntil': 'Valid until {t}',
  'doc.reviewAfter': 'Review after {t}',
  'doc.readFail': 'Failed to load',

  'time.justNow': 'just now',
  'time.minutesAgo': '{n} min ago',
  'time.hoursAgo': '{n} h ago',
  'time.daysAgo': '{n} d ago',
  'time.today': 'Today',
  'time.yesterday': 'Yesterday',

  'view.overview': 'Overview',
  'view.decisions': 'Decisions',
  'view.lessons': 'Lessons',
  'view.todos': 'To-dos',
  'view.expiring': 'Expiring',
  'view.inferences': 'Unverified inferences',
  'view.projects': 'By project',
  'view.recent': 'Recent',
  'view.overview.desc': 'Overall state: note counts, type distribution, expiry alerts',
  'view.decisions.desc': 'Decisions made and trade-offs taken (type=decision)',
  'view.lessons.desc': 'Pitfalls already hit — avoid repeating them (type=lesson)',
  'view.todos.desc': 'Outstanding items (type=todo or status=todo)',
  'view.expiring.desc': 'Expired / needs review / superseded',
  'view.inferences.desc': 'claim=inference entries — treat with care',
  'view.projects.desc': 'Notes grouped by workspace',
  'view.recent.desc': 'Newest first',
  'view.empty': 'This view is empty.<br>Notes of the matching type will appear here automatically — views are computed live, nothing to maintain.',
  'view.total': 'Total notes',
  'view.byWorkspace': 'By workspace',
  'view.loadFail': 'Failed to load view',

  'cfg.title': 'Service settings',
  'cfg.subtitle': 'Config lives in the library (versioned by git, rollback-able) and applies immediately.',
  'cfg.defaultInstructions': 'Using built-in default instructions. They cover the two core rules: "no hallucination" and "memory discipline". Saving applies immediately (clients must reconnect to re-fetch).',
  'cfg.disabledInstructions': 'Instructions are disabled (file contains _disabled). Connected agents receive no behavioural guidance.',
  'cfg.instructions': 'Instructions for agents',
  'cfg.instructionsHint': 'This text is sent to every connected agent during the MCP handshake — the only place the server can "speak to each agent".<br>Because its impact is the widest, changes are recorded in git. Empty = built-in default; <code>_disabled</code> = fully disabled.',
  'cfg.writeRules': 'Write rules',
  'cfg.writeRulesHint': 'Applied when notes are written. The default policy is auto-fill + warn, never hard-reject — hard rejection makes agents retry with different wording and burn turns.',
  'cfg.policy': 'Missing-field policy',
  'cfg.policyHint': 'fill = auto-fill defaults (recommended); warn = warn only; reject = refuse the write',
  'cfg.maxTitle': 'Max title length',
  'cfg.maxTitleHint': 'Prevents agents from writing a whole paragraph as the title. 0 = unlimited.',
  'cfg.requireWs': 'Require a workspace on every note',
  'cfg.requireWsHint': 'When true, a missing workspace triggers a warning (the note is still written under the default policy).',
  'cfg.inferType': 'Type-inference keywords',
  'cfg.inferTypeHint': 'JSON. When a keyword hits the title or body, the type is inferred — this drives the views (decisions / lessons / to-dos). Empty = inference disabled.',
  'cfg.runParams': 'Runtime parameters',
  'cfg.runParamsHint': 'These apply immediately.',
  'cfg.debounce': 'Auto-commit delay (seconds)',
  'cfg.debounceHint': 'Git commit this long after the last write. 0 = commit on every write.',
  'cfg.searchLimit': 'Default search results',
  'cfg.refresh': 'Panel auto-refresh (seconds)',
  'cfg.refreshHint': '0 = no auto-refresh.',
  'cfg.save': 'Save',
  'cfg.saving': 'Saving…',
  'cfg.reload': 'Discard',
  'cfg.saved': 'Saved ✓ applied',
  'cfg.saveFail': 'Save failed: ',
  'cfg.source': 'Config source',
  'cfg.sourceHint': 'Config lives inside the vault — versioned by git, rollback-able, portable with the library:',
  'cfg.isDefault': 'yes',
  'cfg.changed': 'no (you changed it)',
  'cfg.inferJsonErr': 'Invalid inference-rule JSON: ',

  'admin.users': 'Users and libraries',
  'admin.usersHint': 'Add a person → issue them a token → they can connect their own agent. <strong>Personal libraries are readable only by their owner</strong> (not even admins — by design).',
  'admin.tokens': 'Access tokens',
  'admin.tokensHint': 'One token per device/agent, bound to a person.<br>On leak you can <strong>revoke that single token</strong> without disturbing anyone else; on offboarding revoke by username to clear all their devices at once.',
  'admin.col.user': 'User',
  'admin.col.role': 'Role',
  'admin.col.groups': 'Groups',
  'admin.col.status': 'Status',
  'admin.col.lib': 'Library',
  'admin.col.owner': 'Owner',
  'admin.col.memberWrite': 'Members can write',
  'admin.col.fingerprint': 'Fingerprint',
  'admin.col.tokenUser': 'Owner',
  'admin.col.label': 'Label',
  'admin.col.scope': 'Scope',
  'admin.col.lastUsed': 'Last used',
  'admin.col.actions': '\u200b',
  'admin.libs': 'Libraries ({n})',
  'admin.addUser': 'Add user',
  'admin.creating': 'Creating…',
  'admin.userName': 'Username (e.g. zhangsan)',
  'admin.displayName': 'Display name (optional)',
  'admin.groups': 'Groups (comma-separated, optional)',
  'admin.role': 'Role',
  'admin.nameRequired': 'Username required',
  'admin.createFail': 'Creation failed',
  'admin.userCreated': 'Created {u}',
  'admin.pwOnce': 'Initial password (<strong>shown only once</strong> — copy it now):',
  'admin.issueToken': 'Issue token',
  'admin.issuing': 'Issuing…',
  'admin.tokenUser': 'For which user (username)',
  'admin.tokenLabel': 'Label (e.g. office laptop)',
  'admin.scopeRW': 'Read-write',
  'admin.scopeRO': 'Read-only (for newcomers)',
  'admin.needUser': 'A user is required',
  'admin.tokenIssued': 'Issued for {u} ({s})',
  'admin.tokenOnce': 'This token (<strong>shown only once</strong> — copy it now):',
  'admin.revoke': 'Revoke',
  'admin.revokeConfirm': 'Revoke {t}? Clients using this token will disconnect immediately.',
  'admin.revokeFail': 'Revoke failed',
  'admin.readFail': 'Failed to load',
  'admin.onlyAdmin': 'Only administrators can manage users and credentials.',

  'ws.title': 'Workspaces and sharing',
  'ws.hint': 'One workspace, one library. Sharing scope is the library owner — <strong>people without access cannot open the library at all</strong> (it is not "search then filter"). Changes take effect immediately, no data movement.',
  'ws.mine': 'You can manage ({n}) — change the dropdown to apply',
  'ws.others': 'Others\' / system ({n}) — read-only for you',
  'ws.col.space': 'Workspace',
  'ws.col.share': 'Shared with',
  'ws.col.read': 'You can read',
  'ws.col.write': 'You can write',
  'ws.ownerYou': 'You own this',
  'ws.new': 'New workspace',
  'ws.namePlaceholder': 'Workspace name (e.g. lanzaluoduo, clientA)',
  'ws.create': 'Create',
  'ws.creating': 'Creating…',
  'ws.nameRequired': 'Name required',
  'ws.shareConfirm': 'Change sharing of “{lib}” to “{to}”?\n\nThis immediately changes who can see its contents.',
  'ws.shareFail': 'Change failed',
  'ws.shareChanged': 'Changed: {from} → {to}',
  'ws.created': 'Created “{l}”',
  'ws.writeHint': 'Pass <code>lib="{lib}"</code> when writing to land in this workspace.',
  'ws.footHint': 'Defaults to <strong>private</strong> (safest). Sharing can be changed or revoked at any time — revoked access takes effect immediately.',

  'share.self': 'Private',
  'share.selfDesc': 'Not shared. Only you can read it — not even admins.',
  'share.group': 'Group: {g}',
  'share.groupDesc': 'Every member of group {g} can read it.',
  'share.company': 'Company',
  'share.companyDesc': 'Everyone can read. Note: company libraries are read-only for members.',

  'login.title': 'Sign in required',
  'login.hint': 'Sign in with your username and password.',
  'login.username': 'Username',
  'login.usernamePh': 'Enter username',
  'login.password': 'Password',
  'login.passwordPh': 'Enter password',
  'login.button': 'Sign in',
  'login.submitting': 'Signing in…',
  'login.invalid': 'Incorrect username or password',

  'account.title': 'My account',
  'account.hint': 'After changing your password the current session stays valid, but the old password stops working immediately.',
  'account.curPw': 'Current password',
  'account.curPwPh': 'Enter current password',
  'account.newPw': 'New password',
  'account.newPwPh': 'At least 8 characters',
  'account.newPw2': 'Confirm new password',
  'account.newPw2Ph': 'Type it again',
  'account.change': 'Change password',
  'account.changing': 'Submitting…',
  'account.changed': 'Password changed',
  'account.mismatch': 'The two new passwords do not match',
  'account.tooShort': 'New password must be at least 8 characters',
  'account.needCur': 'Enter your current password first',
  'account.logout': 'Sign out',
  'account.logoutConfirm': 'Sign out? You will need to sign in again.',
  'account.loggedOut': 'Signed out',

  'common.loading': 'Loading…',
  'common.confirm': 'OK',
  'common.cancel': 'Cancel',
  'common.yes': 'Yes',
  'common.no': 'No',
  'common.close': 'Close',
  'lang.switch': 'Language',
};

/* ══════════════════════ 运行时 ══════════════════════ */

const PACKS = { zh: ZH, en: EN };

/** 支持的语言（顺序 = 切换器里的顺序）。默认第一个。 */
const LANGS = [
  { code: 'zh', label: '中文', pack: ZH },
  { code: 'en', label: 'English', pack: EN },
];

const DEFAULT_LANG = 'zh';   // ⚠️ 默认中文（用户要求）

/** 当前语言代码。 */
let LANG = DEFAULT_LANG;

/**
 * 决定初始语言。
 *
 * ━━━ 优先级（顺序很重要）━━━
 *
 *   1. 用户在面板里选过的（localStorage）—— 尊重用户决定
 *   2. URL 参数 `?lang=en` —— 方便分享/调试
 *   3. 浏览器语言 —— **只在明确偏好英文时才切**
 *   4. 默认中文
 *
 * 第 3 条的细节：不能"浏览器不是中文就用英文" ——
 * 那样日文/韩文/德文用户都会看到英文，而他们可能更想看中文
 * （这是团队内部工具）。所以只有**明确是英文**才自动切。
 */
function detectLang(){
  try {
    const saved = localStorage.getItem('shm-lang');
    if (saved && PACKS[saved]) return saved;
  } catch {}
  const url = new URLSearchParams(location.search).get('lang');
  if (url && PACKS[url]) return url;
  const nav = (navigator.language || '').toLowerCase();
  if (nav.startsWith('en')) return 'en';
  return DEFAULT_LANG;
}

/**
 * 取一条文案。
 *
 * ━━━ 缺失时的行为 ━━━
 *
 * 回落到中文（默认语言），再回落到 key 本身 ——
 * **绝不返回空串**。空串会让界面上出现"什么都没有"的区域，
 * 而显示 key 至少能让人看出"这里缺翻译"。
 */
function t(key, params){
  let s = (PACKS[LANG] && PACKS[LANG][key]) ?? ZH[key];
  if (s === undefined) {
    // 缺翻译：显示 key，便于发现（而不是静默显示空）
    console.warn('[i18n] 缺文案:', key);
    return key;
  }
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      s = s.split('{' + k + '}').join(String(v));
    }
  }
  return s;
}

/** 切换语言并重渲染。 */
function setLang(code){
  if (!PACKS[code]) return;
  LANG = code;
  try { localStorage.setItem('shm-lang', code); } catch {}
  // 全量重渲染（简单可靠 —— 面板不大，不值得做增量更新）
  applyStaticText();
  if (typeof STATE !== 'undefined') {
    renderChips?.(); renderList?.();
    if (STATE.tab === 'knowledge') loadKnowledge?.();
    if (STATE.tab === 'settings') loadSettings?.();
  }
}

/**
 * 把 HTML 里的静态文案替换掉。
 *
 * ━━━ 为什么用 data-i18n 属性而不是 innerHTML 重建 ━━━
 *
 * 重建会把事件监听器全弄丢（每次切换语言都要重新绑定一遍，
 * 容易漏）。用属性标记只改 textContent，监听器不受影响。
 */
function applyStaticText(){
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const key = el.dataset.i18n;
    const s = t(key);
    // 允许含 HTML 的文案（部分提示语有 <strong>/<code>）
    if (/<[a-z][\s\S]*>/i.test(s)) el.innerHTML = s;
    else el.textContent = s;
  });
  document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
    el.placeholder = t(el.dataset.i18nPlaceholder);
  });
  document.querySelectorAll('[data-i18n-title]').forEach(el => {
    el.title = t(el.dataset.i18nTitle);
  });
  // 语言切换器自身
  const sel = document.getElementById('lang-select');
  if (sel && sel.value !== LANG) sel.value = LANG;
}
