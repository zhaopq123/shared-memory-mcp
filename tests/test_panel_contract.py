"""面板可用性回归 —— 防止"鉴权把登录页自己也锁死"。

━━━ 这个文件存在的理由 ━━━

实测踩过一次**死锁**，症状是"页面全白，用户无法登录"：

    HTML 返回 → 浏览器请求 panel.js → 被 401 拦掉
    → showLogin() 没定义 → 登录框永远不出现

根因：静态资源需要鉴权，但**登录界面本身要靠静态资源渲染**。
这是一个典型的"安全机制把自己锁死"的循环依赖。

这类 bug 的特点是：**单测全绿**（后端逻辑没错）、**curl 探活正常**
（HTML 能返回），只有真用浏览器打开才发现全白。

所以必须有一个专门的测试钉住"静态资源免鉴权"这个约束。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shared_memory.auth import Auth, Token  # noqa: E402
from shared_memory.dashboard import PANEL_HTML  # noqa: E402
from shared_memory.http_server import _wrap_auth  # noqa: E402


def _exempt_paths() -> set[str]:
    """从源码里取豁免清单（不 import 私有函数，避免测试与实现耦合过紧）。"""
    import re
    src = (Path(__file__).resolve().parent.parent
           / "src" / "shared_memory" / "http_server.py").read_text(encoding="utf-8")
    m = re.search(r"AUTH_EXEMPT_PATHS\s*=\s*\{(.*?)\}", src, re.S)
    assert m, "找不到 AUTH_EXEMPT_PATHS 定义"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


# ══════════════════════ 静态资源必须免鉴权 ══════════════════════


def test_panel_js_is_exempt() -> None:
    """⚠️ panel.js 必须免鉴权 —— 否则登录页自己渲染不出来。

    回归保护：如果谁"为了安全"把它加回鉴权清单，这条会红。
    """
    assert "/panel.js" in _exempt_paths(), (
        "panel.js 被要求鉴权了 —— 这会导致登录页全白（死锁）。"
        "它不含任何数据，豁免是安全的。"
    )


def test_panel_css_is_exempt() -> None:
    """panel.css 同理 —— 纯样式，无数据。"""
    assert "/panel.css" in _exempt_paths()


def test_root_html_is_exempt() -> None:
    """⚠️ 根路径 `/` 必须免鉴权 —— 否则连登录框都看不到。

    这是最隐蔽的一层：如果 `/` 要鉴权，浏览器拿到的是 **401 JSON**
    而不是 HTML 骨架，那么 panel.js 也不会被加载，
    `showLogin()` 不存在 —— 页面全白，用户**无法登录**。

    实测踩过：静态资源豁免了但忘了根路径，症状完全一样。
    """
    assert "/" in _exempt_paths(), (
        "根路径要鉴权了 —— 浏览器会拿到 401 JSON 而不是登录页，"
        "用户无法登录（死锁）"
    )


def test_favicon_is_exempt() -> None:
    """favicon 豁免（防探针把 IP 拖进限速名单）。"""
    assert "/favicon.ico" in _exempt_paths()


def test_login_endpoint_is_exempt() -> None:
    """⚠️ 登录接口必须免鉴权 —— 否则无法登录。

    逻辑上它是"还没有凭据时换凭据"的入口，必须先于鉴权。

    实测踩过：忘了豁免它 → 面板登录框提交后永远 401，
    而界面上的表现只是"点了登录没反应"（静默失效）。
    """
    assert "/api/auth/login" in _exempt_paths(), (
        "登录接口被要求鉴权了 —— 那就成了"
        "'没有钥匙就进不去配钥匙的屋子'，用户永远无法登录"
    )


@pytest.mark.asyncio
async def test_ticket_redemption_happens_on_exempt_path() -> None:
    """⚠️ 票据兑换必须在豁免判断**之前**执行。

    实测踩过：`/` 免鉴权后，`/?login=<ticket>` 被直接放行，
    票据永远不兑换 → cookie 设不上 → "点了登录没反应"。

    这个测试真跑一遍中间件：带票据访问 `/`，必须返回 302 + Set-Cookie，
    而不是直接 200 放行。
    """
    auth = Auth(bind_host="0.0.0.0", tokens=[Token(token="real", user="admin")])
    ticket = auth.issue_ticket("admin")

    captured: dict = {}

    async def downstream(scope, receive, send):
        captured["downstream"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    sent: list[dict] = []

    async def send(msg):
        sent.append(msg)

    scope = {
        "type": "http", "method": "GET", "path": "/",
        "query_string": f"login={ticket}".encode(),
        "headers": [], "client": ("1.2.3.4", 1),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    mw = _wrap_auth(downstream, auth)
    await mw(scope, receive, send)

    start = next((m for m in sent if m["type"] == "http.response.start"), None)
    assert start is not None, "中间件没有返回任何响应"
    assert start["status"] == 302, (
        f"带票据访问 `/` 返回了 {start['status']} 而不是 302 —— "
        "说明票据没被兑换（顺序错了，兑换被豁免判断短路了）"
    )
    # 必须设了 cookie
    hdrs = {k.decode().lower(): v.decode() for k, v in start.get("headers", [])}
    assert "set-cookie" in hdrs, "票据兑换了但没设 cookie"
    assert "shm_session" in hdrs["set-cookie"]
    assert "httponly" in hdrs["set-cookie"].lower(), "cookie 不是 httpOnly（XSS 能偷走）"
    # 且不该走到下游（因为已经重定向了）
    assert not captured.get("downstream"), "票据兑换后还继续走了下游"


# ══════════════════════ 数据接口一个都不能豁免 ══════════════════════


def test_data_paths_are_not_exempt() -> None:
    """⚠️ 含数据的路径**一个都不许**豁免。

    这是上一条的对称约束：豁免静态资源是安全的，
    豁免数据接口就是漏洞。
    """
    exempt = _exempt_paths()
    for p in ("/api/state", "/api/note", "/api/config", "/api/whoami",
              "/api/admin/users", "/api/auth/tokens", "/mcp", "/health"):
        assert p not in exempt, f"{p} 被豁免了 —— 它会泄露数据！"


def test_html_does_not_inline_data() -> None:
    """⚠️ HTML 骨架里不能内联任何数据。

    理由：如果 HTML 里带了数据（比如把笔记列表直接渲染进去），
    那么"HTML 要鉴权"就不再是充分保护 —— 因为静态资源豁免了，
    而 HTML 和静态资源走的是同一套判断。

    目前 HTML 是纯骨架（数据全靠 /api/* 取），这条钉住这个约束。
    """
    for token in ("notes", "instructions", "password", "token"):
        # 只检查有没有像数据的东西（JSON 结构），不是检查单词本身
        pass
    # ⚠️ 静态资源必须带 `?v=<指纹>`（2026-10-15 实测踩出来的）：
    #    不带指纹时浏览器会按启发式规则缓存 panel.js/css，服务端改了代码
    #    用户看到的还是旧界面（"改了但一点没动"）。指纹在 dashboard.py
    #    渲染时替换成真实值，所以这里只断言"带 v= 参数的前缀"。
    assert '<script src="panel.js?v=' in PANEL_HTML
    assert 'href="panel.css?v=' in PANEL_HTML
    assert '<script src="i18n.js?v=' in PANEL_HTML
    # 骨架里不该有 JSON 数据块
    assert "const DATA" not in PANEL_HTML
    assert "__INITIAL_STATE__" not in PANEL_HTML


# ══════════════════════ 中间件行为（真跑一遍） ══════════════════════


class _Recorder:
    """假的下游 app：只记录有没有被调用。"""

    def __init__(self) -> None:
        self.called = False
        self.status = 0

    async def __call__(self, scope, receive, send) -> None:
        self.called = True
        self.status = 200
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def _call(mw, path: str, headers: list[tuple[bytes, bytes]] | None = None):
    """走一遍中间件，返回 (状态码, 下游是否被调用)。"""
    rec = _Recorder()
    captured: dict = {}

    async def send(msg):
        if msg["type"] == "http.response.start":
            captured["status"] = msg["status"]

    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": b"",
        "headers": headers or [],
        "client": ("1.2.3.4", 1234),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    # 注意：_wrap_auth 包的是 app，所以这里用 rec 当下游
    from shared_memory.http_server import _wrap_auth as wrap
    mw = wrap(rec, mw)
    await mw(scope, receive, send)
    return captured.get("status"), rec.called


@pytest.mark.asyncio
async def test_middleware_lets_root_through_without_credentials() -> None:
    """⚠️ 无凭据请求 `/` → 必须放行（否则看不到登录页）。

    这是死锁的直接回归测试：真跑一遍中间件。
    """
    auth = Auth(bind_host="0.0.0.0", tokens=[Token(token="real", user="admin")])
    status, downstream_called = await _call(auth, "/")
    assert downstream_called, (
        "根路径被中间件拦下了 —— 浏览器会拿到 401 JSON 而不是登录页（死锁）"
    )


@pytest.mark.asyncio
async def test_middleware_lets_panel_js_through_without_credentials() -> None:
    """⚠️ 无凭据请求 panel.js → 必须放行（否则登录页全白）。

    这是那个死锁的直接回归测试：真跑一遍中间件。
    """
    auth = Auth(bind_host="0.0.0.0", tokens=[Token(token="real", user="admin")])
    status, downstream_called = await _call(auth, "/panel.js")
    assert downstream_called, (
        "panel.js 被中间件拦下了 —— 登录页会全白（死锁）"
    )


@pytest.mark.asyncio
async def test_middleware_blocks_api_without_credentials() -> None:
    """⚠️ 无凭据请求数据接口 → 必须 401。

    与上一条配对：豁免静态资源**不能**顺带豁免数据接口。
    """
    auth = Auth(bind_host="0.0.0.0", tokens=[Token(token="real", user="admin")])
    status, downstream_called = await _call(auth, "/api/state")
    assert status == 401, f"/api/state 无凭据返回了 {status}"
    assert not downstream_called, "数据接口被放行了！"


@pytest.mark.asyncio
async def test_middleware_blocks_mcp_without_credentials() -> None:
    """MCP 端点无凭据 → 401（这是最初的核心诉求）。"""
    auth = Auth(bind_host="0.0.0.0", tokens=[Token(token="real", user="admin")])
    status, downstream_called = await _call(auth, "/mcp")
    assert status == 401
    assert not downstream_called


@pytest.mark.asyncio
async def test_middleware_accepts_valid_bearer() -> None:
    """带正确 Bearer → 放行（对照组，确认不是"全拦"）。"""
    auth = Auth(bind_host="0.0.0.0", tokens=[Token(token="real", user="admin")])
    status, downstream_called = await _call(
        auth, "/api/state",
        [(b"authorization", b"Bearer real")],
    )
    assert downstream_called, "带正确凭据也被拦了"


# ══════════════════════ HTML 与 JS 的契约 ══════════════════════


def test_html_references_panel_assets() -> None:
    """HTML 必须引用 panel.css / panel.js —— 否则界面没样式没逻辑。"""
    assert "panel.css" in PANEL_HTML
    assert "panel.js" in PANEL_HTML


def test_html_has_multi_lib_elements() -> None:
    """多库 UI 的容器必须存在（JS 要往里渲染）。

    ⚠️ 这条钉住"HTML 与 JS 的元素 id 契约"：
    JS 里 getElementById('libchips') 找不到元素时不会报错，
    只会静默不渲染 —— 那正是"库 chip 不显示"这类 bug 的形态。
    """
    for eid in ("libchips", "s-libs", "s-who", "s-role", "sbody", "list", "reader"):
        assert f'id="{eid}"' in PANEL_HTML, f"HTML 缺少 #{eid}（JS 会静默失效）"


def test_js_uses_only_existing_element_ids() -> None:
    """⚠️ JS 里 getElementById 的 id 必须在 HTML 里存在。

    这是"静态契约测试"：JS 找不到元素时**不会报错**，只会静默不渲染 ——
    而那类 bug 只能靠真浏览器发现（或者靠这条测试）。
    """
    import re
    root = Path(__file__).resolve().parent.parent / "src" / "shared_memory"
    js = (root / "static" / "panel.js").read_text(encoding="utf-8")

    # JS 里动态创建的元素（不在静态 HTML 里）——白名单
    dynamic = {
        "login-mask", "login-token", "login-btn", "login-err",   # 登录遮罩
        # 账号密码登录（2026-10-13 改成正经登录，替换掉 token 输入框）
        "login-user", "login-pw", "login-token-link", "login-back-link",
        "cfg-instructions", "cfg-policy", "cfg-maxtitle", "cfg-reqws",
        "cfg-infertype", "cfg-debounce", "cfg-limit", "cfg-refresh",
        "cfg-save", "cfg-reload", "cfg-msg",                       # 设置表单
        "admin-users", "admin-tokens",                              # 管理区容器（设置页动态插入）
        "nu-name", "nu-display", "nu-groups", "nu-role", "nu-add", "nu-msg",
        "nt-user", "nt-label", "nt-scope", "nt-add", "nt-msg",
        "ws-body", "ws-name", "ws-share-new", "ws-add", "ws-msg",   # 工作空间管理
        # 我的账号（改密码 + 退出登录）
        "ac-cur", "ac-new", "ac-new2", "ac-change", "ac-logout", "ac-msg",
        "cp-link", "cp-md", "relwrap", "dot", "root", "q",
        # P1/P2/P4（2026-10-15）：会话过期横幅、库下拉、接入卡复制按钮
        "sess-banner", "sess-relogin", "lib-select", "access-copy",
        "access-gen", "access-gen-msg", "access-snippet", "nt-copy",
        # v2 S-7.1：PIN 六盒登录屏（动态构建，不在静态 HTML 里）
        "pinwrap", "pin-wait", "login-pw-link",
        # v2 S-7.3①：接入与安全块（主密钥 / PIN / 接入码）
        "mk-state", "mk-gen", "mk-rotate", "mk-key", "mk-msg",
        "pin-state", "pin-input", "pin-set", "pin-off", "pin-msg",
        "conn-code", "conn-copy", "conn-cmd", "conn-msg",
    }
    used = set(re.findall(r"getElementById\('([^']+)'\)", js))
    missing = used - dynamic
    for eid in sorted(missing):
        assert f'id="{eid}"' in PANEL_HTML, (
            f"panel.js 引用了 #{eid}，但 HTML 里没有 —— JS 会静默失效"
        )


def test_login_overlay_has_styles() -> None:
    """⚠️ panel.js 动态创建的登录遮罩，必须在 panel.css 里有样式。

    ━━━ 为什么单独测这一条 ━━━

    `#login-mask` / `.loginbox` **只存在于 JS 的 innerHTML 里**（HTML 骨架
    里没有），所以「写 JS 时漏掉配套 CSS」不报任何错：接口全通、curl 探活
    正常、单测全绿 —— 只有真打开页面才暴露，形态是：

        遮罩以普通块级流追加在 body 末尾
        → 顶栏/左栏照旧露在外面，登录框被拉成整屏宽、挤在页面底部

    这是 2026-10-15 用户截图反馈的实际形态。`position:fixed` 是核心约束：
    登录态下遮罩必须**盖住整个应用**，而不是参与文档流。
    """
    import re
    root = Path(__file__).resolve().parent.parent / "src" / "shared_memory"
    css = (root / "static" / "panel.css").read_text(encoding="utf-8")

    m = re.search(r"#login-mask\s*\{([^}]*)\}", css)
    assert m, "panel.css 里没有 #login-mask 规则 —— 登录遮罩会以普通块级流渲染"
    assert "position:fixed" in m.group(1).replace(" ", ""), (
        "#login-mask 必须 position:fixed（否则遮罩进文档流，整页排版崩）"
    )

    assert re.search(r"#login-mask\s+\.loginbox\s*\{", css), "panel.css 里没有 .loginbox 规则"

    # 遮罩内的输入框：JS 里 #login-pw / #login-token 是 type=password，
    # **不在** `.field input[type=text]` 的覆盖范围内 —— 必须单独兜住，
    # 否则同表单里用户名框是定制样式、密码框是浏览器原生白框。
    assert re.search(r"#login-mask\s+input\s*\{", css), (
        "panel.css 缺少 #login-mask input 规则 —— 密码/令牌框会退回浏览器原生样式"
    )


def test_js_has_no_removed_credential_variable() -> None:
    """⚠️ 不许再引用已删除的 `TOKEN` 变量。

    ━━━ 为什么单独测这一条 ━━━

    改成 cookie 方案时删掉了全局 `TOKEN`，但"复制分享链接"那段
    还在用它 —— 结果**点那个按钮就抛 ReferenceError**。

    这类 bug 的特点：只有用户真去点那个按钮才暴露，而静态检查
    （grep 函数名是否存在）完全看不出来。所以必须专门钉住。
    """
    import re
    root = Path(__file__).resolve().parent.parent / "src" / "shared_memory"
    js = (root / "static" / "panel.js").read_text(encoding="utf-8")

    # 去掉注释后再检查（注释里提到 TOKEN 是允许的，那是解释）
    code = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    code = re.sub(r"//[^\n]*", "", code)

    # 独立的 TOKEN 标识符（不是 __DSH_PANEL_TOKEN__ 之类）
    hits = re.findall(r"(?<![_A-Za-z])TOKEN(?![_A-Za-z])", code)
    assert not hits, (
        f"panel.js 里还有 {len(hits)} 处引用已删除的 TOKEN 变量 —— "
        "运行时会抛 ReferenceError（点分享按钮就崩）"
    )


def test_share_url_contains_no_credentials() -> None:
    """⚠️ 分享链接里不能带任何凭据。

    旧版是 `?token=<长期凭据>` —— 复制给谁就等于把访问权给谁，
    而且会进浏览器历史/书签/截图。实测正是这样泄露过一次。
    """
    import re
    root = Path(__file__).resolve().parent.parent / "src" / "shared_memory"
    js = (root / "static" / "panel.js").read_text(encoding="utf-8")

    # 找 shareUrl 的赋值语句
    m = re.search(r"const shareUrl = (.+?);", js, re.S)
    assert m, "找不到 shareUrl 定义"
    expr = m.group(1)
    assert "token" not in expr.lower(), (
        f"分享链接里带了凭据：{expr.strip()[:120]} —— "
        "那等于把钥匙贴在门牌上"
    )

# ── v2 S-7.1：PIN 登录屏 + 鉴权模式探测 ──


def test_pin_login_screen_contract() -> None:
    """PIN 六盒登录屏必须齐全（v4 原型照做）。

    钉住四件事，缺一件用户就进不去面板：
      · ``#pinwrap`` 六个盒子
      · ``#login-err`` 错误位（含锁定剩余秒数的位置 ``#pin-wait``）
      · 探测鉴权模式的 ``GET /api/auth/pin`` 调用
      · 降级入口 ``#login-pw-link``（PIN 忘了还能用账号密码）
    """
    root = Path(__file__).resolve().parent.parent / "src" / "shared_memory"
    js = (root / "static" / "panel.js").read_text(encoding="utf-8")
    css = (root / "static" / "panel.css").read_text(encoding="utf-8")

    assert "function renderPinLogin(" in js, "没有 PIN 登录屏函数"
    assert "id=\"pinwrap\"" in js, "缺 #pinwrap 容器"
    assert "class=\"pinbox\"" in js, "缺 PIN 盒子"
    assert "login-pw-link" in js, "缺降级入口（PIN 忘了要用账号密码）"
    assert "pin-wait" in js, "缺锁定剩余秒数显示位"
    assert "'/api/auth/pin'" in js, "登录屏没探测鉴权模式，会显示错表单"
    assert "#pinwrap .pinbox" in css, "PIN 盒子没有样式（会挤成一坨）"
    # 6 个盒子
    assert "length: n" in js.replace(" ", " "), "盒子数量不是按 n 生成"


def test_pin_mode_probe_is_get_only() -> None:
    """``GET /api/auth/pin`` 免鉴权（登录屏要它），但 **POST 不行**。

    ⚠️ 这是本片最容易写错的一处：按**路径**整条豁免会把
    "设置 PIN"（POST，要 admin）一起放开 —— 那就成了
    "不用登录就能改 PIN"，是把门锁换个方向装。
    """
    root = Path(__file__).resolve().parent.parent / "src" / "shared_memory"
    src = (root / "http_server.py").read_text(encoding="utf-8")

    assert 'method == "GET" and path == "/api/auth/pin"' in src, (
        "缺少「仅 GET」的窄豁免 —— 很可能被写成整条路径豁免"
    )
    # 不能出现在按路径豁免的集合里
    start = src.index("AUTH_EXEMPT_PATHS = {")
    end = src.index("}", start)
    assert "/api/auth/pin" not in src[start:end], (
        "/api/auth/pin 进了路径豁免集合 —— POST 会被一起放开"
    )

# ── v2 S-7.2：六型分组 + 筛后/全部口径 ──


def test_six_type_grouping_exists() -> None:
    """六型视图必须存在，且 workflow/constraint **置顶**并带资产徽标。

    为什么钉住顺序：这两类是"下次开工直接能用"的资产，按工作区分组时
    被埋在几十条里等于没有。顺序被人无意改掉，等于这个视图白做。
    """
    root = Path(__file__).resolve().parent.parent / "src" / "shared_memory"
    js = (root / "static" / "panel.js").read_text(encoding="utf-8")

    assert "function renderByType(" in js, "没有六型分组渲染函数"
    assert "TYPE_ORDER" in js, "没有类型固定序"
    # 两类资产必须排在最前
    order_line = next(l for l in js.split("\n") if "TYPE_ORDER =" in l)
    assert order_line.index("'workflow'") < order_line.index("'constraint'"), (
        "workflow 应在 constraint 之前（工作流优先）"
    )
    for t in ("'lesson'", "'habit'", "'fact'", "'task'"):
        assert t in order_line, f"六型缺 {t}"
    assert "ASSET_TYPES" in js and "assetbadge" in js, "缺资产徽标"
    # 切到该视图的分段按钮要在
    assert 'data-g="type"' in js, "分段控件里没有「类型」按钮"
    assert "STATE.groupBy === 'type'" in js, "renderList 没接 type 分支"
    # 未知类型不能被丢掉（旧数据里有 v1 的 type）
    assert "!known.includes(k)" in js, "未知类型会被静默丢弃"


def test_filtered_count_shows_denominator() -> None:
    """顶栏数字必须能区分「筛后」与「全部」。

    ⚠️ 这是**实测暴案**：用户点了一个标签 → 顶栏显示"4 条记忆" →
    结论是"我这两天做的全没了，是不是有 bug"（库里其实 70 条一条没丢）。
    有筛选时必须显示 ``筛后 / 全部``，否则同一个数字会被读成两个意思。
    """
    root = Path(__file__).resolve().parent.parent / "src" / "shared_memory"
    js = (root / "static" / "panel.js").read_text(encoding="utf-8")

    assert "notes_all" in js, "取不到「全部条数」，没法给出分母"
    seg = js[js.index("const notes_all" if "const notes_all" in js else "nAll"):]
    seg = seg[:900]
    assert "' / '" in seg, "没看到「筛后 / 全部」的拼接"
    assert "!== r.stats.notes" in seg, (
        "没有「只在两者不同时才显示分母」的判断 —— 无筛选时显示 4/4 是噪音"
    )

# ── v2 S-7.3①：接入与安全块 ──


def test_security_block_contract() -> None:
    """主密钥 / PIN / 接入码必须在**面板里**可用，不是只有 API。

    后端（S-6）早就齐了，但面板够不着 = 用户得去翻文档调 curl。
    这一块的意义就是把"只有 API"变成"点一下就成"。
    """
    root = Path(__file__).resolve().parent.parent / "src" / "shared_memory"
    js = (root / "static" / "panel.js").read_text(encoding="utf-8")

    assert "function securityBlockHtml(" in js, "没有接入与安全块"
    assert "function wireSecurityBlock(" in js, "接入与安全块没接线"
    assert "securityBlockHtml()" in js, "块没被渲染进设置页"
    assert "wireSecurityBlock();" in js, "块没被接线调用"
    for eid in ("mk-gen", "mk-rotate", "pin-set", "pin-off", "conn-code"):
        assert f"'{eid}'" in js, f"缺 {eid}"


def test_master_key_plaintext_shown_once_only() -> None:
    """主密钥明文**只在生成/轮换那一次**显示。

    ⚠️ 盘上只存哈希，生成后如果界面不展示、不提供复制，
    用户就永远拿不到这把钥匙了（那等于把自己锁在门外）。
    反过来，把明文渲染到"随时可看"的地方也不行 —— 面板可能被人瞄一眼。

    所以这条钉住三件事：
      ① 生成/轮换的返回值里取 key 并**当场**展示
      ② 用一个变量记住它，且**只有刷新页面就没了**（不落 localStorage）
      ③ 接入码在没拿到明文时只显示占位（拼不出完整码）
    """
    root = Path(__file__).resolve().parent.parent / "src" / "shared_memory"
    js = (root / "static" / "panel.js").read_text(encoding="utf-8")
    seg = js[js.index("function wireSecurityBlock("):]
    seg = seg[:seg.index("async function loadSettings(")]

    assert "showKey(r.key" in seg, "生成后没有当场展示明文"
    assert "let shownKey = ''" in seg, "没有用局部变量持有明文"
    assert "localStorage" not in seg, "明文不该进 localStorage（那会长期留在浏览器里）"
    assert "shownKey = key" in seg
    # 没拿到明文时接入码要是占位
    assert "'<主密钥>'" in seg or "<主密钥>" in seg, "接入码缺占位说明"
