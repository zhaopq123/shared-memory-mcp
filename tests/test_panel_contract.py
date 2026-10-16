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
    for p in ("/api/state", "/api/note", "/api/versions", "/api/config",
              "/api/whoami", "/api/admin/users", "/api/auth/tokens",
              "/mcp", "/health"):
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


# ══════════════════════ v2 S-7.8：阅读器的版本历史（按条取） ══════════════════════


def test_reader_version_history_is_per_note_and_read_only() -> None:
    """阅读器必须显示**这一条**的版本历史，而且只列不写。

    ━━━ 为什么要换掉"整库 git log" ━━━

    原型（v4）的这个区块就是按条的：`版本历史` + 一列 sha/时间/说明，
    未标 versioned 时明说"没有版本控制"。生产原来铺的是整库日志，
    还自带一句"这是整个库的历史，不是这一条" —— 那句话本身就承认了
    它没回答用户点开这条时想问的问题（"这条被我改过几次"）。
    S-8 之后普通写入不进版本库，那份日志更是绝大多数时候空着。

    所以钉四件事：
      ① 有区块、有载入占位（缺占位 = 静默空着，老 bug 的形态）
      ② 数据来自 `/api/versions`（后端复用 get() 的可见性闸，见 tenancy.versions）
      ③ 异步回来要验身份 —— 用户已切走时不能把上一条的历史画进当前这条
      ④ **没有任何写入口**（退回某一版走 agent 的工具面，面板是阅览室）
    """
    root = Path(__file__).resolve().parent.parent / "src" / "shared_memory"
    js = (root / "static" / "panel.js").read_text(encoding="utf-8")
    py = (root / "dashboard.py").read_text(encoding="utf-8")

    assert 'id="verwrap"' in js and 'id="ver-body"' in js, "区块或载入占位没了"
    assert "function loadVersions(" in js, "没有按条取版本历史的函数"
    assert "loadVersions(n);" in js, "渲染正文时没调用 —— 区块会永远停在「载入中」"
    assert "api('/api/versions'" in js, "没接 /api/versions"
    # ③ 串条护栏
    assert "STATE.cur !== ident" in js, "异步结果没验当前条目（会把上一条的历史画进来）"
    # ② 后端那一端确实挂在 hub.versions 上，不是自己去摸文件
    assert 'Route("/api/versions", note_versions, methods=["GET"])' in py
    seg = py[py.index("async def note_versions("):]
    seg = seg[:seg.index("async def view(")]
    assert "hub.versions(" in seg, "没走 hub.versions（可见性闸就被绕过）"
    for bad in ("vault", "write_text", ".put("):
        assert bad not in seg, f"版本历史接口里出现了 {bad} —— 它必须纯读"
    # ④ 前端也不许在这里写
    vseg = js[js.index("async function loadVersions("):]
    vseg = vseg[:vseg.index("/* ═══════════════ 标签页 2")]
    assert "apiPost" not in vseg, "版本历史区块里有写调用（阅览室原则破口）"


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
        # v2 S-7.3②：管理页的备份块与指令编辑器
        "adm-backup", "adm-access", "adm-instr",
        "bk-export", "bk-refresh", "bk-list", "bk-msg",
        "instr-save", "instr-msg",
        # v2 S-7.2 收口：上下文条 / 生效域 chip / 会话卡
        "ctxbar", "scopechips", "sesscard",
        # v2 S-7.4：治理抽屉
        "gov-close",
        # v2 S-7.8：阅读器的版本历史区块（随正文动态渲染）
        "ver-body",
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
    assert "securityBlockHtml()" in js, "块没被渲染进「管理 → 接入」"
    assert "wireSecurityBlock();" in js, "块没被接线调用"
    for eid in ("mk-gen", "mk-rotate", "pin-set", "pin-off", "conn-code"):
        assert f"'{eid}'" in js, f"缺 {eid}"

    # ⚠️ 接线只能有一处，而且不能在设置页里
    assert js.count("wireSecurityBlock();") == 1, (
        "wireSecurityBlock() 被调了多次 —— 设置页没有 mk-*、pin-*、conn-* 这些元素，"
        "多调一次会在 `$('mk-gen').onclick` 上抛 TypeError，"
        "把紧随其后的 cfg-save 接线一起打断（实测过的静默失效：设置页照常渲染、"
        "「保存配置」点不动）"
    )
    settings_seg = js[js.index("async function loadSettings("):]
    settings_seg = settings_seg[:settings_seg.index("\n}\n")]
    # 查的是"调用"（带分号），不是标识符 —— 那里留了一段解释为什么不能调的注释
    assert "wireSecurityBlock();" not in settings_seg, (
        "设置页又去接安全块了（那块在「管理 → 接入」）"
    )


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

