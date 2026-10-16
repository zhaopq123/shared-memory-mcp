"""工作空间共享控制测试 —— 越权测试优先。

━━━ 这个文件的重点 ━━━

「改共享范围」是**能决定谁能看到内容**的操作。所以这里测的核心不是
"功能能不能用"，而是"**谁能改、谁不能改**"：

  · 别人能不能把你私人的库改成公开？→ 必须不能
  · member 能不能把内容推给全公司？→ 必须不能
  · 共享收回后，没权限的人还读得到吗？→ 必须读不到
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shared_memory.errors import ErrorCode, ToolError  # noqa: E402
from shared_memory.tenancy import TenantHub  # noqa: E402
from shared_memory.tenant import Library, User  # noqa: E402
from shared_memory.workspace import (  # noqa: E402
    WS_PREFIX,
    can_manage_lib,
    lib_id_to_ws,
    share_targets,
    ws_lib_id,
)


@pytest.fixture()
def hub(tmp_path: Path) -> TenantHub:
    """三用户环境：admin / bob（研发）/ carol（销售）。"""
    h = TenantHub(tmp_path / "home")
    s = h.store
    s.users["admin"] = User(name="admin", role="admin", display="管理员")
    s.users["bob"] = User(name="bob", role="member", groups=["研发"], display="Bob")
    s.users["carol"] = User(name="carol", role="member", groups=["销售"], display="Carol")
    s.libs["company"] = Library(id="company", label="公司", owner="company",
                                writable_by_members=False)
    s.libs["user-admin"] = Library(id="user-admin", owner="user:admin", label="admin 的")
    s.libs["user-bob"] = Library(id="user-bob", owner="user:bob", label="bob 的")
    s.save()
    return h


# ══════════════════════ 1. 工作空间名 → 库 id ══════════════════════


def test_ws_lib_id_keeps_chinese() -> None:
    """中文工作空间名要保留（用户就是这么起名的）。"""
    assert ws_lib_id("乱七八糟") == WS_PREFIX + "乱七八糟"
    assert ws_lib_id("个人工作中枢") == WS_PREFIX + "个人工作中枢"


def test_ws_lib_id_blocks_path_traversal() -> None:
    """⚠️ 路径穿越必须被挡 —— 库 id 会变成**目录名**。"""
    for evil in ("../../etc", "a/../../../b", "..", "./../x"):
        lid = ws_lib_id(evil)
        assert "/" not in lid, f"{evil} → {lid} 含斜杠"
        assert ".." not in lid, f"{evil} → {lid} 含 .."
        assert lid.startswith(WS_PREFIX)


def test_ws_lib_id_handles_illegal_chars() -> None:
    """跨平台非法字符（Windows 不允许 : * ? 等）要替换掉。"""
    lid = ws_lib_id("a:b*c?d|e")
    for ch in ':*?|<>"':
        assert ch not in lid, f"{ch} 没被替换"


def test_ws_lib_id_empty_falls_back() -> None:
    """空名字给一个确定的兜底（不能生成空目录名）。"""
    assert ws_lib_id("") == WS_PREFIX + "unnamed"
    assert ws_lib_id("   ") == WS_PREFIX + "unnamed"


def test_lib_id_roundtrip_for_simple_names() -> None:
    """简单名字能往返（有损转换只在含特殊字符时发生）。"""
    for name in ("乱七八糟", "proj1", "a-b"):
        assert lib_id_to_ws(ws_lib_id(name)) == name


# ══════════════════════ 2. 共享范围的可选项 ══════════════════════


def test_share_targets_include_self_first() -> None:
    """第一项永远是"仅自己" —— 默认最安全。"""
    bob = User(name="bob", groups=["研发"])
    targets = share_targets(bob, [])
    assert targets[0].owner == "user:bob"
    assert "仅自己" in targets[0].label


def test_share_targets_include_own_groups() -> None:
    """能看到自己所属的组。"""
    bob = User(name="bob", groups=["研发", "架构"])
    owners = {t.owner for t in share_targets(bob, [])}
    assert "group:研发" in owners
    assert "group:架构" in owners


def test_share_targets_exclude_other_groups() -> None:
    """⚠️ 不能共享给自己**不在**的组（那会建出谁都读不到的黑洞）。"""
    bob = User(name="bob", groups=["研发"])
    owners = {t.owner for t in share_targets(bob, [])}
    assert "group:销售" not in owners


def test_share_targets_company_only_for_admin() -> None:
    """⚠️ 只有管理员能设公司级 —— 否则 member 能把内容推给全公司。"""
    member = share_targets(User(name="bob", groups=["研发"]), [])
    assert not any(t.owner == "company" for t in member)
    admin = share_targets(User(name="admin", role="admin"), [])
    assert any(t.owner == "company" for t in admin)


def test_no_groups_means_no_group_option() -> None:
    """不在任何组的人，没有"共享给组"选项（否则是黑洞）。"""
    lone = User(name="lone", groups=[])
    owners = {t.owner for t in share_targets(lone, [])}
    assert owners == {"user:lone"}


# ══════════════════════ 3. 谁能改共享范围 ══════════════════════


def test_owner_can_manage_own_lib() -> None:
    """库所有者能改自己的。"""
    bob = User(name="bob")
    assert can_manage_lib(bob, Library(id="user-bob", owner="user:bob"))


def test_admin_can_manage_any_lib() -> None:
    """管理员能改任何库。"""
    admin = User(name="admin", role="admin")
    assert can_manage_lib(admin, Library(id="user-bob", owner="user:bob"))
    assert can_manage_lib(admin, Library(id="g", owner="group:研发"))


def test_others_cannot_manage_your_lib() -> None:
    """⚠️ 越权核心：别人不能改你的库归属。

    能改归属 = 能决定泄露。所以这条必须挡住。
    """
    carol = User(name="carol", groups=["销售"])
    assert not can_manage_lib(carol, Library(id="user-bob", owner="user:bob"))


def test_member_cannot_manage_company_lib() -> None:
    """member 不能改公司库的归属。"""
    bob = User(name="bob", groups=["研发"])
    assert not can_manage_lib(bob, Library(id="company", owner="company"))


def test_disabled_user_cannot_manage() -> None:
    """停用的用户不能改任何东西。"""
    bob = User(name="bob", disabled=True)
    assert not can_manage_lib(bob, Library(id="user-bob", owner="user:bob"))


# ══════════════════════ 4. 建工作空间 ══════════════════════


def test_create_defaults_to_private(hub: TenantHub) -> None:
    """⚠️ 不传 share 时默认**仅自己** —— 最安全的默认值。"""
    r = hub.workspace.create("bob", workspace="我的实验")
    assert r["ok"] is True
    assert r["owner"] == "user:bob"
    assert "仅" in r["share_label"]
    lib = hub.store.lib(r["lib"])
    assert lib is not None and lib.owner == "user:bob"


def test_create_with_group_share(hub: TenantHub) -> None:
    """能共享给自己的组。"""
    r = hub.workspace.create("bob", workspace="研发规范", share="group:研发")
    assert r["owner"] == "group:研发"
    assert "研发" in r["share_label"]


def test_create_company_requires_admin(hub: TenantHub) -> None:
    """⚠️ member 不能建公司级工作空间。"""
    with pytest.raises(ToolError) as ei:
        hub.workspace.create("bob", workspace="公开的", share="company")
    assert ei.value.code == ErrorCode.INVALID_ARGUMENT


def test_create_admin_can_company(hub: TenantHub) -> None:
    """管理员能建公司级（对照组）。"""
    r = hub.workspace.create("admin", workspace="全员规范", share="company")
    assert r["owner"] == "company"
    # 公司库自动只读
    lib = hub.store.lib(r["lib"])
    assert lib.writable_by_members is False


def test_create_rejects_foreign_group(hub: TenantHub) -> None:
    """⚠️ 不能共享给自己不在的组（会建出谁都读不到的黑洞）。"""
    with pytest.raises(ToolError) as ei:
        hub.workspace.create("bob", workspace="错的", share="group:销售")
    assert ei.value.code == ErrorCode.INVALID_ARGUMENT


def test_create_duplicate_is_conflict(hub: TenantHub) -> None:
    """同名工作空间报 CONFLICT（而不是静默复用）。"""
    hub.workspace.create("bob", workspace="重复的")
    with pytest.raises(ToolError) as ei:
        hub.workspace.create("bob", workspace="重复的")
    assert ei.value.code == ErrorCode.CONFLICT


def test_create_empty_name_rejected(hub: TenantHub) -> None:
    """空名字拒绝。"""
    with pytest.raises(ToolError) as ei:
        hub.workspace.create("bob", workspace="   ")
    assert ei.value.code == ErrorCode.INVALID_ARGUMENT


def test_create_rejects_unknown_share(hub: TenantHub) -> None:
    """⚠️ 随便填的共享范围要拒绝（打错字会变成黑洞）。"""
    with pytest.raises(ToolError) as ei:
        hub.workspace.create("bob", workspace="x", share="group:不存在的组")
    assert ei.value.code == ErrorCode.INVALID_ARGUMENT


# ══════════════════════ 5. 改共享范围（核心场景） ══════════════════════


def test_share_progression_private_to_group(hub: TenantHub) -> None:
    """**典型用法**：先自己写，然后共享给组。"""
    r = hub.workspace.create("bob", workspace="渐进共享")
    lid = r["lib"]
    assert r["owner"] == "user:bob"

    # 共享给组
    r2 = hub.workspace.share("bob", lib=lid, share="group:研发")
    assert r2["ok"] is True
    assert r2["owner"] == "group:研发"
    assert "仅" in r2["before"] and "研发" in r2["after"]


def test_share_revoke_removes_access(hub: TenantHub) -> None:
    """⚠️ **收回**共享后，别人读不到了（物理上打不开那个库）。"""
    # bob 建一个共享给研发组的库
    r = hub.workspace.create("bob", workspace="会收回的", share="group:研发")
    lid = r["lib"]
    # 给研发组加一个成员 dave
    hub.store.users["dave"] = User(name="dave", role="member", groups=["研发"])
    hub.store.save()
    # dave 能读
    assert hub.tenant.vault_for("dave", lid) is not None

    # bob 收回（改成仅自己）
    hub.workspace.share("bob", lib=lid, share="user:bob")

    # ⚠️ dave 现在读不到了
    with pytest.raises(ToolError) as ei:
        hub.tenant.vault_for("dave", lid)
    assert ei.value.code == ErrorCode.FORBIDDEN, "收回共享后别人还能读！"


def test_cannot_share_others_lib(hub: TenantHub) -> None:
    """⚠️ 越权核心：carol 不能改 bob 的库归属。"""
    r = hub.workspace.create("bob", workspace="bob 的私人空间")
    with pytest.raises(ToolError) as ei:
        hub.workspace.share("carol", lib=r["lib"], share="group:销售")
    assert ei.value.code == ErrorCode.FORBIDDEN


def test_member_cannot_publish_to_company(hub: TenantHub) -> None:
    """⚠️ member 不能把自己的库改成公司级（那是把内容推给全公司）。"""
    r = hub.workspace.create("bob", workspace="bob 想公开的")
    with pytest.raises(ToolError) as ei:
        hub.workspace.share("bob", lib=r["lib"], share="company")
    assert ei.value.code == ErrorCode.INVALID_ARGUMENT


def test_admin_can_publish_to_company(hub: TenantHub) -> None:
    """管理员能发布到公司级。"""
    r = hub.workspace.create("admin", workspace="管理员发布的")
    r2 = hub.workspace.share("admin", lib=r["lib"], share="company")
    assert r2["owner"] == "company"
    # 自动变只读
    assert r2["writable_by_members"] is False


def test_share_unknown_lib_is_not_found(hub: TenantHub) -> None:
    """不存在的库报 NOT_FOUND。"""
    with pytest.raises(ToolError) as ei:
        hub.workspace.share("admin", lib="不存在的库", share="company")
    assert ei.value.code == ErrorCode.NOT_FOUND


def test_share_rejects_foreign_group(hub: TenantHub) -> None:
    """不能共享给自己不在的组。"""
    r = hub.workspace.create("bob", workspace="x")
    with pytest.raises(ToolError) as ei:
        hub.workspace.share("bob", lib=r["lib"], share="group:销售")
    assert ei.value.code == ErrorCode.INVALID_ARGUMENT


# ══════════════════════ 6. 列举（面板要用） ══════════════════════


def test_list_shows_share_label(hub: TenantHub) -> None:
    """列举要带人类可读的共享说明（面板直接显示）。"""
    hub.workspace.create("bob", workspace="给组看的", share="group:研发")
    r = hub.workspace.list_for("bob")
    labels = {x["label"]: x["share_label"] for x in r["libraries"]}
    assert "研发" in labels["给组看的"]


def test_list_shows_can_manage(hub: TenantHub) -> None:
    """⚠️ 列举要带 can_manage —— 面板据此决定按钮是否可点。

    前端**不自己算权限**（那会出现"前端说能改、后端拒绝"的不一致）。
    """
    hub.workspace.create("bob", workspace="bob 的")
    r = hub.workspace.list_for("bob")
    by_label = {x["label"]: x for x in r["libraries"]}
    assert by_label["bob 的"]["can_manage"] is True
    assert by_label["admin 的"]["can_manage"] is False


def test_list_includes_share_targets(hub: TenantHub) -> None:
    """列举要带可选的共享范围（面板下拉用）。"""
    r = hub.workspace.list_for("bob")
    owners = {t["owner"] for t in r["share_targets"]}
    assert "user:bob" in owners
    assert "group:研发" in owners
    assert "company" not in owners   # member 不能设公司级


def test_list_marks_workspace_libs(hub: TenantHub) -> None:
    """要能区分"工作空间库"和"系统库"（company / user-xxx）。"""
    r = hub.workspace.create("bob", workspace="项目A")
    listed = hub.workspace.list_for("bob")
    by_lib = {x["lib"]: x for x in listed["libraries"]}
    assert by_lib[r["lib"]]["is_workspace"] is True
    assert by_lib["company"]["is_workspace"] is False


# ══════════════════════ 7. 端到端：写入 → 共享 → 别人读到 ══════════════════════


@pytest.mark.asyncio
async def test_e2e_write_then_share_then_visible(hub: TenantHub) -> None:
    """端到端：bob 写进私人空间 → 别人读不到 → 共享后别人能读到。"""
    # 1. 建私人空间并写入
    r = hub.workspace.create("bob", workspace="端到端")
    lid = r["lib"]
    await hub.write("bob", title="bob 的秘密", body="SECRET_MARKER", lib_id=lid)

    # 2. dave（同组）现在读不到
    hub.store.users["dave"] = User(name="dave", role="member", groups=["研发"])
    hub.store.save()
    found = hub.search("dave", "SECRET_MARKER")
    assert found["total"] == 0, "共享前别人就搜到了！"

    # 3. bob 共享给研发组
    hub.workspace.share("bob", lib=lid, share="group:研发")

    # 4. dave 现在能搜到
    found2 = hub.search("dave", "SECRET_MARKER")
    assert found2["total"] >= 1, "共享后别人搜不到"
    assert any(x.get("_lib") == lid for x in found2["results"])


# ══════════════════════ 8. 视图的完整契约 ══════════════════════


@pytest.mark.asyncio
async def test_view_includes_label_and_desc(hub: TenantHub) -> None:
    """⚠️ 视图必须带 label / desc —— 否则前端标题区域是空白。

    实测踩过：`tenancy.view()` 的**跨库分支**漏了这两个字段，
    而单库分支有。症状不是报错，而是 `<h2 class="sechead"></h2>`
    —— 用户看到「一片空」而不是「哪里错了」。

    这类静默缺陷只能靠契约测试或真浏览器发现。
    """
    await hub.write("admin", title="随便写一条", body="x", lib_id="company")
    for kind in ("overview", "lessons", "decisions", "recent"):
        r = hub.view("admin", kind)
        assert r.get("ok") is True
        assert r.get("kind") == kind
        assert r.get("label"), f"{kind} 视图缺 label（前端标题会空白）"
        assert r.get("desc"), f"{kind} 视图缺 desc（副标题会空白）"


@pytest.mark.asyncio
async def test_view_label_matches_knowledge_source(hub: TenantHub) -> None:
    """label 必须与 knowledge.VIEWS 一致（单一数据源，不许各写一份）。"""
    from shared_memory.knowledge import VIEWS

    await hub.write("admin", title="测试条目", body="x", lib_id="company")
    for kind in VIEWS:
        r = hub.view("admin", kind)
        assert r["label"] == VIEWS[kind]["label"], (
            f"{kind} 的 label 与 knowledge.VIEWS 不一致 —— "
            "两处各写一份必然出现偏差"
        )
        assert r["desc"] == VIEWS[kind]["desc"]
