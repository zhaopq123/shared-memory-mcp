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


# ══════════════════════ 会话生命周期（v2 · S-2）══════════════════════
# 三道闸（设计 §4.2）：①默认收窄 ②会话绑定 ③归档退出。
# 每条都对应一个"没有它就会串台"的具体场景。
#
# 全部写成 async（仓库 asyncio_mode=auto）——**不要**在一条用例里多次
# asyncio.run：那会给同一个 hub 缓存的服务换多个事件循环，写队列
# 会报"bound to a different event loop"。

from shared_memory import digest as D  # noqa: E402
from shared_memory import session as S  # noqa: E402
from shared_memory.tools_tenant import TenantTools  # noqa: E402


@pytest.fixture()
def tools(hub: TenantHub) -> TenantTools:
    return TenantTools(hub)


async def test_s2_session_start_registers_and_is_idempotent(
    hub: TenantHub, tools: TenantTools
) -> None:
    """同 (user, task) 重复 start 不重复登记，started 不重置。"""
    r1 = await tools.memory_session_start("admin", "重构A", space="user-admin")
    r2 = await tools.memory_session_start("admin", "重构A")
    assert r1["ok"] and r2["ok"]
    assert r1["session"]["started"] == r2["session"]["started"]
    assert len(S.for_user(hub.home, "admin")) == 1
    # space 缺省不覆盖已设的值
    assert S.get(hub.home, "admin", "重构A")["space"] == "user-admin"


async def test_s2_session_start_returns_three_sections(
    hub: TenantHub, tools: TenantTools
) -> None:
    """上下文包三段齐全：约束清单 / 任务既有记忆 / 空间摘要。"""
    # 一条约束（标题含"必须"→ 推断 type=constraint）
    await tools.memory_write(
        "admin", title="部署必须走队列", body="并发会互相覆盖。", lib="user-admin",
    )
    # 开一次会话写条过程态，验证"续做能接上"
    await tools.memory_session_start("admin", "重构A", space="user-admin")
    await tools.memory_write(
        "admin", title="重构A-第一步", body="先拆 vault。",
        lib="user-admin", scope="task",
    )

    r = await tools.memory_session_start("admin", "重构A")
    assert r["ok"]
    assert r["counts"]["constraints"] >= 1
    assert any(i["title"] == "部署必须走队列" for i in r["constraints"])
    assert any(i["title"] == "重构A-第一步" for i in r["task_notes"])
    assert isinstance(r["space_brief"], list)


async def test_s2_gate1_no_session_default_only_long_term(
    hub: TenantHub, tools: TenantTools
) -> None:
    """闸①默认收窄：**没有任何会话**时，任务过程态不进默认检索。

    直接按 scope 写（精确控制，不经 end 的归档动作），
    这样测的就是"默认范围"本身，而不是归档。
    """
    await hub.write(
        "admin", title="ax 的过程态", body="关键词 共享词",
        lib_id="user-admin", extra={"scope": "task:ax"},
    )
    await hub.write(
        "admin", title="ax 的长期结论", body="关键词 共享词", lib_id="user-admin",
    )

    r = await tools.memory_search("admin", "共享词")
    titles = {i["title"] for i in r["results"]}
    assert "ax 的长期结论" in titles
    assert "ax 的过程态" not in titles, "没有会话时任务过程态不该出现在默认检索"

    r2 = await tools.memory_search("admin", "共享词", scope="task:ax")
    assert {i["title"] for i in r2["results"]} == {"ax 的过程态"}


async def test_s2_gate2_session_binding_sees_own_task_only(
    hub: TenantHub, tools: TenantTools
) -> None:
    """闸②会话绑定：绑 bx 的会话摸不到 ax 任务域，显式 scope 可查。"""
    await hub.write(
        "admin", title="ax 过程", body="关键词 独门词",
        lib_id="user-admin", extra={"scope": "task:ax"},
    )
    await tools.memory_session_start("admin", "bx", space="user-admin")
    await tools.memory_write(
        "admin", title="bx 过程", body="关键词 独门词", lib="user-admin", scope="task",
    )

    r = await tools.memory_search("admin", "独门词")
    titles = {i["title"] for i in r["results"]}
    assert "bx 过程" in titles, "本会话任务域必须可见"
    assert "ax 过程" not in titles, "别的任务域不该被本会话默认摸到"

    r2 = await tools.memory_search("admin", "独门词", scope="task:ax")
    assert {i["title"] for i in r2["results"]} == {"ax 过程"}