# ── v2 S-7.2/S-7.3②：三 tab 结构与视图落位 ──


def test_three_tabs_are_memory_admin_settings() -> None:
    """三个顶级 tab 必须是 **记忆 / 管理 / 设置**，且「体系」不再单独成页。

    ━━━ 为什么合并「体系」━━━

    视图（决策/踩坑/待办/时效告警）与记忆列表是同一批数据的两种看法。
    做成两个顶级 tab 的后果很具体：想扫一眼待办得切走，正在读的那条就没了；
    来回切三次以后人就不用了。
    """
    root = Path(__file__).resolve().parent.parent / "src" / "shared_memory"
    html = (root / "dashboard.py").read_text(encoding="utf-8")

    for tab in ('data-tab="library"', 'data-tab="admin"', 'data-tab="settings"'):
        assert tab in html, f"缺 tab {tab}"
    assert 'id="page-admin"' in html, "缺管理页容器"
    assert "page-knowledge" not in html, "「体系」页还在（应已并入记忆页）"
    assert 'data-tab="knowledge"' not in html, "还在用旧的 knowledge tab"
    assert "nav.knowledge" not in html, "还引用着已删的 nav.knowledge 键"

    # 视图元素必须落在记忆页里
    lib_page = html[html.index('id="page-library"'):html.index('id="page-admin"')]
    for eid in ('id="viewbar"', 'id="kbody"'):
        assert eid in lib_page, f"{eid} 不在记忆页里（视图没并进来）"
    # 且只能出现一次（重复 id 会让 getElementById 拿到错的那个）
    assert html.count('id="viewbar"') == 1, "viewbar 重复定义"
    assert html.count('id="kbody"') == 1, "kbody 重复定义"


def test_admin_page_has_four_blocks() -> None:
    """管理页四块：备份与恢复 / 接入 / 空间 / 指令。

    四块都必须在同一个页面容器里 —— 用户找"备份"不该先在设置页翻半天。
    """
    root = Path(__file__).resolve().parent.parent / "src" / "shared_memory"
    html = (root / "dashboard.py").read_text(encoding="utf-8")
    admin = html[html.index('id="page-admin"'):]

    for eid in ('id="adm-backup"', 'id="adm-access"',
                'id="ws-body"', 'id="adm-instr"'):
        assert eid in admin, f"管理页缺 {eid}"


def test_restore_guards_against_path_traversal() -> None:
    """恢复接口**只能**读 exports 目录下的 bundle。

    ⚠️ 这类接口最经典的洞：前端传来的名字直接拼路径 →
    ``../../`` 就能让服务端读走任意目录。所以必须在拼完路径后
    做包含校验（resolve 之后比前缀），而不是只检查"文件存在"。
    """
    root = Path(__file__).resolve().parent.parent / "src" / "shared_memory"
    src = (root / "dashboard.py").read_text(encoding="utf-8")
    seg = src[src.index("async def backup_restore("):]
    seg = seg[:seg.index("async def pin_status(") if "async def pin_status(" in seg else 3000]

    assert "resolve()" in seg, "没有 resolve 后比对（前缀校验形同虚设）"
    assert "startswith(str(base))" in seg, "缺包含校验 —— 路径能越出备份目录"

# ── v2 S-7.2 收口：上下文条 / 生效域 chip / 会话卡 ──


def test_context_bar_scope_chips_and_session_card_exist() -> None:
    """三块"隐形机制的可视化"必须在位。

    它们回答同一个问题：**现在这一屏是谁的角度、看的是什么范围**。
    此前全是隐形自动行为 —— 越是自动的东西越要能看见。
    """
    root = Path(__file__).resolve().parent.parent / "src" / "shared_memory"
    html = (root / "dashboard.py").read_text(encoding="utf-8")
    js = (root / "static" / "panel.js").read_text(encoding="utf-8")

    for eid in ('id="ctxbar"', 'id="scopechips"', 'id="sesscard"'):
        assert eid in html, f"左栏缺 {eid}"
    assert "function loadContextBar(" in js, "没有上下文条渲染函数"
    assert "loadContextBar()" in js, "上下文条没被调用"
    assert "'/api/context'" in js, "没接后端上下文接口"
    assert "session/visibility" in js, "会话卡没接可见性接口"


