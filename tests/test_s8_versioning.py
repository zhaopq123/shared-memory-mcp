"""v2 · S-8 版本控制按需 + 备份/恢复。

━━━ 这一片要钉住的四件事 ━━━

1. **改前快照人人有**（S-8.1）：覆盖正文是不可逆的，所以每次覆盖前
   往 `.history/<id>/<ts>.md` 存一份上一版，默认留 3 份。
2. **git 只给 versioned**（S-8.2）：标了 `versioned: true` 的长文才提交进
   跨库共享的 `_versioned/` 仓库；普通写入零 git 操作。
3. **"我改坏了"永远问得出答案**（S-8.3）：`memory_history(title=…)` 对
   非 versioned 条目不说"没有历史"，而是给出快照与上一版正文。
4. **备份不再是 git**（S-8.4）：恢复一台机器靠 tar 整目录；
   恢复要 confirm，且恢复前自动打一个回退快照。

⚠️ 这些用例同时是"别回退"的护栏：旧模型（每次写都提交）看起来更省心，
   但它换来的历史没人读得动，而它给的"可恢复"是错觉 ——
   真出事时要的是整目录，不是提交序列。
"""

from __future__ import annotations

import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shared_memory.tenancy import TenantHub  # noqa: E402
from shared_memory.tenant import Library, User  # noqa: E402
from shared_memory.tools_tenant import TenantTools  # noqa: E402
from shared_memory.vault import VERSIONED_DIRNAME, Vault  # noqa: E402


@pytest.fixture()
def hub(tmp_path: Path) -> TenantHub:
    h = TenantHub(tmp_path / "home")
    h.store.users["alice"] = User(name="alice", role="admin", groups=["研发"])
    h.store.users["bob"] = User(name="bob", role="member", groups=["研发"])
    h.store.libs["group-研发"] = Library(id="group-研发", owner="group:研发")
    h.store.libs["group-销售"] = Library(id="group-销售", owner="group:销售")
    h.store.save()
    return h


def vault_of(h: TenantHub, lib_id: str = "group-研发") -> Vault:
    return h.libs.vault(h.store.lib(lib_id))


# ══════════════════════ S-8.1 改前快照 ══════════════════════


@pytest.mark.asyncio
async def test_five_updates_keep_exactly_three_snapshots(hub: TenantHub) -> None:
    """连改 5 次后 `.history/` 里恰好 3 份（验收口径原文）。"""
    for i in range(6):
        await hub.write("alice", title="长期演进的记忆", body=f"第 {i} 版",
                        lib_id="group-研发", author="alice")
    v = vault_of(hub)
    snaps = v.list_history("长期演进的记忆")
    assert len(snaps) == 3, f"应只留 3 份，实际 {len(snaps)}"


@pytest.mark.asyncio
async def test_snapshot_holds_the_previous_version_not_the_new_one(hub: TenantHub) -> None:
    """快照存的是**上一版**正文 —— 存成当前值的话这层就毫无意义。"""
    await hub.write("alice", title="口径文件", body="v1 原文", lib_id="group-研发")
    await hub.write("alice", title="口径文件", body="v2 改过", lib_id="group-研发")
    v = vault_of(hub)
    assert v.newest_history_body("口径文件").strip() == "v1 原文"


@pytest.mark.asyncio
async def test_snapshot_filenames_have_no_colon(hub: TenantHub) -> None:
    """⚠️ 快照文件名里不能有冒号 —— Windows 不允许。

    生产在 Linux、开发在 Windows：只按 Linux 想的话，
    时间戳直接进文件名（ISO 带 `+08:00`）会在开发机上抛 OSError，
    而这层是"防手滑"的唯一保险，静默失效最坏。
    """
    await hub.write("alice", title="带冒号的坑", body="第一版", lib_id="group-研发")
    await hub.write("alice", title="带冒号的坑", body="第二版", lib_id="group-研发")
    d = vault_of(hub).history_dir
    names = [p.name for p in d.rglob("*.md")]
    assert names, "快照一个都没生成"
    for n in names:
        assert ":" not in n, f"快照文件名含冒号（Windows 会失败）：{n}"


@pytest.mark.asyncio
async def test_first_create_makes_no_snapshot(hub: TenantHub) -> None:
    """新建没有"上一版"，不该凭空造一份空快照。"""
    r = await hub.write("alice", title="只写一次", body="x", lib_id="group-研发")
    assert r["snapshot"] is None
    assert vault_of(hub).list_history("只写一次") == []


