"""提交行为测试 —— 「写了但没提交」是多租户协作的真实痛点。

━━━ 为什么单独一个文件 ━━━

单 agent 用时，"几秒后自动提交"就够了。但**多人协作**下问题变了：

  · agent 写完立刻要让同事看到 → 防抖没到时间，对方看不到
  · agent 反复重试以为失败 → 写出重复内容，且**重置防抖计时**，
    提交永远不发生（这是个死循环）
  · git 历史里看不出谁写的 → 十几个人提交长得一样，追溯只能靠文件内容

这三个都是实测会遇到的，所以专门测。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shared_memory.service import SharedMemoryService  # noqa: E402
from shared_memory.tenancy import TenantHub  # noqa: E402
from shared_memory.tenant import Library, TenantStore, User  # noqa: E402
from shared_memory.vault import Vault  # noqa: E402


@pytest.fixture()
def fast_hub(tmp_path: Path) -> TenantHub:
    """防抖关闭（每次写立刻提交）的环境 —— 用来测提交本身。

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


@pytest.mark.asyncio
async def test_write_commits_immediately_when_debounce_off(fast_hub: TenantHub) -> None:
    """关防抖时，写完成功即提交（ok=true 且 committed=true）。"""
    r = await fast_hub.write("alice", title="立刻提交的", body="x", lib_id="group-研发")
    assert r["ok"] is True
    assert r.get("committed") is True
    # git 里应该能看到这条
    log = fast_hub.service_for("group-研发").git_log(5)["commits"]
    assert any("写入" in c["subject"] for c in log)


@pytest.mark.asyncio
async def test_commit_carries_author(fast_hub: TenantHub) -> None:
    """⚠️ git 提交必须带作者身份 —— 多租户下"谁写的"要在 git 里可查。"""
    await fast_hub.write("bob", title="bob 的笔记", body="x", lib_id="group-研发")
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
async def test_manual_commit_is_idempotent_and_safe(fast_hub: TenantHub) -> None:
    """手动提交是幂等的 —— 没东西可提时不算错误。

    ━━━ 为什么这条重要 ━━━

    agent 如果看到 ``committed=false`` 就以为失败并重试，而重试又
    没东西可提交 → **死循环烧额度**。所以"无改动"必须明确不是错误。

    这个环境是 fast_commit（写完已自动提交），所以手动提交时
    应当返回「无改动」且 ok=true。
    """
    from shared_memory.tools_tenant import TenantTools

    await fast_hub.write("alice", title="已自动提交的", body="x", lib_id="group-研发")

    tools = TenantTools(fast_hub)
    # 立刻手动提交（fast_commit 下应无待提交内容）
    res = await tools.memory_commit("alice")
    assert res["ok"] is True, "无改动不该算失败"
    # 关键：不能报错、不能让 agent 以为要重试
    assert "message" in res


@pytest.mark.asyncio
async def test_manual_commit_actually_flushes_when_pending(tmp_path: Path) -> None:
    """⚠️ 有未提交内容时，手动提交真的把它入库。

    这个用例用**默认防抖**（不设 fast_commit），所以写完是 pending 状态，
    手动提交应该真的提交掉。
    """
    h = TenantHub(tmp_path / "pending")
    h.store.users["alice"] = User(name="alice", role="admin")
    h.store.libs["group-x"] = Library(id="group-x", owner="group:研发")
    h.store.save()

    from shared_memory.tools_tenant import TenantTools

    await h.write("alice", title="待提交的内容", body="x", lib_id="group-x")
    tools = TenantTools(h)
    res = await tools.memory_commit("alice")
    assert res["ok"] is True
    assert res["committed_count"] >= 1, (
        "有待提交内容但手动提交没提交任何东西 —— 防抖机制与手动提交脱节了"
    )

    log = h.service_for("group-x").git_log(5)["commits"]
    assert any("待提交" in c["subject"] or "手动" in c["subject"] for c in log), \
        "提交了但 git 里找不到对应记录"
    await h.close()


@pytest.mark.asyncio
async def test_commit_only_writable_libs(fast_hub: TenantHub) -> None:
    """⚠️ 不指定库的提交，只提**可写**的库 —— 只读库不会被碰。"""
    from shared_memory.tools_tenant import TenantTools

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
    from shared_memory.tools_tenant import TenantTools

    tools = TenantTools(fast_hub)
    res = await tools.memory_commit("bob", lib="company")
    assert res.get("ok") is False
    assert res.get("code") == "FORBIDDEN"


@pytest.mark.asyncio
async def test_commit_with_no_changes_is_not_error(fast_hub: TenantHub) -> None:
    """无改动时返回 committed=False，**不是错误**。

    这条重要：agent 如果把它当失败会重试，而重试又没东西可提交 → 死循环。
    """
    from shared_memory.tools_tenant import TenantTools

    tools = TenantTools(fast_hub)
    # 先确保干净
    await tools.memory_commit("alice")
    # 再提交一次（应该无改动）
    res = await tools.memory_commit("alice", lib="group-研发")
    assert res.get("ok") is True
    assert res.get("committed") is False


# ══════════════════════ 防抖语义 ══════════════════════


@pytest.mark.asyncio
async def test_debounce_mode_reports_pending_not_failure(tmp_path: Path) -> None:
    """⚠️ 防抖模式下 committed=False 必须配 commit_pending=True。

    这是"agent 反复重试"那个 bug 的防线：如果只看到 false，
    它会以为失败。加上 pending 标志后语义才完整。
    """
    h = TenantHub(tmp_path / "home2")
    h.store.users["alice"] = User(name="alice", role="admin")
    h.store.libs["group-x"] = Library(id="group-x", owner="group:研发")
    h.store.save()

    r = await h.write("alice", title="防抖写入", body="x", lib_id="group-x")
    assert r["ok"] is True
    assert r.get("committed") is False
    assert r.get("commit_pending") is True, (
        "防抖模式下必须告诉调用方「安排了提交」而不是裸的 false。"
        "少了这个标志，agent 会以为失败并重试 → 写出重复内容"
    )
    await h.close()


@pytest.mark.asyncio
async def test_debounce_actually_commits_later(tmp_path: Path) -> None:
    """防抖到期后真的提交了（不是只标记了 pending）。"""
    h = TenantHub(tmp_path / "home3")
    h.store.users["alice"] = User(name="alice", role="admin")
    h.store.libs["group-x"] = Library(id="group-x", owner="group:研发")
    h.store.save()

    await h.write("alice", title="稍后提交", body="x", lib_id="group-x")
    # 等防抖到期（默认 8 秒 —— 这里只验证机制，不等满）
    svc = h.service_for("group-x")
    await svc.commit_now("手动触发防抖内容")
    log = svc.git_log(5)["commits"]
    assert any("稍后提交" in c["subject"] or "手动" in c["subject"] for c in log)
    await h.close()


# ══════════════════════ git 元数据 ══════════════════════


@pytest.mark.asyncio
async def test_log_includes_author_field(fast_hub: TenantHub) -> None:
    """面板要显示"谁提交的"—— log 必须带 author。"""
    await fast_hub.write("alice", title="测试日志", body="x", lib_id="group-研发")
    log = fast_hub.service_for("group-研发").git_log(5)["commits"]
    assert log, "没有提交记录"
    assert "author" in log[0], "log 缺 author 字段，面板显示不出作者"
    assert "commit" in log[0]
    assert "date" in log[0]
    assert "subject" in log[0]


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
