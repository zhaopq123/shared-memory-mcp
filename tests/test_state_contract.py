"""`/api/state` 的 commits 契约 —— 接口变更必须同步所有调用方。

━━━ 实测踩过的断点 ━━━

另一个 agent 做了性能优化：``stats`` 默认**不再返回** commits
（每次 git log 要 fork 子进程约 60ms，而 stats 的核心用途不依赖它）。
优化本身是对的（stats 从 ~350ms 降到 ~6ms）。

但它**漏了面板**：dashboard.py 的 ``/api/state`` 还在按旧契约读
``r.commits``，结果详情页的「版本历史」永远显示
「最近 0 次提交」—— 界面不报错，只是永远空着。

这就是**接口变更没同步调用方**的典型形态：
  · 后端：完全正常（甚至更快了）
  · 前端：静默失效（没有异常，只是没数据）
  · 单测：全绿（没人测过这条链路）

所以这里钉住：``/api/state`` 必须真的返回 commits，
且 stats 的 with_commits 参数必须真的生效。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shared_memory.tenancy import TenantHub  # noqa: E402
from shared_memory.tenant import Library, User  # noqa: E402


@pytest.fixture()
def hub(tmp_path: Path) -> TenantHub:
    h = TenantHub(tmp_path / "home")
    h.store.users["admin"] = User(name="admin", role="admin", display="管理员")
    h.store.users["bob"] = User(name="bob", role="member", groups=["研发"])
    h.store.libs["company"] = Library(id="company", owner="company",
                                      writable_by_members=False)
    h.store.libs["group-研发"] = Library(id="group-研发", owner="group:研发")
    h.store.libs["user-bob"] = Library(id="user-bob", owner="user:bob",
                                       created_by="bob")
    h.store.save()
    return h


# ══════════════════════ with_commits 参数必须生效 ══════════════════════


@pytest.mark.asyncio
async def test_stats_without_commits_by_default(hub: TenantHub) -> None:
    """默认不带 commits（性能优化的本意）。"""
    await hub.write("admin", title="测试条目", body="x", lib_id="company")
    s = hub.stats("admin", lib_id="company")
    assert "commits" not in s or not s.get("commits"), (
        "stats 默认带了 commits —— 那性能优化的意义就没了"
    )


@pytest.mark.asyncio
async def test_stats_with_commits_returns_history(hub: TenantHub) -> None:
    """⚠️ with_commits=True 必须真的返回提交历史。

    这条钉住那个断点：参数存在但**没透传到 hub** 的话，
    调用方拿到空列表，界面静默失效。
    """
    await hub.write("admin", title="测试条目", body="x", lib_id="company")
    await hub.service_for("company").commit_now("测试提交")

    s = hub.stats("admin", lib_id="company", with_commits=True)
    commits = s.get("commits") or []
    assert commits, "with_commits=True 但没返回任何提交"
    assert all(c.get("author") for c in commits), "提交历史缺 author"
    assert any("写入" in c.get("subject", "") or "测试提交" in c.get("subject", "")
               for c in commits), f"提交内容不含预期说明：{[c.get('subject') for c in commits]}"


@pytest.mark.asyncio
async def test_stats_cross_lib_with_commits(hub: TenantHub) -> None:
    """跨库统计也要支持 with_commits（面板不传 lib 时走这条）。"""
    await hub.write("admin", title="跨库条目", body="x", lib_id="company")
    await hub.service_for("company").commit_now("跨库提交")

    r = hub.stats("admin", with_commits=True)
    entries = r.get("libraries") or []
    company = next((e for e in entries if e["lib"] == "company"), None)
    assert company is not None, "跨库统计里没有 company"
    assert company.get("commits"), "跨库统计的 with_commits 没生效"


# ══════════════════════ /api/state 必须返回 commits ══════════════════════


@pytest.mark.asyncio
async def test_api_state_includes_commits(hub: TenantHub) -> None:
    """⚠️ 面板的 `/api/state` 契约：必须返回 commits。

    这是那个 bug 的直接回归测试。面板详情页的「版本历史」
    区块完全依赖这个字段 —— 缺了它界面就永远显示
    「最近 0 次提交」，而且**不报错**。
    """
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from shared_memory.dashboard import make_routes
    from shared_memory.http_server import _build_auth
    from shared_memory.request_ctx import current_user

    await hub.write("admin", title="面板条目", body="x", lib_id="company")
    await hub.service_for("company").commit_now("面板测试提交")

    routes = make_routes(hub, auth=None)

    # 造一个"已登录"的 app：绕过中间件，直接注入身份
    from starlette.middleware.base import BaseHTTPMiddleware

    class FakeAuth:
        def __init__(self) -> None:
            self.required = False

    class InjectUser:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            from shared_memory.request_ctx import set_identity
            set_identity("admin", "readwrite", "test")
            await self.app(scope, receive, send)

    app = Starlette(routes=routes)
    app.add_middleware(InjectUser)

    client = TestClient(app)
    r = client.get("/api/state")
    assert r.status_code == 200, f"/api/state 返回 {r.status_code}"
    d = r.json()
    assert d.get("ok") is True

    commits = d.get("commits")
    assert commits, (
        "/api/state 没返回 commits —— 面板的「版本历史」会永远空着"
        "（这正是实测踩过的静默断点）"
    )
    assert any("面板测试提交" in c.get("subject", "") for c in commits), (
        f"commits 里没有预期的提交：{[c.get('subject') for c in commits]}"
    )


# ══════════════════════ 前端也依赖这个契约 ══════════════════════


def test_panel_js_reads_commits_from_state() -> None:
    """⚠️ 面板必须从 /api/state 读 commits（契约的另一端）。

    如果前端改成读别的地方而后端没跟上，就又是同一个断点。
    """
    js = (Path(__file__).resolve().parent.parent
          / "src" / "shared_memory" / "static" / "panel.js").read_text(encoding="utf-8")
    assert "STATE.commits = r.commits" in js, (
        "panel.js 不再从 /api/state 读 commits —— 版本历史区块会没数据"
    )
