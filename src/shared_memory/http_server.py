"""HTTP 入口（streamable-http）—— 多租户 + 鉴权 + 可视化面板。

━━━ 分层 ━━━

    auth.py        凭据 → 你是谁
    tenant.py      用户/库模型 + 权限纯函数
    library.py     多库集合 + 权限入口
    tenancy.py     组合成完整请求处理
    request_ctx.py 把身份从中间件传到工具层（contextvar）
    本文件         组装 ASGI、注册工具、接鉴权

━━━ 三个实测踩过的 ASGI 坑（都在这里）━━━

1. **不能用 ``Mount('/mcp')``** —— Mount 会把 `/mcp` 307 重定向到 `/mcp/`，
   而 MCP 应用只认精确的 `/mcp`。症状：面板正常、curl 探活正常，
   但**所有 MCP 工具调用失败**。

2. **包一层必须转发 lifespan** —— MCP 的 session manager 靠它启动。
   症状更具迷惑性：面板 200、探活正常，工具全部 500。

3. **静态资源要带凭据** —— ``<link>`` / ``<script>`` 发不出自定义头，
   会被鉴权拦掉、页面裸奔成纯 HTML。所以同步/异步资源也走统一鉴权，
   面板用 cookie 解决。

━━━ 鉴权位置 ━━━

**最外层**，包住面板 + API + MCP —— 不存在"某个端点忘了加鉴权"的可能。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

from .auth import COOKIE_NAME, Auth, Token
from .errors import ErrorCode, ToolError
from .tenancy import TenantHub, default_home
from .tools_tenant import TenantTools

logger = logging.getLogger("shared_memory.http")

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8200
MCP_PATH = "/mcp"


# ══════════════════════ 鉴权构造 ══════════════════════


def _build_auth(host: str, hub: TenantHub) -> Auth:
    """构造鉴权器并做启动自检。

    token 来源（按优先级）：
      1. ``SHARED_MEMORY_TOKEN``       逗号分隔；``user:token`` 可绑用户
      2. ``SHARED_MEMORY_TOKEN_FILE``  从文件读（**推荐** —— 环境变量会
         出现在 ``ps e``、容器 inspect、崩溃转储里）
      3. 都没有 → 依赖租户库里的用户（面板登录）
    """
    tokens: list[Token] = []

    token_file = os.environ.get("SHARED_MEMORY_TOKEN_FILE", "").strip()
    raw = os.environ.get("SHARED_MEMORY_TOKEN", "").strip()

    def parse_line(line: str, idx: int) -> Token | None:
        line = line.strip()
        if not line or line.startswith("#"):
            return None
        # 支持四种写法：
        #   <token>                        → 绑到 admin
        #   <user>:<token>                 → 绑到指定用户
        #   <user>:<label>:<token>         → 带标签（哪台设备）
        #   <user>:<label>:<scope>:<token> → 显式指定读写范围
        #
        # ⚠️ 第四种为什么必须有：前三段写法里 Token.scope 走 dataclass 默认值
        # ``readwrite``，也就是**在 token 文件里根本写不出"只读"** ——
        # 面板通过 add_token 发的只读凭据，服务一重启就变成可写。
        # 那是权限提升漏洞，不是功能缺失。
        parts = line.split(":")
        if len(parts) == 1:
            return Token(token=parts[0], user="admin", label=f"env-{idx}", managed=False)
        if len(parts) == 2:
            return Token(token=parts[1], user=parts[0], label=f"env-{idx}", managed=False)
        if len(parts) == 3:
            return Token(token=parts[2], user=parts[0], label=parts[1], managed=False)
        user, label, scope, token = parts[0], parts[1], parts[2], ":".join(parts[3:])
        if scope not in ("read", "readwrite"):
            logger.error(
                "token 文件第 %d 行 scope 非法（%r），按 readwrite 处理；"
                "只允许 read 或 readwrite", idx, scope,
            )
            scope = "readwrite"
        return Token(token=token, user=user, label=label, scope=scope, managed=False)

    if token_file:
        try:
            for i, line in enumerate(
                Path(token_file).read_text(encoding="utf-8").splitlines(), 1
            ):
                t = parse_line(line, i)
                if t:
                    tokens.append(t)
            logger.info("从 %s 读取了 %d 把 token", token_file, len(tokens))
        except OSError as exc:
            logger.error("读取 token 文件失败: %s", exc)
    elif raw:
        for i, part in enumerate(raw.split(","), 1):
            t = parse_line(part, i)
            if t:
                tokens.append(t)

    auth = Auth(bind_host=host, tokens=tokens, store=hub.store)

    allowed, reason = auth.preflight()
    if not allowed:
        if auth.allow_insecure():
            logger.warning(
                "⚠️ 以**无鉴权**模式启动（绑定 %s，SHARED_MEMORY_ALLOW_INSECURE=1）。"
                "同一网段的任何人都能读写全部记忆。", host,
            )
        else:
            raise SystemExit(f"\n[拒绝启动] {reason}\n")
    else:
        logger.info(reason)

    return auth


# ══════════════════════ MCP 工具注册 ══════════════════════

# ⚠️ 所有工具必须 ``output_schema=None``。
#
# fastmcp 会为每个返回 dict 的工具**自动生成 outputSchema**。而严格模型
# （GLM-5.3-Flash、Deepseek-V4.1-Flash、Step-5-Preview）逐字校验每个工具的
# schema，见到 ``outputSchema`` 直接拒**整个请求**（错误码 11129
# "The tool definition does not meet the model requirements"）——
# 不是跳过这一个工具，是整个 services 对它不可用。
#
# 我们的工具全部返回 dict（结果集），结构化输出 schema 对调用方没有
# 增量价值（客户端拿到的都是 content[0].text 里的 JSON），
# 所以显式关掉。**新增工具时照抄这个参数。**
#
# 回归保障：tests/test_tools_schema_contract.py 钉住"一个都不许多"。
#
# ────────────────────────────────────────────────────────────────
# 第二道毒：anyOf
#
# ``X | None = None`` 参数（如 ``tags: list[str] | None = None``）会被
# fastmcp 生成成 ``anyOf: [{...}, {"type": "null"}]``。严格模型同样
# 逐字拒载（与 outputSchema 同一个错）。output_schema 逐个工具显式关，
# 而 anyOf 取决于参数写法——漏一个就是一颗雷。所以在注册完成后做一次
# **全量归一化**，把 ``anyOf: [T, null]`` 压成 ``T``：
#   · 参数本来就是可选的（有 default），语义不变
#   · default / description 等兄弟字段原样保留
# ────────────────────────────────────────────────────────────────
def _flatten_null_union(node: Any) -> Any:
    """递归压平 ``anyOf: [T, {"type": "null"}]`` → ``T``。"""
    if isinstance(node, dict):
        any_of = node.get("anyOf")
        if isinstance(any_of, list):
            non_null = [
                s
                for s in any_of
                if not (isinstance(s, dict) and s.get("type") == "null")
            ]
            if len(non_null) == 1:
                merged: dict[str, Any] = dict(non_null[0])
                for k, v in node.items():
                    if k != "anyOf":
                        merged[k] = _flatten_null_union(v)
                return merged
        return {k: _flatten_null_union(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_flatten_null_union(item) for item in node]
    return node


def _normalize_anyof(mcp: Any) -> int:
    """压平全部已注册工具 inputSchema 里的 anyOf，返回处理的工具数。"""
    import json as _json

    probes = (
        lambda m: getattr(getattr(m, "_local_provider", None), "_components", None),
        lambda m: getattr(getattr(m, "_provider", None), "_components", None),
        lambda m: getattr(getattr(m, "_tool_manager", None), "_tools", None),
    )
    tools: list[Any] = []
    for probe in probes:
        try:
            store = probe(mcp)
        except Exception:  # noqa: BLE001 —— 探测失败就试下一种布局
            continue
        if isinstance(store, dict):
            tools = [
                v
                for v in store.values()
                if hasattr(v, "parameters") and hasattr(v, "output_schema")
            ]
            if tools:
                break
    n = 0
    for tool in tools:
        params = getattr(tool, "parameters", None)
        if isinstance(params, dict) and "anyOf" in _json.dumps(params, ensure_ascii=False):
            tool.parameters = _flatten_null_union(params)
            n += 1
    if not tools:
        # 静默跳过 = 把故障放回去。显式告警。
        import sys

        print(
            "[schema] anyOf 归一化失败：未探测到已注册工具"
            "（fastmcp 版本布局可能已变），严格模型可能拒载",
            file=sys.stderr,
        )
    return n


def build_app(hub: TenantHub) -> Any:
    """构造 FastMCP 应用并注册全部工具。

    ━━━ 身份怎么进来 ━━━

    工具签名里**没有** username 参数（有的话客户端就能自己填）。
    身份由鉴权中间件写进 contextvar，工具层用 ``require_user()`` 取。

    这样"伪造身份"在结构上不可能。
    """
    try:
        from fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            f"缺少 fastmcp。请安装：pip install fastmcp uvicorn\n原始错误：{exc}"
        ) from exc

    from .request_ctx import require_user, require_write_scope

    t = TenantTools(hub)
    mcp = FastMCP(
        "shared-memory-mcp",
        instructions=(
            "这是一个**多租户共享记忆库**。你看到的范围由你的凭据决定 —— "
            "调用 memory_whoami 可以看到你的身份和可读的库。\n\n"
            "使用约定：\n"
            "1. **开工先调 memory_whoami** —— 知道你是谁、能看哪些库；\n"
            "2. **动手前先检索** —— memory_search，它默认跨你有权限的全部库搜；\n"
            "3. **有结论就写回** —— memory_write（不传 lib 就写进你自己的个人库）；\n"
            "4. 记忆是纯 Markdown + git，误删有回收站。\n\n"
            "**诚实性要求**：记忆里没有的东西不许补；区分事实（fact）与\n"
            "推断（inference）；把推断当事实写是这个库最大的污染源。"
        ),
    )

    # 启动变更摘要（方案 11）：initialize 时把「你不在时发生了什么」
    # 追加进 instructions。零变更不播报；任何失败静默跳过。
    from .digest import make_digest_middleware

    mcp.add_middleware(make_digest_middleware(hub, hub.home))

    # ────────── 身份 ──────────

    @mcp.tool(output_schema=None)
    async def memory_whoami() -> dict:
        """我是谁、能看哪些库（**开工先调它**）。

        返回：
          · 你的身份（用户名、角色、所属组）
          · 你能读的**每一个库**及其归属（公司 / 小组 / 个人）
          · 其中哪些你能写

        角色：
          · ``admin``  —— 能管用户与库；能写公司库
          · ``member`` —— 读公司库；读写自己组的库与个人库

        返回的库列表**只有你有权限的那些**，所以不构成泄露
        （知道自己有什么权限是应该的）。
        """
        return await t.memory_whoami(require_user())

    @mcp.tool(output_schema=None)
    async def memory_libs() -> dict:
        """列出你可读的库（含每个库的条数与写入队列状况）。

        ⚠️ 只统计你有权限的库 —— 全局统计本身就是泄露。

        想看「**这个工作空间共享给谁**」用 memory_workspaces。
        """
        return await t.memory_libs(require_user())

    # ────────── 会话生命周期（v2 防串台）──────────

    @mcp.tool(output_schema=None)
    async def memory_session_start(
        task: str, space: str = "", visibility: str = "private"
    ) -> dict:
        """**开工登记会话** + 领取上下文包（动手前调它）。

        ━━━ 为什么必须先登记 ━━━

        共享记忆跨会话可见，不登记就会**串台**：别的会话搜天搜地，
        把你的任务过程态当成现行口径。登记之后，检索范围默认收窄到
        「长期记忆 + 本会话任务域」，互不打扰。

        参数：
          · task        任务名（如"共享记忆v2开发"）。**同任务重复调用幂等**
          · space       在哪个空间/库做（留空 = 跨库）
          · visibility  ``private``（默认）/ ``space`` / ``shared``

        返回三段：
          1. ``constraints`` —— 约束清单（硬约束，常驻，**必须遵守**）
          2. ``task_notes``  —— 这个任务此前留下的记忆（续做时接得上）
          3. ``space_brief`` —— 空间长期摘要（workflow/constraint 优先）

        过程态记忆用 ``memory_write(scope="task")`` 写；
        收工务必 ``memory_session_end``，否则任务过程态不会归档。
        """
        return await t.memory_session_start(
            require_user(), task, space, visibility
        )

    @mcp.tool(output_schema=None)
    async def memory_session_end(
        task: str = "", outcome: str = "", summary: str = ""
    ) -> dict:
        """**收工收尾会话** —— 把任务记忆归类（留 / 归档，都不删）。

        归类规则（按 type，确定性）：
          · ``workflow`` / ``constraint`` / ``habit`` / ``lesson``
            → 升 **long-term**（可复用的东西最重要）
          · 其余 → 标 **archived**（退出默认检索，``scope="archived"`` 仍可考古）

        参数：
          · task     任务名（留空 = 当前唯一活跃会话）
          · outcome  结果标记（如 done / aborted）
          · summary  收尾小结；非空会额外写一条 long-term 小结留在库里

        ⚠️ **不调这个 = 任务过程态永远留在默认检索范围里**，
        是"记忆越来越大、越来越乱"的主因之一。收工就该调。
        """
        require_write_scope("memory_session_end")
        return await t.memory_session_end(require_user(), task, outcome, summary)

    # ────────── 工作空间（共享控制）──────────

    @mcp.tool(output_schema=None)
    async def memory_workspaces() -> dict:
        """列出**工作空间**及其共享范围（**管理时用**）。

        ━━━ 与 memory_libs 的区别 ━━━

        · ``memory_libs``      → "库有多少条、队列堵不堵"（**运行状况**）
        · ``memory_workspaces`` → "**这个工作空间共享给谁**"（**权限状况**）

        每个工作空间带：
          · ``share_label`` —— 人类可读的共享范围
            （"仅 admin 自己" / "小组：研发" / "公司（全员可读）"）
          · ``readable`` / ``writable`` —— 你能不能读/写
          · ``can_manage`` —— **你能不能改它的共享范围**

        什么时候用：
          · 确认"我这条记忆会不会被别人看到"
          · 写敏感内容前，先看落哪个工作空间合适
          · 想把某个工作空间共享给组里
        """
        return await t.memory_workspaces(require_user())

    @mcp.tool(output_schema=None)
    async def memory_workspace_create(
        workspace: str, share: str = "", label: str = ""
    ) -> dict:
        """为一个**工作空间**建独立记忆库（决定它共享给谁）。

        ━━━ 为什么要「一个工作空间一个库」━━━

        共享范围必须绑在**库**上（物理隔离），不能绑在条目标签上：

        · 库级隔离 —— 没权限的人**根本打不开那个库**
        · 条目标签 —— 得"搜完再过滤"，漏一处（分页/计数/高亮）就泄露

        所以「这个工作空间不共享」= 它的库 owner 是 ``user:你``。

        参数：
          · ``workspace`` 工作空间名（中文可以，会安全化后作目录名）
          · ``share`` 共享范围，**留空 = 仅自己**（最安全的默认）：
              - ``user:<你的用户名>``  仅自己（不共享）
              - ``group:<组名>``       组内可读（你必须在那个组里）
              - ``company``            全员可读（**仅管理员能设**）
          · ``label`` 显示名，默认用工作空间名

        建完后写入时传 ``lib="<返回的 lib>"`` 即落进这个工作空间。
        """
        return await t.memory_workspace_create(
            require_user(), workspace, share, label
        )

    @mcp.tool(output_schema=None)
    async def memory_workspace_share(lib: str, share: str) -> dict:
        """改一个工作空间的**共享范围** —— 逐步放开或收回。

        ━━━ 这个操作能决定"谁能看到内容" ━━━

        所以只有**库所有者**或**管理员**能做。别人能改的话，
        就能把你私人的东西改成公开 —— 那是泄露，不是配置。

        典型用法（逐步放开）：
          1. 先 ``share="user:我"`` —— 自己先写、先整理
          2. 可以给组里看了 → ``share="group:研发"``
          3. 变成通用规范 → ``share="company"``（管理员）

        **收回也一样**（``company`` → ``user:我``），立即生效：
        没权限的人物理上打不开那个库了。

        ⚠️ 改成 ``company`` 时库会**自动变成只读**（成员不能写）——
        公司库是所有新人看到的第一份材料，让所有人随手写会让它烂掉。
        """
        return await t.memory_workspace_share(require_user(), lib, share)

    # ────────── 读 ──────────

    @mcp.tool(output_schema=None)
    async def memory_search(
        query: str = "",
        tags: list[str] | None = None,
        workspace: str = "",
        lib: str = "",
        limit: int = 20,
        offset: int = 0,
        since: str = "",
        until: str = "",
        sort: str = "relevance",
        claim: str = "",
        status: str = "",
        scope: str = "",
    ) -> dict:
        """检索共享记忆（**开工前先调这个**）。

        默认**跨你有权限的全部库**搜索，结果里每条带 ``_lib_label``
        标出来源（公司 / 某小组 / 某人个人库）。

        参数：
          · query     关键词，空格分隔（多词 AND）
          · tags      标签过滤（全部命中）
          · workspace 工作区过滤；``__none__`` 查无工作区的
          · **lib**   只搜某一个库。**只能收窄，不能扩大** ——
                      传一个你没权限的库会返回 403，而不是拿到内容
          · limit / offset / since / until / sort  同前
          · **claim** ``fact`` / ``inference`` / ``proposal``，空 = 不限。
                      问"现行口径"用 ``fact``；看到 ``inference`` 该多留心眼
          · **status** ``active``（默认）只返回现行口径，排除已被取代/
                      已过期的；``all`` = 含旧代的考古模式。
                      问"现在该怎么做"就用默认，别让过期口径误导你
          · **scope** 生效域（防串台）。**不传=长期记忆 + 当前会话任务域**；
                      ``all`` 考古 / ``task:xx`` 指定任务域 / ``archived`` 已归档
                      过程态。别人的任务过程态摸不到，除非显式指定 scope

        ``_lib_label`` 决定你该不该采信这条 —— 公司库的结论比
        某个人的个人笔记更权威。

        找不到时不要放弃：换短关键词，或用 tags/lib 单独看。
        """
        return await t.memory_search(
            require_user(), query, tags, workspace, lib, limit, offset,
            since, until, sort, claim, status, scope,
        )

    @mcp.tool(output_schema=None)
    async def memory_get(ident: str, lib: str = "") -> dict:
        """按标题或 id 读一条记忆的全文。

        不传 ``lib`` 时**只在你可读的库里**找 —— 找不到就是找不到，
        不会去读无权限的库（那是越权）。

        传 ``lib`` 可精确定位（同名条目在不同库里可能都有）。
        """
        return await t.memory_get(require_user(), ident, lib)

    @mcp.tool(output_schema=None)
    async def memory_list(
        limit: int = 50, tag: str = "", workspace: str = "", lib: str = ""
    ) -> dict:
        """列出记忆（最近更新在前），跨库合并。

        适合「看看我能看到的范围里都有什么」。结果不含正文。
        """
        return await t.memory_list(require_user(), limit, tag, workspace, lib)

    # ────────── 写 ──────────

    @mcp.tool(output_schema=None)
    async def memory_write(
        title: str,
        body: str,
        tags: list[str] | None = None,
        workspace: str = "",
        lib: str = "",
        mode: str = "upsert",
        author: str = "",
        claim: str = "",
        valid_until: str = "",
        review_after: str = "",
        superseded_by: str = "",
        scope: str = "",
    ) -> dict:
        """写入一条共享记忆（**有结论 / 踩坑 / 口径就该写回**）。

        ━━━ 写到哪个库 ━━━

        不传 ``lib`` → **写进你自己的个人库**（默认最安全）。

        要往共享库写必须**显式**传 ``lib``：
          · ``lib="company"``    公司库（**默认只读**，member 会拿到 403）
          · ``lib="group-xxx"``  你们组的库（组员可写）

        为什么默认落个人库：写是最容易污染别人的操作。默认落自己名下，
        永远不会误改公司规范或同事的东西。

        ━━━ 内容要求 ━━━

        权限范围内的人都能读到你写的东西，所以写成别人能直接用的形式：
        **背景（为什么）、结论（是什么）、依据（凭什么）**。

        · title     标题（按标题去重，同标题 = 同一条）
        · body      正文，Markdown
        · tags      标签，如 ["部署", "vivo"]
        · workspace 项目/工作区名
        · **lib**   写到哪个库（不传 = 个人库）
        · mode      upsert / create / append
        · author    可选，谁写的（仅溯源，**不用于权限**）
        · **claim** 这条记忆的性质：``fact`` / ``inference`` / ``proposal``。
                    **反幻觉的结构性落点** —— 搜到它的人知道该用什么
                    姿势采信。不传则服务端按规则推断
        · **valid_until**    硬过期日期（``YYYY-MM-DD`` 或 ISO）。到点后
                              search 默认不再返回它，expiring 视图告警
        · **review_after**   该复查日期。到点后 expiring 视图提醒复查
        · **superseded_by**  本记忆被哪条取代了（填**新记忆的标题**）。
                              "取代而非删除"的抓手：旧条标上它即退出现行口径，
                              但原文仍在，后人知道"这条路走过、不通"
        · **scope** 生效域（v2）。不传 = ``long-term``（长期，默认可见）；
                    ``"task"`` = 绑到当前会话的任务域（**任务过程态用这个**，
                    收工后自动归档，不污染别人的检索）；也接受
                    ``"task:名字"`` / ``"archived"``

        upsert 更新已有记忆时，这三个时效字段**显式传才覆盖**，
        不传保留原值 —— 避免"只想改正文"却把有效期信息清掉。

        什么时候该写：搞清根因 / 踩了别人也会踩的坑 / 定下口径 /
        发现"看起来是但其实不是"的事实。

        不需要写：能从代码直接读到的、临时中间状态、只对当前这句话
        有意义的上下文。

        ━━━ 关于提交（重要，别误读返回）━━━

        写入成功时 ``ok=true``，且**内容已经安全落盘**。

        返回里的 ``committed`` 是「本次是否**立刻** git 提交」。
        默认是防抖模式（静默 8 秒后自动提交），所以它通常是 ``false`` ——
        **这不代表失败**，别因此重试（重试会写出重复内容）。

        想立刻入库（给别人看 / 确认生效），调 ``memory_commit``。
        """
        require_write_scope("memory_write")
        return await t.memory_write(
            require_user(), title, body, tags, workspace, lib, mode, author,
            claim, valid_until, review_after, superseded_by, scope,
        )

    @mcp.tool(output_schema=None)
    async def memory_delete(ident: str, confirm: bool = False, lib: str = "") -> dict:
        """删除一条记忆（**软删除**，移入回收站，可恢复）。

        需要 ``confirm=True`` —— 防误删的一道闸。

        不传 ``lib`` 时在**你可写的库**里找它。找不到说明它不在你的
        写权限范围内（可能只读，或者是别人的）。
        """
        require_write_scope("memory_delete")
        return await t.memory_delete(require_user(), ident, confirm, lib)

    @mcp.tool(output_schema=None)
    async def memory_restore(filename: str, lib: str) -> dict:
        """从回收站恢复一条被软删除的记忆。

        ``lib`` 必填 —— 回收站是**库内**的概念，恢复必须指明哪个库。
        """
        require_write_scope("memory_restore")
        return await t.memory_restore(require_user(), filename, lib)

    # ────────── 体系 ──────────

    @mcp.tool(output_schema=None)
    async def memory_view(
        kind: str = "", lib: str = "", workspace: str = "",
        tag: str = "", limit: int = 200, include_superseded: bool = False,
    ) -> dict:
        """看「视图」—— 按规则实时算出来的入口（**不知道搜什么时用它**）。

        可用 kind：
          · ``overview``    总览（类型分布、时效告警统计）
          · ``decisions``   决策记录 —— 定过的口径与取舍
          · ``lessons``     踩坑清单 —— 避免重复踩
          · ``todos``       待办
          · ``expiring``    时效告警 —— 已过期/该复查/已被取代
          · ``inferences``  未核实的推断 —— 引用前当心
          · ``projects``    按项目
          · ``recent``      最近更新
          · 不填 → 返回视图目录

        · ``include_superseded`` 默认 False：已被取代的条目不进普通视图
          （现行口径里混着"已走不通的旧路"会误导人）。要考古显式传 True；
          ``expiring`` 视图恒含它们，它本来就是管这个的。

        跨库时**逐库算再合并**（不能先合并全库再算 —— 那会把无权库
        的内容算进统计，泄露规模）。
        """
        return await t.memory_view(
            require_user(), kind, lib, workspace, tag, limit, include_superseded
        )

    @mcp.tool(output_schema=None)
    async def memory_backlinks(title: str, lib: str = "") -> dict:
        """查一条记忆的**引用关系**：谁引用了它、它引用了谁。

        引用是自动识别的（正文里的 ``[[标题]]`` + 提到已存在的标题），
        不需要维护关系表。

        ⚠️ 只在**同库内**统计 —— 跨库引用会泄露"另一库存在某个标题"。
        """
        return await t.memory_backlinks(require_user(), title, lib)

    # ────────── 运维 ──────────

    @mcp.tool(output_schema=None)
    async def memory_stats(
        lib: str = "", commit_now: bool = False, with_commits: bool = False,
    ) -> dict:
        """看库况 —— 排查「写了没生效」「是不是卡住了」时先看这个。

        · 不传 ``lib`` → 列出**你可读的全部库**及其条数与队列积压
        · 传 ``lib``   → 那个库的详细状况
        · ``commit_now=True`` → 顺便立刻提交
        · ``with_commits=True`` → 附带 git 提交历史（**默认不带**：
          每次要 fork git 子进程约 60ms，核心字段不依赖它）

        ⚠️ 只统计你有权限的库 —— 全局统计本身就是泄露。

        ━━━ 返回里最该看的两个字段 ━━━
        · ``write_queue.pending`` —— 还有多少写没落盘。
          非 0 且长时间不变 = 写入卡住了
        · ``git`` —— 有没有「改了没提交」的改动。写完想确认是否已入库看这个
        """
        return await t.memory_stats(require_user(), lib, commit_now, with_commits)

    @mcp.tool(output_schema=None)
    async def memory_commit(lib: str = "", message: str = "") -> dict:
        """立刻提交（**不用等防抖**）。

        ━━━ 什么时候需要它 ━━━

        默认是「静默 8 秒后自动提交」，所以写完**通常什么都不用做**。

        但这三种情况要手动提交：
          1. **写完马上要给别人看** —— 防抖还没到时间
          2. **要确认提交真的成功了** —— 排查「写了没生效」
          3. **要写一个有意义的提交说明** —— 自动提交的说明是模板

        ``lib`` 不传则提交**你所有可写的库**（一次全提）。
        传 ``lib`` 只提那一个（需要写权限）。

        成功会返回 commit 短哈希。返回「无改动」**不是错误**，
        意思是"没有需要提交的东西"。
        """
        require_write_scope("memory_commit")
        return await t.memory_commit(require_user(), lib, message)

    # ────────── 导入 / 导出 ──────────

    @mcp.tool(output_schema=None)
    async def memory_export(
        workspace: str = "", lib: str = "", out_dir: str = "",
    ) -> dict:
        """以**工作空间**为单位导出记忆 bundle（字节级保留，可无损再导入）。

        用途：项目交接 / 换机器 / 备份 / 跨环境同步。

        · ``workspace`` 导出粒度。传项目名只导该项目；不传 = 全部
        · ``lib``       只导一个库（需读权限）；不传 = 你有权读的每个库
                        各导一份、合并进一个 bundle
        · ``out_dir``   产物目录，默认 ``<home>/exports/``

        bundle 结构：``manifest.json``（schema 版本 + 每条 sha256 +
        来源库）+ ``libs/<库>/notes/*.md``（字节级原样拷贝）。
        """
        return await t.memory_export(require_user(), workspace, lib, out_dir)

    @mcp.tool(output_schema=None)
    async def memory_import(
        bundle: str, target_lib: str = "", workspace: str = "",
        on_conflict: str = "skip",
    ) -> dict:
        """导入 memory_export 产出的 bundle（写路径，走队列串行）。

        · ``bundle``      bundle 目录路径（含 manifest.json 那个）
        · ``target_lib``  导入到哪个库，需**写权限**。不传 = 你的个人库
        · ``workspace``   **过滤器**不是改写器：只导入该工作区的条目
        · ``on_conflict`` skip（默认，不覆盖）/ update / rename

        ⚠️ 导入**不重写内容** —— 字节级还原是这条链路的意义
        （manifest 里有 sha256，导入前后可校验）。
        """
        require_write_scope("memory_import")
        return await t.memory_import(
            require_user(), bundle, target_lib, workspace, on_conflict
        )

    _normalize_anyof(mcp)
    return mcp


# ══════════════════════ 鉴权中间件 ══════════════════════


def _wrap_auth(app: Any, auth: Auth) -> Any:
    """统一的鉴权中间件（ASGI 最外层）。

    ━━━ 凭据来源（按优先级）━━━

      1. ``Authorization: Bearer <token>`` —— 程序化访问（MCP 客户端）
      2. ``Cookie: shm_session=…`` —— 浏览器（登录后）
      3. ``?login=<ticket>`` —— 一次性票据换 cookie（仅 GET）

    ⚠️ **不接受 ``?token=``** —— 这是刻意的：URL 会进浏览器历史、
    书签、Referer、代理日志、截图。之前正是这样泄露过一次。
    要人工打开面板，走「票据换 cookie」这条一次性路径。

    ━━━ 身份注入 ━━━

    鉴权通过后把身份写进 contextvar，工具层用 ``require_user()`` 取。
    工具签名里没有 username 参数 —— 客户端无法伪造身份。
    """
    from starlette.responses import JSONResponse, RedirectResponse
    from urllib.parse import parse_qs

    from .request_ctx import set_identity

    #: 不需要鉴权的路径。
    #:
    #: ━━━ 为什么这些必须豁免（实测踩出的死锁）━━━
    #:
    #: **要鉴权就死锁了。** 流程本该是：
    #:   ① 打开 `/` → 服务端返回 HTML 骨架
    #:   ② 骨架里的 `<script src="panel.js">` 由**浏览器**发起，
    #:      **不带 Authorization 头**；首次访问也还没有 cookie
    #:   ③ panel.js 被 401 拦掉 → `showLogin()` 这个函数压根没定义
    #:   ④ 登录框永远不出现 → **页面全白，用户没有任何办法登录**
    #:
    #: 更早一层：如果 `/` 本身要鉴权，浏览器拿到的是一个 **401 JSON**
    #: 而不是 HTML —— 连骨架都没有，后面全都不用谈了。
    #:
    #: 这个 bug 是改成 cookie 方案后引入的：以前 token 在 URL 里，
    #: 静态资源能带上 `?token=`，所以不会暴露。
    #:
    #: ━━━ 豁免它们为什么是安全的 ━━━
    #:
    #: 这三个都是**纯骨架，不含任何数据**：
    #:   · `/`        —— HTML 结构，数据全靠 /api/* 取
    #:   · panel.css  —— 纯样式
    #:   · panel.js   —— 前端逻辑（数据请求都要凭据）
    #:   · favicon    —— 空响应
    #:
    #: 判据很简单：**把它们贴到公网上，泄露的是"这个服务长什么样"，
    #: 而不是"库里有什么"**。真正含数据的路径（/api/*、/mcp、/health）
    #: 一个都不豁免 —— 见 tests/test_panel_contract.py 的对称测试。
    #:
    #: favicon 额外理由：浏览器每次开页面都请求它，要它鉴权会让
    #: 探针/监控把 IP 拖进限速名单、误伤真人（实测踩过）。
    AUTH_EXEMPT_PATHS = {
        "/",
        "/favicon.ico",
        "/panel.css",
        "/panel.js",
        # ⚠️ 语言包也要豁免（与 panel.js 同理）：
        # 它由 <script src> 加载、不含数据、而且 panel.js 的 t() 依赖它。
        "/i18n.js",
        # ⚠️ 登录接口**必须**豁免 —— 它的作用正是"还没有凭据时换凭据"，
        # 逻辑上必须先于鉴权。要它鉴权就成了"没有钥匙就进不去配钥匙的屋子"。
        # 实测踩过：忘了豁免它 → 面板登录框提交后永远 401。
        "/api/auth/login",
        # 退出登录/改密码同样必须先于鉴权：
        # · logout 的语义是"作废当前凭据"，不能要求凭据有效才让作废
        #   （否则 cookie 过期后用户点退出会拿到 401，体验上是"坏了"）
        # · password 内部会自己校验旧密码，鉴权由它负责
        "/api/auth/logout",
        "/api/auth/password",
    }

    async def guarded(scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await app(scope, receive, send)
            return

        # ⓪ 绝对形式请求目标先归一化（RFC 7230 §5.3.2）。必须在取 path 之前 ——
        #    否则路由拿整个 URL 当路径匹配，MCP 端点 404（面板却正常）。
        _normalize_absolute_form(scope)

        method = scope.get("method", "GET")
        path = scope.get("path", "/")
        headers = {
            k.decode().lower(): v.decode() for k, v in scope.get("headers", [])
        }
        client = scope.get("client") or ("?", 0)
        source = str(client[0])

        # ── ① 一次性票据 → 换 cookie → 跳转到干净 URL ──
        #
        # ⚠️ 这一步必须在"豁免判断"**之前**。
        #
        # 实测踩过：`/` 免鉴权后，`/?login=<ticket>` 被直接放行，
        # 根本没走到票据兑换 —— 于是 cookie 永远设不上，
        # 表现为"点了登录、页面刷新了、但还是未登录"。
        #
        # 顺序错了功能会**静默失效**：没有报错、没有日志，
        # 只是"点了没反应"。这类 bug 只能靠真浏览器验证发现。
        if method in ("GET", "HEAD"):
            qs = parse_qs(scope.get("query_string", b"").decode())
            ticket = qs.get("login", [""])[0]
            if ticket:
                u = auth.redeem_ticket(ticket)
                if u:
                    sess = auth.make_session(u)
                    resp = RedirectResponse(path, status_code=302)
                    resp.set_cookie(
                        COOKIE_NAME, sess,
                        httponly=True,        # JS 读不到 → XSS 偷不走
                        samesite="strict",    # 防 CSRF
                        max_age=7 * 24 * 3600,
                        path="/",
                        # secure=False：本服务常跑在纯 HTTP 内网。
                        # **已知取舍**：要 HTTPS 请在前面挂反代。
                    )
                    await resp(scope, receive, send)
                    return
                logger.warning("无效登录票据来自 %s", source)

        # ── ② 豁免路径直接放行（静态资源 / 登录接口 —— 无数据、无副作用）──
        if path in AUTH_EXEMPT_PATHS:
            await app(scope, receive, send)
            return

        # ── ②b 未启用鉴权（loopback 绑定）时，/health 也放行 ──
        #
        # 实测踩过：绑 127.0.0.1 时日志说"未强制鉴权"，但 /health 仍 401 ——
        # 监控探针（systemd / 容器 healthcheck / 守护脚本）拿不到 token，
        # 探活直接不可用。行为与自己承诺的话自相矛盾。
        #
        # ⚠️ 只在 ``auth.required=False``（loopback）时豁免。绑 0.0.0.0 时
        # /health 仍要凭据 —— 那种部署下无鉴权探活会泄露"这里有服务"。
        # health 只返回聚合状态（ok/problems/计数/队列），不含记忆内容。
        if path == "/health" and not auth.required:
            await app(scope, receive, send)
            return

        # ── 限速 ──
        throttled, wait = auth.throttled(source)
        if throttled:
            logger.warning("限速拦截: %s → %s", source, path)
            resp = JSONResponse(
                {"ok": False, "error": "too_many_failures",
                 "message": f"失败次数过多，请等待 {int(wait)} 秒后重试"},
                status_code=429, headers={"Retry-After": str(int(wait))},
            )
            await resp(scope, receive, send)
            return

        user = ""
        token_scope = ""

        # ① Bearer
        authz = headers.get("authorization", "")
        if authz.lower().startswith("bearer "):
            user, token_scope = auth.resolve(authz[7:].strip())

        # ② Cookie
        if not user:
            for part in headers.get("cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE_NAME and v:
                    user, token_scope = auth.resolve(v)
                    break

        # （一次性票据已在函数开头处理 —— 那里必须先于豁免判断，
        #   否则免鉴权的 `/?login=` 会被直接放行、票据永远不兑换。）

        if not user:
            auth.note_failure(source)
            logger.warning("未授权: %s %s（来源 %s）", method, path, source)
            resp = JSONResponse(
                {"ok": False, "error": "unauthorized",
                 "message": "缺少或无效的凭据。用 Authorization: Bearer <token> "
                            "访问；浏览器请走面板登录。"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="shared-memory"'},
            )
            await resp(scope, receive, send)
            return

        # ── scope 检查：只读凭据不能写 ──
        # ⚠️ MCP 路径**不能**在这里按 method 判读写：MCP 里读和写全是 POST，
        # 那样只读凭据连 initialize / memory_search 都调不通（踩过一次，
        # 表现为只读 token 一律 403）。MCP 的写判定放在工具层
        # （request_ctx.require_write_scope，按工具语义）。
        is_mcp = path.rstrip("/") == "/mcp"
        action = "read" if (is_mcp or method in ("GET", "HEAD", "OPTIONS")) else "write"
        if not auth.scope_allows(token_scope, action):
            logger.warning("scope 不足: %s %s（scope=%s）", method, path, token_scope)
            resp = JSONResponse(
                {"ok": False, "error": "forbidden",
                 "message": f"此凭据是只读的（scope={token_scope}）"},
                status_code=403,
            )
            await resp(scope, receive, send)
            return

        # ⚠️ 注入身份 —— 必须在放行之前
        set_identity(user, token_scope, source)
        await app(scope, receive, send)

    return guarded


# ══════════════════════ ASGI 组装 ══════════════════════


def _normalize_absolute_form(scope: dict) -> None:
    """把**绝对形式**请求目标归一化成路径（就地改 scope）。

    ━━━ 为什么需要它 ━━━

    RFC 7230 §5.3.2：服务端**必须**接受绝对形式请求目标
    （``POST http://host:port/mcp HTTP/1.1``）。代理/网关转发、
    以及某些客户端（httpx 带 trust_env 走代理时）会这么发。

    不做这一步时，ASGI 的 ``scope["path"]`` 就是整个 URL 字符串
    （``http://host:port/mcp``），路由层拿它去匹配 → 匹配不到 → 404。
    症状极难定位：**面板正常（路径不带前缀），只有 MCP 端点 404**，
    看起来像"MCP 坏了"。

    实测：本机 WorkBuddy 沙箱设了 ``HTTP_PROXY``，httpx 默认 trust_env
    走代理 → 请求变绝对形式 → MCP 全挂、面板正常。生产环境无此变量时
    一切正常，所以这类问题只在特定网络环境下才现形。

    归一化放在最外层，让鉴权与路由都看到干净的路径。
    """
    if scope.get("type") != "http":
        return
    path = scope.get("path", "")
    if not path.startswith(("http://", "https://")):
        return

    from urllib.parse import urlsplit

    parts = urlsplit(path)
    new_path = parts.path or "/"
    scope["path"] = new_path
    scope["raw_path"] = new_path.encode("utf-8")
    # 绝对形式里 query 通常挂在 URL 上；只有原本没有 query_string 时才补，
    # 避免覆盖 ASGI 服务器已经解析好的值。
    if parts.query and not scope.get("query_string"):
        scope["query_string"] = parts.query.encode("utf-8")


def build_asgi(hub: TenantHub, auth: Auth) -> Any:
    """把面板 + MCP 组装成一个 ASGI 应用。

    见模块 docstring 里的三个 ASGI 坑。
    """
    from starlette.applications import Starlette

    from .dashboard import make_routes

    mcp_app = build_app(hub).http_app(path=MCP_PATH)
    panel = Starlette(routes=make_routes(hub, auth=auth))
    mcp_paths = {MCP_PATH, MCP_PATH + "/"}

    async def root_app(scope: dict, receive: Any, send: Any) -> None:
        # 注：绝对形式请求目标已在最外层 _wrap_auth 里归一化（见
        # _normalize_absolute_form）—— 这里拿到的 scope["path"] 一定是路径。
        # ① lifespan → 交给 MCP（它需要启动 session manager）
        if scope.get("type") == "lifespan":
            await mcp_app(scope, receive, send)
            return
        # ② MCP 端点（含带斜杠变体，容错客户端写法差异）
        if scope.get("type") == "http" and scope.get("path") in mcp_paths:
            await mcp_app(scope, receive, send)
            return
        # ③ 其余 → 面板
        await panel(scope, receive, send)

    # 鉴权在最外层：所有端点无差别覆盖
    return _wrap_auth(root_app, auth)


# ══════════════════════ 入口 ══════════════════════


def main() -> None:
    """进程入口。

    环境变量：
      HOST / PORT / LOG_LEVEL / SHARED_MEMORY_HOME
      SHARED_MEMORY_TOKEN       逗号分隔；``user:token`` 可绑用户
      SHARED_MEMORY_TOKEN_FILE  从文件读（推荐）
      SHARED_MEMORY_ALLOW_INSECURE  显式接受无鉴权（默认拒绝启动）
    """
    import uvicorn

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        stream=sys.stdout,
    )

    host = os.environ.get("HOST", DEFAULT_HOST)
    port = int(os.environ.get("PORT", DEFAULT_PORT))

    # ── 租户中枢（用户 + 多库）──
    hub = TenantHub(default_home())
    boot = hub.bootstrap()
    if boot:
        # 首次启动的初始密码**只打印一次**，且不写进日志文件
        print("\n" + "═" * 62)
        print("  首次启动：已创建管理员账号")
        print(f"    用户名：{boot['user']}")
        print(f"    密码　：{boot['password']}")
        print("  ⚠️ 这个密码**只显示这一次**，请立刻记下。")
        print("     登录面板后可在「设置」里修改。")
        print("═" * 62 + "\n")

    # ── 鉴权（先于业务初始化自检）──
    auth = _build_auth(host, hub)

    # token 持久化：从 <home>/state/tokens.json 加载，之后增删写回。
    # ⚠️ 必须在"自动发放"之前 —— 加载到的 token 要能抑制重复发放，
    # 否则每次重启都会打印一把新 token、旧的全失效（实测踩过）。
    # state/ 是非 git 目录；绝不能放进 libs/（那是会 commit 的 vault）。
    try:
        auth.attach_persistence(hub.home / "state" / "tokens.json")
    except RuntimeError as exc:
        raise SystemExit(f"\n[拒绝启动] {exc}\n") from exc

    # 把 hub 里已登记的用户，各发一把 token（方便首次接入）
    for name, u in hub.store.users.items():
        if not any(x.user == name and x.active for x in auth._tokens):
            t = auth.add_token(user=name, label=f"auto-{name}", scope="readwrite")
            logger.info("为用户 %s 自动发放 token（指纹 %s）", name, t.fingerprint)
            print(f"  [{name}] token: {t.token}")

    app = build_asgi(hub, auth)

    logger.info("可视化面板: http://%s:%d/", host, port)
    logger.info("MCP 端点:   http://%s:%d%s", host, port, MCP_PATH)
    logger.info("库根目录:   %s", hub.home)
    logger.info("鉴权: %s", "已启用" if auth.required else "未启用（loopback）")

    uvicorn.run(
        app, host=host, port=port,
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
        timeout_graceful_shutdown=10,
    )


if __name__ == "__main__":
    main()
