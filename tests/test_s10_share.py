"""共享粒度（v2 · S-10）—— 档位与可见性矩阵。

━━━ 为什么单独一个文件、而且以"越权"为主 ━━━

S-10 引入的是**第二层隔离**：库权限（第一层）之上再加"同一条记忆
对不同人有不同可见性"。第二层的失败模式与第一层一样是**静默**的 ——
本该看不见的人看见了，不会有任何报错。所以这里的断言全部是
"用 B 的身份去够 A 的东西，必须够不到，而且不能告诉他够不到的东西存在"。

矩阵口径（决策 D-2 / D-3）：

    条目 share      mine     仅作者；无作者的存量条目按 space 处理
                    space    同空间（同库）全部读者
                    shared   同空间 + 被目标空间授权后可跨空间
    会话 visibility private  任务域只对自己开放（默认）
                    space    同空间成员可把该任务域纳入默认检索
                    shared   任何已鉴权身份可纳入（仍受库权限约束）

"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shared_memory import session as _session  # noqa: E402
from shared_memory.config import Config, DEFAULT_SPACE  # noqa: E402
from shared_memory.errors import ErrorCode, ToolError  # noqa: E402
from shared_memory.tenancy import TenantHub  # noqa: E402
from shared_memory.tenant import Library, User  # noqa: E402
from shared_memory.tools_tenant import TenantTools  # noqa: E402
from shared_memory.vault import effective_share, share_visible  # noqa: E402


@pytest.fixture()
def hub(tmp_path: Path) -> TenantHub:
    """alice/bob 同组（都能读 group-研发），carol 在另一组。

    这个布局刚好覆盖三档需要的关系：
      · 同空间读者      —— alice 与 bob 对 group-研发
      · 不同空间的读者  —— carol 对 group-研发 **没有**读权限（跨空间场景）
    """
    h = TenantHub(tmp_path / "home")
    s = h.store
    s.users["alice"] = User(name="alice", role="admin", groups=["研发"], display="A")
    s.users["bob"] = User(name="bob", role="member", groups=["研发"], display="B")
    s.users["carol"] = User(name="carol", role="member", groups=["销售"], display="C")
    s.libs["company"] = Library(id="company", owner="company",
                                writable_by_members=False)
    s.libs["group-研发"] = Library(id="group-研发", label="研发组", owner="group:研发")
    s.libs["group-销售"] = Library(id="group-销售", label="销售组", owner="group:销售")
    s.libs["user-carol"] = Library(id="user-carol", owner="user:carol")
    s.save()
    return h


@pytest.fixture()
def tools(hub: TenantHub) -> TenantTools:
    return TenantTools(hub)


# ══════════════════════ 1. 纯函数：档位解析与可见性 ══════════════════════


@pytest.mark.parametrize("share,default,expected", [
    ("", "space", "space"),        # 空 = 继承
    ("", "mine", "mine"),
    ("", "", "space"),             # 两边都没有 → 兜底 space
    ("mine", "space", "mine"),     # 显式覆盖继承
    ("shared", "mine", "shared"),
    ("  ", "mine", "mine"),        # 只有空格的值不算显式
    ("乱写的", "space", "space"),   # 非法值不污染判定（写侧另有校验）
])
def test_effective_share_inheritance(share: str, default: str, expected: str) -> None:
    assert effective_share(share, default) == expected


@pytest.mark.parametrize("share,author,viewer,cross,default,expected", [
    # 没给身份 = 内部通道，全放行（面板治理、迁移、导出都靠这条）
    ("mine", "alice", "", False, "space", True),
    # mine：作者本人看得见
    ("mine", "alice", "alice", False, "space", True),
    # mine：别人看不见
    ("mine", "alice", "bob", False, "space", False),
    # mine 但**没有作者**（存量条目）→ 按 space 处理，不消失
    ("mine", "", "bob", False, "space", True),
    # space：同空间读者都看得见
    ("space", "alice", "bob", False, "space", True),
    # 空 = 继承空间默认档：默认 space 时人人可见
    ("", "alice", "bob", False, "space", True),
    # 默认档改成 mine，继承条目的可见性跟着收紧
    ("", "alice", "bob", False, "mine", False),
    ("", "alice", "alice", False, "mine", True),
    # 跨空间授权通道：**只**放行 shared
    ("shared", "alice", "bob", True, "space", True),
    ("space", "alice", "bob", True, "space", False),
    ("mine", "alice", "alice", True, "space", False),   # 自己的东西也不从跨空间通道走
    ("", "alice", "bob", True, "space", False),          # 继承 space → 跨空间不放行
    ("", "alice", "bob", True, "shared", True),          # 默认档设成 shared 才放行
])
def test_share_visible_matrix(share: str, author: str, viewer: str,
                              cross: bool, default: str, expected: bool) -> None:
    got = share_visible(share, author, viewer,
                        space_default=default, cross_space=cross)
    assert got is expected, (share, author, viewer, cross, default)


def test_inherited_mine_from_space_default_is_enforced() -> None:
    """空间默认档设成 mine 时，**没标 share 的条目也变私密**。

    这是"改一个配置就收紧整库"的那条路径，必须真的生效。
    """
    assert not share_visible("", "alice", "bob", space_default="mine")
    assert share_visible("", "alice", "alice", space_default="mine")


# ══════════════════════ 2. 空间默认档：读写与校验 ══════════════════════


def test_space_config_defaults_without_file(hub: TenantHub) -> None:
    """没有 space.json 时读默认值，且不创建文件（读侧无副作用）。"""
    svc = hub.service_for("company")
    assert svc.config.space() == DEFAULT_SPACE
    assert svc.config.default_share() == "space"
    assert svc.config.accept_shared_from() == []
    assert not svc.config.space_path.exists()


def test_space_config_bad_default_falls_back_not_raises(hub: TenantHub) -> None:
    """配置文件被手改坏 → 读侧退回 space，不炸。

    读侧每次检索都会走这里，抛异常等于整库不可用。
    """
    svc = hub.service_for("company")
    svc.config.dir.mkdir(parents=True, exist_ok=True)
    svc.config.space_path.write_text(
        json.dumps({"default_share": "Space "}, ensure_ascii=False), encoding="utf-8"
    )
    assert svc.config.default_share() == "space"


@pytest.mark.parametrize("space,expect_problem", [
    ({"default_share": "mine", "accept_shared_from": []}, False),
    ({"default_share": "everyone", "accept_shared_from": []}, True),
    ({"default_share": "", "accept_shared_from": []}, True),
    ({"default_share": "space", "accept_shared_from": "company"}, True),
    ({"default_share": "space", "accept_shared_from": [""]}, True),
    ({"default_share": "space"}, False),
])
def test_validate_space(hub: TenantHub, space: dict, expect_problem: bool) -> None:
    problems = hub.service_for("company").config.validate_space(space)
    assert bool(problems) is expect_problem


async def test_update_config_writes_space_json(hub: TenantHub) -> None:
    """写侧：保存后**立即生效**（配置每次读盘，不需要重启）。"""
    svc = hub.service_for("company")
    r = await svc.update_config(space={"default_share": "mine",
                                       "accept_shared_from": ["company"]})
    assert r["ok"], r
    assert "space.json" in r["changed"]
    assert svc.config.default_share() == "mine"
    assert svc.config.accept_shared_from() == ["company"]


async def test_update_config_rejects_bad_space(hub: TenantHub) -> None:
    """非法值必须**挡在写盘之前**，不能写进去再靠读侧兜底。"""
    svc = hub.service_for("company")
    with pytest.raises(Exception) as exc:
        await svc.update_config(space={"default_share": "all"})
    assert "default_share" in str(exc.value)
    assert not svc.config.space_path.exists()


async def test_config_snapshot_carries_space_and_is_default(hub: TenantHub) -> None:
    """面板要能区分"这是默认值"还是"我改过的"。"""
    snap = hub.service_for("company").config_snapshot()
    assert snap["space"] == DEFAULT_SPACE
    assert snap["space_is_default"] is True
    assert snap["defaults"]["space"] == DEFAULT_SPACE
    await hub.service_for("company").update_config(space={"default_share": "shared",
                                                           "accept_shared_from": []})
    snap2 = hub.service_for("company").config_snapshot()
    assert snap2["space"]["default_share"] == "shared"
    assert snap2["space_is_default"] is False


# ══════════════════════ 3. 写入侧：档位落盘与继承 ══════════════════════


async def test_remember_stores_explicit_share(tools: TenantTools) -> None:
    r = await tools.memory_write("alice", "研发组口径", "内容",
                                 lib="group-研发", share="mine")
    assert r["ok"], r
    got = await tools.memory_get("alice", "研发组口径", lib="group-研发")
    assert got["note"]["share"] == "mine"
    assert got["note"]["share_effective"] == "mine"


async def test_remember_mine_stamps_authenticated_author(tools: TenantTools) -> None:
    """``share=mine`` 必须有主 —— 没传 author 时用**鉴权身份**补。

    身份来自中间件，不是参数：否则任何人都能写一条"别人的仅自己"。
    """
    await tools.memory_write("alice", "Alice 的私密", "x",
                             lib="group-研发", share="mine")
    n = (await tools.memory_get("alice", "Alice 的私密", lib="group-研发"))["note"]
    assert n["author"] == "alice"


async def test_explicit_author_is_preserved_for_shared_tiers(tools: TenantTools) -> None:
    """显式传了 author 就保留（代录场景），不覆盖成调用者。"""
    await tools.memory_write("alice", "代录条目", "x",
                             lib="group-研发", share="space", author="carol")
    n = (await tools.memory_get("alice", "代录条目", lib="group-研发"))["note"]
    assert n["author"] == "carol"


async def test_mine_with_foreign_author_is_rejected(tools: TenantTools) -> None:
    """``share=mine`` + 别人的 author → 写侧直接报错。

    不拦的话这条会**写成功**、然后从写者的检索与列举里永久消失
    （作者不是他，档位是仅自己）—— "成功的黑洞"比报错难查得多。
    """
    r = await tools.memory_write("alice", "黑洞条目", "x", lib="group-研发",
                                 share="mine", author="carol")
    assert r["ok"] is False and r["code"] == "INVALID_ARGUMENT", r
    v = tools.hub.service_for("group-研发").vault
    assert v.search("黑洞条目", scope="")["total"] == 0, "报错就必须**没落盘**"


async def test_share_inherited_writes_no_field(tools: TenantTools) -> None:
    """不传 share → **不写这个字段**（继承在读侧解析）。

    写侧继承要给存量条目补 frontmatter，diff 噪音大、且改默认档时
    老数据不会跟着变 —— 所以继承只发生在读侧，这里钉住。
    """
    await tools.memory_write("alice", "没标档位的条目", "x", lib="group-研发")
    v = tools.hub.service_for("group-研发").vault
    raw = v.resolve("没标档位的条目").read_text(encoding="utf-8")
    assert "\nshare:" not in raw, "不传 share 就不该落这个字段"
    n = (await tools.memory_get("alice", "没标档位的条目", lib="group-研发"))["note"]
    assert "share" not in n                      # 没显式值
    assert n["share_effective"] == "space"       # 但生效档位给了前端


async def test_bad_share_returns_envelope(tools: TenantTools) -> None:
    r = await tools.memory_write("alice", "坏档位", "x", lib="group-研发",
                                 share="everybody")
    assert r["ok"] is False and r["code"] == "INVALID_ARGUMENT", r


# ══════════════════════ 4. 检索侧：同空间内按档位过滤 ══════════════════════


async def test_search_hides_others_mine_notes(tools: TenantTools) -> None:
    await tools.memory_write("alice", "Alice 的私密", "只有她能看",
                             lib="group-研发", share="mine")
    await tools.memory_write("alice", "组内公开", "两人都能看",
                             lib="group-研发", share="space")

    mine = await tools.memory_search("bob", "", lib="group-研发")
    titles = [x["title"] for x in mine.get("results", [])]
    assert "组内公开" in titles
    assert "Alice 的私密" not in titles, "同空间的别人不该看到 mine 档条目"

    hers = await tools.memory_search("alice", "", lib="group-研发")
    assert "Alice 的私密" in [x["title"] for x in hers.get("results", [])]


async def test_search_total_excludes_invisible(tools: TenantTools) -> None:
    """⚠️ 计数泄露测试：``total`` 不许把看不见的条目算进去。

    "共 2 条"本身就是信息泄露 —— 有人能从数字推出"这里还有一条私密的"。
    """
    await tools.memory_write("alice", "私密一", "x", lib="group-研发", share="mine")
    await tools.memory_write("alice", "公开一", "x", lib="group-研发", share="space")
    r = await tools.memory_search("bob", "", lib="group-研发")
    assert r["total"] == 1, f"bob 眼里只该有 1 条，实得 {r['total']}"


async def test_internal_channel_sees_everything(hub: TenantHub) -> None:
    """不给身份（空 viewer）= 内部通道，看全貌。

    面板治理、迁移脚本、导出都走这条路 —— 它们本来就该看全量。
    """
    await hub.write("alice", title="私密条", body="x",
                    lib_id="group-研发", extra={"share": "mine"})
    v = hub.service_for("group-研发").vault
    assert v.search("", scope="")["total"] == 1


async def test_list_notes_uses_the_same_gate(tools: TenantTools) -> None:
    """列举与检索必须同一套判定。

    面板左栏走 list_notes —— 只给检索加闸的话，
    别人的私密条目会**明晃晃列在左栏里**。
    """
    await tools.memory_write("alice", "左栏私密", "x", lib="group-研发", share="mine")
    r = await tools.memory_list("bob", lib="group-研发")
    assert "左栏私密" not in [n["title"] for n in r.get("notes", [])]
    r2 = await tools.memory_list("alice", lib="group-研发")
    assert "左栏私密" in [n["title"] for n in r2.get("notes", [])]


async def test_view_uses_the_same_gate(tools: TenantTools) -> None:
    """视图（memory_view）是**第三个读入口**，同样得过闸。

    这条是写测试时才发现的洞：检索与列举都加了闸，而视图直接拿
    ``all_notes()`` 算 —— 别人的「仅自己」条目会连着 ``stats`` 一起端出去。
    视图的统计数是从条目算的，所以过滤必须发生在**算之前**。
    """
    await tools.memory_write("alice", "左栏私密", "x", lib="group-研发",
                             share="mine")
    await tools.memory_write("alice", "组内公开", "x", lib="group-研发",
                             share="space")

    hers = await tools.memory_view("alice", "recent", lib="group-研发")
    assert hers["ok"], hers
    assert "左栏私密" in [i["title"] for i in hers["items"]]

    r = await tools.memory_view("bob", "recent", lib="group-研发")
    titles = [i["title"] for i in r["items"]]
    assert "组内公开" in titles
    assert "左栏私密" not in titles
    assert r["total"] == 1, f"视图统计同样不许计数泄露：{r['total']}"


async def test_get_of_others_mine_is_not_found_not_forbidden(
    tools: TenantTools
) -> None:
    """越权读返回 NOT_FOUND，**不能**返回 FORBIDDEN。

    FORBIDDEN 等于承认"这条存在、内容不给你" —— 存在性泄露。
    消息里只回显调用方自己给的 ident（它本来就知道这个字符串），
    不新增任何"它确实存在"的信息。
    """
    await tools.memory_write("alice", "存在性测试", "x", lib="group-研发", share="mine")
    r = await tools.memory_get("bob", "存在性测试", lib="group-研发")
    assert r["ok"] is False and r["code"] == "NOT_FOUND", r
    assert "note" not in r
    ghost = await tools.memory_get("bob", "压根没有的标题", lib="group-研发")
    assert ghost["code"] == r["code"] and ghost["hint"] == r["hint"], \
        "越权读与真不存在必须返回**完全一样**的错误，否则错误体就是存在性探针"
    # 同一条对作者自己当然读得到 —— 证明不是写坏了/索引丢了
    ok = await tools.memory_get("alice", "存在性测试", lib="group-研发")
    assert ok["ok"] is True and ok["note"]["share"] == "mine"


# ══════════════════════ 5. 跨空间：目标空间授权才放行 ══════════════════════


async def test_cross_space_needs_target_authorization(tools: TenantTools) -> None:
    """carol 没授权 → 研发组的 shared 条目也搜不到。"""
    await tools.memory_write("alice", "可共享的技术口径", "x",
                             lib="group-研发", share="shared")
    r = await tools.memory_search("carol", "可共享")
    assert "可共享的技术口径" not in [x["title"] for x in r.get("results", [])]


async def test_authorized_target_sees_only_shared(tools: TenantTools) -> None:
    """销售组授权接收研发组后：只出 ``shared``，不出 space/mine。"""
    await tools.memory_write("alice", "跨空间可见", "x",
                             lib="group-研发", share="shared")
    await tools.memory_write("alice", "组内可见", "x",
                             lib="group-研发", share="space")
    await tools.memory_write("alice", "Alice 私密", "x",
                             lib="group-研发", share="mine")

    svc = tools.hub.service_for("group-销售")
    await svc.update_config(space={"default_share": "space",
                                   "accept_shared_from": ["group-研发"]})

    r = await tools.memory_search("carol", "")
    titles = {x["title"]: x for x in r.get("results", [])}
    assert "跨空间可见" in titles, "授权后 shared 条目应进入检索面"
    assert titles["跨空间可见"].get("_shared_incoming") is True
    assert titles["跨空间可见"].get("_lib") == "group-研发"
    assert "组内可见" not in titles, "space 档不该顺着授权通道漏出去"
    assert "Alice 私密" not in titles, "mine 档不该顺着授权通道漏出去"


async def test_cross_space_get_honours_gate(tools: TenantTools) -> None:
    """读全文与检索同一套闸：未授权时 NOT_FOUND。"""
    await tools.memory_write("alice", "跨空间读测试", "x",
                             lib="group-研发", share="shared")
    r = await tools.memory_get("carol", "跨空间读测试")
    assert r["ok"] is False and r["code"] == "NOT_FOUND"
    await tools.hub.service_for("group-销售").update_config(
        space={"default_share": "space", "accept_shared_from": ["group-研发"]}
    )
    ok = await tools.memory_get("carol", "跨空间读测试")
    assert ok["ok"] is True
    assert ok["note"]["_shared_incoming"] is True


async def test_personal_lib_is_never_opened_by_grant(tools: TenantTools) -> None:
    """个人库条目标了 ``shared`` 也不外泄：库权限先拦住，且没人授权接收。

    两层都是刻意的：``can_read`` 对个人库**没有 admin 特例**（管理员要的是
    管理能力不是读内容），而跨空间通道只从"我自己可读的库"的配置里算授权。
    """
    await tools.memory_write("carol", "个人库东西", "x", lib="user-carol",
                             share="shared")
    r = await tools.memory_search("alice", "个人库东西")
    assert "个人库东西" not in [x["title"] for x in r.get("results", [])]
    assert [lib.id for lib in tools.hub.tenant.shared_incoming("alice")] == []


def test_shared_incoming_only_reads_readable_libs(hub: TenantHub) -> None:
    """授权集合只从**我有权限的库**的配置里算 —— 不去翻别人的目录。"""
    inc = hub.tenant.shared_incoming("carol")
    assert [lib.id for lib in inc] == []
    hub.service_for("group-销售").config.dir.mkdir(parents=True, exist_ok=True)
    (hub.service_for("group-销售").config.dir / "space.json").write_text(
        json.dumps({"default_share": "space",
                    "accept_shared_from": ["group-研发", "user-bob"]},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    assert [lib.id for lib in hub.tenant.shared_incoming("carol")] == ["group-研发"]


# ══════════════════════ 6. 会话可见性三档（D-2）══════════════════════


async def test_private_session_scope_is_invisible(tools: TenantTools) -> None:
    """默认 private：别人的任务域不进我的默认检索。"""
    _session.start(tools.hub.home, "alice", "研发重构", space="group-研发")
    await tools.memory_write("alice", "过程态笔记", "x", lib="company",
                             scope="task:研发重构")
    r = await tools.memory_search("bob", "过程态")
    assert "过程态笔记" not in [x["title"] for x in r.get("results", [])]
    # 更根本的一层：bob 的默认生效域里**连任务名都不该出现**
    assert "task:研发重构" not in tools._effective_scopes("bob", "")


async def test_explicit_private_task_scope_returns_empty_not_error(
    tools: TenantTools
) -> None:
    """显式点名别人的 private 任务域 → 空结果、ok=True（不报错）。

    报错会把"这个任务域存在"泄露出去；串台防的就是这个。
    """
    _session.start(tools.hub.home, "alice", "私密任务", space="group-研发")
    await tools.memory_write("alice", "私密过程态", "x", lib="group-研发",
                             scope="task:私密任务")
    r = await tools.memory_search("bob", "", scope="task:私密任务")
    assert r["ok"] is True, r
    assert r.get("results", []) == [], "越权读必须返回空，而不是内容"


async def test_writing_into_others_task_scope_is_denied(tools: TenantTools) -> None:
    """写侧用 SCOPE_DENIED（F-2 留下的那颗钉子在这里落地）。

    读侧静默、写侧明确 —— 写失败了调用方必须知道是"没权限"，
    否则它会换个参数重试个没完。
    """
    _session.start(tools.hub.home, "alice", "别人的任务", space="group-研发")
    r = await tools.memory_write("bob", "我想塞进去", "x", lib="group-研发",
                                 scope="task:别人的任务")
    assert r["ok"] is False and r["code"] == "SCOPE_DENIED", r


async def test_space_visibility_opens_scope_to_same_space_only(
    tools: TenantTools
) -> None:
    """切 space：同空间的 bob 能捞到，另一空间的 carol 还是不能。

    条目故意放在 **company**（三人都有读权限）—— 否则 carol 搜不到
    是"库权限"拦的，测不到会话档位这一层。档位与库权限要各测各的。
    """
    _session.start(tools.hub.home, "alice", "协作任务", space="group-研发",
                   visibility="space")
    await tools.memory_write("alice", "协作过程态", "x", lib="company",
                             scope="task:协作任务")

    bob = await tools.memory_search("bob", "协作过程态")
    assert "协作过程态" in [x["title"] for x in bob.get("results", [])]
    assert "task:协作任务" in bob.get("scopes", [])

    carol = await tools.memory_search("carol", "协作过程态")
    assert "协作过程态" not in [x["title"] for x in carol.get("results", [])]
    assert "task:协作任务" not in tools._effective_scopes("carol", "")


async def test_space_visibility_without_space_registration_stays_closed(
    tools: TenantTools
) -> None:
    """visibility=space 但会话没登记 space → **不开放**。

    判不出"同空间"就当没授权：放宽的方向必须显式。
    """
    _session.start(tools.hub.home, "alice", "没登记空间", visibility="space")
    await tools.memory_write("alice", "无空间过程态", "x", lib="company",
                             scope="task:没登记空间")
    r = await tools.memory_search("bob", "无空间过程态")
    assert "无空间过程态" not in [x["title"] for x in r.get("results", [])]
    assert tools._open_tasks("bob") == []


async def test_shared_visibility_opens_across_spaces(tools: TenantTools) -> None:
    """切 shared：其它空间的已鉴权身份也能把这个任务域纳入默认检索。

    会话登记在 ``group-研发``（carol 不是那个组的成员），条目放在
    ``company``（carol 有读权限）。这样"够得着"的唯一原因就是会话档位 ——
    库权限这一层**不会**被 shared 越过：carol 依然搜不到 group-研发 里的
    任何东西（那是另一条闸，见 test_library_permission_still_first）。
    """
    _session.start(tools.hub.home, "alice", "公开协作", space="group-研发",
                   visibility="shared")
    await tools.memory_write("alice", "公开过程态", "x", lib="company",
                             scope="task:公开协作")
    r = await tools.memory_search("carol", "公开过程态")
    assert "公开过程态" in [x["title"] for x in r.get("results", [])]
    assert "task:公开协作" in tools._effective_scopes("carol", "")


async def test_library_permission_still_first(tools: TenantTools) -> None:
    """shared 档**不能**越过库权限：别人组的库照样读不到。

    两层闸的顺序是刻意的：库权限决定"开不打开这个库"，档位决定
    "打开之后哪些条目给你"。若档位能反过来放宽库权限，第二层就白设计了。
    """
    _session.start(tools.hub.home, "alice", "组内公开", space="group-研发",
                   visibility="shared")
    await tools.memory_write("alice", "研发库过程态", "x", lib="group-研发",
                             scope="task:组内公开")
    r = await tools.memory_search("carol", "研发库过程态")
    assert "研发库过程态" not in [x["title"] for x in r.get("results", [])]


async def test_back_to_private_revokes_immediately(tools: TenantTools) -> None:
    """切回 private → **立即**收回（档位在读侧判定，不需要改条目）。"""
    _session.start(tools.hub.home, "alice", "反复横跳", space="group-研发",
                   visibility="space")
    await tools.memory_write("alice", "横跳过程态", "x", lib="group-研发",
                             scope="task:反复横跳")
    before = await tools.memory_search("bob", "横跳过程态")
    assert "横跳过程态" in [x["title"] for x in before.get("results", [])]

    _session.set_visibility(tools.hub.home, "alice", "反复横跳", "private")
    after = await tools.memory_search("bob", "横跳过程态")
    assert "横跳过程态" not in [x["title"] for x in after.get("results", [])], \
        "切 private 后不该还能搜到"


async def test_ended_session_scope_is_not_opened(tools: TenantTools) -> None:
    """已收工的会话不再对外开放任务域（过程态该走 archived 考古）。"""
    _session.start(tools.hub.home, "alice", "收工任务", space="group-研发",
                   visibility="space")
    await tools.memory_write("alice", "收工的过程态", "x", lib="group-研发",
                             scope="task:收工任务")
    _session.end(tools.hub.home, "alice", "收工任务", outcome="done")
    r = await tools.memory_search("bob", "收工的过程态")
    assert "收工的过程态" not in [x["title"] for x in r.get("results", [])]


# ══════════════════════ 7. set_visibility 不越权、不复活 ══════════════════════


def test_set_visibility_does_not_create_or_reopen(hub: TenantHub) -> None:
    """改档位只改属性：不新建会话、不重开已结束会话。

    曾经这里直接复用 ``start``，副作用是把收工的任务偷偷复活。
    """
    assert _session.set_visibility(hub.home, "alice", "不存在", "space") is None
    assert _session.load(hub.home) == {}, "不该凭空造出会话条目"

    _session.start(hub.home, "alice", "已结束", space="company")
    _session.end(hub.home, "alice", "已结束")
    before = _session.get(hub.home, "alice", "已结束")
    out = _session.set_visibility(hub.home, "alice", "已结束", "shared")
    assert out is not None
    assert out["status"] == "ended", "改档位不许把 ended 变回 active"
    assert out["visibility"] == "shared"
    assert out["started"] == before["started"], "started 不该被动过"


def test_set_visibility_rejects_bad_value(hub: TenantHub) -> None:
    _session.start(hub.home, "alice", "档位校验", space="company")
    with pytest.raises(Exception) as exc:
        _session.set_visibility(hub.home, "alice", "档位校验", "public")
    assert "visibility" in str(exc.value)


# ══════════════════════ 8. 面板契约：徽标数据来自真字段 ══════════════════════


async def test_panel_payload_carries_effective_share(tools: TenantTools) -> None:
    """徽标不许前端猜：后端把**生效档位**一起给出去。

    ``share`` 可能为空（继承），而徽标必须显示"仅自己/本空间/已共享"
    之一 —— 所以 ``share_effective`` 是契约字段。
    """
    await tools.memory_write("alice", "徽标条目", "x", lib="group-研发")
    r = await tools.memory_search("bob", "徽标条目", lib="group-研发")
    item = r["results"][0]
    assert item["share_effective"] == "space", item
    assert "share" not in item, "没显式标过就不该出现 share"


async def test_space_default_changes_badge_for_existing_notes(
    tools: TenantTools
) -> None:
    """改空间默认档，**存量条目**的徽标跟着变（继承在读侧，非快照）。"""
    await tools.memory_write("alice", "跟随默认档", "x", lib="group-研发",
                             author="alice")
    before = (await tools.memory_search("alice", "跟随默认档", lib="group-研发"))[
        "results"][0]
    assert before["share_effective"] == "space"

    await tools.hub.service_for("group-研发").update_config(
        space={"default_share": "mine", "accept_shared_from": []}
    )
    after = (await tools.memory_search("alice", "跟随默认档", lib="group-研发"))[
        "results"][0]
    assert after["share_effective"] == "mine"
    # 收紧后 bob 再也看不见它，且**不知道它存在**
    gone = await tools.memory_search("bob", "跟随默认档", lib="group-研发")
    assert gone["total"] == 0
    assert gone["results"] == []


async def test_authorless_note_survives_a_tightened_default(
    tools: TenantTools
) -> None:
    """没作者的条目在默认档收紧后**照样可见**（fail-open，刻意的）。

    ``mine`` 要靠 author 判定"自己"是谁；存量条目大多没有 author。
    若按 fail-close 处理，"把整库默认档改成仅自己"会**一次把所有老数据
    对所有人隐藏** —— 包括写它的人（老数据里根本没记是谁写的）。
    那种"配置改一个字段、整库内容凭空消失"的下场比泄露更糟。

    所以：要收紧就得**逐条标 share**，或者让写入带上作者。
    这条测试钉住的就是这个边界，别把它当 bug 改掉。
    """
    await tools.memory_write("alice", "没作者的存量条目", "x", lib="group-研发")
    await tools.hub.service_for("group-研发").update_config(
        space={"default_share": "mine", "accept_shared_from": []}
    )
    r = await tools.memory_search("bob", "没作者的存量条目", lib="group-研发")
    assert "没作者的存量条目" in [x["title"] for x in r["results"]]
    assert r["results"][0]["share_effective"] == "mine", "徽标如实显示档位"


def test_panel_js_renders_share_badge() -> None:
    """面板确实读了 ``share_effective``（不是只在后端算完没人用）。"""
    js = (Path(__file__).resolve().parent.parent / "src" / "shared_memory"
          / "static" / "panel.js").read_text(encoding="utf-8")
    assert "share_effective" in js
    assert "share-mine" in js and "share-space" in js and "share-shared" in js


# ══════════════════════ S-7.5 面板改档（走同一条写通道）══════════════════════


@pytest.mark.asyncio
async def test_set_share_changes_only_the_tier(hub: TenantHub) -> None:
    """改档位只动 ``share``：正文、标题、作者、标签、工作区都原样保留。

    ━━━ 为什么钉这一条 ━━━

    面板上点一下徽标就改了权限，用户绝不会想到"这一改把正文重写了一遍"。
    实现走的是 remember 同一条通道（读出来再写回去），
    所以**必须**证明写回去的内容与读出来的一致 —— 否则"改个档位"
    会把 agent 写的长文连同格式一起换掉。
    """
    tools = TenantTools(hub)
    await tools.memory_write("alice", "改档不动正文", "原正文\n\n第二段",
                             tags=["重要", "运维"], lib="group-研发",
                             workspace="乱七八糟", author="alice")
    before = (await tools.memory_get("alice", "改档不动正文", "group-研发"))["note"]

    r = await hub.set_share("alice", "改档不动正文", "mine", lib_id="group-研发")
    assert r["ok"] is True
    after = (await tools.memory_get("alice", "改档不动正文", "group-研发"))["note"]

    assert after["body"] == before["body"], "正文被重写过了"
    assert after["title"] == before["title"]
    assert after["author"] == before["author"], "作者丢了 —— 溯源断掉比没改还糟"
    assert sorted(after["tags"]) == sorted(before["tags"])
    assert after.get("workspace") == before.get("workspace")
    assert after["share"] == "mine"


@pytest.mark.asyncio
async def test_repeated_tier_changes_do_not_grow_the_body(hub: TenantHub) -> None:
    """⚠️ 读出来再写回去这条路**不能每次多一个空行**。

    写盘是 ``正文 + "\\n"``，而读回来的 body 结尾本来就带换行 ——
    不归一化的话"改一次档长一行"，改二十次正文后面全是空行。
    任何"读-改-写"的路径（面板改档、S-12 自动补字段）都吃这个亏。
    """
    await hub.write("alice", title="反复改档", body="就这一句", lib_id="group-研发",
                    author="alice")
    tools = TenantTools(hub)
    first = (await tools.memory_get("alice", "反复改档", "group-研发"))["note"]["body"]
    for tier in ("mine", "space", "shared", "mine"):
        await hub.set_share("alice", "反复改档", tier, lib_id="group-研发")
    now = (await tools.memory_get("alice", "反复改档", "group-研发"))["note"]["body"]
    assert now == first, f"正文被改档撑长了：{first!r} → {now!r}"


@pytest.mark.asyncio
async def test_set_share_on_invisible_note_is_not_found(hub: TenantHub) -> None:
    """⚠️ 别人的 ``mine`` 条目：改档也报 NOT_FOUND，与"没这条"不可分辨。

    "只是改个档位"不是放水理由 —— 能改就等于能确认存在。
    """
    await hub.write("alice", title="alice 的私密", body="x", lib_id="group-研发",
                    author="alice", extra={"share": "mine"})
    with pytest.raises(ToolError) as exc:
        await hub.set_share("bob", "alice 的私密", "shared", lib_id="group-研发")
    assert exc.value.code == ErrorCode.NOT_FOUND

    # 内容一个字没变
    n = (await TenantTools(hub).memory_get("alice", "alice 的私密", "group-研发"))["note"]
    assert n["share"] == "mine"


@pytest.mark.asyncio
async def test_set_share_requires_write_permission(hub: TenantHub) -> None:
    """⚠️ 只读库里的条目能看不能改档（不能绕过库的写权限）。"""
    await hub.write("alice", title="公司口径", body="x", lib_id="company", author="alice")
    with pytest.raises(ToolError) as exc:
        await hub.set_share("bob", "公司口径", "mine", lib_id="company")
    assert exc.value.code == ErrorCode.FORBIDDEN, (
        "bob 对 company 只读，却能改里面条目的档位 —— 写权限闸没生效"
    )


@pytest.mark.asyncio
async def test_set_share_empty_follows_space_default(hub: TenantHub) -> None:
    """传空 = 撤掉显式档位、回到跟随空间默认（面板的"跟随默认"就是这个）。"""
    await hub.write("alice", title="跟着默认", body="x", lib_id="group-研发",
                    author="alice", extra={"share": "mine"})
    await hub.set_share("alice", "跟着默认", "", lib_id="group-研发")
    n = (await TenantTools(hub).memory_get("alice", "跟着默认", "group-研发"))["note"]
    # 空档位在 to_dict 里**不输出** share（空 = 继承，不是值）
    assert "share" not in n, f"档位没撤掉：{n.get('share')!r}"
    assert n["share_effective"] == hub.service_for("group-研发").config.default_share()


def test_panel_share_endpoint_goes_through_the_write_channel() -> None:
    """⚠️ 面板的改档接口必须走 ``set_share``，不许自己摸文件。

    这条钉的是"阅览室只有一个例外"这个前提：那个例外之所以成立，
    是因为它和 agent 写记忆走同一条路（队列 + 校验 + 权限）。
    哪天有人图省事在这里直接 ``vault.put``，这条测试会红。
    """
    src = (Path(__file__).resolve().parent.parent / "src" / "shared_memory"
           / "dashboard.py").read_text(encoding="utf-8")
    assert 'Route("/api/share/set", share_set, methods=["POST"])' in src
    assert "hub.set_share(" in src
    # 该 handler 里不许出现直接落盘的口子
    seg = src[src.index("async def share_set"):src.index("# ────────── 主密钥与 PIN")]
    assert "vault" not in seg and ".write_text" not in seg
