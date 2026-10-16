"""可视化面板 —— 服务端路由 + HTML 骨架（多租户版）。

━━━ 面板的读写边界 ━━━

**记忆只读，配置可写。**

  · 记忆的写入只能走 MCP 工具 —— 那条路径有队列串行 + git 提交 +
    软删除三道保护。在面板开记忆写入口 = 绕过它们。
  · 配置**可以**写，但走**同一个 service/队列**（并发安全）。

━━━ 多租户下的面板 ━━━

面板的所有数据接口都经过 ``TenantHub`` —— 也就是说**面板看到的
内容与你用 MCP 看到的完全一致**（同样的权限过滤）。

这一点很重要：如果面板绕过权限单独查库，就会出现"MCP 看不到但
面板能看到"的漏洞。所以面板复用同一套 Tenant 权限入口。

━━━ 为什么把 CSS/JS 拆成独立文件 ━━━

塞在 Python 字符串里的后果：编辑器不给语法高亮、不能写注释、
774 行的文件里真正的 Python 逻辑只有几十行。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 静态资源目录（panel.css / panel.js）
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _static_ver() -> str:
    """静态资源版本指纹（内容/时间变了指纹就变）。

    用途只有一个：让 `panel.css?v=xxx` 这种 URL 在**文件变了以后必然变**，
    从而绕开浏览器缓存。

    ⚠️ 为什么不用内容哈希：面板资源 ~200KB，每个请求都读一遍算 sha
    是浪费。mtime_ns + size 对"文件被改过"这件事已经足够灵敏
    （真改了内容，mtime 必变）。
    """
    import hashlib

    h = hashlib.sha1()
    for name in ("panel.css", "panel.js", "i18n.js"):
        f = STATIC_DIR / name
        try:
            st = f.stat()
            h.update(f"{name}:{st.st_mtime_ns}:{st.st_size}".encode())
        except OSError:
            h.update(f"{name}:missing".encode())
    return h.hexdigest()[:8]


#: 配置接口的请求体上限（字节）。
#: 配置只有几百字节，1MB 已经宽松得离谱。
#: 为什么必须有：没有上限时反复发大 body 就能把内存吃光，
#: 而**服务是共享的** —— 一个客户端能把所有人一起搞挂。
MAX_CONFIG_BODY_BYTES = 1_048_576

#: HTML 骨架。刻意很短 —— 主体由 JS 渲染，
#: 避免"Python 里写一半、JS 里改一半"的分裂。
PANEL_HTML = """<!DOCTYPE html>
<html lang="zh-CN" id="html-root">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>共享记忆 · 档案库</title>
<link rel="stylesheet" href="panel.css?v=__PANEL_VER__" id="panel-css">
<!-- i18n 必须在 panel.js **之前**加载：panel.js 顶层的 t() 调用
     需要它已就绪（否则 ReferenceError，页面全白）。
     用同步 <script> 而不是 defer —— 保证顺序。 -->
<script src="i18n.js?v=__PANEL_VER__"></script>
</head>
<body>
<header>
  <div class="hrow">
    <div class="seal" data-i18n="app.seal">记</div>
    <div class="brand"><span data-i18n="app.brand">共享记忆</span><em id="root">…</em></div>
    <nav class="navtabs">
      <button class="navtab on" data-tab="library" data-i18n="nav.library">记忆</button>
      <button class="navtab" data-tab="admin" data-i18n="nav.admin">管理</button>
      <button class="navtab" data-tab="settings" data-i18n="nav.settings">设置</button>
    </nav>
    <div class="spacer"></div>
    <!-- 语言切换器。放在顶栏右侧（不占主流程），下拉即改。
         默认中文；切换后记住选择（localStorage）。 -->
    <select id="lang-select" title="Language / 语言"
            style="font:inherit;font-size:12px;padding:3px 6px;border-radius:7px;
                   background:var(--panel2);border:1px solid var(--line);
                   color:var(--muted);cursor:pointer;margin-right:10px">
      <option value="zh">中文</option>
      <option value="en">English</option>
    </select>
    <div class="kbd-hint">
      <kbd>/</kbd><span data-i18n="hint.search">搜索</span>
      <kbd>j</kbd><kbd>k</kbd><span data-i18n="hint.nav">切换</span>
      <kbd>1</kbd><kbd>2</kbd><kbd>3</kbd><span data-i18n="hint.pages">页</span> ·
      <kbd>Esc</kbd><span data-i18n="hint.clear">清空</span>
    </div>
  </div>
  <div class="stats">
    <div class="st"><span class="dot" id="dot"></span><b id="s-notes">–</b><span data-i18n="stat.notes">条记忆</span></div>
    <div class="st"><b id="s-libs">–</b><span data-i18n="stat.libs">个库</span></div>
    <div class="st"><b id="s-ws">–</b><span data-i18n="stat.workspaces">个工作区</span></div>
    <div class="st"><b id="s-tags">–</b><span data-i18n="stat.tags">个标签</span></div>
    <div class="st" id="st-q"><b id="s-queue">–</b><span data-i18n="stat.queue">队列积压</span></div>
    <!-- 读取失败计数：库里有坏文件时显示（panel.js 会按需 display 它）。
         必须有这个元素 —— 否则那个提示永远不显示（实测被契约测试抓到）。 -->
    <div class="st warn" id="st-bad" style="display:none"><b id="s-bad">–</b><span data-i18n="stat.readFail">读取失败</span></div>
    <div class="st ok"><b id="s-who">–</b><span id="s-role"></span></div>
    <button class="btn" id="gov-btn" data-i18n="gov.btn">体检</button>
  </div>
</header>

<!-- ── 页 1：记忆库 ── -->
<div id="page-library">
  <div class="wrap">
    <div class="rail">
      <div class="tools">
        <!-- 自动上下文条（v2 · S-7.2）：把"agent 会被喂什么"摊开给用户看 -->
        <div id="ctxbar"></div>
        <div class="searchwrap">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="2.2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/>
               <path d="M20 20l-3.5-3.5"/></svg>
          <input id="q" placeholder="搜索（跨你可读的全部库）…" autocomplete="off" spellcheck="false">
        </div>
        <div id="libchips"></div>
        <!-- 生效域三档 chip + 会话卡（v2 · S-7.2） -->
        <div id="scopechips"></div>
        <div id="sesscard"></div>
        <div id="groupby"></div>
        <div id="chips"></div>
        <!-- 视图 chip（原「体系」页搬来，v2 · S-7.2）：记忆页与体系本来就是
             同一件事的两种看法 —— 分成两个顶级 tab 会让"我想看的那个视图"
             和"我正在读的那条"分家。 -->
        <div id="viewbar"></div>
      </div>
      <div class="scroll" id="list"></div>
    </div>
    <div class="reader" id="reader">
      <div class="doc" id="notedoc">
        <div class="empty"><span class="big">◫</span>从左侧选一条记忆开始阅读</div>
      </div>
      <div class="doc" id="kbody" style="display:none"></div>
    </div>
  </div>
