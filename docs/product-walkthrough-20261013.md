# 产品走查记录（2026-10-13，新用户视角）

> 方法：按 README 快速开始在本机真实走一遍 —— 装包、启动、首次管理员、
> 面板登录、MCP 客户端全旅程（whoami→write→search→get→view→ws→stats→export）、
> 错误路径、监控探针。每一步只看"用户能看到什么"。

## 环境说明（重要，别误判）

本机（WorkBuddy 沙箱）环境变量里有 `HTTP_PROXY=http://127.0.0.1:50778`
（沙箱自己的出口代理）。httpx 客户端默认 `trust_env=True` 会走代理，
代理用**绝对形式**请求目标转发，我们的服务 404。
→ 沙箱内测 MCP 客户端要先 `env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy`。
→ 生产/vivo 部署无此变量，fastmcp `Client(URL)` 形态完全正常（已验证）。
→ 但这暴露了真实缺口：RFC 7230 要求服务端必须接受绝对形式请求目标。见 P1-3。

## 发现的问题

### P0-1：17 个工具全部带 outputSchema → 严格模型拒用整个服务

fastmcp 4.1.0 自动为每个返回 `dict` 的工具生成 `outputSchema`。
严格模型（GLM-5.3-Flash / Deepseek-V4.1-Flash / Step-5-Preview）逐字校验
工具 schema，见到 `outputSchema` 直接拒**整个请求**（11129
"The tool definition does not meet the model requirements"），不是跳过这一个工具。

后果：整个服务对一整类模型不可用 —— 而且是"连上之后第一个请求就失败"，
用户完全不知道原因（客户端只显示通用错误码）。

实测：`scripts/probe_tools_list.py` → 17/17 个工具带 outputSchema。
修法已验证：`@mcp.tool(output_schema=None)` 移除，dict 返回值不受影响。

### P1-2：/health 在未启用鉴权时仍 401 → 监控探针不可用

绑 127.0.0.1 时 `auth.required=False`，但 `_wrap_auth` 的 guarded() **从未
引用它** —— 日志说"未强制鉴权"，实际每个数据路径（含 /health）都强制。
systemd/容器探针拿不到 token 就没法探活。

修法（外科手术式）：`auth.required=False` 时 /health 免鉴权。
health 只返回聚合状态（ok/problems/counts/queue），不含记忆内容，
loopback 绑定下放开不扩大泄露面。

### P1-3：绝对形式请求目标 404（RFC 7230 不合规）

`POST http://host:port/mcp HTTP/1.1` 这种合法请求目标被路由层当成路径
字符串匹配，404。代理/网关转发、部分客户端构造会出现。
官方 SDK 客户端一切正常（本旅程验证过全生命周期），但这是白捡的健壮性。

修法：root_app 里把绝对形式 target 归一化为路径。

### P2-4：scripts/e2e.py 钉死「工具齐全（12 个）」

实际已 17 个工具 → e2e 永远误报失败（生产验证脚本失去作用）。
另：e2e.py 里 `sys.path.insert(0, "/opt/shared-memory-mcp/src")` 硬编码，
不可移植。

### P2-5：README 工具表缺参数名

agent 首次调用连错两次（`content`→应为 `body`，`title`→应为 `ident`）。
工具 docstring 写得很全，但 README 的表格只有用途没有参数。
agent 读 README 不够，读 tools/list 才够 —— 但减少试错就是产品价值。

### P2-6：README 的 health 示例已过期

文档写 `curl /health?token=$TOKEN`，但 `?token=` 早已刻意不支持
（URL 泄密），只接受 `Authorization: Bearer`。照文档做会 401。

### P2-7：两个「workspace」概念重叠（设计层观察，本次只文档化）

- note 级：`memory_write(workspace=...)` 是 frontmatter 里的项目标签，
  `memory_export(workspace=...)` 按它过滤
- lib 级：`memory_workspace_create` 建的是 `ws-<名>` 库

走查时把库 id 传给 export 的 workspace，得到"3 个库 0 条记忆"的静默空结果。
→ 导出 0 条时给出提示"workspace 是项目标签不是库名；按库导出传 lib="。

## 体验好的部分（保持）

- 首次启动：管理员密码一次性显示 + 自动发 token，流程顺
- 错误路径提示语质量高（"用 memory_libs 看有哪些库"、"换个标题，或用 mode=upsert 更新它"）
- write 返回自动 claim 推断说明、关联记忆提示、committed=false 的解释
- 面板登录 API 一步换 ticket
- 官方 MCP SDK 客户端全生命周期（initialize→202 通知→SSE→DELETE）干净