@pytest.mark.asyncio
async def test_history_keep_zero_disables_snapshots(hub: TenantHub) -> None:
    """`history_keep: 0` = 关快照（有人不想要库里的副本）。"""
    lib_dir = hub.libs.path_of(hub.store.lib("group-研发"))
    lib_dir.mkdir(parents=True, exist_ok=True)
    cfg = lib_dir / "_config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "settings.json").write_text('{"history_keep": 0}', encoding="utf-8")

    v = Vault(lib_dir, history_keep=0)
    v.put(title="不留快照", body="v1")
    v.put(title="不留快照", body="v2")
    assert v.list_history("不留快照") == []


@pytest.mark.asyncio
async def test_append_mode_snapshots_old_body(hub: TenantHub) -> None:
    """append 也是覆盖同一文件 —— 快照同样要存住追加前的内容。"""
    await hub.write("alice", title="流水账", body="第一段", lib_id="group-研发")
    await hub.write("alice", title="流水账", body="第二段", lib_id="group-研发",
                    mode="append")
    old = vault_of(hub).newest_history_body("流水账")
    assert old.strip() == "第一段"


@pytest.mark.asyncio
async def test_snapshots_stay_out_of_git(hub: TenantHub) -> None:
    """⚠️ 快照**不进 git** —— 包括删除触发的那次审计提交。

    审计提交是 ``git add -A``，整个库目录都扫，`.history/` 就在里面。
    一旦它被提进库历史，"防手滑与版本控制分开"这条口径就破了：
    库里会出现一堆没人要的副本提交。
    """
    v = vault_of(hub)
    await hub.write("alice", title="进快照不进git", body="v1", lib_id="group-研发")
    await hub.write("alice", title="进快照不进git", body="v2", lib_id="group-研发")
    assert v.list_history("进快照不进git"), "前提没了：一条快照都没生成"

    # 删除会**立刻**提交所在库（审计兜底）
    await TenantTools(hub).memory_delete("alice", "进快照不进git", True, "group-研发")
    tracked = v._git("ls-files").stdout.splitlines()
    assert not [t for t in tracked if t.startswith(".history/")], (
        f"快照被提进版本库了：{[t for t in tracked if t.startswith('.history/')][:3]}"
    )


@pytest.mark.asyncio
async def test_hand_edited_id_cannot_escape_history_dir(hub: TenantHub) -> None:
    """⚠️ 快照目录名来自 frontmatter 的 id，而那是**用户可以手改**的字段。

    不消毒的话 `id: ../../x` 就把"往库里存快照"变成"往库外写文件"。
    这里手写一个恶意 id，确认快照仍落在库内。
    """
    await hub.write("alice", title="恶意id试验", body="v1", lib_id="group-研发")
    p = vault_of(hub).resolve("恶意id试验")
    txt = p.read_text(encoding="utf-8").replace("id: ", "id: ../../evil\n#", 1)
    p.write_text(txt, encoding="utf-8")
    vault_of(hub).invalidate_cache(None)

    await hub.write("alice", title="恶意id试验", body="v2", lib_id="group-研发")
    root = vault_of(hub).root
    assert not (root.parent.parent / "evil").exists(), "快照写到库外了"
    assert all(str(x).startswith(str(root)) for x in vault_of(hub).history_dir.rglob("*"))


# ══════════════════════ S-8.2 共享版本库 ══════════════════════


@pytest.mark.asyncio
async def test_versioned_note_lands_in_shared_repo(hub: TenantHub) -> None:
    r = await hub.write("alice", title="架构长文", body="第一版正文",
                        lib_id="group-研发", extra={"versioned": True})
    assert r["ok"] is True
    assert r["committed"] is True, "versioned 写入应进版本库"
    repo = hub.home / VERSIONED_DIRNAME
    assert (repo / ".git").is_dir(), "版本库没建起来"
    assert list((repo / "files" / "group-研发").glob("*.md")), "版本库里没有这条"


@pytest.mark.asyncio
async def test_two_libs_share_one_version_repo(hub: TenantHub) -> None:
    """⚠️ 是**一个**共享仓库按库分目录，不是每库一个仓库。

    每库一个仓库就退回 v1 的老问题：几十个仓库各有一串没人读的历史，
    运维要逐个 git log 才能看清"最近哪些长文动过"。
    """
    await hub.write("alice", title="研发长文", body="x", lib_id="group-研发",
                    extra={"versioned": True})
    await hub.write("alice", title="销售长文", body="y", lib_id="group-销售",
                    extra={"versioned": True})
    repo = hub.home / VERSIONED_DIRNAME
    assert sum(1 for _ in repo.rglob(".git")) == 1, "版本库不止一个"
    assert (repo / "files" / "group-研发").is_dir()
    assert (repo / "files" / "group-销售").is_dir()


