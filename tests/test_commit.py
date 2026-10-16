"""提交行为测试 —— v2 S-8 之后，「写」和「入库」是两件分开的事。

━━━ 为什么单独一个文件 ━━━

v1 的模型是"每次写都防抖提交"，于是三件事被绑在一起：
写入是否成功、内容是否入库、谁写的。多人协作下实测出三个问题：

  · agent 看到 ``committed=false`` 以为写失败 → 重试 → 写出重复内容，
    而且每次重试都重置防抖计时，**提交永远不发生**（死循环）
  · 连记 5 条 = 5 个提交，历史被"改个措辞"淹没，
    真想 diff 的那条长文反而找不到
  · git 历史里看不出谁写的 → 十几个人提交长得一样

v2 的分工（这一版测试就是在钉这几条边界）：

  · 普通写入 **零 git 提交**（S-8.2 的验收口径）
  · 防手滑 = 库内 `.history/` 改前快照；版本控制 = 只有 ``versioned`` 进共享库
  · **删除/恢复仍然立刻提交** —— 破坏性动作必须留痕
  · 手动提交仍然幂等、"无改动"不是错误、只提可写库、提交带作者
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shared_memory.tenancy import TenantHub  # noqa: E402
from shared_memory.tenant import Library, User  # noqa: E402
from shared_memory.tools_tenant import TenantTools  # noqa: E402
from shared_memory.vault import Vault  # noqa: E402


@pytest.fixture()
def fast_hub(tmp_path: Path) -> TenantHub:
    """配置/导入类改动不等防抖的环境（``fast_commit``）。

    ⚠️ 别按旧语义理解这个开关：v2 之后**写入根本不产生提交**，
    所以 fast_commit=True 也不会让写变成"写完即提交"。它只影响
    配置变更/批量导入那两条仍然走防抖的路径。

    ━━━ 为什么不 monkey-patch service_for ━━━

    第一版用 patch，结果 ``TenantHub.write`` 内部直接访问
    ``self._services`` 而绕过了 patch —— 测试"看起来在测"，其实
    走的是默认防抖路径。**这就是为什么三个提交测试全失败。**

    改成用一个真实的开关（``fast_commit`` 属性）让 TenantHub 自己
    去用 debounce=0，而不是从外面替换它的方法。
    """
    h = TenantHub(tmp_path / "home")
    h.store.users["alice"] = User(name="alice", role="admin", groups=["研发"])
    h.store.users["bob"] = User(name="bob", role="member", groups=["研发"])
    h.store.libs["company"] = Library(id="company", owner="company",
                                      writable_by_members=False)
    h.store.libs["group-研发"] = Library(id="group-研发", owner="group:研发")
    h.store.libs["user-bob"] = Library(id="user-bob", owner="user:bob")
    h.store.save()
    # 真实开关：TenantHub.service_for 会读它来决定防抖
    h.fast_commit = True
    return h


# ══════════════════════ S-8.2：写入不产生提交 ══════════════════════


@pytest.mark.asyncio
async def test_ordinary_write_makes_no_commit(fast_hub: TenantHub) -> None:
    """⚠️ S-8.2 的核心验收：**没标 versioned 的写入零 git 提交**。

    断言的是"git 里查无此题"，不是"有一条提交但不该有"——
    只有前者能证明写入路径真的没 fork git。
    """
    r = await fast_hub.write("alice", title="普通写入", body="x", lib_id="group-研发")
    assert r["ok"] is True
    assert r.get("committed") is False
    log = fast_hub.service_for("group-研发").git_log(20)["commits"]
    assert not any("普通写入" in c["subject"] for c in log), (
        "普通写入仍产生提交 —— S-8.2 的零 git 操作口径被破坏"
    )


@pytest.mark.asyncio
async def test_write_result_no_longer_promises_commit_pending(fast_hub: TenantHub) -> None:
    """``commit_pending`` 这个键必须消失。

    它当年用来解释"防抖还没到点，稍后会提交"。新模型里写入压根不排队
    提交，留着这个键就是在暗示一个**永远不会发生**的提交 ——
    agent 会据此等下去或反复确认。
    """
    r = await fast_hub.write("alice", title="不该有 pending", body="x", lib_id="group-研发")
    assert "commit_pending" not in r, (
        "写入返回里还在承诺'稍后提交'：新模型下这个期待不成立"
    )


@pytest.mark.asyncio
async def test_delete_still_commits_for_audit(fast_hub: TenantHub) -> None:
    """⚠️ 删除是**破坏性**动作，仍要立刻进 git —— 这是审计兜底。

    为什么单拎一条测：新模型把"写不提交"讲得很响，顺手把删除也改成
    不提交是很自然的做法 —— 而那样 `.trash` 被 gc 物理清掉之后真的没了
    （gc_trash 的安全性论证靠的就是"git 历史还能捞回来"）。
    """
    await fast_hub.write("bob", title="将被删的", body="x", lib_id="group-研发")
    r = await fast_hub.delete("bob", "将被删的", lib_id="group-研发")
    assert r["ok"] is True
    assert r["committed"] is True, "删除没提交：误删之后 git 里连痕迹都查不到"
    log = fast_hub.service_for("group-研发").git_log(10)["commits"]
    assert any("删除" in c["subject"] for c in log)


# ══════════════════════ 手动提交：仍然存在的入库通道 ══════════════════════


@pytest.mark.asyncio
async def test_manual_commit_sees_uncommitted_writes(tmp_path: Path) -> None:
    """⚠️ 手动提交必须能收掉"写了但没提交"的改动。

    旧实现靠 service 内部的脏标记判断有没有东西要提。**新模型里写入
    不再置脏**，所以如果还按脏标记判，手动提交会回答"无改动"，
    而工作树里其实躺着几十条没入库的写。这个 bug 只有这条用例抓得住。
    """
    h = TenantHub(tmp_path / "pending")
    h.store.users["alice"] = User(name="alice", role="admin")
    h.store.libs["group-x"] = Library(id="group-x", owner="group:研发")
    h.store.save()

    await h.write("alice", title="待提交的内容", body="x", lib_id="group-x")
    tools = TenantTools(h)
    res = await tools.memory_commit("alice")
    assert res["ok"] is True
    assert res["committed_count"] >= 1, (
        "工作树有未提交内容，手动提交却说无改动 —— 脏标记已经不能当判据了"
    )

    log = h.service_for("group-x").git_log(5)["commits"]
    assert any("待提交" in c["subject"] or "手动" in c["subject"] for c in log), \
        "提交了但 git 里找不到对应记录"
    await h.close()


@pytest.mark.asyncio
async def test_manual_commit_is_idempotent_and_safe(fast_hub: TenantHub) -> None:
    """手动提交是幂等的 —— 没东西可提时不算错误。

    ━━━ 为什么这条重要 ━━━

    agent 看到 ``committed=false`` 若以为失败就重试，而重试又没东西可提交
    → **死循环烧额度**。所以"无改动"必须是 ok=true 且说得出口。
    """
    tools = TenantTools(fast_hub)
    await fast_hub.write("alice", title="提一次就够了", body="x", lib_id="group-研发")

    first = await tools.memory_commit("alice", lib="group-研发")
    assert first["ok"] is True
    second = await tools.memory_commit("alice", lib="group-研发")
    assert second["ok"] is True, "无改动不该算失败"
    assert second.get("committed") is False
    assert "无改动" in (second.get("message") or ""), (
        f"第二次提交应明确说「无改动」，实际 {second!r}"
    )


@pytest.mark.asyncio
async def test_commit_only_writable_libs(fast_hub: TenantHub) -> None:
    """⚠️ 不指定库的提交，只提**可写**的库 —— 只读库不会被碰。"""
    tools = TenantTools(fast_hub)
    # bob 不是 admin，company 库对他只读
    res = await tools.memory_commit("bob")
    lib_ids = {r["lib"] for r in res["results"]}
    assert "company" not in lib_ids, (
        f"只读库被提交了（{lib_ids}）。"
        "虽然内容一样，但这说明代码没按写权限过滤"
    )


@pytest.mark.asyncio
async def test_commit_to_unauthorized_lib_refused(fast_hub: TenantHub) -> None:
    """⚠️ 指定只读库提交要被拒（不能绕过权限）。"""
    tools = TenantTools(fast_hub)
    res = await tools.memory_commit("bob", lib="company")
    assert res.get("ok") is False
    assert res.get("code") == "FORBIDDEN"


@pytest.mark.asyncio
async def test_commit_with_no_changes_is_not_error(fast_hub: TenantHub) -> None:
    """无改动时返回 committed=False，**不是错误**。

    这条重要：agent 如果把它当失败会重试，而重试又没东西可提交 → 死循环。
    """
    tools = TenantTools(fast_hub)
    await tools.memory_commit("alice")          # 先提干净
    res = await tools.memory_commit("alice", lib="group-研发")
    assert res.get("ok") is True
    assert res.get("committed") is False


# ══════════════════════ 作者可追溯性（新模型下更要测）══════════════════════


@pytest.mark.asyncio
async def test_commit_carries_author(fast_hub: TenantHub) -> None:
    """⚠️ git 提交必须带作者身份 —— 多租户下"谁写的"要在 git 里可查。

    写入不再自动提交之后，作者归属只剩两条来源：显式提交与删除提交。
    所以这条比 v1 更关键 —— 没有它，git 历史里全是 shared-memory。
    """
    await fast_hub.write("bob", title="bob 的笔记", body="x", lib_id="group-研发")
    await fast_hub.service_for("group-研发").commit_now("收一下 bob 的写")
    log = fast_hub.service_for("group-研发").git_log(5)["commits"]
    authors = {c.get("author") for c in log}
    assert "bob" in authors, (
        f"git 里找不到 bob 这个作者（实际 {authors}）。"
        "frontmatter 的 author 可以被随手删，git author 删不掉 —— 这才是可追溯的"
    )


@pytest.mark.asyncio
async def test_commit_author_survives_frontmatter_deletion(fast_hub: TenantHub) -> None:
    """⚠️ 删掉 frontmatter 的 author 后，git 仍然记得是谁。

    这是"提交带作者"的真正价值：**文件内容可改，git 元数据不可抵赖**。
    """
    await fast_hub.write("bob", title="会被改的记忆", body="x", lib_id="group-研发")
    await fast_hub.service_for("group-研发").commit_now("先入库")

    # 直接改文件，把 author 字段抹掉
    vault = fast_hub.libs.vault(fast_hub.store.lib("group-研发"))
    for p in vault.notes_dir.glob("*.md"):
        if "会被改的记忆" in p.name:
            txt = p.read_text(encoding="utf-8")
            txt = txt.replace("author: bob\n", "")
            p.write_text(txt, encoding="utf-8")

    log = fast_hub.service_for("group-研发").git_log(5)["commits"]
    assert any(c.get("author") == "bob" for c in log), "作者信息随文件内容丢失了"


@pytest.mark.asyncio
async def test_log_includes_author_field(fast_hub: TenantHub) -> None:
    """面板要显示"谁提交的"—— log 必须带 author。"""
    await fast_hub.write("alice", title="测试日志", body="x", lib_id="group-研发")
    await fast_hub.service_for("group-研发").commit_now("入库")
    log = fast_hub.service_for("group-研发").git_log(5)["commits"]
    assert log, "没有提交记录"
    assert "author" in log[0], "log 缺 author 字段，面板显示不出作者"
    assert "commit" in log[0]
    assert "date" in log[0]
    assert "subject" in log[0]


# ══════════════════════ vault 层的 git 行为（与模型无关，保留）══════════════════════


def test_commit_without_repo_returns_not_error(tmp_path: Path) -> None:
    """非 git 仓库时返回 False 而不是抛（不能因为没 git 就写不了）。"""
    v = Vault(tmp_path / "plain")
    v.notes_dir.mkdir(parents=True, exist_ok=True)
    r = v.commit("测试")
    assert r["committed"] is False
    assert "不是 git 仓库" in r["message"]


def test_commit_author_uses_single_override(tmp_path: Path) -> None:
    """作者用单次 -c 覆盖，**不改仓库级 config**。

    这一点重要：仓库级 config 是全局的，会被下一次别人的提交覆盖，
    导致"上一条显示 bob，这一条显示 alice"的混乱。
    """
    v = Vault(tmp_path / "cfgtest")
    v.init_repo()
    (v.notes_dir / "a.md").write_text("# a\n\n内容\n", encoding="utf-8")
    v.commit("第一条", author="alice")

    # 仓库级 config 不该被改成 alice
    r = v._git("config", "user.name")
    assert r.stdout.strip() == "shared-memory", (
        f"仓库级 user.name 被改成了 {r.stdout.strip()} —— "
        "这会让后续所有提交都用这个身份"
    )


def test_commit_email_is_local_not_fake_external(tmp_path: Path) -> None:
    """作者邮箱用 @shared-memory.local，**不伪造外部邮箱**。

    伪造成某公司邮箱会让人误以为提交真的来自那里。
    """
    v = Vault(tmp_path / "emailtest")
    v.init_repo()
    (v.notes_dir / "a.md").write_text("# a\n\n内容\n", encoding="utf-8")
    v.commit("测试", author="bob")
    r = v._git("log", "-1", "--format=%ae")
    assert r.stdout.strip() == "bob@shared-memory.local"


@pytest.mark.asyncio
async def test_flush_stops_at_shutdown(tmp_path: Path) -> None:
    """停机时把防抖待提交的内容收掉（否则最后几条改动永远不进 git）。

    这条专门盯着**仍走防抖**的那类改动（配置变更）：
    写入不再自动提交之后，配置是防抖机制剩下的主要用户，
    而"改了配置但没落库"在停机时会静默丢掉。
    """
    h = TenantHub(tmp_path / "shutdown")
    h.store.users["alice"] = User(name="alice", role="admin")
    h.store.libs["group-x"] = Library(id="group-x", owner="group:研发")
    h.store.save()
    svc = h.service_for("group-x")
    await svc.update_config(settings={"history_keep": 2})
    await h.close()
    log = svc.git_log(5)["commits"]
    assert any("服务停止" in c["subject"] for c in log), (
        f"停机没把防抖中的配置改动收掉：{[c['subject'] for c in log]}"
    )
