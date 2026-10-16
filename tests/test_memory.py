"""验收测试 —— 只测真正会出问题的地方。

━━━ 测什么（以及为什么不测别的）━━━

1. **FIFO 串行**：并发写 N 条，一条都不能丢（整套设计的核心承诺）
2. **队列满要拒绝**，不是无限等待 —— 这条曾经真的挂死过
3. **ack 在落盘之后**：submit 返回时数据必须已可读
4. **异常不能弄死写者**：一次失败不该让整个服务永久失效
5. vault 的增删改查、软删除与恢复、搜索排序、时间过滤、分页
6. git 提交（含中文消息 —— 曾经因编码崩过）
7. 错误信封形状（agent 依赖它识别失败）

**不测**：代码行数、行覆盖率这类与"承诺"无关的指标。
每个测试都对应一条对外承诺或一个真实踩过的坑。
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from shared_memory.errors import ErrorCode, ToolError  # noqa: E402
from shared_memory.queue import WriteQueue  # noqa: E402
from shared_memory.service import SharedMemoryService  # noqa: E402
from shared_memory.vault import Vault  # noqa: E402


# ═════════════════════  fixtures  ═════════════════════


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    """一个空的、已初始化的 vault。"""
    v = Vault(tmp_path / "vault")
    v.init_repo()
    return v


@pytest.fixture
def service(vault: Vault) -> SharedMemoryService:
    """关掉防抖的服务实例（测试里要立刻提交，便于断言）。"""
    svc = SharedMemoryService(vault, commit_debounce=0)
    svc.start()
    return svc


# ═════════════════════  队列：核心承诺  ═════════════════════


@pytest.mark.asyncio
async def test_fifo_serializes_concurrent_writes() -> None:
    """并发写 50 条，**一条都不能丢** —— 串行化生效的核心证据。"""
    q = WriteQueue()
    q.start()
    seen: list[int] = []

    async def job(i: int) -> int:
        # 制造交错的 await 点：如果并发没有被串行化，这里就会互相踩
        await asyncio.sleep(0)
        seen.append(i)
        await asyncio.sleep(0)
        return i

    results = await asyncio.gather(*(q.submit(lambda i=i: job(i)) for i in range(50)))
    await q.stop()

    assert len(results) == 50
    assert len(seen) == 50, "并发写入丢数据"
    assert results == list(range(50)), "队列没有保持 FIFO 顺序"


@pytest.mark.asyncio
async def test_sync_callable_also_accepted() -> None:
    """同步函数也要能用。

    回归保护：`_run` 曾经无条件 `await fn()`，传普通函数会抛
    `TypeError: object dict can't be used in 'await' expression`，
    而且错误发生在写者循环里、只回给 future，极难定位。
    """
    q = WriteQueue()
    q.start()
    result = await q.submit(lambda: {"ok": True})
    assert result == {"ok": True}
    await q.stop()


@pytest.mark.asyncio
async def test_exception_does_not_break_queue() -> None:
    """一个写失败不能让写者退出 —— 否则整个记忆服务永久失效。"""
    q = WriteQueue()
    q.start()

    async def boom() -> None:
        raise RuntimeError("模拟写失败")

    with pytest.raises(RuntimeError):
        await q.submit(boom)

    # 队列必须还活着，且能继续处理
    assert await q.submit(lambda: asyncio.sleep(0, result="ok")) == "ok"
    assert q.stats()["worker_alive"] is True
    await q.stop()


@pytest.mark.asyncio
async def test_queue_rejects_when_full() -> None:
    """队列满要**明确拒绝**，而不是无限等待或无限堆积。

    ⚠️ 这条测试曾经真的挂死过整个套件：容量检查当时只数 qsize()，
    没算正在执行的任务 —— 于是满员时不是拒绝而是**永久等待**。

    用 wait_for 包住：如果回归成静默挂起，测试应该**失败**而不是挂死。
    """
    q = WriteQueue(max_depth=2)
    gate = asyncio.Event()

    async def slow() -> str:
        await gate.wait()
        return "done"

    tasks = [asyncio.create_task(q.submit(slow)) for _ in range(2)]
    await asyncio.sleep(0.05)  # 让前两个占满名额

    with pytest.raises(ToolError) as ei:
        await asyncio.wait_for(q.submit(slow), timeout=2)
    assert ei.value.code == ErrorCode.QUEUE_FULL
    assert q.stats()["rejected"] == 1

    gate.set()
    await asyncio.gather(*tasks)
    await q.stop()


@pytest.mark.asyncio
async def test_outstanding_returns_to_zero_after_all_done() -> None:
    """全部完成后计数必须归零。

    回归保护：计数若在某条路径上漏减，队列容量会随调用次数"缩水"，
    最终永久拒绝所有写入 —— 而且症状是"服务用久了就写不进去"，很难查。
    """
    q = WriteQueue()
    q.start()
    await asyncio.gather(*(q.submit(lambda: asyncio.sleep(0)) for _ in range(10)))
    st = q.stats()
    assert st["outstanding"] == 0, "名额泄漏"
    assert st["pending"] == 0
    await q.stop()


@pytest.mark.asyncio
async def test_ack_happens_after_work_is_done() -> None:
    """submit 返回时，副作用必须**已经发生**（不能入队即 ack）。"""
    q = WriteQueue()
    q.start()
    done = False

    async def work() -> str:
        nonlocal done
        await asyncio.sleep(0.01)
        done = True
        return "finished"

    result = await q.submit(work)
    assert done is True, "ack 早于实际完成"
    assert result == "finished"
    await q.stop()


# ═════════════════════  vault：文件读写  ═════════════════════


def test_put_then_get_roundtrip(vault: Vault) -> None:
    """写入后能按标题读回，元数据完整。"""
    vault.put(title="测试记忆", body="正文内容", tags=["t1", "t2"], author="pytest")
    note = vault.get("测试记忆")
    assert note.body.strip() == "正文内容"
    assert note.tags == ["t1", "t2"]
    assert note.author == "pytest"
    assert note.id.startswith("mem-")
    assert note.created and note.updated


def test_get_by_id_also_works(vault: Vault) -> None:
    """id 与标题都能作为主键 —— 两者都要能解析。"""
    r = vault.put(title="按 id 读", body="x")
    assert vault.get(r["id"]).title == "按 id 读"


def test_same_title_updates_not_duplicates(vault: Vault) -> None:
    """同标题 = 同一条记忆（更新而非新建）。"""
    vault.put(title="重复标题", body="第一次")
    vault.put(title="重复标题", body="第二次")
    assert len(vault.all_notes()) == 1
    assert vault.get("重复标题").body.strip() == "第二次"


def test_update_preserves_created_timestamp(vault: Vault) -> None:
    """更新不能改 created —— 它记录的是这条记忆最初被写下的时间。

    否则"最近创建"这类排序会失真（老记忆每次被改都变成"新"的）。
    """
    vault.put(title="时间戳", body="v1")
    created = vault.get("时间戳").created
    vault.put(title="时间戳", body="v2")
    assert vault.get("时间戳").created == created


def test_update_keeps_tags_when_not_given(vault: Vault) -> None:
    """只改正文时不传 tags，原有标签必须保留（不能静默清空）。"""
    vault.put(title="标签保留", body="v1", tags=["a", "b"])
    vault.put(title="标签保留", body="v2")  # 不传 tags
    assert set(vault.get("标签保留").tags) == {"a", "b"}


def test_create_mode_conflicts(vault: Vault) -> None:
    """create 模式已存在要报冲突（防误覆盖）。"""
    vault.put(title="已存在", body="x")
    with pytest.raises(ToolError) as ei:
        vault.put(title="已存在", body="y", mode="create")
    assert ei.value.code == ErrorCode.CONFLICT
    assert vault.get("已存在").body.strip() == "x", "冲突时不该改动原内容"


def test_append_mode_keeps_old_content(vault: Vault) -> None:
    """append 保留已有内容。"""
    vault.put(title="追加测试", body="第一段")
    vault.put(title="追加测试", body="第二段", mode="append")
    body = vault.get("追加测试").body
    assert "第一段" in body and "第二段" in body


def test_blank_title_rejected(vault: Vault) -> None:
    """空标题要明确报错，而不是创建出无法用标题索引的孤儿文件。"""
    for bad in ("", "   ", "\n"):
        with pytest.raises(ToolError) as ei:
            vault.put(title=bad, body="x")
        assert ei.value.code == ErrorCode.INVALID_ARGUMENT


def test_missing_note_raises_not_found(vault: Vault) -> None:
    with pytest.raises(ToolError) as ei:
        vault.get("根本不存在的标题")
    assert ei.value.code == ErrorCode.NOT_FOUND


def test_same_slug_different_title_does_not_overwrite(vault: Vault) -> None:
    """slug 撞名时不能互相覆盖。

    回归保护：两个不同标题 slug 后可能得到同一个文件名
    （空白与 `/` 都会被压成 `-`）。不加后缀的话会**静默丢一条**。
    """
    vault.put(title="部署 A", body="内容一")
    vault.put(title="部署/A", body="内容二")  # 斜杠被 _slug 去掉后同名
    notes = vault.all_notes()
    assert len(notes) == 2, f"slug 撞名导致覆盖，只剩 {len(notes)} 条"
    bodies = {n.body.strip() for n in notes}
    assert bodies == {"内容一", "内容二"}


# ═════════════════════  软删除与恢复  ═════════════════════


def test_delete_is_soft(vault: Vault) -> None:
    """删除是软删除：内容进回收站，可从磁盘找回。"""
    vault.put(title="待删除", body="内容")
    result = vault.delete("待删除")
    assert result["action"] == "deleted"
    trashed = list((vault.root / ".trash").glob("*.md"))
    assert len(trashed) == 1
    assert "内容" in trashed[0].read_text(encoding="utf-8")
    # 原位置应该已经没了
    with pytest.raises(ToolError):
        vault.get("待删除")


def test_restore_brings_it_back(vault: Vault) -> None:
    """回收站能恢复，且内容完整。"""
    vault.put(title="先删后恢复", body="要保住的内容")
    vault.delete("先删后恢复")
    items = vault.trash()
    assert len(items) == 1
    r = vault.restore(items[0]["file"])
    assert r["action"] == "restored"
    assert vault.get("先删后恢复").body.strip() == "要保住的内容"


def test_restore_does_not_overwrite_existing(vault: Vault) -> None:
    """恢复时若已有同名记忆，**不能覆盖它**。

    ⚠️ 这个测试揭示了一个必须明确的语义：标题重名时，
    ``get(标题)`` 返回哪一条是**不确定的**（取决于文件名排序）。
    所以校验必须按 id，不能按标题 —— 否则测试会在"碰巧读到哪条"
    上摇摆，变成 flaky 测试。

    正式结论：**标题不保证唯一**，需要精确定位时用 id。
    """
    r_old = vault.put(title="同名", body="老内容")
    old_id = r_old["id"]
    vault.delete("同名")

    r_new = vault.put(title="同名", body="新内容")
    new_id = r_new["id"]
    assert new_id != old_id

    # 恢复老的那条
    vault.restore(vault.trash()[0]["file"])

    # 两条都必须在（按 id 精确校验，不依赖标题解析）
    assert len(vault.all_notes()) == 2, "恢复覆盖了已有记忆"
    assert vault.get(new_id).body.strip() == "新内容"
    assert vault.get(old_id).body.strip() == "老内容"


def test_delete_same_title_twice_keeps_both(vault: Vault) -> None:
    """同一条被删两次不能互相覆盖（回收站文件名带时间戳）。"""
    vault.put(title="反复删", body="v1")
    vault.delete("反复删")
    vault.put(title="反复删", body="v2")
    vault.delete("反复删")
    assert len(vault.trash()) == 2


# ═════════════════════  搜索  ═════════════════════


def test_search_ranks_title_hits_first(vault: Vault) -> None:
    """标题命中排在正文命中前面（权重设计）。"""
    vault.put(title="vivo 部署记录", body="无关正文")
    vault.put(title="其它", body="正文里提到 vivo 一次")
    hits = vault.search("vivo")["results"]
    assert hits and hits[0]["title"] == "vivo 部署记录"


def test_search_requires_all_words(vault: Vault) -> None:
    """多词是 AND：只命中一个词的不算。"""
    vault.put(title="A", body="苹果 香蕉")
    vault.put(title="B", body="苹果")
    hits = vault.search("苹果 香蕉")["results"]
    assert [h["title"] for h in hits] == ["A"]


def test_search_by_tags_and_logic(vault: Vault) -> None:
    """多标签是 AND。"""
    vault.put(title="A", body="x", tags=["部署", "vivo"])
    vault.put(title="B", body="x", tags=["部署"])
    hits = vault.search("", tags=["部署", "vivo"])["results"]
    assert [h["title"] for h in hits] == ["A"]


def test_search_time_filter(vault: Vault) -> None:
    """since/until 时间过滤生效。"""
    vault.put(title="旧", body="x", extra={"created": "2020-01-01T00:00:00+08:00",
                                           "updated": "2020-01-01T00:00:00+08:00"})
    vault.put(title="新", body="x")
    hits = vault.search("", since="2025-01-01")["results"]
    assert [h["title"] for h in hits] == ["新"]


def test_search_pagination(vault: Vault) -> None:
    """分页：total 是过滤后总数，count 是本页条数。"""
    for i in range(15):
        vault.put(title=f"分页-{i:02d}", body="相同正文", tags=["pg"])
    r1 = vault.search("", tags=["pg"], limit=5, offset=0)
    r2 = vault.search("", tags=["pg"], limit=5, offset=5)
    assert r1["total"] == 15 and r1["count"] == 5
    assert r2["count"] == 5
    # 两页不重叠
    t1 = {n["title"] for n in r1["results"]}
    t2 = {n["title"] for n in r2["results"]}
    assert not (t1 & t2)


def test_search_sort_by_updated(vault: Vault) -> None:
    """按更新时间排序时，最新的在最前。"""
    vault.put(title="早", body="x", extra={"updated": "2020-01-01T00:00:00+08:00"})
    vault.put(title="晚", body="x", extra={"updated": "2030-01-01T00:00:00+08:00"})
    hits = vault.search("", sort="updated")["results"]
    assert hits[0]["title"] == "晚"


def test_search_no_match_returns_empty(vault: Vault) -> None:
    """无命中返回空列表（不是报错）。"""
    vault.put(title="有内容", body="x")
    r = vault.search("绝对不存在的词")
    assert r["ok"] is True and r["total"] == 0 and r["results"] == []


# ═════════════════════  git  ═════════════════════


def test_git_commit_and_history(vault: Vault) -> None:
    """提交能进历史；无改动时不算失败。"""
    vault.put(title="提交测试", body="内容")
    r = vault.commit("test: 首次提交")
    assert r["committed"] is True and r["commit"]
    assert any("test: 首次提交" in c["subject"] for c in vault.log(5))
    # 无改动 → committed=False，但不是错误
    assert vault.commit("noop")["committed"] is False


def test_git_handles_chinese_commit_message(vault: Vault) -> None:
    """中文提交信息不能崩。

    回归保护：subprocess 不指定 encoding 时会用系统编码（Windows 上 GBK）
    解码 git 的 UTF-8 输出，中文会抛 UnicodeDecodeError，
    把一次**已经成功**的提交变成异常。
    """
    vault.put(title="中文提交", body="内容")
    r = vault.commit("中文提交信息：验证编码")
    assert r["committed"] is True
    assert any("验证编码" in c["subject"] for c in vault.log(3))


def test_uncommitted_then_committed(vault: Vault) -> None:
    """写入后未提交时，git 状态应显示有改动（否则防抖提交永远不会触发）。"""
    vault.put(title="未提交", body="x")
    r = subprocess.run(
        ["git", "-C", str(vault.root), "status", "--porcelain"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.stdout.strip(), "写入后 git 应该显示有未提交改动"


# ═════════════════════  统计与导出  ═════════════════════


def test_stats_counts(vault: Vault) -> None:
    vault.put(title="A", body="x", tags=["t1"], author="alice")
    vault.put(title="B", body="x", tags=["t1", "t2"], author="bob")
    s = vault.stats()
    assert s["notes"] == 2
    assert s["tags"] == 2
    assert dict(s["authors"])["alice"] == 1
    assert s["is_git"] is True
    assert s["unreadable"] == 0


def test_stats_reports_unreadable_file(vault: Vault) -> None:
    """读不出来的文件要被计数，而不是静默消失。

    用户应该能看到"库里有坏数据"，而不是发现条目莫名其妙变少。
    """
    vault.put(title="好的", body="x")
    # 造一个非 UTF-8 的坏文件
    (vault.notes_dir / "坏的.md").write_bytes(b"\xff\xfe\x00\x00invalid")
    s = vault.stats()
    assert s["unreadable"] == 1
    assert s["notes"] == 1, "坏文件不该让其它条目也读不出来"


def test_export_markdown_contains_all_notes(vault: Vault) -> None:
    """导出包含所有条目标题与正文。"""
    vault.put(title="导出甲", body="甲的内容", tags=["组一"])
    vault.put(title="导出乙", body="乙的内容")
    text = vault.export_markdown()
    for token in ("导出甲", "甲的内容", "导出乙", "乙的内容", "组一"):
        assert token in text


def test_export_index_only(vault: Vault) -> None:
    """include_body=False 时只给索引，不带正文。"""
    vault.put(title="只有标题", body="这段正文不该出现")
    text = vault.export_markdown(include_body=False)
    assert "只有标题" in text
    assert "这段正文不该出现" not in text


# ═════════════════════  service  ═════════════════════


@pytest.mark.asyncio
async def test_service_concurrent_writes_all_land(service: SharedMemoryService) -> None:
    """端到端：并发写 30 条，vault 里必须正好 30 条。"""
    await asyncio.gather(
        *(service.write(title=f"并发-{i}", body=f"内容 {i}", tags=["concurrent"])
          for i in range(30))
    )
    notes = service.vault.all_notes()
    assert len(notes) == 30, f"期望 30 条，实际 {len(notes)}"
    assert {n.title for n in notes} == {f"并发-{i}" for i in range(30)}


@pytest.mark.asyncio
async def test_service_rejects_bad_mode(service: SharedMemoryService) -> None:
    with pytest.raises(ToolError) as ei:
        await service.write(title="x", body="y", mode="不存在的模式")
    assert ei.value.code == ErrorCode.INVALID_ARGUMENT


@pytest.mark.asyncio
async def test_service_rejects_blank_title(service: SharedMemoryService) -> None:
    with pytest.raises(ToolError) as ei:
        await service.write(title="  ", body="y")
    assert ei.value.code == ErrorCode.INVALID_ARGUMENT


@pytest.mark.asyncio
async def test_service_stats_and_health(service: SharedMemoryService) -> None:
    await service.write(title="统计测试", body="x")
    s = service.stats()
    assert s["notes"] == 1
    assert s["write_queue"]["processed"] >= 1
    assert s["uptime_seconds"] >= 0

    h = service.health()
    assert h["ok"] is True, h["problems"]
    assert h["problems"] == []
    assert h["notes"] == 1


@pytest.mark.asyncio
async def test_health_ok_on_fresh_service(vault: Vault) -> None:
    """**刚启动、还没有任何写入**的服务必须报告健康。

    回归保护：写者循环是惰性创建的（首次 submit 才建），所以新服务里
    ``worker_alive`` 必然是 false。早期实现直接把它判成故障 ——
    结果是**每个正常启动的服务都被误报"写者循环未运行"**，
    实测中健康检查真的这样报过。
    """
    svc = SharedMemoryService(vault, commit_debounce=0)
    svc.start()
    h = svc.health()
    assert h["ok"] is True, f"新服务被误判为不健康: {h['problems']}"
    assert h["queue"]["worker_alive"] is False, "前提：此时写者确实还没创建"


@pytest.mark.asyncio
async def test_health_flags_dead_worker_after_use(vault: Vault) -> None:
    """**用过之后**写者没了才算故障 —— 这是真正该报警的情形。"""
    svc = SharedMemoryService(vault, commit_debounce=0)
    svc.start()
    await svc.write(title="用一下", body="x")  # 触发写者创建
    assert svc.health()["ok"] is True

    # 模拟写者崩溃
    await svc.queue.stop()
    svc.queue._worker = None
    # processed>0 但 worker 没了 → 应该报警
    svc.queue._processed = 5
    h = svc.health()
    assert h["ok"] is False
    assert any("写者循环已停止" in p for p in h["problems"])


@pytest.mark.asyncio
async def test_health_detects_unwritable_vault(tmp_path: Path) -> None:
    """vault 不可写时健康检查必须报出来（用真写测试，不是 os.access）。"""
    v = Vault(tmp_path / "v")
    v.init_repo()
    svc = SharedMemoryService(v, commit_debounce=0)
    svc.start()
    # 把 notes 目录换成一个同名文件 → 写入必然失败
    import shutil
    shutil.rmtree(v.notes_dir)
    v.notes_dir.write_text("not a dir", encoding="utf-8")
    h = svc.health()
    assert h["ok"] is False
    assert any("不可写" in p for p in h["problems"])


@pytest.mark.asyncio
async def test_service_trash_and_restore_via_service(service: SharedMemoryService) -> None:
    """service 层的删除/恢复/回收站列举串起来能用。"""
    await service.write(title="走服务删", body="内容")
    await service.delete("走服务删")
    items = service.trash()["items"]
    assert len(items) == 1
    await service.restore(items[0]["file"])
    assert service.get("走服务删")["note"]["body"].strip() == "内容"


@pytest.mark.asyncio
async def test_service_search_pagination_passthrough(service: SharedMemoryService) -> None:
    for i in range(7):
        await service.write(title=f"透传-{i}", body="同", tags=["pt"])
    r = service.search("", tags=["pt"], limit=3)
    assert r["total"] == 7 and r["count"] == 3
    assert "hint" in r


# ═════════════════════  工作区维度  ═════════════════════


def test_workspace_field_and_auto_tag(vault: Vault) -> None:
    """写工作区时：frontmatter 有字段 + 自动补 ws/ 标签（与 wiki 插件同口径）。"""
    vault.put(title="项目笔记", body="x", workspace="乱七八糟")
    n = vault.get("项目笔记")
    assert n.workspace == "乱七八糟"
    assert "ws/乱七八糟" in n.tags


def test_workspace_tag_not_duplicated(vault: Vault) -> None:
    """调用方自己写了 ws/ 标签时不要重复加。"""
    vault.put(title="重复标签", body="x", workspace="proj", tags=["ws/proj"])
    assert vault.get("重复标签").tags.count("ws/proj") == 1


def test_no_workspace_is_legal(vault: Vault) -> None:
    """不填工作区是合法的（跨项目/基础设施类记忆）。"""
    vault.put(title="基础设施笔记", body="x")
    n = vault.get("基础设施笔记")
    assert n.workspace == ""
    # 空工作区不输出到响应里（无信息字段）
    assert "workspace" not in n.to_dict()


def test_update_preserves_workspace_when_not_given(vault: Vault) -> None:
    """只改正文时不传 workspace，**原工作区必须保留**。

    回归保护：否则一次"只想改正文"的更新会把记忆从原项目里摘出去，
    变成"无工作区"，之后按项目检索就找不到它了。
    """
    vault.put(title="别丢工作区", body="v1", workspace="proj-a")
    vault.put(title="别丢工作区", body="v2")  # 不传 workspace
    assert vault.get("别丢工作区").workspace == "proj-a"


def test_update_can_change_workspace(vault: Vault) -> None:
    """显式传 workspace 时可以改（记忆被移到别的项目下）。"""
    vault.put(title="换项目", body="x", workspace="old")
    vault.put(title="换项目", body="x", workspace="new")
    assert vault.get("换项目").workspace == "new"


def test_search_filters_by_workspace(vault: Vault) -> None:
    """按工作区过滤：只要那个项目的记忆。"""
    vault.put(title="A 项目的部署", body="x", workspace="proj-a")
    vault.put(title="B 项目的部署", body="x", workspace="proj-b")
    hits = vault.search("部署", workspace="proj-a")["results"]
    assert [h["title"] for h in hits] == ["A 项目的部署"]


def test_search_workspace_none_sentinel(vault: Vault) -> None:
    """``__none__`` 查"无工作区"的记忆。

    哨兵值的存在理由：空字符串已经表示"不过滤"，
    所以需要一个独立的值来表示"我要没有工作区的那些"。
    """
    vault.put(title="有项目", body="x", workspace="proj")
    vault.put(title="无项目", body="x")
    hits = vault.search("", workspace="__none__")["results"]
    assert [h["title"] for h in hits] == ["无项目"]


def test_search_matches_workspace_name_as_keyword(vault: Vault) -> None:
    """搜工作区名本身也能命中（"找 vivo 相关的东西"这种查法）。"""
    vault.put(title="随便标题", body="正文里没有那个词", workspace="vivo")
    hits = vault.search("vivo")["results"]
    assert len(hits) == 1


def test_list_filters_by_workspace(vault: Vault) -> None:
    """list 的工作区过滤（走 service 层 —— list_notes 定义在那里）。"""
    from shared_memory.service import SharedMemoryService

    vault.put(title="一", body="x", workspace="w1")
    vault.put(title="二", body="x", workspace="w2")
    svc = SharedMemoryService(vault, commit_debounce=0)
    r = svc.list_notes(workspace="w1")
    assert r["total"] == 1 and r["notes"][0]["title"] == "一"


def test_list_workspace_none_sentinel(vault: Vault) -> None:
    """list 也支持 __none__ 哨兵。"""
    from shared_memory.service import SharedMemoryService

    vault.put(title="有项目", body="x", workspace="w1")
    vault.put(title="无项目", body="x")
    svc = SharedMemoryService(vault, commit_debounce=0)
    r = svc.list_notes(workspace="__none__")
    assert r["total"] == 1 and r["notes"][0]["title"] == "无项目"


def test_stats_reports_workspaces(vault: Vault) -> None:
    """统计里要有工作区分布 —— 多项目共用一库时这是分类是否健康的主要依据。"""
    vault.put(title="甲", body="x", workspace="w1")
    vault.put(title="乙", body="x", workspace="w1")
    vault.put(title="丙", body="x")
    ws = dict(vault.stats()["workspaces"])
    assert ws["w1"] == 2
    assert ws["(无工作区)"] == 1, "无工作区应单独归一类，不与其它混淆"


# ═════════════════════  体系：时效 / 引用 / 视图 / 配置  ═════════════════════
# 这一组测试对应「闭环漏洞清单」里的每一条对策，命名直接标明漏洞编号。


# ── 漏洞 2：过期的记忆还在被当真 ──


def test_expiry_states() -> None:
    """时效四种状态判定正确。"""
    from shared_memory.knowledge import expiry_state

    assert expiry_state({})["state"] == "active"
    assert expiry_state({"valid-until": "2020-01-01"})["state"] == "expired"
    assert expiry_state({"review-after": "2020-01-01"})["state"] == "review"
    assert expiry_state({"superseded-by": "新条"})["state"] == "superseded"


def test_superseded_beats_expired() -> None:
    """被取代优先级最高 —— 即使日期也没过期，它也不该再当现行口径。"""
    from shared_memory.knowledge import expiry_state

    st = expiry_state({"valid-until": "2020-01-01", "superseded-by": "新条"})
    assert st["state"] == "superseded"


def test_future_valid_until_is_active() -> None:
    """未到期的还是 active。"""
    from shared_memory.knowledge import expiry_state

    assert expiry_state({"valid-until": "2099-01-01"})["state"] == "active"


def test_bad_date_does_not_crash() -> None:
    """时效字段写错格式不该让条目失效（宽容解析）。"""
    from shared_memory.knowledge import expiry_state

    assert expiry_state({"valid-until": "不是日期"})["state"] == "active"


def test_expiring_view_lists_problems(vault: Vault) -> None:
    """时效告警视图能把有问题的条目列出来。"""
    from shared_memory.knowledge import build_view

    vault.put(title="过期的", body="x", extra={"valid-until": "2020-01-01"})
    vault.put(title="被取代的", body="x", extra={"superseded-by": "别的"})
    vault.put(title="正常的", body="x")
    v = build_view("expiring", vault.all_notes())
    titles = [i["title"] for i in v["items"]]
    assert "过期的" in titles and "被取代的" in titles
    assert "正常的" not in titles


# ── 漏洞 4：编造的事实混进库 ──


def test_claim_field_roundtrip(vault: Vault) -> None:
    """claim 字段能存取，并在摘要里暴露给读取方。"""
    vault.put(title="标注推断", body="x", extra={"claim": "inference"})
    n = vault.get("标注推断")
    assert n.meta.get("claim") == "inference"
    # 摘要里要有 —— agent 读列表时就该看到"这是推断"
    assert n.to_dict(with_body=False).get("claim") == "inference"


def test_inferences_view(vault: Vault) -> None:
    """未核实推断视图 —— 引用前需当心的那些条目。"""
    from shared_memory.knowledge import build_view

    vault.put(title="实锤", body="x", extra={"claim": "fact"})
    vault.put(title="猜的", body="x", extra={"claim": "inference"})
    v = build_view("inferences", vault.all_notes())
    assert [i["title"] for i in v["items"]] == ["猜的"]


# ── 漏洞 3：旧记忆被默默删掉 ──


def test_supersede_pattern_keeps_old(vault: Vault) -> None:
    """取代用标记而非删除：旧条必须还在，且指向新条。"""
    vault.put(title="旧口径", body="旧", extra={"superseded-by": "新口径"})
    vault.put(title="新口径", body="新")
    old = vault.get("旧口径")
    assert old.meta["superseded-by"] == "新口径"
    # 两条都在
    assert len(vault.all_notes()) == 2


# ── 漏洞 1：agent 不填元数据 ──


def test_auto_infer_type_from_title() -> None:
    """标题含「教训」→ 自动推断 type=lesson。"""
    from shared_memory.config import infer_meta

    rules = {"auto_infer": True, "infer_type": {"lesson": ["教训"]}}
    assert infer_meta(title="教训：别这么干", body="", workspace="", rules=rules)["type"] == "lesson"


def test_auto_infer_claim_prefers_inference() -> None:
    """同时出现「实测」和「可能」时，标成 inference。

    理由：把"可能是"标成 fact 是**有害**的（后人当真理用），
    反过来只是保守。错误代价不对称，所以推测优先。
    """
    from shared_memory.config import infer_meta

    rules = {
        "auto_infer": True,
        "infer_claim_inference": ["可能"],
        "infer_claim_fact": ["实测"],
    }
    r = infer_meta(title="t", body="实测了一下，可能是缓存问题", workspace="", rules=rules)
    assert r["claim"] == "inference"


def test_auto_infer_does_not_scan_whole_body() -> None:
    """⚠️ 回归保护：正文里**列举关键词**不能被误判成 type。

    实测踩过：一条部署文档因为正文写了"视图包括决策/踩坑/待办"，
    被推断成 type=lesson。

    根因很本质：**正文里出现一个词，通常是在"讨论这个概念"，
    而不是"这条笔记本身就是那个东西"**。列举关键词的地方必然误命中。

    修法：type **只看标题**（标题是作者对内容的一句话概括，最强信号）。
    """
    from shared_memory.config import infer_meta

    rules = {
        "auto_infer": True,
        "infer_type": {"lesson": ["教训", "踩坑"], "decision": ["决定"]},
    }
    # 正文列举了所有类型关键词，但标题是中性的 → 类型不该被推断出来
    r = infer_meta(
        title="shared-memory-mcp 部署档案",
        body="## 视图\n包括决策记录、踩坑清单、待办。\n\n"
             "## 设计\n每条记忆可以标类型，比如教训类的。",
        workspace="",
        rules=rules,
    )
    assert "type" not in r, f"正文列举被误判成 type: {r}"


def test_auto_infer_still_works_on_title() -> None:
    """收紧后，标题里的关键词仍然生效（不能因为修 bug 让功能失效）。"""
    from shared_memory.config import infer_meta

    rules = {"auto_infer": True, "infer_type": {"lesson": ["教训"]}}
    assert infer_meta(title="教训：别这么干", body="", workspace="", rules=rules)["type"] == "lesson"
    # 正文里提到"教训"不影响 —— 但标题命中就够
    r = infer_meta(title="教训：正文也提到了教训", body="教训", workspace="", rules=rules)
    assert r["type"] == "lesson"


def test_claim_inference_reads_first_paragraph() -> None:
    """claim 看正文**首段** —— 作者怎么起头陈述的。"""
    from shared_memory.config import infer_meta

    rules = {
        "auto_infer": True,
        "infer_claim_inference": ["可能"],
        "infer_claim_fact": ["实测"],
    }
    r = infer_meta(title="某问题分析", body="这可能是缓存问题，我还没验证。", workspace="", rules=rules)
    assert r["claim"] == "inference"


def test_claim_inference_ignores_later_paragraphs() -> None:
    """首段之后的内容不该影响判定（那里常是引用/列举/他人说法）。

    这条钉住的是"首段"而非"前 N 字"：body 里第二段就出现了「可能」，
    但如果实现改成"扫前 300 字"，短文档会整段命中 —— 所以这里
    第一段给足长度，确保超出 300 字，从而区分两种实现。
    """
    from shared_memory.config import infer_meta

    rules = {
        "auto_infer": True,
        "infer_claim_inference": ["可能"],
        "infer_claim_fact": ["实测"],
    }
    body = ("实测确认：缓存配置错了。" + "细节描述。" * 80
            + "\n\n别人可能会说是网络问题，但日志不支持这个说法。")
    assert len(body.split("\n\n")[0]) > 300, "前提：首段应超过 300 字"
    r = infer_meta(title="缓存问题", body=body, workspace="", rules=rules)
    assert r.get("claim") == "fact", f"第二段的关键词不该影响判定: {r}"
    """auto_infer=false 时什么都不推断。"""
    from shared_memory.config import infer_meta

    rules = {"auto_infer": False, "infer_type": {"lesson": ["教训"]}}
    assert infer_meta(title="教训", body="", workspace="", rules=rules) == {}


def test_apply_rules_does_not_reject_by_default() -> None:
    """默认策略下，长标题是补全+提醒，**不拒绝**。

    理由：硬拒会让 agent 反复换写法重试，浪费轮次烧额度 ——
    而它只是想记一条笔记。
    """
    from shared_memory.config import apply_rules_on_write

    tags, extra, notes = apply_rules_on_write(
        title="超长" * 100, body="x", tags=[], workspace="",
        extra={}, rules={"missing_field_policy": "fill", "max_title_chars": 20,
                         "auto_infer": False},
    )
    assert notes, "应该给出提醒"
    assert any("标题超长" in n for n in notes)


def test_apply_rules_reject_when_configured() -> None:
    """显式配成 reject 时才拒绝。"""
    from shared_memory.config import apply_rules_on_write

    with pytest.raises(ToolError):
        apply_rules_on_write(
            title="超长" * 100, body="x", tags=[], workspace="", extra={},
            rules={"missing_field_policy": "reject", "max_title_chars": 20,
                   "auto_infer": False},
        )


@pytest.mark.asyncio
async def test_write_returns_rule_notes(service: SharedMemoryService) -> None:
    """写入结果里要带 notes，告诉 agent「我给你补了什么」。"""
    r = await service.write(title="教训：测试自动推断", body="实测发现的问题")
    assert r["ok"] is True
    assert "notes" in r
    # 推断出的 type 应落到文件里
    n = service.vault.get("教训：测试自动推断")
    assert n.meta.get("type") == "lesson"


# ── 引用 ──


def test_extract_wiki_links() -> None:
    """[[标题]] 形式能识别。"""
    from shared_memory.knowledge import extract_links

    assert extract_links("见 [[某条目]]", set()) == ["某条目"]


def test_extract_links_ignores_short_titles() -> None:
    """太短的标题不认（会满篇误命中，那是巧合不是引用）。"""
    from shared_memory.knowledge import extract_links

    assert extract_links("这里有 配置 两个字", {"配置"}) == []


def test_backlinks_finds_referrers(vault: Vault) -> None:
    """反向引用：谁提了我。"""
    from shared_memory.knowledge import build_backlinks

    vault.put(title="被引用的条目", body="内容")
    vault.put(title="引用者", body="详见 [[被引用的条目]]")
    back = build_backlinks(vault.all_notes())
    assert "引用者" in back.get("被引用的条目", [])


@pytest.mark.asyncio
async def test_service_backlinks(service: SharedMemoryService) -> None:
    """双向引用：谁指向我（backlinks）+ 我指向谁（outgoing）。

    构造一条链：A → B → C
      · 对 B：backlinks 应有 A，outgoing 应有 C
    """
    await service.write(title="丙条目内容", body="链尾")
    await service.write(title="乙条目内容", body="参见 [[丙条目内容]]")
    await service.write(title="甲条目内容", body="参见 [[乙条目内容]]")

    r = service.backlinks("乙条目内容")
    assert "甲条目内容" in r["backlinks"], "谁引用了乙"
    assert "丙条目内容" in r["outgoing"], "乙引用了谁"

    # 链尾没有出边
    r_tail = service.backlinks("丙条目内容")
    assert "乙条目内容" in r_tail["backlinks"]
    assert r_tail["outgoing"] == [], "链尾不该有出边"


# ── 视图 ──


def test_all_views_render(vault: Vault) -> None:
    """每个内置视图都能算出来（不会因空库或边界情况炸）。"""
    from shared_memory.knowledge import VIEWS, build_view

    vault.put(title="教训：有个坑", body="x")
    vault.put(title="决定：用 A 方案", body="x")
    notes = vault.all_notes()
    for kind in VIEWS:
        r = build_view(kind, notes)
        assert r["ok"] is True, f"{kind} 视图失败"
        assert r["kind"] == kind


def test_unknown_view_rejected(vault: Vault) -> None:
    """未知视图名给出明确错误与可用列表。"""
    from shared_memory.knowledge import build_view

    with pytest.raises(ToolError) as ei:
        build_view("不存在的视图", [])
    assert ei.value.code == ErrorCode.INVALID_ARGUMENT
    assert "可用视图" in (ei.value.hint or "")


def test_view_tag_filter(vault: Vault) -> None:
    """tag 参数把视图收窄成"主题页"。"""
    from shared_memory.knowledge import build_view

    vault.put(title="A", body="x", tags=["主题甲"])
    vault.put(title="B", body="x", tags=["主题乙"])
    v = build_view("recent", vault.all_notes(), params={"tag": "主题甲"})
    assert [i["title"] for i in v["items"]] == ["A"]


def test_overview_reports_expiry_counts(vault: Vault) -> None:
    """总览要报时效告警数量（体检入口）。"""
    from shared_memory.knowledge import build_view

    vault.put(title="过期了", body="x", extra={"valid-until": "2020-01-01"})
    vault.put(title="正常", body="x")
    s = build_view("overview", vault.all_notes())["stats"]
    assert s["expiry"]["expired"] == 1


# ── 漏洞 6 + 9：配置 ──


def test_config_defaults_without_files(tmp_path: Path) -> None:
    """配置文件不存在时用内置默认值（不需要先初始化）。"""
    from shared_memory.config import Config, DEFAULT_INSTRUCTIONS

    c = Config(tmp_path)
    assert c.instructions() == DEFAULT_INSTRUCTIONS
    assert c.rules()["missing_field_policy"] == "fill"
    assert c.settings()["commit_debounce_seconds"] == 8


def test_default_instructions_contains_no_hallucination() -> None:
    """默认指令必须包含"不幻觉"约定 —— 这是数据质量的前提。"""
    from shared_memory.config import DEFAULT_INSTRUCTIONS

    for token in ("不", "fact", "inference", "proposal", "不知道"):
        assert token in DEFAULT_INSTRUCTIONS, f"默认指令缺少 {token}"


def test_config_broken_json_falls_back(tmp_path: Path) -> None:
    """配置损坏时回落默认值，**不让服务起不来**。

    一个手滑写坏的配置文件不该连带所有 agent 都用不了。
    """
    from shared_memory.config import Config, DEFAULT_RULES, config_dir

    d = config_dir(tmp_path)
    d.mkdir(parents=True)
    (d / "rules.json").write_text("{ 这不是合法 JSON", encoding="utf-8")
    c = Config(tmp_path)
    assert c.rules() == DEFAULT_RULES


def test_config_empty_instructions_falls_back(tmp_path: Path) -> None:
    """指令文件被清空时回落默认值（"没有指令"比默认值更糟）。"""
    from shared_memory.config import Config, DEFAULT_INSTRUCTIONS, config_dir

    d = config_dir(tmp_path)
    d.mkdir(parents=True)
    (d / "instructions.md").write_text("   \n", encoding="utf-8")
    assert Config(tmp_path).instructions() == DEFAULT_INSTRUCTIONS


def test_config_instructions_can_be_disabled(tmp_path: Path) -> None:
    """显式写 _disabled 才停用（而不是清空文件）。"""
    from shared_memory.config import Config, config_dir

    d = config_dir(tmp_path)
    d.mkdir(parents=True)
    (d / "instructions.md").write_text("_disabled", encoding="utf-8")
    assert Config(tmp_path).instructions() == ""


# ── v2 行为契约（S-3）──


def test_default_instructions_contains_v2_contract() -> None:
    """新空间的默认指令必须含 v2 契约七条（开工/收工是硬动作）。"""
    from shared_memory.config import CONTRACT_MARKER, DEFAULT_INSTRUCTIONS

    assert CONTRACT_MARKER in DEFAULT_INSTRUCTIONS
    assert "memory_session_start" in DEFAULT_INSTRUCTIONS
    assert "memory_session_end" in DEFAULT_INSTRUCTIONS
    # 六型与两类诚实性标注都点到
    for tok in ("workflow", "constraint", "fact", "inference"):
        assert tok in DEFAULT_INSTRUCTIONS


def test_custom_instructions_are_not_silently_injected(tmp_path: Path) -> None:
    """空间自定义指令**原样返回** —— 服务端不偷偷往里塞契约。

    契约由 MCP initialize 层保证（见 test_http 系列）；这里偷偷注入的话，
    用户在面板删掉契约、保存后又冒出来，是 bug 级困惑。
    """
    from shared_memory.config import Config, config_dir

    d = config_dir(tmp_path)
    d.mkdir(parents=True)
    mine = "# 我们组的规矩\n\n先看 README。\n"
    (d / "instructions.md").write_text(mine, encoding="utf-8")

    assert Config(tmp_path).instructions() == mine


def test_migrate_instructions_contract_prepends_physically(tmp_path: Path) -> None:
    """物理迁移 = 头部追加；原文逐字保留；重复调用幂等。"""
    from shared_memory.config import CONTRACT_MARKER, Config, config_dir

    d = config_dir(tmp_path)
    d.mkdir(parents=True)
    p = d / "instructions.md"
    mine = "用户的原文，一个字都不许动。\n"
    p.write_text(mine, encoding="utf-8")

    r = Config(tmp_path).migrate_instructions_contract()
    assert r["changed"] is True and r["reason"] == "prepended"

    text = p.read_text(encoding="utf-8")
    assert CONTRACT_MARKER in text
    assert mine.strip() in text, "原文必须逐字保留"
    assert text.index(CONTRACT_MARKER) < text.index("用户的原文")

    r2 = Config(tmp_path).migrate_instructions_contract()
    assert r2["changed"] is False and r2["reason"] == "up-to-date"


def test_migrate_does_not_create_missing_file(tmp_path: Path) -> None:
    """没有指令文件的空间**不建文件** —— 建了会让 is_default 误报成"改过"。"""
    from shared_memory.config import Config

    r = Config(tmp_path).migrate_instructions_contract()
    assert r["changed"] is False and r["reason"] == "missing"
    assert not (tmp_path / "_config" / "instructions.md").exists()


def test_migrate_respects_disabled(tmp_path: Path) -> None:
    """显式 _disabled 的指令不该被迁移碰（那是"我就是要关掉"）。"""
    from shared_memory.config import Config, config_dir

    d = config_dir(tmp_path)
    d.mkdir(parents=True)
    (d / "instructions.md").write_text("_disabled", encoding="utf-8")
    r = Config(tmp_path).migrate_instructions_contract()
    assert r["changed"] is False and r["reason"] == "up-to-date"


# ── 接入码解析（S-5.1）──


def test_parse_connect_uri_standard() -> None:
    from shared_memory.config import parse_connect_uri

    d = parse_connect_uri("sm://192.168.x.x:8200?k=abc123")
    assert d["host"] == "192.168.x.x" and d["port"] == 8200
    assert d["token"] == "abc123"
    assert d["mcp_url"] == "http://192.168.x.x:8200/mcp"


def test_parse_connect_uri_port_optional_and_key_aliases() -> None:
    """端口可省（默认 8200）；密钥键名兼容 k / key / token。"""
    from shared_memory.config import DEFAULT_PORT, parse_connect_uri

    assert parse_connect_uri("sm://10.0.0.5?k=x")["port"] == DEFAULT_PORT
    assert parse_connect_uri("sm://10.0.0.5?key=y")["token"] == "y"
    assert parse_connect_uri("sm://10.0.0.5?token=z")["token"] == "z"


def test_parse_connect_uri_errors_are_specific() -> None:
    """报错必须指明**哪一段**不对 —— 否则等于让用户猜。"""
    from shared_memory.config import parse_connect_uri
    from shared_memory.errors import ToolError

    cases = {
        "": "为空",
        "http://h?k=x": "协议头",
        "sm://?k=x": "主机",
        "sm://h": "密钥",
        "sm://h:notaport?k=x": "端口",
    }
    for bad, keyword in cases.items():
        with pytest.raises(ToolError) as ei:
            parse_connect_uri(bad)
        assert keyword in ei.value.message, f"{bad!r} 的报错没点明「{keyword}」：{ei.value.message}"


def test_validate_rules_catches_type_errors(tmp_path: Path) -> None:
    """漏洞 9：非法配置要被拦下并指明是哪一项。"""
    from shared_memory.config import Config

    c = Config(tmp_path)
    assert c.validate_rules({"missing_field_policy": "fill"}) == []
    problems = c.validate_rules({"missing_field_policy": "瞎写"})
    assert problems and "missing_field_policy" in problems[0]
    assert c.validate_rules({"auto_infer": "yes"})
    assert c.validate_rules({"max_title_chars": -1})
    assert c.validate_rules({"infer_type": "应该是对象"})


def test_validate_settings_bounds(tmp_path: Path) -> None:
    """参数越界要被拦下。"""
    from shared_memory.config import Config

    c = Config(tmp_path)
    assert c.validate_settings({"search_default_limit": 20}) == []
    assert c.validate_settings({"search_default_limit": 9999})
    assert c.validate_settings({"commit_debounce_seconds": -1})
    assert c.validate_settings({"panel_refresh_seconds": 1.5})


@pytest.mark.asyncio
async def test_update_config_persists_and_validates(service: SharedMemoryService) -> None:
    """配置能存、能读回、非法值被拒。"""
    r = await service.update_config(settings={"search_default_limit": 42})
    assert r["ok"] is True
    assert "settings.json" in r["changed"]
    assert service.config.settings()["search_default_limit"] == 42

    # 非法值被拒，且**不写盘**（原来的 42 还在）
    with pytest.raises(ToolError) as ei:
        await service.update_config(settings={"search_default_limit": 99999})
    assert ei.value.code == ErrorCode.INVALID_ARGUMENT
    assert service.config.settings()["search_default_limit"] == 42


@pytest.mark.asyncio
async def test_update_instructions_via_service(service: SharedMemoryService) -> None:
    """instructions 能改，且改完立即生效（读的是文件，没有缓存）。"""
    await service.update_config(instructions="自定义指令：先查再答")
    assert service.welcome() == "自定义指令：先查再答"


@pytest.mark.asyncio
async def test_config_write_goes_through_queue(service: SharedMemoryService) -> None:
    """漏洞 6：配置写必须走队列 —— 验证队列计数增加。"""
    before = service.queue.stats()["processed"]
    await service.update_config(rules={"missing_field_policy": "warn"})
    after = service.queue.stats()["processed"]
    assert after > before, "配置写没有经过队列"


# ═════════════════════  错误信封  ═════════════════════


def test_error_envelope_shape() -> None:
    """agent 依赖这个形状识别失败，不能随便改。"""
    d = ToolError(ErrorCode.NOT_FOUND, "找不到", reason="细节", hint="提示").to_dict()
    assert d["ok"] is False
    assert d["code"] == "NOT_FOUND"
    assert d["message"] == "找不到"
    assert d["reason"] == "细节"
    assert d["hint"] == "提示"


def test_error_envelope_omits_empty_fields() -> None:
    """可选字段为空时不出现在信封里（避免 agent 看到一堆空字符串）。"""
    d = ToolError(ErrorCode.NOT_FOUND, "找不到").to_dict()
    assert "reason" not in d and "hint" not in d


# ─────────────────────────────────────────────────────────────
# 旧 v1 工具层（shared_memory.tools.MemoryTools）的测试已迁移：
# 信封契约 + 写搜导链路 → tests/test_optimization.py::
#   test_tools_return_envelope_not_exception
#   test_tools_write_search_export_roundtrip
# 源文件 src/shared_memory/tools.py 已删（方案 7，2026-10-13）。


# ═════════════════════  v2 概念字段（S-1）  ═════════════════════
# 存储层落 v2 三件事：记忆六型（type）、生效域（scope）、共享档（share）。
# 每条都对应一条对外承诺或一处兼容红线，不做无谓覆盖。


def test_note_types_is_the_six_type_contract() -> None:
    """六型是 v2 的公开契约，改名/增减都算破坏性变更。"""
    from shared_memory.vault import NOTE_TYPES

    assert NOTE_TYPES == (
        "workflow", "constraint", "habit", "fact", "lesson", "task",
    )


def test_valid_type_accepts_six_rejects_unknown() -> None:
    from shared_memory.vault import NOTE_TYPES, valid_type

    assert all(valid_type(t) for t in NOTE_TYPES)
    assert not valid_type("decision")  # 旧值可读，但不是合法的新写入
    assert not valid_type("")
    assert not valid_type("FACT")  # 大小写敏感，不给模糊地带


def test_valid_scope_covers_three_layers() -> None:
    from shared_memory.vault import valid_scope

    assert valid_scope("long-term")
    assert valid_scope("archived")
    assert valid_scope("task:周报")
    assert not valid_scope("task:")  # 有前缀无任务名 = 非法
    assert not valid_scope("task")  # 少冒号
    assert not valid_scope("forever")


def test_put_defaults_type_fact_and_scope_long_term(vault: Vault) -> None:
    """agent 赶时间不传字段时也要有合法缺省 —— 不能让脏值躺进文件。"""
    vault.put(title="默认字段", body="什么都没标")

    note = vault.get("默认字段")
    assert note.type == "fact"
    assert note.scope == "long-term"
    assert note.versioned is False
    assert note.share == ""


def test_put_explicit_type_and_scope_land_in_frontmatter(vault: Vault) -> None:
    vault.put(
        title="部署铁律",
        body="必须走队列",
        extra={"type": "constraint", "scope": "long-term"},
    )

    note = vault.get("部署铁律")
    assert note.type == "constraint"
    raw = list(vault.notes_dir.glob("*.md"))[0].read_text(encoding="utf-8")
    assert "type: constraint" in raw  # 真进了文件，不是只在内存 property


def test_put_rejects_unknown_type(vault: Vault) -> None:
    with pytest.raises(ToolError) as ei:
        vault.put(title="坏类型", body="", extra={"type": "decision"})
    # F-2：类型非法独立成码（客户端据此提示"改类型"，而不是泛泛的"参数错了"）
    assert ei.value.code == ErrorCode.INVALID_TYPE


def test_put_rejects_bad_scope(vault: Vault) -> None:
    with pytest.raises(ToolError) as ei:
        vault.put(title="坏域", body="", extra={"scope": "task:"})
    assert ei.value.code == ErrorCode.INVALID_ARGUMENT


def test_put_rejects_bad_share(vault: Vault) -> None:
    with pytest.raises(ToolError) as ei:
        vault.put(title="坏共享", body="", extra={"share": "public"})
    assert ei.value.code == ErrorCode.INVALID_ARGUMENT


def test_update_does_not_clobber_explicit_type(vault: Vault) -> None:
    """更新正文时不能把显式标过的类型/域改回缺省（setdefault 的价值）。"""
    vault.put(
        title="流程A", body="v1",
        extra={"type": "workflow", "scope": "long-term"},
    )
    vault.put(title="流程A", body="v2 只改正文")

    note = vault.get("流程A")
    assert note.type == "workflow"
    assert note.scope == "long-term"


def test_legacy_note_reads_as_fact_longterm_without_rewrite(vault: Vault) -> None:
    """S-1.7 兼容红线：旧 note 按 fact/long-term 读，**文件一个字节都不动**。"""
    legacy = (
        "---\n"
        "id: mem-20260101-000000-legacy\n"
        "title: 上古条目\n"
        "tags: [老数据]\n"
        "created: 2026-01-01T00:00:00+08:00\n"
        "updated: 2026-01-01T00:00:00+08:00\n"
        "---\n\n"
        "老格式正文，没有 type / scope。\n"
    )
    p = vault.notes_dir / "上古条目.md"
    p.write_text(legacy, encoding="utf-8")
    before = p.read_bytes()

    note = vault.get("上古条目")
    assert note.type == "fact"
    assert note.scope == "long-term"

    assert p.read_bytes() == before, "读取旧数据绝不能改写文件"


def test_search_default_scope_hides_task_process_notes(vault: Vault) -> None:
    """防串台第一闸：默认检索看不到 task: 过程态。"""
    vault.put(title="长期口径 alpha", body="定下来的口径")
    vault.put(
        title="任务过程 alpha", body="过程中随手记的",
        extra={"scope": "task:周报"},
    )

    titles = {r["title"] for r in vault.search("alpha")["results"]}
    assert titles == {"长期口径 alpha"}


def test_search_scope_task_returns_only_that_task(vault: Vault) -> None:
    vault.put(title="任务A alpha", body="a", extra={"scope": "task:A"})
    vault.put(title="任务B alpha", body="b", extra={"scope": "task:B"})
    vault.put(title="长期 alpha", body="c")

    titles = [r["title"] for r in vault.search("alpha", scope="task:A")["results"]]
    assert titles == ["任务A alpha"]


def test_search_scope_all_and_empty_are_escape_hatches(vault: Vault) -> None:
    vault.put(title="任务A beta", body="a", extra={"scope": "task:A"})
    vault.put(title="归档 beta", body="b", extra={"scope": "archived"})
    vault.put(title="长期 beta", body="c")
    everyone = {"任务A beta", "归档 beta", "长期 beta"}

    got_all = {r["title"] for r in vault.search("beta", scope="all")["results"]}
    assert got_all == everyone
    # 空串 = 不过滤，给按旧签名调用的老客户端留的逃生门
    got_esc = {r["title"] for r in vault.search("beta", scope="")["results"]}
    assert got_esc == everyone
    # 默认只 long-term
    got_dflt = {r["title"] for r in vault.search("beta")["results"]}
    assert got_dflt == {"长期 beta"}


def test_search_rejects_bad_scope(vault: Vault) -> None:
    with pytest.raises(ToolError) as ei:
        vault.search("x", scope="nope")
    assert ei.value.code == ErrorCode.INVALID_ARGUMENT


def test_share_and_versioned_roundtrip(vault: Vault) -> None:
    """share/versioned 走 meta 且在白名单里 —— 写进去必须原样读回来。"""
    vault.put(
        title="可复用工作流", body="步骤 1/2/3",
        extra={"type": "workflow", "share": "space", "versioned": True},
    )

    note = vault.get("可复用工作流")
    assert note.share == "space"
    assert note.versioned is True
    # 再写一次（改正文）也不能丢
    vault.put(title="可复用工作流", body="步骤 1/2/3/4")
    note2 = vault.get("可复用工作流")
    assert note2.share == "space"
    assert note2.versioned is True


def test_default_infer_rules_speak_six_types() -> None:
    """v2 缺省推断规则只产出六型；约束/流程信号优先于通用词。"""
    from shared_memory.config import DEFAULT_RULES, infer_meta

    rules = {"auto_infer": True, **DEFAULT_RULES}
    assert infer_meta(
        title="部署必须走队列", body="", workspace="", rules=rules
    )["type"] == "constraint"
    assert infer_meta(
        title="发布流程步骤", body="", workspace="", rules=rules
    )["type"] == "workflow"
    assert infer_meta(
        title="口径约定：先冷启动", body="", workspace="", rules=rules
    )["type"] == "fact"


def test_views_see_legacy_decision_and_new_task(vault: Vault) -> None:
    """视图兼容：旧 decision 与新 fact/constraint/task 都要能被视图捞到。"""
    from shared_memory.knowledge import build_view

    vault.put(title="新口径", body="", extra={"type": "fact"})
    vault.put(title="新铁律", body="", extra={"type": "constraint"})
    vault.put(title="新任务", body="", extra={"type": "task"})
    # 直接塞一条老 decision（绕过 put 的校验）模拟历史数据
    (vault.notes_dir / "旧决策.md").write_text(
        "---\nid: mem-x\ntitle: 旧决策\ntype: decision\n---\n\n老决策正文\n",
        encoding="utf-8",
    )

    notes = vault.all_notes()
    dec = {i["title"] for i in build_view("decisions", notes)["items"]}
    assert {"新口径", "新铁律", "旧决策"} <= dec
    todo = {i["title"] for i in build_view("todos", notes)["items"]}
    assert "新任务" in todo
# ─────────────────────────────────────────────────────────────