</div>

<!-- ── 页 2：管理（v2 · S-7.2/S-7.3）──
     这一页动的是「库本身」：备份、接入、空间、指令 —— 不是某一条记忆。
     与「设置」的分界：设置是"这个服务怎么跑"（规则/账号/外观），
     管理是"这个库怎么活下去"（备份/谁能进/空间/给 agent 的指令）。 -->
<div id="page-admin" style="display:none">
  <div class="wrap" style="grid-template-columns:1fr">
    <div class="reader">
      <div class="doc" style="max-width:86ch">
        <h2 class="sechead" style="margin-top:0">管理</h2>
        <p class="sectip">
          备份、接入、空间、指令 —— 这一页动的是<strong>库本身</strong>，不是某条记忆。
        </p>

        <h3 class="sechead">备份与恢复</h3>
        <p class="sectip">导出是字节级 + sha256 校验；导入<strong>不重写内容</strong>。</p>
        <div id="adm-backup"></div>

        <h3 class="sechead">接入</h3>
        <p class="sectip">新机器 / 新 agent 接上来的钥匙与命令。</p>
        <div id="adm-access"></div>

        <h3 class="sechead" data-i18n="ws.title">工作空间与共享</h3>
        <p class="sectip" data-i18n="ws.hint">工作空间 = 一个独立的库，共享范围决定谁能看。</p>
        <div id="ws-body"><div class="empty">载入中…</div></div>

        <h3 class="sechead" data-i18n="cfg.instructions">给 agent 的指令</h3>
        <p class="sectip" data-i18n="cfg.instructionsHint">
          每次握手都会把这段下发给 agent；契约七条由服务端保证，这里是空间级补充。
        </p>
        <div id="adm-instr"></div>
      </div>
    </div>
  </div>
</div>

<!-- ── 页 3：设置 ── -->
<div id="page-settings" style="display:none">
  <div class="wrap" style="grid-template-columns:1fr">
    <div class="reader">
      <div class="doc" style="max-width:86ch">
        <h2 class="sechead" data-i18n="cfg.title">服务设置</h2>
        <p class="sectip" data-i18n="cfg.subtitle">
          配置存在库里（跟着 git 走、可回滚），改完立即生效。
        </p>
        <div id="sbody"></div>
      </div>
    </div>
  </div>
</div>

<!-- 治理抽屉（v2 · S-7.4）：体检报告 —— 只列候选与建议，不提供任何删除入口 -->
<div id="gov-drawer" style="display:none"></div>