def test_panel_default_scope_is_long_term() -> None:
    """面板默认生效域必须是 **long-term**（与 agent 的默认闸一致）。

    ⚠️ 这是防串台的界面一侧：``list_notes`` 返回库里**全部**条目，
    默认不过滤的话，面板会把"别人正在做的半成品"和"过期老账"一起摊开。

    最容易写错的一处：**long-term 是缺省值、不输出字段**（见 Note.to_dict），
    所以判断必须按缺省补上 —— 否则默认视图会把最常见的条目全滤掉
    （一个"看起来正常但列表空了"的 bug）。
    """
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from shared_memory.dashboard import _filter_by_scope

    notes = [
        {"title": "长期条"},                                  # 缺省 = long-term
        {"title": "显式长期", "scope": "long-term"},
        {"title": "任务过程态", "scope": "task:某任务"},
        {"title": "老账", "scope": "archived"},
    ]
    lt = [n["title"] for n in _filter_by_scope(notes, "long-term")]
    assert lt == ["长期条", "显式长期"], f"默认视图错了：{lt}"

    task = [n["title"] for n in _filter_by_scope(notes, "task")]
    assert task == ["任务过程态"]

    arc = [n["title"] for n in _filter_by_scope(notes, "archived")]
    assert arc == ["老账"]

    allt = [n["title"] for n in _filter_by_scope(notes, "all")]
    assert len(allt) == 4, "考古模式要能看见全部"

# ── v2 S-7.4 / S-7.6：治理抽屉 + 面板只读原则 ──


def test_governance_drawer_contract() -> None:
    """体检抽屉在位，且**只出报告**。

    体检的四个指标 + 重复候选必须来自真数据（/api/governance），
    不能是写死的演示数字 —— 演示数据和真实数据混在一起时，
    用户没法判断"这个 0 是真的没有，还是没接上"。
    """
    root = Path(__file__).resolve().parent.parent / "src" / "shared_memory"
    html = (root / "dashboard.py").read_text(encoding="utf-8")
    js = (root / "static" / "panel.js").read_text(encoding="utf-8")

    assert 'id="gov-btn"' in html and 'id="gov-drawer"' in html
    assert "function openGovernance(" in js, "没有抽屉渲染函数"
    assert "'/api/governance'" in js, "没接真实体检接口"
    assert "_govBtn.onclick = () => openGovernance()" in js, "按钮没接线"
    # 四个核心指标都要显示
    for k in ("total", "growth_30d", "duplicate_rate", "low_reference_count"):
        assert k in js, f"抽屉缺指标 {k}"


def test_governance_module_has_no_delete_path() -> None:
    """⚠️ 治理模块**不许有删除/改写路径**（红线，代码级确认）。

    理由不是"暂时没写"：自动清理是在你没看着的时候跑的功能，
    出错了也不会弹报错。所以治理只算账、列候选、给建议 ——
    真要动由人或 agent 显式执行。

    这条测试就是那道红线的守门人：以后谁往里加一句 unlink，
    这里会立刻变红。
    """
    root = Path(__file__).resolve().parent.parent / "src" / "shared_memory"
    src = (root / "governance.py").read_text(encoding="utf-8")
    body = "\n".join(
        l for l in src.split("\n") if not l.strip().startswith("#")
    )

    for bad in ("unlink(", "rmtree(", "os.remove(", "shutil.move(", "rename("):
        assert bad not in body, f"governance.py 出现删除/移动路径：{bad}"
    # 也不该反过来写库（那等于偷偷改内容）
    for bad in ("vault.put", "vault.delete", "self.put(", ".write_note("):
        assert bad not in body, f"governance.py 出现写库调用：{bad}"