@pytest.mark.asyncio
async def test_versioned_repo_gets_a_commit_per_edit(hub: TenantHub) -> None:
    """versioned 条目每改一次一条提交 —— 这正是它值得版本化的含义。"""
    for i in range(3):
        await hub.write("alice", title="要逐版留痕的口径", body=f"v{i}",
                        lib_id="group-研发", extra={"versioned": True})
    vr = vault_of(hub).version_repo
    name = vault_of(hub).resolve("要逐版留痕的口径").name
    h = vr.history("group-研发", name)
    assert h["ok"] is True
    assert len(h["versions"]) == 3, f"应有 3 个版本，实际 {len(h['versions'])}"


@pytest.mark.asyncio
async def test_unversioned_note_never_appears_in_version_repo(hub: TenantHub) -> None:
    await hub.write("alice", title="一条普通记忆", body="x", lib_id="group-研发")
    repo = hub.home / VERSIONED_DIRNAME
    hits = list((repo / "files").rglob("*.md")) if repo.is_dir() else []
    assert not hits, f"普通记忆混进了版本库：{[p.name for p in hits]}"


@pytest.mark.asyncio
async def test_deleting_versioned_note_removes_it_from_repo(hub: TenantHub) -> None:
    """删除要把它从版本库摘走：那个目录只放"还活着的长文"。

    历史不丢 —— 摘除本身就是一次提交，之前的版本仍能从 git 捞回。
    """
    await hub.write("alice", title="活不长", body="x", lib_id="group-研发",
                    extra={"versioned": True})
    repo = hub.home / VERSIONED_DIRNAME
    assert list((repo / "files" / "group-研发").glob("*.md"))
    r = await hub.delete("alice", "活不长", lib_id="group-研发")
    assert r["ok"] is True
    assert not list((repo / "files" / "group-研发").glob("*.md")), "删除后版本库里还留着"


# ══════════════════════ S-8.3 memory_history(title) ══════════════════════


@pytest.mark.asyncio
async def test_versions_of_unversioned_note_says_so_but_gives_body(hub: TenantHub) -> None:
    """验收口径：非 versioned 返回"未启用版本控制"。

    ⚠️ 但不能只返回这句话 —— 用户问"改坏了怎么退"时，
    快照里有上一版正文，就必须给出来，否则这层等于没做。
    """
    await hub.write("alice", title="普通记忆", body="老样子", lib_id="group-研发")
    await hub.write("alice", title="普通记忆", body="新样子", lib_id="group-研发")
    tools = TenantTools(hub)
    r = await tools.memory_versions("alice", "普通记忆", lib="group-研发")
    assert r["ok"] is True
    assert r["versioned"] is False
    assert "未启用版本控制" in r["message"]
    assert r["versions"] == []
    assert r["snapshots"], "非 versioned 也该有改前快照"
    assert r["previous_body"].strip() == "老样子"


@pytest.mark.asyncio
async def test_versions_of_versioned_note_returns_diff(hub: TenantHub) -> None:
    await hub.write("alice", title="口径 v3", body="原来的说法", lib_id="group-研发",
                    extra={"versioned": True})
    await hub.write("alice", title="口径 v3", body="改后的说法", lib_id="group-研发",
                    extra={"versioned": True})
    tools = TenantTools(hub)
    r = await tools.memory_versions("alice", "口径 v3", lib="group-研发")
    assert r["versioned"] is True
    assert len(r["versions"]) == 2
    assert "原来的说法" in r["diff"] and "改后的说法" in r["diff"], \
        "diff 里看不到改动，等于只给了个 sha"


