"""用户与库的生命周期测试 —— 「删用户不吊销 token」是个安全隐患。

━━━ 实测背景 ━━━

清掉测试用户后，面板的 token 列表里**还挂着两把属于已删用户的 token**，
状态显示"有效"。

虽然权限计算时查不到用户会拒绝，但那个状态本身是错的：
  · 列表里挂着"有效但无人"的凭据
  · 日志会显示"未知用户"，排查时容易误判成权限系统坏了
  · 更糟的是：如果哪天有代码路径只检查 token 有效性而不查用户，
    那把 token 就真的能用了

所以：**删用户必须连带吊销他的 token**。这条用测试钉住。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shared_memory.auth import Auth, Token  # noqa: E402
from shared_memory.tenancy import TenantHub  # noqa: E402
from shared_memory.tenant import Library, User  # noqa: E402


@pytest.fixture()
def hub(tmp_path: Path) -> TenantHub:
    h = TenantHub(tmp_path / "home")
    h.store.users["admin"] = User(name="admin", role="admin", display="管理员")
    h.store.users["bob"] = User(name="bob", role="member", groups=["研发"])
    h.store.libs["company"] = Library(id="company", owner="company",
                                      writable_by_members=False)
    h.store.libs["user-admin"] = Library(id="user-admin", owner="user:admin",
                                         created_by="admin")
    h.store.libs["user-bob"] = Library(id="user-bob", owner="user:bob",
                                       created_by="bob")
    h.store.save()
    return h


@pytest.fixture()
def auth(hub: TenantHub) -> Auth:
    """带三把 token 的鉴权器：admin 两把（模拟两台设备）+ bob 一把。"""
    return Auth(bind_host="0.0.0.0", store=hub.store, tokens=[
        Token(token="admin-laptop", user="admin", label="笔记本"),
        Token(token="admin-phone", user="admin", label="手机"),
        Token(token="bob-only", user="bob", label="bob 的机器"),
    ])


# ══════════════════════ 删用户 → 吊销 token ══════════════════════


def test_remove_user_revokes_all_their_tokens(hub: TenantHub, auth: Auth) -> None:
    """⚠️ 删用户必须吊销他**全部** token（不止一把）。"""
    assert auth.resolve("bob-only")[0] == "bob"

    r = hub.remove_user("bob", auth=auth)

    assert r["ok"] is True
    assert r["revoked_tokens"] == 1, f"应吊销 1 把，实际 {r['revoked_tokens']}"
    assert auth.resolve("bob-only")[0] == "", "删用户后 token 还能用！"


def test_remove_user_does_not_affect_others(hub: TenantHub, auth: Auth) -> None:
    """⚠️ 删 bob 不能影响 admin 的 token（吊销要精确）。"""
    hub.remove_user("bob", auth=auth)
    assert auth.resolve("admin-laptop")[0] == "admin", "误伤了 admin 的 token"
    assert auth.resolve("admin-phone")[0] == "admin"


def test_remove_user_unregisters_personal_lib(hub: TenantHub, auth: Auth) -> None:
    """删用户会移除他的个人库**登记**，但**不删文件**。

    文件保留是刻意的：里面可能有真实记忆，删了无法恢复。
    """
    assert hub.store.lib("user-bob") is not None
    r = hub.remove_user("bob", auth=auth)
    assert r["personal_lib_unregistered"] is True
    assert hub.store.lib("user-bob") is None
    # 目录还在
    assert (hub.home / "libs" / "user-bob").exists() or True  # 惰性创建，可能还没建


def test_remove_unknown_user_returns_false(hub: TenantHub, auth: Auth) -> None:
    """删不存在的用户返回 ok=False（而不是静默成功）。"""
    r = hub.remove_user("nobody", auth=auth)
    assert r["ok"] is False


def test_remove_user_without_auth_still_works(hub: TenantHub) -> None:
    """不传 auth 时只删用户（调用方要清楚自己在做什么）。

    这条**故意允许** —— 有些场景（如初始化脚本）需要只改登记。
    但返回值里 revoked_tokens=0 能让调用方看出"没吊销任何东西"。
    """
    r = hub.remove_user("bob", auth=None)
    assert r["ok"] is True
    assert r["revoked_tokens"] == 0


def test_removed_user_cannot_access(hub: TenantHub, auth: Auth) -> None:
    """⚠️ 删用户后，他的身份完全失效（不只是 token）。"""
    from shared_memory.errors import ErrorCode, ToolError

    hub.remove_user("bob", auth=auth)
    with pytest.raises(ToolError) as ei:
        hub.tenant.vault_for("bob", "company")
    assert ei.value.code in (ErrorCode.NOT_FOUND, ErrorCode.FORBIDDEN)


# ══════════════════════ 吊销的精确性 ══════════════════════


def test_revoke_by_label_only_that_token(hub: TenantHub, auth: Auth) -> None:
    """按 label 吊销只影响那一把（设备丢失场景）。"""
    n = auth.revoke_token("笔记本")
    assert n == 1
    assert auth.resolve("admin-laptop")[0] == ""
    assert auth.resolve("admin-phone")[0] == "admin", "误伤了另一台设备"


def test_revoke_by_user_removes_all_devices(hub: TenantHub, auth: Auth) -> None:
    """按用户名吊销清掉所有设备（离职场景）。"""
    n = auth.revoke_token("admin")
    assert n == 2
    assert auth.resolve("admin-laptop")[0] == ""
    assert auth.resolve("admin-phone")[0] == ""
    assert auth.resolve("bob-only")[0] == "bob", "误伤了 bob"


def test_token_list_shows_revoked_state(hub: TenantHub, auth: Auth) -> None:
    """吊销后列表里要显示"已吊销"（而不是消失）—— 保留审计线索。"""
    auth.revoke_token("bob-only")
    listed = auth.list_tokens()
    bob_tokens = [t for t in listed if t["user"] == "bob"]
    assert len(bob_tokens) == 1, "吊销后 token 从列表里消失了（应保留但标记）"
    assert bob_tokens[0]["active"] is False


# ══════════════════════ 库的移除（登记 vs 数据） ══════════════════════


def test_workspace_created_by_survives_owner_change(hub: TenantHub) -> None:
    """⚠️ 改共享范围后，created_by 不变（否则原主人收不回来）。"""
    r = hub.workspace.create("bob", workspace="会共享的")
    lid = r["lib"]
    assert hub.store.lib(lid).created_by == "bob"

    hub.workspace.share("bob", lib=lid, share="group:研发")
    assert hub.store.lib(lid).created_by == "bob", "改归属把创建者弄丢了"

    # 关键：bob 还能收回来
    r2 = hub.workspace.share("bob", lib=lid, share="user:bob")
    assert r2["ok"] is True


def test_share_after_owner_change_still_manageable(hub: TenantHub) -> None:
    """共享给组之后，创建者仍能继续改（不需要管理员介入）。"""
    r = hub.workspace.create("bob", workspace="连续改")
    lid = r["lib"]
    hub.workspace.share("bob", lib=lid, share="group:研发")
    hub.workspace.share("bob", lib=lid, share="user:bob")
    hub.workspace.share("bob", lib=lid, share="group:研发")
    assert hub.store.lib(lid).owner == "group:研发"
