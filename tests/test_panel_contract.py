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
    assert "<script src=\"panel.js\"" in PANEL_HTML
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
    }
    used = set(re.findall(r"getElementById\('([^']+)'\)", js))
    missing = used - dynamic
    for eid in sorted(missing):
        assert f'id="{eid}"' in PANEL_HTML, (
            f"panel.js 引用了 #{eid}，但 HTML 里没有 —— JS 会静默失效"
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