@pytest.mark.asyncio
async def test_versions_of_missing_note_is_not_found(hub: TenantHub) -> None:
    tools = TenantTools(hub)
    r = await tools.memory_versions("alice", "压根没有的标题", lib="group-研发")
    assert r["ok"] is False and r["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_versions_do_not_leak_others_private_notes(hub: TenantHub) -> None:
    """⚠️ 版本查询是**读路径**：别人的 mine 条目必须与"不存在"不可分辨。

    这条是 S-10.5 那个教训的延续 —— 新读接口最容易忘带闸，
    而 `memory_history(title=…)` 正好是个能试探"这个标题存不存在"的形状。
    """
    # 必须带 author：没有作者的 mine 条目按 fail-open 处理（S-10 的定案），
    # 那种条目谁都看得见，用它测"越权"会测出一个假绿。
    title = "alice 的私密"
    ghost_title = "压根没有的标题"
    await hub.write("alice", title=title, body="x", lib_id="group-研发",
                    author="alice", extra={"share": "mine"})
    tools = TenantTools(hub)
    mine = await tools.memory_versions("alice", title, lib="group-研发")
    other = await tools.memory_versions("bob", title, lib="group-研发")
    assert mine["ok"] is True
    assert other["ok"] is False and other["code"] == "NOT_FOUND"
    ghost = await tools.memory_versions("bob", "压根没有的标题", lib="group-研发")
    # 只比 code / hint —— message 里刻意回显了**调用方自己输入的**标题，
    # 那条标题 bob 本来就知道，把它算作泄露是搞错了对象。
    # 真正会泄露的是「两种情况的错误码/提示不一样」。
    assert other["code"] == ghost["code"]
    assert other["hint"] == ghost["hint"], "提示语能区分「存在但没权限」与「没有」"
    assert other["message"] == f"找不到记忆：{title}"
    assert ghost["message"] == f"找不到记忆：{ghost_title}"


# ══════════════════════ S-8.4 整目录快照 / 恢复 ══════════════════════


@pytest.mark.asyncio
async def test_snapshot_roundtrip_restores_deleted_note(hub: TenantHub) -> None:
    """备份 → 删掉 → 恢复 → 内容回来（验收：round-trip）。"""
    await hub.write("alice", title="要保住的记忆", body="正文内容",
                    lib_id="group-研发")
    await hub.write("alice", title="长文", body="长文正文", lib_id="group-研发",
                    extra={"versioned": True})
    tools = TenantTools(hub)
    snap = await tools.memory_snapshot("alice")
    assert snap["ok"] is True and Path(snap["path"]).is_file()

    await hub.delete("alice", "要保住的记忆", lib_id="group-研发")
    gone = await tools.memory_get("alice", "要保住的记忆", lib="group-研发")
    assert gone["ok"] is False

    res = await tools.memory_restore("alice", snap["path"], confirm=True)
    assert res["ok"] is True, res
    assert res["pre_restore_snapshot"], "恢复前没打回退快照"

    # 恢复完换个 hub 读（旧 hub 的内存与文件缓存还是旧状态；
    # TenantHub 构造时从磁盘读 tenants.json —— 正是"改完必须重启"的体现）
    h2 = TenantHub(hub.home)
    got = await TenantTools(h2).memory_get("alice", "要保住的记忆", lib="group-研发")
    assert got["ok"] is True
    assert got["note"]["body"].strip() == "正文内容"


@pytest.mark.asyncio
async def test_snapshot_read_back_matches_member_count(hub: TenantHub) -> None:
    """打完必须**真解一遍**：只报"文件存在"的备份工具会让人带废包去恢复。"""
    await hub.write("alice", title="一条", body="x", lib_id="group-研发")
    r = hub.snapshot_home()
    assert r["ok"] is True
    with tarfile.open(r["path"], "r:gz") as tf:
        names = [m.name for m in tf.getmembers() if m.isfile()]
    assert len(names) == r["members"]
    assert any(n.startswith("libs/group-研发/") for n in names)
    assert any(n.startswith(f"{VERSIONED_DIRNAME}/") or n == "tenants.json"
               for n in names), "版本库/身份表没进包"


@pytest.mark.asyncio
async def test_snapshot_excludes_credentials_by_default(hub: TenantHub) -> None:
    """⚠️ 默认**不含** state/ —— 那里是令牌与主密钥。

    备份包是最容易被顺手发出去的东西（传网盘、发群里"帮我看看"）。
    默认把凭据打进去，等于每次备份都在制造一个泄露面。
    """
    st = hub.home / "state"
    st.mkdir(parents=True, exist_ok=True)
    (st / "tokens.json").write_text('{"secret": "x"}', encoding="utf-8")
    plain = hub.snapshot_home()
    with tarfile.open(plain["path"], "r:gz") as tf:
        names = [m.name for m in tf.getmembers()]
    assert not any(n.startswith("state/") for n in names), "凭据被默认打进备份了"

    with_state = hub.snapshot_home(with_state=True)
    with tarfile.open(with_state["path"], "r:gz") as tf:
        names2 = [m.name for m in tf.getmembers()]
    assert any(n.startswith("state/") for n in names2), "显式要了却没带上"
    assert any("按机密对待" in w for w in with_state["warnings"])


@pytest.mark.asyncio
async def test_restore_without_confirm_only_previews(hub: TenantHub) -> None:
    """不 confirm 只给预览，**一个字节都不动**。"""
    await hub.write("alice", title="别乱覆盖我", body="x", lib_id="group-研发")
    snap = hub.snapshot_home()["path"]
    await hub.delete("alice", "别乱覆盖我", lib_id="group-研发")

    tools = TenantTools(hub)
    r = await tools.memory_restore("alice", snap, confirm=False)
    assert r["ok"] is False and r["code"] == "GOV_CONFIRM_REQUIRED"
    assert "libs" in r["preview"]["will_replace"]
    still_gone = await TenantTools(TenantHub(hub.home)).memory_get(
        "alice", "别乱覆盖我", lib="group-研发"
    )
    assert still_gone["ok"] is False, "没 confirm 也动了数据 —— 预览必须零副作用"


@pytest.mark.asyncio
async def test_restore_refuses_tar_slip(hub: TenantHub) -> None:
    """⚠️ 包里带越界路径就**整包拒绝**，不是"跳过坏成员"。

    一个畸形包通常意味着它被改过或损坏 —— 那种情况下"部分恢复"
    比不恢复更糟：库会变成一半新一半旧，没人说得清现在是哪个状态。
    """
    import io

    evil = hub.home / "evil.tar.gz"
    outside = hub.home.parent / "PWNED.md"
    data = b"escape\n"
    with tarfile.open(evil, "w:gz") as tf:
        for name in ("../../PWNED.md", "libs/group-研发/notes/x.md"):
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))

    tools = TenantTools(hub)
    r = await tools.memory_restore("alice", str(evil), confirm=True)
    assert r["ok"] is False and r["code"] == "INVALID_ARGUMENT"
    assert not outside.exists(), "tar slip：包里的越界路径被解到了目录外"


