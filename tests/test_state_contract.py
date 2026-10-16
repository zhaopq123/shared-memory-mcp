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

当时钉住的是"`/api/state` 必须返回 commits"。v2 · S-8/S-7.8 之后这条
换了方向：版本历史改成**按条取**（`/api/versions`），整库 git log 不再
随每次刷新去 fork。这个文件仍然守同一件事 —— **这条链路两端都得有人管**。
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


# ══════════════════════ /api/state 不再带 commits（v2 · S-7.8）══════════════════════


@pytest.mark.asyncio
async def test_api_state_has_no_commits(hub: TenantHub) -> None:
    """面板的 `/api/state` **不返回** commits —— 每次刷新都 fork git 的成本取消。

    ━━━ 这条与上面那条"必须返回 commits"的关系 ━━━

    那个 bug 是真的（stats 默认不给 commits，面板还在读，区块静默空着），
    修法当时是"在 /api/state 里显式 with_commits=True"。
    S-8 之后模型变了：普通写入**不进版本库**，整库 git log 绝大多数时候
    只剩破坏性动作的审计提交 —— 每次刷新花 60ms 取一份没有内容的日志，
    而阅读器要的是"**这一条**改过几次"。

    所以区块改成按条取（``/api/versions``），整库那份直接不取了。
    教训仍然有效：**接口改默认值时必须把所有调用方一起过一遍** ——
    这次的"调用方"就是同一条链路上的前端，所以放在同一个文件里改。
    """
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from shared_memory.dashboard import make_routes
    from shared_memory.request_ctx import set_identity

    await hub.write("admin", title="面板条目", body="x", lib_id="company")
    await hub.service_for("company").commit_now("面板测试提交")

    routes = make_routes(hub, auth=None)

    class InjectUser:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            set_identity("admin", "readwrite", "test")
            await self.app(scope, receive, send)

    app = Starlette(routes=routes)
    app.add_middleware(InjectUser)

    d = TestClient(app).get("/api/state").json()
    assert d.get("ok") is True
    assert "commits" not in d, (
        "/api/state 又带 commits 了 —— 每次刷新都会多 fork 一次 git log"
    )


# ══════════════════════ 前端那一端：按条取版本历史 ══════════════════════


def test_panel_reads_per_note_versions() -> None:
    """⚠️ 阅读器的版本历史区块必须读 `/api/versions`（契约的前端一侧）。

    这个文件存在的意义就是"链路的另一端"：后端给了接口、前端没接上，
    界面不报错、只是永远空着。所以两端各钉一条。
    """
    js = (Path(__file__).resolve().parent.parent
          / "src" / "shared_memory" / "static" / "panel.js").read_text(encoding="utf-8")
    assert "api('/api/versions'" in js, "面板没接 /api/versions —— 区块会永远显示载入中"
    assert "STATE.commits" not in js, "还在用整库 git log（S-7.8 已取消这个字段）"
    assert "groupCommits" not in js, "整库日志的合并函数还在（已无调用方）"
    # 退回某一版是写动作，面板不放按钮（"阅览室"契约）
    seg = js[js.index("async function loadVersions("):]
    seg = seg[:seg.index("/* ═══════════════ 标签页 2")]
    assert "apiPost" not in seg, "版本历史区块里出现了写调用"