<script src="panel.js?v=__PANEL_VER__"></script>
</body>
</html>
"""


#: 生效域三档（面板 chip）。与 vault 的 scope 语义一致。
PANEL_SCOPE_CHIPS = ("long-term", "task", "all")


def _filter_by_scope(notes: list[dict], scope: str) -> list[dict]:
    """按生效域过滤面板列表（v2 · S-7.2）。

    ━━━ 为什么这一刀必须切 ━━━

    ``list_notes`` 返回的是**库里全部条目** —— 包括别的会话写下的任务过程态
    （``scope=task:xxx``）和早已归档的老账。默认不过滤的话，面板会把
    "别人正在做的半成品"和"过期老账"一起摊在你面前：那正是防串台要挡的
    东西，只不过发生在界面上而不是检索里。

    ⚠️ **缺省 scope（long-term）是不输出字段的**（见 ``Note.to_dict``），
    所以"没带 scope"等于 long-term，不是"未知" —— 判断时要按缺省补上，
    否则默认视图会把最常见的条目全滤掉。
    """
    want = (scope or "long-term").strip()
    if want == "all":
        return notes
    if want == "archived":
        return [n for n in notes if str(n.get("scope", "")) == "archived"]
    if want == "task" or want.startswith("task:"):
        prefix = want if want.startswith("task:") else "task:"
        return [n for n in notes if str(n.get("scope", "")).startswith(prefix)]
    return [n for n in notes if str(n.get("scope", "") or "long-term") == "long-term"]


def make_routes(hub: Any, *, auth: Any = None) -> list[Any]:
    """返回面板用的 Starlette 路由。

    ⚠️ 所有数据接口都从 ``request_ctx.current_user()`` 取身份 ——
    那个值由鉴权中间件注入，**不从请求参数读**。
    所以面板无法越权（它拿到的身份就是请求者的身份）。

    ━━━ 为什么要有 auth 参数 ━━━
    登录/退出/改密码/token 管理都要动**凭据本身**，而那些凭证存在
    ``auth`` 里（不在 hub）。所以这里必须同时拿到两者。
    """
    from starlette.responses import FileResponse, HTMLResponse, JSONResponse
    from starlette.routing import Route

    # COOKIE_NAME / User 在这里 import：dashboard 被 http_server 导入，
    # 而 auth/tenant 不依赖 dashboard —— 放函数内可避免任何循环导入风险。
    from .auth import COOKIE_NAME, MASTER_KEY_GRACE_SECONDS
    from .request_ctx import current_user, require_user
    from .tenant import User

    async def dashboard(request: Any) -> Any:
        """面板骨架。

        ⚠️ 三件都不能省（2026-10-15 用户实测踩出来的）：

        ① **静态资源带 `?v=<指纹>`**：用户改了代码却"一点没动"——
           根因就是浏览器还在跑几小时前缓存的 panel.js/css。
           服务端文件早就换了，是浏览器没重新拉。

        ② **HTML `no-cache`**：否则浏览器按启发式规则缓存 HTML，
           连"指向新指纹的 URL"都拿不到，指纹就白加了。

        ③ **指纹显示在页面上**（panel.js 自己读 `?v=`）：任何截图都能
           自证"你看的是哪个版本"，不再靠猜。
        """
        return HTMLResponse(
            PANEL_HTML.replace("__PANEL_VER__", _static_ver()),
            headers={"Cache-Control": "no-cache"},
        )

    async def static_css(request: Any) -> Any:
        p = STATIC_DIR / "panel.css"
        if not p.exists():
            return JSONResponse({"error": "panel.css missing"}, status_code=500)
        # URL 里带 `?v=<指纹>`（见 _static_ver）→ 内容变了 URL 就变，
        # 所以这里可以放心长缓存；旧 URL 缓存旧内容也是正确行为。
        return FileResponse(p, media_type="text/css", headers={
            "Cache-Control": "public, max-age=31536000, immutable"})

    async def static_js(request: Any) -> Any:
        p = STATIC_DIR / "panel.js"
        if not p.exists():
            return JSONResponse({"error": "panel.js missing"}, status_code=500)
        # URL 里带 `?v=<指纹>`（见 _static_ver）→ 内容变了 URL 就变，
        # 所以这里可以放心长缓存；旧 URL 缓存旧内容也是正确行为。
        return FileResponse(p, media_type="application/javascript", headers={
            "Cache-Control": "public, max-age=31536000, immutable"})

    async def static_i18n(request: Any) -> Any:
        """语言包。

        ⚠️ 必须免鉴权（与 panel.js 同理）：它由 `<script src>` 加载，
        浏览器不会带 Authorization 头，首次访问也还没有 cookie。
        要鉴权的话 panel.js 里的 `t()` 会全部 ReferenceError。

        而且它**不含任何数据**（只有 UI 文案），豁免是安全的。
        """
        p = STATIC_DIR / "i18n.js"
        if not p.exists():
            return JSONResponse({"error": "i18n.js missing"}, status_code=500)
        return FileResponse(p, media_type="application/javascript")

    def _envelope(fn: Any) -> Any:
        """把异常转成结构化 JSON（不吐堆栈给浏览器）。"""
        try:
            return JSONResponse(fn())
        except Exception as exc:  # noqa: BLE001
            logger.exception("面板接口失败")
            return JSONResponse(
                {"ok": False, "message": f"{type(exc).__name__}: {exc}"[:300]},
                status_code=500,
            )

    # ────────── 数据 ──────────

    async def state(request: Any) -> Any:
        """左栏与顶栏的全部数据。

        身份来自 contextvar（中间件注入），**不接受请求参数指定用户**。
        """
        user = require_user()
        q = request.query_params.get("q", "")
        tag = request.query_params.get("tag", "")
        ws = request.query_params.get("workspace", "")
        lib = request.query_params.get("lib", "")
        # v2（S-7.2）：生效域三档 chip。默认 long-term —— 与 agent 的默认闸一致，
        # 否则面板会显示别的会话的任务过程态（那正是防串台要挡的东西）。
        scope_req = request.query_params.get("scope", "long-term").strip() or "long-term"

        def build() -> dict[str, Any]:
            me = hub.whoami(user)
            if q or tag or ws or lib:
                found = hub.search(
                    user, q, lib_id=lib,
                    tags=[tag] if tag else None,
                    workspace=ws, limit=200,
                )
                notes = found.get("results", [])
            else:
                notes = hub.list_notes(user, limit=300, lib_id=lib).get("notes", [])

            notes = _filter_by_scope(notes, scope_req)

            # 标签统计只算当前可见范围（全库统计会泄露无权库的规模）
            tag_counts: dict[str, int] = {}
            ws_counts: dict[str, int] = {}
            for n in notes:
                for t in (n.get("tags") or []):
                    if not str(t).startswith("ws/"):
                        tag_counts[t] = tag_counts.get(t, 0) + 1
                w = n.get("workspace") or "(无工作区)"
                ws_counts[w] = ws_counts.get(w, 0) + 1

            libs = me.get("libraries", [])
            # ── 顶栏统计：优先直接读**当前选中库**的数字 ──
            #
            # ⚠️ 原来的写法是「把每个库的 notes 加起来」，但只在
            # `not lib`（未选库）时才算，选了库反而 total=0。
            # 而且跨库求和要 open 每个库的 Vault —— 有缓存还好，
            # 但语义上「当前筛选范围内有多少条」才是用户要的。
            if lib:
                st_now = hub.stats(user, lib_id=lib)
                total = st_now.get("notes", 0)
            else:
                total = len(notes)

            # ── 未筛选总量（"4 / 70" 里的 70）──
            #
            # ⚠️ 为什么必须给（2026-10-15 用户实测暴案）：用户点了一个标签，
            # 顶栏显示"4 条记忆"，于是认定"我这两天的记忆全没了，有 bug"。
            # 实际 70 条一条没丢 —— 界面把**筛后数字当总数**显示，
            # 还没告诉用户筛了什么。前端拿这个值显示 "4 / 70"。
            #
            # 只在真有筛选时多算一次（否则白付一次全库列举的成本）。
            notes_all = total
            if q or tag or ws:
                notes_all = len(
                    hub.list_notes(user, limit=300).get("notes") or []
                )

            # ── git 提交历史（版本历史区块用）──
            #
            # ⚠️ 必须显式 with_commits=True：另一个 agent 做过性能优化，
            # stats 默认**不再返回** commits（每次 git log 要 fork 子进程
            # 约 60ms，而 stats 的核心用途不依赖它）。
            #
            # 但那次的优化**漏了面板**：这里读 `r.commits` 的旧契约，
            # 结果详情页的「版本历史」永远显示「最近 0 次提交」。
            # 这就是接口变更没同步调用方的典型断点。
            commits: list[dict] = []
            commit_lib = lib or (libs[0]["id"] if libs else "")
            if commit_lib:
                try:
                    commits = hub.stats(
                        user, lib_id=commit_lib, with_commits=True
                    ).get("commits") or []
                except Exception as exc:  # noqa: BLE001
                    logger.warning("取 git 历史失败（%s）: %s", commit_lib, exc)

            return {
                "ok": True,
                "me": me,
                "notes": notes,
                "tags": sorted(tag_counts.items(), key=lambda kv: -kv[1])[:20],
                "workspaces": sorted(ws_counts.items(), key=lambda kv: -kv[1]),
                "libs": libs,
                "commits": commits,
                "stats": {
                    "notes": total,
                    "notes_all": notes_all,
                    "libs": len(libs),
                    "tags": len(tag_counts),
                    "workspaces": len(ws_counts),
                },
            }

        return _envelope(build)

    async def note(request: Any) -> Any:
        user = require_user()
        ident = request.query_params.get("ident", "")
        lib = request.query_params.get("lib", "")
        def build() -> dict[str, Any]:
            r = hub.get(user, ident, lib_id=lib)
            # ⚠️ 把 _lib / _lib_label 提到顶层，别只藏在 note 里。
            #
            # 原因：面板详情页要显示「这条来自哪个库」（多租户下的
            # 基本信息），而 `list_notes`/`search` 返回的条目是**带**
            # 这两个字段的，`get` 却不带 —— 两条路径不一致，
            # 结果详情页的来源标记永远是空。
            #
            # 实测被收尾脚本抓到（列表有标记、详情页没有）。
            note_obj = r.get("note") or {}
            lib_id = r.get("_lib") or note_obj.get("_lib") or lib
            if lib_id:
                r["_lib"] = lib_id
                lib_obj = hub.store.lib(lib_id)
                r["_lib_label"] = (lib_obj.label if lib_obj else lib_id)
            return r
        return _envelope(build)

    async def view(request: Any) -> Any:
        user = require_user()
        return _envelope(lambda: hub.view(
            user,
            request.query_params.get("kind", "overview"),
            lib_id=request.query_params.get("lib", ""),
            workspace=request.query_params.get("workspace", ""),
            tag=request.query_params.get("tag", ""),
            limit=int(request.query_params.get("limit", "200") or 200),
        ))

    async def views(request: Any) -> Any:
        user = require_user()
        libs = hub.tenant.readable(user)
        if not libs:
            return JSONResponse({"ok": True, "views": []})
        # 视图目录与库无关，取任一可读库的即可
        return _envelope(lambda: hub.service_for(libs[0].id).views())

    async def backlinks(request: Any) -> Any:
        user = require_user()
        return _envelope(lambda: hub.backlinks(
            user, request.query_params.get("title", ""),
            lib_id=request.query_params.get("lib", ""),
        ))

    async def health(request: Any) -> Any:
        """健康检查 —— 给守护脚本 / 监控 / 容器探针用。

        只返回判定所需的最小信息，有明确的 ok 布尔与 problems 列表：

            {"ok":true,"problems":[],"notes":42,"queue":{...},"uptime_seconds":3600}

        判定标准（任一不满足则 ok=false）：
          · 写者循环*该在的时候*活着（区分"还没创建"与"崩了"）
          · notes 目录可写（**用一次真实写入探测**，不是 os.access 猜）
          · 写入队列未积压到容量上限

        ⚠️ **鉴权与绑定**：
          · 绑公网（auth.required）→ 需要凭据（无凭据的探活会泄露"这里有服务"）
          · 绑 loopback（未启用鉴权）→ 中间件放行，这里按**全部库**聚合

        判定逻辑只有一份（``SharedMemoryService.health`` → ``TenantHub.health``），
        本路由只做鉴权分支与转发 —— 不在 HTTP 层重写判定（重写必然漂移，
        实测踩过：早期这里的实现是个永远 ok=true 的 stub，与文档承诺不符）。
        """
        user = current_user()  # 可能为空（loopback 未鉴权路径）
        return _envelope(lambda: hub.health(user))

    # ────────── 身份 ──────────

    async def whoami(request: Any) -> Any:
        return _envelope(lambda: hub.whoami(require_user()))

    async def login(request: Any) -> Any:
        """用「用户名 + 密码」或「长期 token」换一次性登录票据。

        ━━━ 为什么用票据而不是直接给 cookie ━━━

        cookie 要通过响应设置，而这条请求本身是 fetch —— 直接 set-cookie
        也可以，但那样密码就没必要经过这一步。用票据的好处是：
        **长期凭据只在这一次 POST body 里出现**，之后浏览器只用
        httpOnly cookie，且 URL 里带的是**一次性、2 分钟过期**的票据。

        这样即使有人看到 URL，也几乎拿不到任何东西。
        """
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "message": "请求体不是合法 JSON"},
                                status_code=400)

        client = request.client.host if request.client else "?"
        username = str((body or {}).get("username", "")).strip()
        password = str((body or {}).get("password", ""))
        token = str((body or {}).get("token", "")).strip()
        pin = str((body or {}).get("pin", "")).strip()

        user = ""

        # ① token 登录（agent/脚本常用）
        if token:
            user, _scope = auth.resolve(token) if auth else ("", "")
            if not user:
                auth.note_failure(client) if auth else None

        # ①b PIN（面板主人用；v2 · S-6.1）
        #
        # 锁定期间**连正确 PIN 也拒绝** —— 否则"锁"只是延迟一次尝试，
        # 挡不住穷举。剩余秒数回给前端，让用户知道等多久（不是干等）。
        elif pin and auth and auth.pin_enabled():
            locked, wait = auth.pin_locked()
            if locked:
                return JSONResponse(
                    {"ok": False, "error": "pin_locked", "code": "PIN_LOCKED",
                     "message": f"PIN 连续输错，已锁定，请 {int(wait)} 秒后再试"},
                    status_code=429, headers={"Retry-After": str(int(wait))},
                )
            if auth.verify_pin(pin):
                user = auth.master_key_user() or "admin"
            else:
                auth.note_failure(client)
                logger.warning("PIN 错误（来源 %s）", client)

        # ② 用户名 + 密码
        elif username and password:
            u = hub.store.user(username)
            if u and not u.disabled and u.check_password(password):
                user = u.name
            else:
                if auth:
                    auth.note_failure(client)
                logger.warning("登录失败（用户 %s，来源 %s）", username, client)

        if not user:
            return JSONResponse({"ok": False, "message": "凭据无效"},
                                status_code=401)

        ticket = auth.issue_ticket(user, source=client) if auth else ""
        logger.info("登录成功: %s（来源 %s）", user, client)
        return JSONResponse({
            "ok": True,
            "user": user,
            "login_url": f"/?login={ticket}",
            "expires_in": 120,
        })

    # ────────── 备份与恢复（v2 · S-7.3②）──────────
    #
    # 为什么要走面板而不是让用户跑命令：备份是"**出事之后**才想起要做"的事，
    # 而那时候用户最没耐心去翻文档找命令。

    async def backup_export(request: Any) -> Any:
        """一键备份：导出当前用户**可读**的库。

        纯读操作（不写任何库）—— 所以只读凭据也能用。
        产物落在 ``<home>/exports/``，**在 vault 之外**（不会被 git 带走）。
        """
        from .tools_tenant import TenantTools

        user = require_user()
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        lib = str((body or {}).get("lib", "")).strip()
        return JSONResponse(await TenantTools(hub).memory_export(user, "", lib, ""))

    async def backups_list(request: Any) -> Any:
        """列出已有备份（时间倒序，最多 30 条）。"""
        import time as _t

        out_dir = hub.home / "exports"
        items: list[dict[str, Any]] = []
        if out_dir.is_dir():
            for p in sorted(
                out_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True
            ):
                if not p.is_dir():
                    continue
                count = None
                manifest = p / "manifest.json"
                if manifest.exists():
                    try:
                        data = json.loads(manifest.read_text(encoding="utf-8"))
                        count = len(data.get("notes") or [])
                    except (OSError, ValueError):
                        count = None
                st = p.stat()
                items.append({
                    "name": p.name,
                    "count": count,
                    "size_kb": round(st.st_size / 1024, 1),
                    "when": _t.strftime("%Y-%m-%d %H:%M", _t.localtime(st.st_mtime)),
                })
        return JSONResponse({"ok": True, "items": items[:30]})

    async def backup_restore(request: Any) -> Any:
        """从备份恢复（写路径，走队列串行；默认 skip 不覆盖同名）。

        ⚠️ **只允许恢复 exports 目录下的 bundle** —— 前端传来的名字拼进
        路径前必须做包含校验，否则 ``../../etc`` 能让这个接口去读
        服务器上任意目录（这不是理论风险，是这类接口最常见的洞）。
        """
        from .tools_tenant import TenantTools

        user = require_user()
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "message": "请求体不是合法 JSON"},
                                status_code=400)
        name = str((body or {}).get("bundle", "")).strip()
        target = str((body or {}).get("target_lib", "")).strip()
        on_conflict = str((body or {}).get("on_conflict", "skip")).strip()
        if on_conflict not in ("skip", "update", "rename"):
            on_conflict = "skip"

        base = (hub.home / "exports").resolve()
        bundle = (base / name).resolve()
        if not str(bundle).startswith(str(base)) or not bundle.is_dir():
            return JSONResponse(
                {"ok": False, "message": "备份不存在（或名字越出了备份目录）"},
                status_code=404,
            )
        return JSONResponse(
            await TenantTools(hub).memory_import(user, str(bundle), target, "", on_conflict)
        )

    async def governance_view(request: Any) -> Any:
        """治理体检（v2 · S-11.5）—— **只出报告，不动任何数据**。

        红线：这个接口以及它背后的 governance 模块里**没有删除路径**。
        自动清理是最容易把人的东西弄丢的一类功能：它在你没看着的时候跑，
        出错了也没有报错弹给你。所以治理只算账、列候选、给建议。
        """
        from . import governance as _gov

        user = require_user()
        notes = hub.list_notes(user, limit=2000).get("notes", [])
        report = _gov.capacity_report(notes, _gov.load_refs(hub.home))
        return JSONResponse({"ok": True, **report})

    # ────────── 上下文条 / 会话卡（v2 · S-7.2）──────────

    async def context_view(request: Any) -> Any:
        """把「agent 开工时会被自动喂什么」摊开给用户看。

        ━━━ 这一条的价值全在透明 ━━━

        用户真实反馈过"我不知道它到底记得什么、又被喂了什么"。
        自动上下文是 v2 最"隐形"的机制 —— 越是自动的东西越要能看见，
        否则一旦它喂错了东西（比如把别的会话的过程态当口径），
        用户连"该怎么查"都不知道。
        """
        from . import session as _session

        user = require_user()
        entries = [
            v for v in _session.load(hub.home).values()
            if v.get("user") == user and v.get("status") == "active"
        ]
        entries.sort(key=lambda v: str(v.get("updated", "")), reverse=True)
        active = entries[0] if entries else None

        # 约束清单：type 会被写成同名标签（见 config._infer_tags），按标签收紧。
        # 用 list_notes + 本地过滤而不是 search —— 空查询下 search 的
        # 语义（能不能只靠标签命中）不该被这里依赖。
        try:
            notes = hub.list_notes(user, limit=500).get("notes", [])
            constraints = [
                {"title": n.get("title", ""), "lib": n.get("_lib_label", "")}
                for n in notes
                if "constraint" in {str(t) for t in (n.get("tags") or [])}
            ][:20]
        except Exception:  # noqa: BLE001
            constraints = []

        return JSONResponse({
            "ok": True,
            "session": ({
                "task": active.get("task", ""),
                "space": active.get("space", ""),
                "visibility": active.get("visibility", "private"),
                "started": active.get("started", ""),
                "updated": active.get("updated", ""),
            } if active else None),
            "constraints": constraints,
            "scope_chips": list(PANEL_SCOPE_CHIPS),
        })

    async def session_visibility(request: Any) -> Any:
        """改当前会话的可见性（三档）。**只动会话条目本身**，不碰记忆内容。"""
        from . import session as _session

        user = require_user()
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "message": "请求体不是合法 JSON"},
                                status_code=400)
        v = str((body or {}).get("visibility", "")).strip()
        task = str((body or {}).get("task", "")).strip()
        if v not in ("private", "space", "shared"):
            return JSONResponse(
                {"ok": False, "message": "可见性只能是 private / space / shared"},
                status_code=400,
            )
        if not task:
            return JSONResponse({"ok": False, "message": "要指定会话（task）"},
                                status_code=400)
        try:
            entry = _session.start(hub.home, user, task, visibility=v)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "visibility": entry.get("visibility", v)})

    # ────────── 主密钥与 PIN（v2 · S-6）──────────

    async def pin_status(request: Any) -> Any:
        """PIN 状态（登录屏据此决定显示 PIN 盒、还是用户名密码、还是直进）。"""
        locked, wait = auth.pin_locked() if auth else (False, 0.0)
        return JSONResponse({
            "ok": True,
            "enabled": bool(auth and auth.pin_enabled()),
            "locked": locked,
            "wait_seconds": int(wait),
            "panel_open": bool(auth and auth.panel_open()),
        })

    async def pin_set(request: Any) -> Any:
        """设置 / 关闭面板 PIN（**仅 admin** —— 它决定谁能进面板）。"""
        user = require_user()
        if not hub.whoami(user)["user"]["is_admin"]:
            return JSONResponse({"ok": False, "message": "只有管理员能改 PIN"},
                                status_code=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "message": "请求体不是合法 JSON"},
                                status_code=400)
        action = str((body or {}).get("action", "set")).strip()
        if action == "disable":
            changed = auth.disable_pin() if auth else False
            return JSONResponse({
                "ok": True, "changed": changed, "enabled": False,
                "message": "PIN 已关闭" + (
                    "" if (auth and auth.panel_open()) else
                    "；但服务绑定可公开到达，面板仍需凭据"
                ),
            })
        try:
            auth.set_pin(str((body or {}).get("pin", "")))
        except ValueError as exc:
            # 直说要求（4~8 位数字），别让用户猜
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "enabled": True})

    async def master_status(request: Any) -> Any:
        return JSONResponse({
            "ok": True,
            "enabled": bool(auth and auth.master_key_enabled()),
            "owner": auth.master_key_user() if auth else "",
        })

    async def master_post(request: Any) -> Any:
        """生成 / 轮换主密钥（**仅 admin**）。

        ⚠️ 明文**只在这一次响应里**出现 —— 盘上只存哈希，丢了只能轮换。
        """
        user = require_user()
        if not hub.whoami(user)["user"]["is_admin"]:
            return JSONResponse({"ok": False, "message": "只有管理员能改主密钥"},
                                status_code=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "message": "请求体不是合法 JSON"},
                                status_code=400)
        action = str((body or {}).get("action", "generate")).strip()
        if action == "rotate":
            key = auth.rotate_master_key()
        elif action == "set":
            given = str((body or {}).get("key", "")).strip()
            if len(given) < 8:
                return JSONResponse(
                    {"ok": False, "message": "主密钥至少 8 个字符（太短挡不住枚举）"},
                    status_code=400,
                )
            key = auth.set_master_key(given)
        else:
            key = auth.set_master_key()
        return JSONResponse({
            "ok": True,
            "action": action,
            "key": key,
            "grace_seconds": MASTER_KEY_GRACE_SECONDS if action == "rotate" else 0,
            "message": "明文只出现这一次：请立即存进客户端凭据管理器。盘上只存哈希。",
        })

    # ────────── 凭据管理 ──────────

    async def logout(request: Any) -> Any:
        """退出登录：吊销当前会话 cookie。

        ━━━ 为什么服务端要吊销（而不是让浏览器删 cookie）━━━

        浏览器删 cookie 只是"我这台设备忘了" —— 服务端那把
        会话凭据**仍然有效**，谁再拿到它照样能进来。

        服务端吊销才是真的断掉。这正是我们对待凭据的一致口径：
        **能吊销的必须吊销，不能只是"看不见"**。
        """
        cookie_hdr = request.headers.get("cookie", "")
        sess = ""
        for part in cookie_hdr.split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE_NAME:
                sess = v
                break
        revoked = 0
        if sess and auth is not None:
            # 按 token 值吊销（比按 label 精确）
            for tok in auth._tokens:
                if tok.active and tok.token == sess:
                    tok.revoked_at = time.time()
                    revoked += 1
                    break
        resp = JSONResponse({"ok": True, "revoked": revoked})
        # 同时让浏览器删掉 cookie（双保险）
        resp.delete_cookie(COOKIE_NAME, path="/")
        logger.info("退出登录：吊销 %d 个会话", revoked)
        return resp

    async def change_password(request: Any) -> Any:
        """改自己的密码。

        ━━━ 安全要点 ━━━

        1. **必须验旧密码** —— 否则一个 XSS 或借用会话就能把主人锁外面
        2. **改完吊销其它会话**（可选，这里保守地只保留当前会话）——
           但至少要**改哈希本身**，让旧密码立即失效
        3. 长度下限：短密码能被暴力破解，而这里的限速窗口是 5 分钟

        密码哈希由 ``User.hash_password`` 生成（scrypt + 随机盐），
        入库后原密码立刻失效。
        """
        user_name = require_user()
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "message": "请求体不是合法 JSON"},
                                status_code=400)
        cur = str((body or {}).get("current", ""))
        new = str((body or {}).get("password", ""))
        confirm = str((body or {}).get("confirm", ""))

        if not cur:
            return JSONResponse({"ok": False, "message": "要先填当前密码"},
                                status_code=400)
        if len(new) < 8:
            return JSONResponse({"ok": False, "message": "新密码至少 8 位"},
                                status_code=400)
        if new != confirm:
            return JSONResponse({"ok": False, "message": "两次输入的新密码不一致"},
                                status_code=400)

        u = hub.store.user(user_name)
        if u is None:
            return JSONResponse({"ok": False, "message": "用户不存在"}, status_code=404)
        # ⚠️ 校验旧密码 —— 不能省（否则会话被借用就能改密码夺号）
        if not u.check_password(cur):
            if auth is not None:
                client = request.client.host if request.client else ""
                auth.note_failure(client)
            return JSONResponse({"ok": False, "message": "当前密码不正确"},
                                status_code=403)

        u.password = User.hash_password(new)
        hub.store.save()
        logger.warning("用户 %s 修改了密码", user_name)
        return JSONResponse({"ok": True, "message": "密码已修改"})

    async def tokens_list(request: Any) -> Any:
        """列出 token 元信息。**永不返回明文**（明文等于再次泄露）。"""
        user = require_user()
        me = hub.whoami(user)
        if not me["user"]["is_admin"]:
            return JSONResponse({"ok": False, "message": "需要管理员权限"},
                                status_code=403)
        return JSONResponse({"ok": True, "tokens": auth.list_tokens() if auth else []})

    async def token_create(request: Any) -> Any:
        """为新设备/新 agent 发一把 token。**明文只返回这一次。**"""
        user = require_user()
        me = hub.whoami(user)
        if not me["user"]["is_admin"]:
            return JSONResponse({"ok": False, "message": "需要管理员权限"},
                                status_code=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "message": "请求体不是合法 JSON"},
                                status_code=400)
        target = str((body or {}).get("user", "")).strip()
        label = str((body or {}).get("label", "")).strip() or "未命名"
        scope = str((body or {}).get("scope", "readwrite")).strip()
        if not hub.store.user(target):
            return JSONResponse({"ok": False, "message": f"用户不存在：{target}"},
                                status_code=400)
        try:
            tok = auth.add_token(user=target, label=label, scope=scope)
        except OSError as exc:
            logger.exception("token 发放失败：持久化不可用")
            return JSONResponse({
                "ok": False,
                "message": "token 未发放：凭据文件无法安全保存。请检查服务数据目录权限和磁盘空间后重试。",
            }, status_code=503)
        return JSONResponse({
            "ok": True, "token": tok.token, "user": target,
            "label": tok.label, "scope": tok.scope,
            "note": "**只显示这一次**，请立刻复制。之后只显示指纹。",
        })

    async def token_revoke(request: Any) -> Any:
        user = require_user()
        me = hub.whoami(user)
        if not me["user"]["is_admin"]:
            return JSONResponse({"ok": False, "message": "需要管理员权限"},
                                status_code=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "message": "请求体不是合法 JSON"},
                                status_code=400)
        target = str((body or {}).get("target", "")).strip()
        try:
            n = auth.revoke_token(target) if auth else 0
        except OSError as exc:
            logger.exception("token 吊销失败：持久化不可用")
            return JSONResponse({
                "ok": False,
                "message": "吊销未生效：凭据文件无法安全更新，本次变更已回滚。请检查服务数据目录权限和磁盘空间后重试。",
            }, status_code=503)
        return JSONResponse({"ok": n > 0, "revoked": n,
                             "message": f"已吊销 {n} 把" if n else "没找到匹配的 token"})

    # ────────── 工作空间（共享控制）──────────

    async def workspaces(request: Any) -> Any:
        """列出工作空间及其共享范围。

        ⚠️ 身份来自 contextvar（中间件注入），**不接受请求参数指定用户**。
        """
        user = require_user()
        return _envelope(lambda: hub.workspace.list_for(user))

    async def ws_create(request: Any) -> Any:
        """建工作空间。默认**仅自己**（最安全的默认值）。"""
        user = require_user()
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "message": "请求体不是合法 JSON"},
                                status_code=400)
        try:
            return JSONResponse(hub.workspace.create(
                user,
                workspace=str((body or {}).get("workspace", "")).strip(),
                share=str((body or {}).get("share", "")).strip(),
                label=str((body or {}).get("label", "")).strip(),
            ))
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "message": str(exc)[:300]},
                                status_code=400)

    async def ws_share(request: Any) -> Any:
        """改共享范围 —— **能决定谁能看到内容**，所以权限检查最严。"""
        user = require_user()
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "message": "请求体不是合法 JSON"},
                                status_code=400)
        try:
            return JSONResponse(hub.workspace.share(
                user,
                lib=str((body or {}).get("lib", "")).strip(),
                share=str((body or {}).get("share", "")).strip(),
            ))
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "message": str(exc)[:300]},
                                status_code=403)

    # ────────── 用户与库管理（管理员）──────────

    async def users_list(request: Any) -> Any:
        user = require_user()
        me = hub.whoami(user)
        if not me["user"]["is_admin"]:
            return JSONResponse({"ok": False, "message": "需要管理员权限"},
                                status_code=403)
        return JSONResponse({
            "ok": True,
            "users": [
                {"name": u.name, "display": u.display, "role": u.role,
                 "groups": u.groups, "disabled": u.disabled}
                for u in hub.store.users.values()
            ],
            "libs": [
                {"id": l.id, "label": l.label, "owner": l.owner,
                 "kind": l.kind, "writable_by_members": l.writable_by_members,
                 "hidden_from_agents": l.hidden_from_agents}
                for l in hub.store.all_libs()
            ],
        })

    async def user_add(request: Any) -> Any:
        """加用户。密码留空则随机生成（**明文只返回这一次**）。"""
        user = require_user()
        me = hub.whoami(user)
        if not me["user"]["is_admin"]:
            return JSONResponse({"ok": False, "message": "需要管理员权限"},
                                status_code=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "message": "请求体不是合法 JSON"},
                                status_code=400)
        name = str((body or {}).get("name", "")).strip()
        if not name:
            return JSONResponse({"ok": False, "message": "缺少用户名"},
                                status_code=400)
        u, plain = hub.store.add_user(
            name,
            str((body or {}).get("password", "")),
            role=str((body or {}).get("role", "member")),
            groups=list((body or {}).get("groups") or []),
            display=str((body or {}).get("display", "")),
        )
        if u is None:
            return JSONResponse({"ok": False, "message": plain or "创建失败"},
                                status_code=400)
        return JSONResponse({
            "ok": True, "user": u.name, "password": plain,
            "note": "密码**只显示这一次**" + ("（随机生成）" if not body.get("password") else ""),
        })

    async def user_remove(request: Any) -> Any:
        """删用户 —— **连带吊销他的全部 token**。

        ━━━ 为什么这一步不能省（实测踩过）━━━

        清掉测试用户后，token 列表里还挂着两把属于已删用户的 token，
        状态显示"有效"。虽然权限计算会拒绝（查不到用户），
        但那个状态本身是错的 —— 排查时容易误判成权限系统坏了。
        """
        user = require_user()
        me = hub.whoami(user)
        if not me["user"]["is_admin"]:
            return JSONResponse({"ok": False, "message": "需要管理员权限"},
                                status_code=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "message": "请求体不是合法 JSON"},
                                status_code=400)
        name = str((body or {}).get("name", "")).strip()
        if not name:
            return JSONResponse({"ok": False, "message": "缺少 name"},
                                status_code=400)
        # ⚠️ 自己删自己会导致无人能管理 —— 明确拒绝
        if name == user:
            return JSONResponse(
                {"ok": False, "message": "不能删除当前登录的自己"},
                status_code=400,
            )
        result = hub.remove_user(name, auth=auth)
        return JSONResponse(result, status_code=200 if result.get("ok") else 400)

    async def lib_remove(request: Any) -> Any:
        """移除工作空间/库的登记（**不删文件** —— 可能有真实记忆）。

        这是"取消登记"而不是"删除数据"：
        目录与 git 历史都保留，只是不再出现在列表里、不再可访问。
        真要删数据必须人工处理目录 —— 那是有意为之的一道闸。
        """
        user = require_user()
        me = hub.whoami(user)
        if not me["user"]["is_admin"]:
            return JSONResponse({"ok": False, "message": "需要管理员权限"},
                                status_code=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "message": "请求体不是合法 JSON"},
                                status_code=400)
        lid = str((body or {}).get("lib", "")).strip()
        lib = hub.store.lib(lid)
        if lib is None:
            return JSONResponse({"ok": False, "message": f"库不存在：{lid}"},
                                status_code=404)
        if lid in ("company",) or lid.startswith("user-"):
            # 系统库不允许移除（company 是全员基础；user-* 是个人库）
            return JSONResponse(
                {"ok": False,
                 "message": f"「{lid}」是系统库，不允许移除（只能移除工作空间库）"},
                status_code=400,
            )
        n = len(hub.libs.vault(lib).all_notes()) if hasattr(hub.libs, "vault") else 0
        del hub.store.libs[lid]
        hub.store.save()
        # 清掉可能存在的 service 缓存（否则下次同名会拿到旧实例）
        hub._services.pop(lid, None)
        logger.warning("用户 %s 移除了库登记 %s（%d 条，文件保留）", user, lid, n)
        return JSONResponse({
            "ok": True,
            "lib": lid,
            "notes_kept": n,
            "note": f"已移除登记（{n} 条记忆的**文件保留**在磁盘上）。"
                    "目录：tenant-home/libs/" + lid,
        })

    async def lib_add(request: Any) -> Any:
        """建库。owner 格式：``company`` / ``group:<组名>`` / ``user:<用户名>``。"""
        user = require_user()
        me = hub.whoami(user)
        if not me["user"]["is_admin"]:
            return JSONResponse({"ok": False, "message": "需要管理员权限"},
                                status_code=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "message": "请求体不是合法 JSON"},
                                status_code=400)
        lib = hub.store.add_lib(
            str((body or {}).get("id", "")).strip(),
            label=str((body or {}).get("label", "")),
            owner=str((body or {}).get("owner", "company")),
            writable_by_members=bool((body or {}).get("writable_by_members", True)),
            hidden_from_agents=bool((body or {}).get("hidden_from_agents", False)),
        )
        if lib is None:
            return JSONResponse({"ok": False, "message": "库已存在或 id 非法"},
                                status_code=400)
        return JSONResponse({"ok": True, "lib": {"id": lib.id, "label": lib.label,
                                                 "owner": lib.owner}})

    # ────────── 配置 ──────────

    async def config_get(request: Any) -> Any:
        user = require_user()
        me = hub.whoami(user)
        libs = hub.tenant.readable(user)
        if not libs:
            return JSONResponse({"ok": True, "instructions": "", "rules": {},
                                 "settings": {}, "dir": ""})
        svc = hub.service_for(libs[0].id)
        snap = svc.config_snapshot()
        snap["_lib"] = libs[0].id
        snap["_can_edit"] = me["user"]["is_admin"]
        return _envelope(lambda: snap)

    async def config_post(request: Any) -> Any:
        """保存配置。

        ⚠️ 权限：只有 admin 能改 —— 因为 instructions 会推给**所有**接入的
        agent，改它等于影响每个人的 agent 行为。
        """
        user = require_user()
        me = hub.whoami(user)
        if not me["user"]["is_admin"]:
            return JSONResponse(
                {"ok": False, "message": "只有管理员能改服务配置"}, status_code=403)

        raw_len = request.headers.get("content-length", "")
        if raw_len.isdigit() and int(raw_len) > MAX_CONFIG_BODY_BYTES:
            return JSONResponse(
                {"ok": False, "message": f"请求体过大（上限 {MAX_CONFIG_BODY_BYTES} 字节）"},
                status_code=413)

        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "message": f"请求体解析失败：{exc}"[:300]},
                                status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "message": "请求体必须是对象"},
                                status_code=400)

        libs = hub.tenant.readable(user)
        if not libs:
            return JSONResponse({"ok": False, "message": "你没有可读的库"},
                                status_code=400)
        svc = hub.service_for(libs[0].id)
        try:
            result = await svc.update_config(
                instructions=body.get("instructions"),
                rules=body.get("rules"),
                settings=body.get("settings"),
            )
            return JSONResponse(result)
        except Exception as exc:  # noqa: BLE001
            # 校验失败的消息里已指明哪一项不合法 —— 直接展示
            return JSONResponse({"ok": False, "message": str(exc)[:400]}, status_code=400)

    async def favicon(request: Any) -> Any:
        return HTMLResponse("", status_code=204)

    return [
        Route("/", dashboard, methods=["GET"]),
        Route("/panel.css", static_css, methods=["GET"]),
        Route("/panel.js", static_js, methods=["GET"]),
        Route("/i18n.js", static_i18n, methods=["GET"]),
        Route("/api/state", state, methods=["GET"]),
        Route("/api/note", note, methods=["GET"]),
        Route("/api/view", view, methods=["GET"]),
        Route("/api/views", views, methods=["GET"]),
        Route("/api/backlinks", backlinks, methods=["GET"]),
        Route("/api/whoami", whoami, methods=["GET"]),
        Route("/api/config", config_get, methods=["GET"]),
        Route("/api/config", config_post, methods=["POST"]),
        # 工作空间（共享控制）—— 非管理员也能用（管自己的）
        Route("/api/workspaces", workspaces, methods=["GET"]),
        Route("/api/workspace/create", ws_create, methods=["POST"]),
        Route("/api/workspace/share", ws_share, methods=["POST"]),
        Route("/api/auth/login", login, methods=["POST"]),
        Route("/api/auth/logout", logout, methods=["POST"]),
        Route("/api/auth/password", change_password, methods=["POST"]),
        Route("/api/auth/tokens", tokens_list, methods=["GET"]),
        Route("/api/auth/pin", pin_status, methods=["GET"]),
        Route("/api/auth/pin", pin_set, methods=["POST"]),
        Route("/api/auth/master-key", master_status, methods=["GET"]),
        Route("/api/auth/master-key", master_post, methods=["POST"]),
        # v2 S-7.3②：备份与恢复（管理页用）
        Route("/api/backup", backup_export, methods=["POST"]),
        Route("/api/backups", backups_list, methods=["GET"]),
        Route("/api/restore", backup_restore, methods=["POST"]),
        # v2 S-7.2：上下文条 / 会话卡
        Route("/api/context", context_view, methods=["GET"]),
        # v2 S-7.4/S-11.5：治理体检
        Route("/api/governance", governance_view, methods=["GET"]),
        Route("/api/session/visibility", session_visibility, methods=["POST"]),
        Route("/api/auth/token/create", token_create, methods=["POST"]),
        Route("/api/auth/token/revoke", token_revoke, methods=["POST"]),
        Route("/api/admin/users", users_list, methods=["GET"]),
        Route("/api/admin/user/add", user_add, methods=["POST"]),
        Route("/api/admin/user/remove", user_remove, methods=["POST"]),
        Route("/api/admin/lib/add", lib_add, methods=["POST"]),
        Route("/api/admin/lib/remove", lib_remove, methods=["POST"]),
        Route("/health", health, methods=["GET"]),
        Route("/favicon.ico", favicon, methods=["GET"]),
    ]
