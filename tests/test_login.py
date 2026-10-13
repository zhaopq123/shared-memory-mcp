"""账号密码登录 + 改密码 + 退出登录。

━━━ 产品背景 ━━━

第一版面板用「贴一长串 token」登录。技术上能跑，但**产品上是错的**：
token 是给 agent 用的（它不会记密码），人应该用账号密码。

用户明确反馈过这点，所以这里钉住正确的形态：
  · ``POST /api/auth/login`` 收 ``username`` + ``password``
  · 密码 scrypt 哈希存储，**明文不落盘**
  · 改密码必须验旧密码（防会话被借用就夺号）
  · 退出登录必须**服务端吊销**会话（不只是浏览器删 cookie）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shared_memory.auth import Auth, Token  # noqa: E402
from shared_memory.tenancy import TenantHub  # noqa: E402
from shared_memory.tenant import Library, TenantStore, User  # noqa: E402


@pytest.fixture()
def hub(tmp_path: Path) -> TenantHub:
    h = TenantHub(tmp_path / "home")
    h.store.users["alice"] = User(
        name="alice", password=User.hash_password("alice-pass-123"),
        role="admin", display="Alice",
    )
    h.store.users["bob"] = User(
        name="bob", password=User.hash_password("bob-pass-456"),
        role="member", groups=["研发"],
    )
    h.store.users["nopw"] = User(name="nopw", role="member")   # 没设密码
    h.store.save()
    return h


# ══════════════════════ 密码存储 ══════════════════════


def test_password_is_hashed_not_plaintext(hub: TenantHub) -> None:
    """⚠️ 密码必须以哈希存储，明文不能出现在配置里。"""
    on_disk = (hub.home / "tenants.json").read_text(encoding="utf-8")
    assert "alice-pass-123" not in on_disk, "密码明文落盘了！"
    assert "bob-pass-456" not in on_disk
    assert "scrypt$" in on_disk, "没看到 scrypt 哈希"


def test_check_password(hub: TenantHub) -> None:
    """校验密码：对的通过，错的拒绝。"""
    u = hub.store.user("alice")
    assert u.check_password("alice-pass-123")
    assert not u.check_password("wrong")


def test_user_without_password_cannot_login(hub: TenantHub) -> None:
    """没设密码的账号不能登录（而不是"任意密码都行"）。"""
    u = hub.store.user("nopw")
    assert not u.password
    assert not u.check_password("")


# ══════════════════════ 用户可被设置密码 ══════════════════════


def test_set_password(hub: TenantHub) -> None:
    """给没密码的账号设密码。"""
    assert not hub.store.user("nopw").password
    ok = hub.store.set_password("nopw", "new-pass-999")
    assert ok
    assert hub.store.user("nopw").check_password("new-pass-999")


def test_set_password_rejects_empty(hub: TenantHub) -> None:
    """空密码拒绝（不能把密码"清空"来绕过）。"""
    assert hub.store.set_password("alice", "") is False


def test_set_password_unknown_user(hub: TenantHub) -> None:
    """不存在的用户返回 False。"""
    assert hub.store.set_password("nobody", "x") is False


# ══════════════════════ 登录接口契约 ══════════════════════


@pytest.mark.asyncio
async def test_login_api_accepts_username_password(hub: TenantHub) -> None:
    """⚠️ `/api/auth/login` 必须收 username + password（不是 token）。

    这是用户明确要求的产品形态。第一版只收 token，
    让人贴 64 位 hex 字符串 —— 那是让用户替架构缺陷买单。
    """
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from shared_memory.dashboard import make_routes

    app = Starlette(routes=make_routes(hub, auth=None))
    client = TestClient(app)

    # 正确的账号密码
    r = client.post("/api/auth/login",
                    json={"username": "alice", "password": "alice-pass-123"})
    assert r.status_code == 200, f"登录失败：{r.status_code} {r.text[:200]}"
    d = r.json()
    assert d.get("ok") is True, d
    assert "login_url" in d, "没返回 login_url（换 cookie 的票据链接）"
    assert "login=" in d["login_url"], "login_url 里没有票据"

    # 错误的密码
    r2 = client.post("/api/auth/login",
                     json={"username": "alice", "password": "wrong"})
    assert r2.status_code == 401, f"错误密码竟返回 {r2.status_code}"

    # 不存在的用户
    r3 = client.post("/api/auth/login",
                     json={"username": "ghost", "password": "x"})
    assert r3.status_code == 401

    # 没设密码的用户
    r4 = client.post("/api/auth/login",
                     json={"username": "nopw", "password": "anything"})
    assert r4.status_code == 401


@pytest.mark.asyncio
async def test_login_api_requires_both_fields(hub: TenantHub) -> None:
    """缺用户名或密码要拒绝（不能"只给密码就进"）。"""
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from shared_memory.dashboard import make_routes

    app = Starlette(routes=make_routes(hub, auth=None))
    client = TestClient(app)

    assert client.post("/api/auth/login", json={"username": "alice"}).status_code in (400, 401)
    assert client.post("/api/auth/login", json={"password": "alice-pass-123"}).status_code in (400, 401)
    assert client.post("/api/auth/login", json={}).status_code in (400, 401)


@pytest.mark.asyncio
async def test_login_ticket_is_single_use(hub: TenantHub) -> None:
    """⚠️ 换来的票据只能⽤一次。"""
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from shared_memory.dashboard import make_routes
    from shared_memory.http_server import _wrap_auth, _build_auth

    # ⚠️ 必须用 0.0.0.0 而不是 127.0.0.1：
    # 票据兑换逻辑在**鉴权中间件**里（因为免鉴权路径不能短路它），
    # 而中间件只在 auth.required=True 时才被 _build_auth 之外用。
    # 127.0.0.1 会让 required=False → 中间件不启用 → 兑换不发生。
    #
    # 需要一把真 token 才能让 preflight 通过（否则拒绝启动）。
    from shared_memory.auth import Token
    auth = Auth(bind_host="0.0.0.0", store=hub.store,
                tokens=[Token(token="seed", user="alice")])

    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    # 造一个极简下游：一个 OK 页 + 真正的登录路由。
    # （票据兑换在中间件里，但 login 路由得在下游 —— 所以不能只放 OK 页）
    from shared_memory.dashboard import make_routes

    async def ok_page(request):
        return PlainTextResponse("OK")

    inner = Starlette(
        routes=[Route("/", ok_page), *make_routes(hub, auth=auth)]
    )
    app = _wrap_auth(inner, auth)
    client = TestClient(app)

    r = client.post("/api/auth/login",
                    json={"username": "alice", "password": "alice-pass-123"})
    assert r.status_code == 200, f"登录失败 {r.status_code}: {r.text[:200]}"
    ticket = r.json()["login_url"].split("login=")[-1]

    # 第一次兑换成功 → 302 到干净 URL
    r1 = client.get(f"/?login={ticket}", follow_redirects=False)
    assert r1.status_code == 302, f"首次兑换应 302，得到 {r1.status_code}"
    assert "set-cookie" in {k.lower() for k in r1.headers}, "兑换成功但没设 cookie"
    assert "httponly" in r1.headers.get("set-cookie", "").lower(), "cookie 不是 httpOnly"

    # 第二次失败（票已用掉）
    r2 = client.get(f"/?login={ticket}", follow_redirects=False)
    assert r2.status_code != 302, "票据被重复使用！"


# ══════════════════════ 改密码 ══════════════════════


@pytest.mark.asyncio
async def test_change_password_requires_current(hub: TenantHub) -> None:
    """⚠️ 改密码必须验旧密码 —— 否则会话被借用就能夺号。"""
    from starlette.middleware.base import BaseHTTPMiddleware

    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from shared_memory.dashboard import make_routes
    from shared_memory.request_ctx import set_identity

    class Inject:
        def __init__(self, app):
            self.app = app
        async def __call__(self, scope, receive, send):
            set_identity("alice", "readwrite", "test")
            await self.app(scope, receive, send)

    app = Starlette(routes=make_routes(hub, auth=None))
    app.add_middleware(Inject)
    client = TestClient(app)

    # 旧密码错 → 拒
    r = client.post("/api/auth/password",
                    json={"current": "wrong", "password": "new-pass-777",
                          "confirm": "new-pass-777"})
    assert r.status_code == 403, f"旧密码错竟返回 {r.status_code}"

    # 旧密码对 → 改
    r2 = client.post("/api/auth/password",
                     json={"current": "alice-pass-123", "password": "new-pass-777",
                           "confirm": "new-pass-777"})
    assert r2.status_code == 200, r2.text[:200]
    assert hub.store.user("alice").check_password("new-pass-777")
    # 旧密码立即失效
    assert not hub.store.user("alice").check_password("alice-pass-123")


@pytest.mark.asyncio
async def test_change_password_validates_length_and_match(hub: TenantHub) -> None:
    """新密码长度 + 两次一致都要校验。"""
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from shared_memory.dashboard import make_routes
    from shared_memory.request_ctx import set_identity

    class Inject:
        def __init__(self, app):
            self.app = app
        async def __call__(self, scope, receive, send):
            set_identity("alice", "readwrite", "test")
            await self.app(scope, receive, send)

    app = Starlette(routes=make_routes(hub, auth=None))
    app.add_middleware(Inject)
    client = TestClient(app)

    # 太短
    assert client.post("/api/auth/password",
                       json={"current": "alice-pass-123", "password": "short",
                             "confirm": "short"}).status_code == 400
    # 不一致
    assert client.post("/api/auth/password",
                       json={"current": "alice-pass-123", "password": "new-pass-777",
                             "confirm": "different"}).status_code == 400
    # 缺当前密码
    assert client.post("/api/auth/password",
                       json={"password": "new-pass-777", "confirm": "new-pass-777"}
                       ).status_code == 400
