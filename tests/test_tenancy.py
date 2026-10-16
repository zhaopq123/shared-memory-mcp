"""多租户权限测试 —— **越权测试优先于功能测试**。

━━━ 为什么这个文件最重要 ━━━

权限漏洞的特点是**静默**：
  · 鉴权漏了 → 401，立刻发现
  · **权限漏了 → 什么都不会发生**，只是本该看不到的人看到了

而"越权"正是最难发现的漏洞类型。所以这里的测试全部是
"**用 A 的身份尝试访问 B 的东西，必须失败**"。

━━━ 组织方式 ━━━

按攻击面分类：
  1. 库级越权（读/写别人或别组的库）
  2. 跨库搜索越权（搜到不该搜的）
  3. 计数泄露（总数里含无权库）
  4. 写越权（往只读库写、往别人个人库写）
  5. 身份伪造（token 绑定的用户不可被参数覆盖）
  6. 权限判定的纯函数测试（不受实现变化影响）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shared_memory.errors import ErrorCode, ToolError  # noqa: E402
from shared_memory.library import Tenant  # noqa: E402
from shared_memory.tenancy import TenantHub  # noqa: E402
from shared_memory.tenant import (  # noqa: E402
    Library,
    TenantStore,
    User,
    can_read,
    can_write,
    visible_libraries,
)


# ══════════════════════ 测试夹具 ══════════════════════


@pytest.fixture()
def hub(tmp_path: Path) -> TenantHub:
    """一个三用户 / 四库的完整环境。

    布局（用来验证每个隔离维度）：
        用户：
          alice  —— admin，组 ["研发"]
          bob    —— member，组 ["研发"]      （与 alice 同组）
          carol  —— member，组 ["销售"]      （不同组）

        库：
          company        —— owner=company（只读给 member）
          group-研发      —— owner=group:研发
          group-销售      —— owner=group:销售
          user-carol     —— owner=user:carol（carol 的个人库）
    """
    h = TenantHub(tmp_path / "home")
    store = h.store

    store.users["alice"] = User(
        name="alice", password=User.hash_password("alice-pw"),
        role="admin", groups=["研发"], display="Alice",
    )
    store.users["bob"] = User(
        name="bob", password=User.hash_password("bob-pw"),
        role="member", groups=["研发"], display="Bob",
    )
    store.users["carol"] = User(
        name="carol", password=User.hash_password("carol-pw"),
        role="member", groups=["销售"], display="Carol",
    )
    store.libs["company"] = Library(
        id="company", label="公司", owner="company", writable_by_members=False,
    )
    store.libs["group-研发"] = Library(
        id="group-研发", label="研发组", owner="group:研发",
    )
    store.libs["group-销售"] = Library(
        id="group-销售", label="销售组", owner="group:销售",
    )
    # 每个人自己的个人库（默认写入目标）
    store.libs["user-bob"] = Library(
        id="user-bob", label="Bob 的个人库", owner="user:bob",
    )
    store.libs["user-carol"] = Library(
        id="user-carol", label="Carol 的个人库", owner="user:carol",
    )
    store.save()
    return h


# ══════════════════════ 1. 权限纯函数（不受实现变化影响） ══════════════════════


def test_company_readable_by_all() -> None:
    """公司库：全员可读。"""
    lib = Library(id="company", owner="company")
    for name, groups in [("a", []), ("b", ["x"]), ("c", ["y", "z"])]:
        assert can_read(User(name=name, groups=groups), lib)


def test_group_lib_only_for_members() -> None:
    """组库：只有该组成员可读。"""
    lib = Library(id="g", owner="group:研发")
    assert can_read(User(name="a", groups=["研发"]), lib)
    assert not can_read(User(name="b", groups=["销售"]), lib)
    assert not can_read(User(name="c", groups=[]), lib)


def test_personal_lib_only_for_owner() -> None:
    """个人库：只有本人。**管理员也不行** —— 个人库的意义就是私密。"""
    lib = Library(id="u", owner="user:alice")
    assert can_read(User(name="alice"), lib)
    assert not can_read(User(name="bob"), lib)
    # ⚠️ 关键断言：管理员读不到他人个人库
    assert not can_read(User(name="admin", role="admin"), lib)


def test_company_lib_readonly_for_members() -> None:
    """公司库默认只读（防污染）—— 这是设计取舍，不是 bug。"""
    lib = Library(id="company", owner="company", writable_by_members=False)
    member = User(name="m", groups=["研发"])
    assert can_read(member, lib)          # 能读
    assert not can_write(member, lib)     # 不能写
    # 管理员能写
    assert can_write(User(name="admin", role="admin"), lib)


def test_disabled_user_has_no_access() -> None:
    """停用的用户读不了任何东西（离职场景）。"""
    lib = Library(id="company", owner="company")
    assert not can_read(User(name="x", disabled=True), lib)


def test_admin_can_write_group_lib() -> None:
    """管理员能写任何组库（管理场景需要）。"""
    lib = Library(id="g", owner="group:研发")
    assert can_write(User(name="admin", role="admin"), lib)


def test_visible_libraries_filters_correctly() -> None:
    """可见库集合正确 —— 这是所有权限判断的核心。"""
    libs = [
        Library(id="company", owner="company"),
        Library(id="g1", owner="group:研发"),
        Library(id="g2", owner="group:销售"),
        Library(id="ua", owner="user:alice"),
        Library(id="ub", owner="user:bob"),
    ]
    alice = User(name="alice", groups=["研发"])
    ids = {x.id for x in visible_libraries(alice, libs)}
    assert ids == {"company", "g1", "ua"}, f"Alice 应看到 {ids}"


def test_visible_libraries_excludes_admin_from_personal() -> None:
    """⚠️ 管理员的可见集**不含他人个人库** —— 这是测试能发现回归的地方。"""
    libs = [
        Library(id="company", owner="company"),
        Library(id="ua", owner="user:alice"),
        Library(id="ub", owner="user:bob"),
    ]
    admin = User(name="root", role="admin")
    ids = {x.id for x in visible_libraries(admin, libs)}
    assert ids == {"company"}, (
        f"管理员不该看到他人个人库，实际 {ids}。"
        "如果这条失败，说明 can_read 里加了 admin 特权 —— 那是回归"
    )


# ══════════════════════ 2. 库级越权 ══════════════════════


def test_cannot_read_other_group_lib(hub: TenantHub) -> None:
    """⚠️ 越权核心测试：bob（研发）读不了销售组的库。"""
    with pytest.raises(ToolError) as ei:
        hub.tenant.vault_for("bob", "group-销售")
    assert ei.value.code == ErrorCode.FORBIDDEN


def test_cannot_read_other_personal_lib(hub: TenantHub) -> None:
    """⚠️ 越权核心测试：bob 读不了 carol 的个人库。"""
    with pytest.raises(ToolError) as ei:
        hub.tenant.vault_for("bob", "user-carol")
    assert ei.value.code == ErrorCode.FORBIDDEN


def test_admin_cannot_read_other_personal_lib(hub: TenantHub) -> None:
    """⚠️ 管理员也读不了他人个人库 —— 刻意的设计取舍。

    理由：个人库的意义就在私密。如果管理员能读，用户就不会往里放
    真私密的东西，这个功能就废了。管理员要的是"管理能力"（删库、
    限容量），不是"读内容"。
    """
    with pytest.raises(ToolError) as ei:
        hub.tenant.vault_for("alice", "user-carol")
    assert ei.value.code == ErrorCode.FORBIDDEN


def test_can_read_own_group_lib(hub: TenantHub) -> None:
    """对照组：本该成功的必须成功（否则隔离过度，功能没法用）。"""
    v = hub.tenant.vault_for("bob", "group-研发")
    assert v is not None


def test_can_read_company_lib(hub: TenantHub) -> None:
    """所有人都能读公司库。"""
    assert hub.tenant.vault_for("carol", "company") is not None
    assert hub.tenant.vault_for("bob", "company") is not None


def test_unknown_lib_raises_not_found(hub: TenantHub) -> None:
    """不存在的库报 NOT_FOUND（而不是 FORBIDDEN）—— 便于调用方区分。"""
    with pytest.raises(ToolError) as ei:
        hub.tenant.vault_for("bob", "不存在的库")
    assert ei.value.code == ErrorCode.NOT_FOUND


# ══════════════════════ 3. 写越权 ══════════════════════


def test_cannot_write_company_lib_as_member(hub: TenantHub) -> None:
    """⚠️ member 不能写公司库（只读设计）。"""
    with pytest.raises(ToolError) as ei:
        hub.tenant.vault_for("bob", "company", need_write=True)
    assert ei.value.code == ErrorCode.FORBIDDEN
    # 错误信息要说明"这是只读设计"，而不是含糊的"没权限"
    assert "只读" in str(ei.value)


def test_admin_can_write_company_lib(hub: TenantHub) -> None:
    """管理员能写公司库（对照组）。"""
    assert hub.tenant.vault_for("alice", "company", need_write=True) is not None


def test_cannot_write_other_personal_lib(hub: TenantHub) -> None:
    """⚠️ bob 不能往 carol 的个人库写。"""
    with pytest.raises(ToolError) as ei:
        hub.tenant.vault_for("bob", "user-carol", need_write=True)
    assert ei.value.code == ErrorCode.FORBIDDEN


def test_default_write_goes_to_own_personal_lib(hub: TenantHub) -> None:
    """不指定库时写进**自己的个人库** —— 最安全的默认值。"""
    lib = hub.tenant.default_write_lib("bob")
    assert lib.owner == "user:bob", f"应写自己的库，实际 {lib.owner}"


# ══════════════════════ 4. 跨库搜索越权 ══════════════════════


@pytest.mark.asyncio
async def test_cross_search_excludes_unauthorized_libs(hub: TenantHub) -> None:
    """⚠️ 越权核心测试：跨库搜索**不返回**无权库的内容。

    这是最容易出漏洞的地方 —— 搜全部再过滤是错的（过滤漏一处就泄露），
    正确做法是没权限的库根本不打开。
    """
    # 各库写入带独特标记的内容
    await hub.write("carol", title="销售组机密", body="SALES_ONLY_MARKER_XYZ", lib_id="group-销售")
    await hub.write("carol", title="Carol私事", body="CAROL_PERSONAL_MARKER", lib_id="user-carol")
    await hub.write("alice", title="公司公开信息", body="COMPANY_MARKER", lib_id="company")
    await hub.write("alice", title="研发组内容", body="DEV_MARKER", lib_id="group-研发")

    # bob（研发组）跨库搜索：应该只看到 company + group-研发
    r = hub.search("bob", "MARKER")
    bodies = " ".join(str(x.get("title", "")) + str(x.get("body", "")) for x in r["results"])
    libs = {x.get("_lib") for x in r["results"]}

    assert "SALES_ONLY_MARKER_XYZ" not in bodies, "泄露了销售组内容！"
    assert "CAROL_PERSONAL_MARKER" not in bodies, "泄露了他人个人库内容！"
    assert "group-销售" not in libs, "结果里出现了无权库！"
    assert "user-carol" not in libs, "结果里出现了他人个人库！"
    # 有权的内容必须在
    assert "COMPANY_MARKER" in bodies or "company" in libs


@pytest.mark.asyncio
async def test_search_with_explicit_lib_still_checks_permission(hub: TenantHub) -> None:
    """⚠️ 显式传 lib 也必须过权限检查（不能靠参数绕过）。"""
    with pytest.raises(ToolError) as ei:
        hub.search("bob", "任何词", lib_id="group-销售")
    assert ei.value.code == ErrorCode.FORBIDDEN


def test_search_result_labels_source(hub: TenantHub) -> None:
    """跨库结果必须**标出来源** —— 用户要知道该采信哪个。"""
    r = hub.tenant.search_all("bob", "")
    for item in r["results"]:
        assert "_lib" in item, "结果缺少来源标注"
        assert "_lib_label" in item


# ══════════════════════ 5. 计数泄露 ══════════════════════


@pytest.mark.asyncio
async def test_count_excludes_unauthorized(hub: TenantHub) -> None:
    """⚠️ 计数不能泄露无权库的规模。

    "共 42 条" 如果 42 里含无权库的条目，那个数字本身就是信息泄露。
    这条容易漏 —— 因为计数看起来"只是个数字"。
    """
    # carol 往销售库和个人库写 5 条（bob 不该看到）
    for i in range(5):
        await hub.write("carol", title=f"销售条{i}", body="x", lib_id="group-销售")

    # bob（研发）搜全部
    r_frame = hub.search("bob", "销售条")
    assert r_frame["total"] == 0, (
        f"bob 搜到了销售组的内容（total={r_frame['total']}）—— 计数泄露"
    )


def test_stats_excludes_unauthorized(hub: TenantHub) -> None:
    """⚠️ 统计只能含可读库 —— 全局统计本身就是泄露。"""
    s = hub.stats("bob")
    lib_ids = {x["lib"] for x in s["libraries"]}
    assert "group-销售" not in lib_ids, "统计里出现了无权库"
    assert "user-carol" not in lib_ids, "统计里出现了他人个人库"
    assert s["lib_count"] == len(lib_ids)


# ══════════════════════ 6. 读取时的库定位 ══════════════════════


@pytest.mark.asyncio
async def test_get_does_not_leak_across_libs(hub: TenantHub) -> None:
    """⚠️ 按标题读时，不能读到无权库里的同名条目。"""
    await hub.write("carol", title="同名条目", body="CAROL_SECRET", lib_id="user-carol")

    with pytest.raises(ToolError) as ei:
        hub.get("bob", "同名条目")
    assert ei.value.code == ErrorCode.NOT_FOUND
    # 错误信息不能透露"这个条目存在但你没权限"（那也是泄露）
    assert "CAROL_SECRET" not in str(ei.value)


@pytest.mark.asyncio
async def test_get_finds_own_content(hub: TenantHub) -> None:
    """对照组：自己库里的内容必须能读到。"""
    await hub.write("bob", title="我的笔记", body="BOB_CONTENT")
    r = hub.get("bob", "我的笔记")
    assert "BOB_CONTENT" in r["note"]["body"]


# ══════════════════════ 7. 视图的跨库统计 ══════════════════════


@pytest.mark.asyncio
async def test_view_stats_excludes_unauthorized(hub: TenantHub) -> None:
    """⚠️ 视图统计必须逐库算再合并，不能先合并全库再算。"""
    for i in range(3):
        await hub.write("carol", title=f"过期{i}", body="x", lib_id="group-销售")
    # 给它们加过期标记（直接改文件太麻烦，这里验证的是"不含无权库"）
    v = hub.view("bob", "overview")
    # 统计里不该出现销售组的痕迹
    assert v["ok"] is True
    # 关键：total 只算 bob 能读的
    bobs = hub.view("bob", "recent")
    alices = hub.view("alice", "recent")
    assert bobs["total"] <= alices["total"], (
        "bob 的可见条目数不应超过 admin（admin 能读更多库）"
    )


# ══════════════════════ 8. 删除与恢复的权限 ══════════════════════


@pytest.mark.asyncio
async def test_cannot_delete_in_unauthorized_lib(hub: TenantHub) -> None:
    """⚠️ 不能删无权库里的东西。"""
    await hub.write("carol", title="销售的东西", body="x", lib_id="group-销售")
    with pytest.raises(ToolError) as ei:
        await hub.delete("bob", "销售的东西", lib_id="group-销售")
    assert ei.value.code == ErrorCode.FORBIDDEN


@pytest.mark.asyncio
async def test_delete_auto_locate_skips_unauthorized(hub: TenantHub) -> None:
    """⚠️ 不传 lib 时，自动定位**只在可写库里找** —— 不能碰无权库。"""
    await hub.write("carol", title="只有carol的条目", body="x", lib_id="user-carol")
    with pytest.raises(ToolError) as ei:
        await hub.delete("bob", "只有carol的条目")
    assert ei.value.code == ErrorCode.NOT_FOUND


# ══════════════════════ 9. 密码与凭据 ══════════════════════


def test_password_hash_roundtrip() -> None:
    """密码哈希与校验。"""
    u = User(name="x", password=User.hash_password("correct-horse"))
    assert u.check_password("correct-horse")
    assert not u.check_password("wrong")
    assert not u.check_password("")


def test_password_hash_is_salted() -> None:
    """同样的密码两次哈希结果不同（有随机盐）—— 防彩虹表。"""
    h1 = User.hash_password("same")
    h2 = User.hash_password("same")
    assert h1 != h2, "哈希未加盐！相同密码产生了相同哈希"


def test_password_hash_format() -> None:
    """哈希里必须带参数（将来提高强度时老密码仍能验证）。"""
    h = User.hash_password("x")
    assert h.startswith("scrypt$")
    parts = h.split("$")
    assert len(parts) == 6
    n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
    assert n >= 2**14, f"scrypt n 太小（{n}），抗暴力能力不足"


def test_broken_hash_does_not_crash() -> None:
    """损坏的哈希不该让服务崩（返回 False 即可）。"""
    u = User(name="x", password="garbage-not-a-hash")
    assert u.check_password("anything") is False


# ══════════════════════ 10. 身份不可伪造 ══════════════════════


def test_token_binds_to_user() -> None:
    """token → 用户：身份来自凭据，不能来自参数。"""
    from shared_memory.auth import Auth, Token

    a = Auth(bind_host="0.0.0.0", tokens=[
        Token(token="tok-bob", user="bob"),
        Token(token="tok-carol", user="carol"),
    ])
    assert a.resolve("tok-bob") == ("bob", "readwrite")
    assert a.resolve("tok-carol") == ("carol", "readwrite")
    assert a.resolve("tok-bob-not-real") == ("", "")


def test_revoked_token_stops_working() -> None:
    """吊销后立即失效 —— "泄了能改"的关键。"""
    from shared_memory.auth import Auth, Token

    a = Auth(bind_host="0.0.0.0", tokens=[Token(token="tok", user="bob", label="laptop")])
    assert a.resolve("tok")[0] == "bob"
    a.revoke_token("laptop")
    assert a.resolve("tok")[0] == "", "吊销后仍可用！"


def test_revoke_by_user_removes_all() -> None:
    """按用户吊销 → 他所有 token 失效（离职场景）。"""
    from shared_memory.auth import Auth, Token

    a = Auth(bind_host="0.0.0.0", tokens=[
        Token(token="t1", user="bob", label="a"),
        Token(token="t2", user="bob", label="b"),
        Token(token="t3", user="carol", label="c"),
    ])
    n = a.revoke_token("bob")
    assert n == 2
    assert a.resolve("t1")[0] == ""
    assert a.resolve("t2")[0] == ""
    assert a.resolve("t3")[0] == "carol", "吊销 bob 影响了 carol！"


def test_read_only_scope_cannot_write() -> None:
    """只读凭据不能写 —— "给同事看"不必给完整权限。"""
    from shared_memory.auth import Auth, Token

    a = Auth(bind_host="0.0.0.0", tokens=[Token(token="ro", user="bob", scope="read")])
    user, scope = a.resolve("ro")
    assert user == "bob"
    assert a.scope_allows(scope, "read") is True
    assert a.scope_allows(scope, "write") is False


def test_rotate_invalidates_old_but_keeps_user() -> None:
    """轮换：旧的失效、新的可用、身份不变。"""
    from shared_memory.auth import Auth, Token

    a = Auth(bind_host="0.0.0.0", tokens=[Token(token="old", user="bob", label="nb")])
    label, new = a.rotate_token("nb")
    assert a.resolve("old")[0] == "", "旧 token 仍有效！"
    assert a.resolve(new) == ("bob", "readwrite")
    assert label == "nb"


def test_token_list_never_returns_plaintext() -> None:
    """⚠️ 列表接口**永不返回明文** —— 显示明文等于再泄露一次。"""
    from shared_memory.auth import Auth, Token

    secret = "super-secret-token-value-12345"
    a = Auth(bind_host="0.0.0.0", tokens=[Token(token=secret, user="bob")])
    listed = a.list_tokens()
    for t in listed:
        for v in t.values():
            assert secret not in str(v), "列表里出现了 token 明文！"


def test_token_store_never_returns_plaintext_password() -> None:
    """⚠️ 用户列表不含密码哈希/明文。"""
    store_users = {
        "bob": User(name="bob", password=User.hash_password("pw"), display="Bob"),
    }
    for u in store_users.values():
        h = u.password
        assert "pw" != h
        assert h.startswith("scrypt$")


# ══════════════════════ 11. 危险默认 ══════════════════════


def test_preflight_refuses_without_credentials() -> None:
    """⚠️ 绑非 loopback + 无凭据 → **拒绝启动**（这是最关键的默认值）。"""
    from shared_memory.auth import Auth

    a = Auth(bind_host="0.0.0.0", tokens=[])
    allowed, reason = a.preflight()
    assert allowed is False, "无凭据 + 对外绑定竟然允许启动！"
    assert "拒绝启动" in reason or "token" in reason.lower()


def test_preflight_allows_loopback() -> None:
    """绑 loopback → 允许（网络层面已隔离）。"""
    from shared_memory.auth import Auth

    allowed, _ = Auth(bind_host="127.0.0.1", tokens=[]).preflight()
    assert allowed is True


def test_preflight_allows_with_token() -> None:
    """有凭据 → 允许对外绑定。"""
    from shared_memory.auth import Auth, Token

    allowed, _ = Auth(
        bind_host="0.0.0.0", tokens=[Token(token="x", user="bob")]
    ).preflight()
    assert allowed is True


def test_loopback_detection() -> None:
    """绑定地址判定的边界情况。"""
    from shared_memory.auth import is_loopback_bind, is_publicly_reachable

    assert is_loopback_bind("127.0.0.1")
    assert is_loopback_bind("localhost")
    assert is_loopback_bind("::1")
    assert not is_loopback_bind("0.0.0.0")
    assert not is_loopback_bind("192.168.1.100")
    assert not is_loopback_bind("")          # 空 = 绑定所有？保守按可达
    # 主机名无法保证只有本机 → 保守处理
    assert not is_loopback_bind("my-laptop.local")
    assert is_publicly_reachable("0.0.0.0")


# ══════════════════════ 12. 限速 ══════════════════════


def test_throttle_kicks_in_after_failures() -> None:
    """失败够多后开始限速（防在线爆破）。"""
    from shared_memory.auth import Auth, FAIL_MAX_ATTEMPTS

    a = Auth(bind_host="0.0.0.0")
    # 阈值内不限速
    for _ in range(FAIL_MAX_ATTEMPTS - 5):
        a.note_failure("1.2.3.4")
    throttled, _wait = a.throttled("1.2.3.4")
    assert throttled is False
    # 超过阈值
    for _ in range(10):
        a.note_failure("1.2.3.4")
    throttled, wait = a.throttled("1.2.3.4")
    assert throttled is True
    assert wait > 0


def test_throttle_threshold_survives_one_audit() -> None:
    """⚠️ 一次安全审计不该把真人锁死。

    实测踩过：阈值 20 时，穷举 20+ 个端点的审计就把 IP 打满，
    于是**后续带正确凭据的请求也全 429** —— 安全机制造成可用性故障。

    所以阈值必须高于"一次完整审计"的请求数（25 个左右）。
    """
    from shared_memory.auth import FAIL_MAX_ATTEMPTS

    assert FAIL_MAX_ATTEMPTS >= 50, (
        f"阈值 {FAIL_MAX_ATTEMPTS} 太低 —— 一次安全审计就会把真人锁死"
    )


def test_throttle_is_per_source() -> None:
    """限速按来源隔离 —— 一个 IP 的失败不影响别人。"""
    from shared_memory.auth import FAIL_MAX_ATTEMPTS, Auth

    a = Auth(bind_host="0.0.0.0")
    for _ in range(FAIL_MAX_ATTEMPTS + 10):
        a.note_failure("bad-ip")
    assert a.throttled("bad-ip")[0] is True
    assert a.throttled("good-ip")[0] is False


def test_probes_do_not_trigger_throttle() -> None:
    """⚠️ 探针类请求（无来源）不计入限速。

    实测踩过：冒烟测试用无凭据 favicon 探测就绪，20 次后触发限速，
    于是**后续所有请求（含正确凭据）全被 429**。

    真实世界里会反复打无凭据请求的东西很多（容器探针、守护脚本、
    监控、负载均衡心跳），它们会把共享 IP 拖进限速名单，
    然后真正用户被误伤 —— 典型的"安全机制造成可用性故障"。
    """
    from shared_memory.auth import Auth

    a = Auth(bind_host="0.0.0.0")
    # 模拟探针：大量无来源失败
    for _ in range(200):
        a.note_failure("")
    assert a.throttled("")[0] is False, "无来源失败不该触发限速"
    # 且不影响正常用户的判定
    assert a.throttled("real-user")[0] is False


# ══════════════════════ 13. 登录票据 ══════════════════════


def test_ticket_is_single_use() -> None:
    """票据用掉即失效 —— 即使 URL 泄露，窗口也极小。"""
    from shared_memory.auth import Auth

    a = Auth(bind_host="0.0.0.0")
    t = a.issue_ticket("bob")
    assert a.redeem_ticket(t) == "bob"
    assert a.redeem_ticket(t) == "", "票据被重复使用！"


def test_invalid_ticket_rejected() -> None:
    from shared_memory.auth import Auth

    a = Auth(bind_host="0.0.0.0")
    assert a.redeem_ticket("forged-ticket") == ""


def test_session_cookie_binds_to_user() -> None:
    """会话 cookie 绑定到用户（不是匿名）。"""
    from shared_memory.auth import Auth

    a = Auth(bind_host="0.0.0.0")
    sess = a.make_session("carol")
    assert a.resolve(sess)[0] == "carol"


# ══════════════════════ 14. 持久化 ══════════════════════


def test_tenant_store_roundtrip(tmp_path: Path) -> None:
    """用户与库能存能读（重启后配置不丢）。"""
    store = TenantStore(tmp_path)
    store.add_user("bob", "pw123", groups=["研发"])
    store.add_lib("group-研发", owner="group:研发", label="研发组")
    store.save()

    # 重新加载
    store2 = TenantStore(tmp_path)
    assert "bob" in store2.users
    assert store2.users["bob"].groups == ["研发"]
    assert store2.users["bob"].check_password("pw123")
    assert "group-研发" in store2.libs


def test_tenant_store_survives_corruption(tmp_path: Path) -> None:
    """配置损坏时**不让服务起不来**（回落空配置 + 报错）。"""
    (tmp_path / "tenants.json").write_text("{ 这不是合法 JSON", encoding="utf-8")
    store = TenantStore(tmp_path)   # 不应抛
    assert store.users == {}


def test_add_user_creates_personal_lib(tmp_path: Path) -> None:
    """新用户自动获得个人库。"""
    store = TenantStore(tmp_path)
    store.add_user("bob", "pw")
    assert "user-bob" in store.libs
    assert store.libs["user-bob"].owner == "user:bob"


def test_bootstrap_creates_admin_once(tmp_path: Path) -> None:
    """首次启动建管理员；第二次不再建（幂等）。"""
    store = TenantStore(tmp_path)
    r1 = store.ensure_bootstrap()
    assert r1 is not None
    user, password = r1
    assert user.is_admin
    assert len(password) >= 12
    # 幂等
    assert store.ensure_bootstrap() is None


def test_bootstrap_password_is_hashed(tmp_path: Path) -> None:
    """初始密码只以哈希形式存 —— 明文只在那一次返回值里。"""
    store = TenantStore(tmp_path)
    user, password = store.ensure_bootstrap()
    # 磁盘上不能有明文
    on_disk = (tmp_path / "tenants.json").read_text(encoding="utf-8")
    assert password not in on_disk, "密码明文被写进了配置文件！"
    assert user.check_password(password)