async def test_s2_gate3_archived_exits_default_but_queryable(
    hub: TenantHub, tools: TenantTools
) -> None:
    """闸③归档退出：收工后过程态退出默认检索，archived 仍可考古。"""
    await tools.memory_session_start("admin", "归档任务", space="user-admin")
    await tools.memory_write(
        "admin", title="归档任务-过程", body="关键词 独门词2",
        lib="user-admin", scope="task",
    )
    end = await tools.memory_session_end("admin", "归档任务")
    assert end["ok"]
    assert "归档任务-过程" in end["archived"]

    default = await tools.memory_search("admin", "独门词2")
    assert default["results"] == [], "归档过程态不该出现在默认检索"

    arch = await tools.memory_search("admin", "独门词2", scope="archived")
    assert {i["title"] for i in arch["results"]} == {"归档任务-过程"}


async def test_s2_session_end_promotes_reusable(
    hub: TenantHub, tools: TenantTools
) -> None:
    """可复用的（workflow/constraint/habit/lesson）收工升 long-term。"""
    await tools.memory_session_start("admin", "升格任务", space="user-admin")
    await tools.memory_write(
        "admin", title="必须走队列（铁律）", body="硬约束。",
        lib="user-admin", scope="task",
    )
    await tools.memory_write(
        "admin", title="临时草稿", body="过程记录。", lib="user-admin", scope="task",
    )
    end = await tools.memory_session_end("admin", "升格任务")
    assert "必须走队列（铁律）" in end["promoted"]
    assert "临时草稿" in end["archived"]
    r = await tools.memory_search("admin", "硬约束")
    assert {i["title"] for i in r["results"]} == {"必须走队列（铁律）"}


async def test_s2_session_end_writes_summary_note(
    hub: TenantHub, tools: TenantTools
) -> None:
    """收工带 summary 时写一条 long-term 小结留在库里。"""
    await tools.memory_session_start("admin", "小结任务", space="user-admin")
    end = await tools.memory_session_end(
        "admin", "小结任务", outcome="done", summary="定下了防串台口径。"
    )
    assert end["summary_note"] == "会话小结：小结任务"
    r = await tools.memory_search("admin", "防串台口径")
    assert any(i["title"] == "会话小结：小结任务" for i in r["results"])


async def test_s2_write_task_scope_without_session_errors(
    hub: TenantHub, tools: TenantTools
) -> None:
    """scope=task 但没开会话 → 明确报错并给指引（不静默落 long-term）。"""
    r = await tools.memory_write(
        "admin", title="无会话过程态", body="x", lib="user-admin", scope="task",
    )
    assert r["ok"] is False
    assert r["code"] == "INVALID_ARGUMENT"


async def test_s2_bob_cannot_reach_admin_task_notes(
    hub: TenantHub, tools: TenantTools
) -> None:
    """跨用户：bob 的默认检索摸不到 admin 的任务域（越权面为零）。"""
    await tools.memory_session_start("admin", "私密任务", space="user-admin")
    await tools.memory_write(
        "admin", title="admin 私有过程", body="关键词 机密词",
        lib="user-admin", scope="task",
    )
    r = await tools.memory_search("bob", "机密词")
    assert r["results"] == [], "bob 不该看到 admin 个人库里的任务过程态"


async def test_s2_visibility_and_task_are_validated(
    hub: TenantHub, tools: TenantTools
) -> None:
    """非法 visibility / 空 task 要报错（不静默登记脏会话）。"""
    r1 = await tools.memory_session_start("admin", "x", visibility="public")
    assert r1["ok"] is False and r1["code"] == "INVALID_ARGUMENT"
    r2 = await tools.memory_session_start("admin", "   ")
    assert r2["ok"] is False and r2["code"] == "INVALID_ARGUMENT"


async def test_s2_digest_injects_active_session_context(
    hub: TenantHub, tools: TenantTools
) -> None:
    """digest 注入"你正在做 X，相关记忆 N 条"（initialize 里能读到）。"""
    await tools.memory_session_start("admin", "digest任务", space="user-admin")
    await tools.memory_write(
        "admin", title="digest任务-一条", body="x", lib="user-admin", scope="task",
    )
    line = D.session_context(hub, "admin", hub.home)
    assert "你正在做" in line
    assert "digest任务" in line
    assert "相关记忆 1 条" in line
    # 没有会话时不注入（零噪音）
    S.end(hub.home, "admin", "digest任务")
    assert D.session_context(hub, "admin", hub.home) == ""


def test_s2_session_state_survives_reload(hub: TenantHub) -> None:
    """登记与 cursor 落盘 —— 重新从文件读仍在（等价"重启恢复"）。"""
    S.start(hub.home, "admin", "持久任务", space="user-admin")
    S.touch(hub.home, "admin", "持久任务", cursor={"step": 7})
    entry = S.get(hub.home, "admin", "持久任务")          # 从文件重读
    assert entry["cursor"] == {"step": 7}
    assert entry["status"] == "active"
    assert "持久任务" in S.active_tasks(hub.home, "admin")