def test_panel_has_no_direct_memory_write_entry() -> None:
    """面板是**阅览室**：不许有任何直写记忆的入口（v2 · S-7.6）。

    这条不是洁癖：面板直写会绕过 write 队列、绕过校验、绕过审计，
    等于在服务端开一个没有守卫的旁门。要改记忆就走 agent（MCP 工具面）。

    ━━━ 唯一的例外（S-7.5）及其边界 ━━━

    档位（share）可以点徽标改 —— 因为它是**权限**不是内容，而且它不直写：
    `/api/share/set` → ``TenantHub.set_share`` → 与 ``memory_remember``
    同一条通道（读闸 + 写闸 + 写队列 + 改前快照）。
    正文/标题/标签仍然一个口子都不开 —— 所以下面禁的仍是那三个入口。
    """
    root = Path(__file__).resolve().parent.parent / "src" / "shared_memory"
    js = (root / "static" / "panel.js").read_text(encoding="utf-8")
    html = (root / "dashboard.py").read_text(encoding="utf-8")

    for bad in ("apiPost('/api/note'", 'apiPost("/api/note"',
                "apiPost('/api/notes'", "apiPost('/api/remember'"):
        assert bad not in js, f"面板出现直写入口：{bad}"
    # /api/note 只允许是 GET（读全文）
    assert 'Route("/api/note", note, methods=["GET"])' in html, (
        "/api/note 不是只读路由 —— 面板可能被改成能写"
    )
    # 例外只有一个，而且只写档位这一个字段
    assert js.count("apiPost('/api/share/set'") == 1, (
        "改档入口不止一处 —— 面板写记忆的口子在扩散"
    )
    assert "share: v" in js or "{ident, lib, share" in js


def test_share_badge_is_clickable_and_reloads() -> None:
    """S-7.5：徽标是**可点**的，且点完必须重画 —— 不然用户以为没生效。

    渲染出徽标但没接线是这类 UI 最常见的半成品形态
    （CSS/HTML 都在、点了没反应，而静态检查看不出来）。
    """
    root = Path(__file__).resolve().parent.parent / "src" / "shared_memory"
    js = (root / "static" / "panel.js").read_text(encoding="utf-8")

    assert "share-edit" in js, "徽标没有可点标记"
    assert "function openShareMenu" in js
    # 列表与阅读器两处都接了线
    assert js.count("openShareMenu(el") + js.count("openShareMenu(e.target") >= 2, (
        "只有列表徽标能点，阅读器里的档位徽标还是死的"
    )
    # 改完刷新：列表走 load()，阅读器走 select()
    assert "openShareMenu(el, () => load())" in js
    assert "openShareMenu(el, () => select(" in js
    # 点徽标不能顺手打开阅读器（两个动作会互相取消）
    assert "e.stopPropagation()" in js


def test_theme_is_single_source_and_set_before_first_paint() -> None:
    """S-7.7：深浅两套色**各只写一处**，且首帧前就定下来。

    ━━━ 为什么钉这两点 ━━━

    ① 用 `@media (prefers-color-scheme)` 再叠一个手动开关，同一套色值
      必然写两遍（跟随系统一遍、手动一遍）—— 漂移只是时间问题。
      所以约定：**CSS 只认 `data-theme`**，"跟随系统"由 JS 翻译成属性值。
    ② 属性由 panel.js 设的话，panel.js 在 body 末尾 ——
      浅色用户会先看到**一帧深色**再翻白。所以首帧必须由 head 里的
      内联引导脚本决定（这是 HTML 里唯一允许的内联脚本，且不含任何数据）。
    """
    root = Path(__file__).resolve().parent.parent / "src" / "shared_memory"
    css = (root / "static" / "panel.css").read_text(encoding="utf-8")
    js = (root / "static" / "panel.js").read_text(encoding="utf-8")
    # 注释里写着"为什么不用 @media"，所以判断前先把注释剥掉 ——
    # 不剥的话这条测试会在**解释自己为什么存在**的那句话上失败。
    import re as _re
    css_code = _re.sub(r"/\*.*?\*/", "", css, flags=_re.S)

    # ① 只有一套 light 定义，且不靠媒体查询
    assert css_code.count('[data-theme="light"]') == 1, "浅色板被抄了两遍"
    assert "@media (prefers-color-scheme" not in css_code, (
        "又用回媒体查询了 —— 手动开关与系统偏好会变成两处维护"
    )
    # 全部走变量：开关只改一个属性，不碰任何具体颜色
    assert "documentElement.dataset.theme" in js
    assert "localStorage.getItem('shm-theme')" in js
    # 三档循环在位（跟随系统 / 深 / 浅），不是硬两态
    assert "'auto'" in js and "THEME_MODES" in js
    # ② 首帧前的内联引导
    assert "matchMedia('(prefers-color-scheme: light)')" in PANEL_HTML, (
        "首帧主题没在 head 里定下来 —— 浅色用户会闪一下深色"
    )
    assert 'id="theme-btn"' in PANEL_HTML