@pytest.mark.asyncio
async def test_restore_is_admin_only(hub: TenantHub) -> None:
    """整目录恢复覆盖的是**所有人的库 + 身份表**，member 不该拿得到。"""
    snap = hub.snapshot_home()["path"]
    tools = TenantTools(hub)
    r = await tools.memory_restore("bob", snap, confirm=True)
    assert r["ok"] is False and r["code"] == "SCOPE_DENIED"


@pytest.mark.asyncio
async def test_snapshot_covers_history_and_trash(hub: TenantHub) -> None:
    """快照打的是**磁盘现状**：回收站与改前快照都在包里。

    这是它和 export 的分界 —— export 导条目 bundle，这两样都不带，
    所以"恢复一台机器"只能靠快照。
    """
    await hub.write("alice", title="先写后删", body="v1", lib_id="group-研发")
    await hub.write("alice", title="先写后删", body="v2", lib_id="group-研发")
    await hub.write("alice", title="在回收站", body="x", lib_id="group-研发")
    await hub.delete("alice", "在回收站", lib_id="group-研发")

    r = hub.snapshot_home()
    with tarfile.open(r["path"], "r:gz") as tf:
        names = [m.name for m in tf.getmembers() if m.isfile()]
    assert any("/.history/" in n for n in names), "改前快照没进备份"
    assert any("/.trash/" in n for n in names), "回收站没进备份"


@pytest.mark.asyncio
async def test_versioned_history_survives_a_snapshot_restore(hub: TenantHub) -> None:
    """`_versioned/` 必须在备份集里 —— 不然恢复出来的库丢了长文历史。"""
    await hub.write("alice", title="要留痕的长文", body="v1", lib_id="group-研发",
                    extra={"versioned": True})
    await hub.write("alice", title="要留痕的长文", body="v2", lib_id="group-研发",
                    extra={"versioned": True})
    r = hub.snapshot_home()
    with tarfile.open(r["path"], "r:gz") as tf:
        names = [m.name for m in tf.getmembers() if m.isfile()]
    assert any(n.startswith(f"{VERSIONED_DIRNAME}/files/group-研发/") for n in names), (
        "版本库没进备份：恢复后 versioned 条目的历史全丢"
    )
