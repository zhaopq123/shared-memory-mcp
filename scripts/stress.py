"""压力 / 并发 / 多工作空间测试 —— 优化期（2026-10-13）的极致调优验证。

━━━ 测什么 ━━━

  1. 多库并发写      5 库 × 8 写者 × 10 条 = 400 笔并发过同一个队列
  2. 读写混合        写进行中的并发检索延迟分位（p50/p95/p99）
  3. 多工作空间      8 工作区 × 25 条：过滤正确性 + 逐区导出导入字节还原
  4. 导入导出量级    300 条单库 bundle 往返耗时 + 字节一致性
  5. digest 并发     10 用户同时连：精确一次（不漏不重）
  6. stats 开销      with_commits 开/关对比

用法：
    python scripts/stress.py            # 全量
    python scripts/stress.py --quick    # 跳过 300 条量级测试
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shared_memory import digest  # noqa: E402
from shared_memory.tenant import Library, User  # noqa: E402
from shared_memory.tenancy import TenantHub  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def report(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = min(len(s) - 1, int(p / 100 * len(s)))
    return s[k] * 1000  # ms


def make_hub(root: Path) -> TenantHub:
    h = TenantHub(root)
    store = h.store
    for name, groups, role in [
        ("alice", ["研发"], "admin"),
        ("bob", ["研发"], "member"),
        ("carol", ["销售"], "member"),
    ]:
        store.users[name] = User(
            name=name, password=User.hash_password(f"{name}-pw"),
            role=role, groups=groups,
        )
    for lid, owner, label, member_w in [
        ("company", "company", "公司", False),
        ("group-研发", "group:研发", "研发组", True),
        ("group-销售", "group:销售", "销售组", True),
        ("user-bob", "user:bob", "Bob", True),
        ("user-carol", "user:carol", "Carol", True),
    ]:
        store.libs[lid] = Library(
            id=lid, label=label, owner=owner, writable_by_members=member_w,
        )
    store.save()
    return h


async def t1_multi_lib_concurrent_writes(hub: TenantHub) -> None:
    """5 库 × 8 写者 × 10 条并发。队列串行 → 0 丢失 0 覆盖。"""
    print("\n── 1. 多库并发写（400 笔过同一队列）──")
    libs = ["company", "group-研发", "group-销售", "user-bob", "user-carol"]
    users = {"company": "alice", "group-研发": "bob", "group-销售": "carol",
             "user-bob": "bob", "user-carol": "carol"}
    t0 = time.perf_counter()

    async def writer(lib: str, w: int) -> None:
        u = users[lib]
        for i in range(10):
            r = await hub.write(
                u, title=f"压测-{lib}-{w}-{i}", body=f"正文 {lib} {w} {i}",
                lib_id=lib, workspace=f"ws-{lib}",
            )
            assert r["ok"], f"写失败: {r}"

    await asyncio.gather(*[writer(lib, w) for lib in libs for w in range(8)])
    dt = time.perf_counter() - t0

    total_ok = True
    for lib in libs:
        svc = hub.service_for(lib)
        notes = [n for n in svc.vault.all_notes()]
        # 该库预期：80 条压测 + （company 无其他）
        if len(notes) != 80:
            total_ok = False
            print(f"    {lib}: {len(notes)} 条（预期 80）")
    report("多库并发写 0 丢失", total_ok, f"400 笔 / {dt:.1f}s = {400/dt:.0f} writes/s")

    # 读回验证（read-your-writes 跨协程）
    s = await asyncio.to_thread(lambda: hub.search("bob", "压测", lib_id="group-研发"))
    report("并发写后可检索", s["total"] == 80, f"total={s['total']}")


async def t2_read_write_mix(hub: TenantHub) -> None:
    """写进行中的并发检索：延迟分位。"""
    print("\n── 2. 读写混合（检索延迟分位）──")
    lat: list[float] = []
    errors = 0
    stop = asyncio.Event()

    async def searcher() -> None:
        nonlocal errors
        while not stop.is_set():
            t0 = time.perf_counter()
            try:
                await asyncio.to_thread(
                    lambda: hub.search("bob", "压测", limit=20))
            except Exception:
                errors += 1
            lat.append(time.perf_counter() - t0)
            await asyncio.sleep(0.01)

    async def writer(w: int) -> None:
        for i in range(10):
            await hub.write(
                "bob", title=f"混合-{w}-{i}", body="x", lib_id="group-研发",
                workspace="ws-混合")

    searchers = [asyncio.create_task(searcher()) for _ in range(20)]
    t0 = time.perf_counter()
    await asyncio.gather(*[writer(w) for w in range(8)])
    stop.set()
    await asyncio.gather(*searchers)
    dt = time.perf_counter() - t0

    report("读写混合 0 检索异常", errors == 0, f"{len(lat)} 次检索 / {dt:.1f}s")
    print(f"    p50={pct(lat, 50):.1f}ms  p95={pct(lat, 95):.1f}ms  "
          f"p99={pct(lat, 99):.1f}ms  max={pct(lat, 100):.1f}ms")
    report("检索 p95 < 500ms", pct(lat, 95) < 500, f"p95={pct(lat, 95):.1f}ms")


async def t3_multi_workspace(hub: TenantHub) -> None:
    """8 工作区：写 / 过滤搜 / 逐区导出 / 导入字节还原。"""
    print("\n── 3. 多工作空间（8 区 × 25 条）──")
    wss = [f"项目{i}" for i in range(8)]
    for ws in wss:
        for i in range(25):
            await hub.write(
                "bob", title=f"{ws}·条目{i}", body=f"{ws} 正文 {i}",
                lib_id="group-研发", workspace=ws,
            )

    filter_ok = True
    for ws in wss:
        s = await asyncio.to_thread(
            lambda ws=ws: hub.search("bob", "", workspace=ws, limit=100))
        if s["total"] != 25:
            filter_ok = False
            print(f"    {ws}: total={s['total']}（预期 25）")
    report("工作区过滤全部正确", filter_ok)

    # 逐区导出 → 导入 bob 个人库 → 字节还原
    rt_ok = True
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory() as td:
        for ws in wss:
            r = hub.export("bob", workspace=ws, lib_id="group-研发",
                           out_dir=str(Path(td) / ws))
            assert r["ok"] and r["count"] == 25, (ws, r)
            i = await hub.import_bundle("bob", r["bundle"], target_lib="user-bob")
            if i["imported"] != 25:
                rt_ok = False
    dt = time.perf_counter() - t0
    # bob 个人库应有 t1 的 80 条 + 本阶段 8×25 = 280 条
    n = len(hub.service_for("user-bob").vault.all_notes())
    report("8 区导出导入往返", rt_ok and n == 280,
           f"200 条 / {dt:.1f}s，个人库现有 {n} 条（预期 280 = t1 的 80 + 200）")

    # 字节级抽验：随机比 3 个文件
    src_dir = None
    with tempfile.TemporaryDirectory() as td:
        r = hub.export("bob", workspace="项目0", lib_id="group-研发",
                       out_dir=str(Path(td) / "p0"))
        src_dir = Path(r["bundle"]) / "libs" / "group-研发" / "notes"
        import random
        random.seed(7)
        sample = random.sample(list(src_dir.glob("*.md")), 3)
        dst_dir = hub.service_for("user-bob").vault.root / "notes"
        byte_ok = all(
            any(s.read_bytes() == d.read_bytes() for d in dst_dir.glob("*.md"))
            for s in sample
        )
    report("字节级还原抽验", byte_ok)


async def t4_export_import_scale(hub: TenantHub) -> None:
    """300 条单库 bundle 往返：耗时 + 完整性。"""
    print("\n── 4. 导入导出量级（300 条）──")
    for i in range(300):
        await hub.write(
            "carol", title=f"量级-{i:03d}", body=f"正文 {i} " * 20,
            lib_id="group-销售", workspace="量级区",
        )
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        t0 = time.perf_counter()
        r = hub.export("carol", workspace="量级区", lib_id="group-销售",
                       out_dir=str(Path(td) / "exp"))
        t_exp = time.perf_counter() - t0
        assert r["count"] == 300, r["count"]

        t0 = time.perf_counter()
        i = await hub.import_bundle("carol", r["bundle"], target_lib="user-carol",
                                    on_conflict="rename")
        t_imp = time.perf_counter() - t0
    n = len(hub.service_for("user-carol").vault.all_notes())
    # 预期 380 = t1 预写进 user-carol 的 80 条 + 本阶段导入的 300 条
    report("300 条导出", r["count"] == 300, f"{t_exp:.2f}s")
    report("300 条导入",
           i["imported"] == 300 and n == 380,
           f"{t_imp:.2f}s，imported={i['imported']} notes={n}（预期 380）"
           f" errors={i.get('errors', [])[:3]}")

    # 冲突全量复导一次（skip）：应全跳过、0 更新
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        r2 = hub.export("carol", workspace="量级区", lib_id="group-销售",
                        out_dir=str(Path(td) / "exp2"))
        i2 = await hub.import_bundle("carol", r2["bundle"], target_lib="user-carol",
                                     on_conflict="skip")
    report("复导冲突全跳过", i2["skipped"] == 300 and i2["updated"] == 0,
           f"skipped={i2['skipped']}")


async def t5_digest_concurrency(hub: TenantHub) -> None:
    """10 用户同时连：精确一次（不漏不重）。"""
    print("\n── 5. digest 并发（10 用户 × 3 连）──")
    users = [f"u{i}" for i in range(10)]
    # 给他们都能读的组库权限（全加进研发组）
    store = hub.store
    for u in users:
        store.users[u] = User(name=u, password=User.hash_password("x"),
                              role="member", groups=["研发"])
    store.save()

    # 每用户先写一条基线
    for u in users:
        await hub.write(u, title=f"{u}-基线", body="x", lib_id="group-研发")

    with tempfile.TemporaryDirectory() as td:
        home = Path(td)

        async def connect(u: str) -> str:
            return await asyncio.to_thread(
                lambda: digest.build_suffix(hub, u, home)[0])

        # 第一连：建基线，全部静默
        s0 = await asyncio.gather(*[connect(u) for u in users])
        first_silent = all(s == "" for s in s0)
        report("首连全部静默", first_silent)

        # 每用户再写 3 条
        for u in users:
            for i in range(3):
                await hub.write(u, title=f"{u}-新{i}", body="x",
                                lib_id="group-研发")

        # 第二连：每人看到组库的 30 条变更（10 用户 × 3）。
        # 注意口径：digest 是「你可读的库变了」，不是「你写的东西」
        # —— 共享组库里别人的写入当然该出现。
        s1 = await asyncio.gather(*[connect(u) for u in users])
        exact = all("研发组" in s and "30 条更新" in s for s in s1)
        report("每人看到组库 30 条变更", exact)

        # 第三连：全部静默（seen 记账生效，不重复播）
        s2 = await asyncio.gather(*[connect(u) for u in users])
        report("第三连全部静默（不重复播）", all(s == "" for s in s2))


async def t6_stats_cost(hub: TenantHub) -> None:
    """stats 开/关 git 历史的开销。

    ━━━ 断言口径（2026-10-13 修正）━━━
    原断言「默认快于 with_commits」在 log TTL 缓存（dsh sync-import）
    就位后**不再成立** —— 稳态下 with_commits 也命中缓存，两个都很快。
    那正是缓存生效的证据，不是缺陷。现在断言两者都 < 50ms。
    """
    print("\n── 6. stats 开销 ──")
    n = 30
    t0 = time.perf_counter()
    for _ in range(n):
        await asyncio.to_thread(lambda: hub.stats("bob", lib_id="group-研发"))
    t_lean = (time.perf_counter() - t0) / n
    t0 = time.perf_counter()
    for _ in range(n):
        await asyncio.to_thread(
            lambda: hub.stats("bob", lib_id="group-研发", with_commits=True))
    t_rich = (time.perf_counter() - t0) / n
    print(f"    默认 {t_lean*1000:.1f}ms/次   with_commits {t_rich*1000:.1f}ms/次")
    report("两种 stats 都 < 50ms（TTL 缓存生效）",
           t_lean < 0.05 and t_rich < 0.05,
           f"lean={t_lean*1000:.1f}ms rich={t_rich*1000:.1f}ms")


async def main() -> int:
    quick = "--quick" in sys.argv
    # ignore_cleanup_errors：Windows 上 .git/objects 常因文件索引/杀软
    # 短暂锁住而删不掉（WinError 145）—— 那是环境问题，不该让压测
    # 的结果汇总被清理步骤带崩。
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        hub = make_hub(Path(td) / "home")
        await t1_multi_lib_concurrent_writes(hub)
        await t2_read_write_mix(hub)
        await t3_multi_workspace(hub)
        if not quick:
            await t4_export_import_scale(hub)
        await t5_digest_concurrency(hub)
        await t6_stats_cost(hub)

    print("\n" + "═" * 64)
    failed = [r for r in RESULTS if not r[1]]
    print(f"  {len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    for name, _, detail in failed:
        print(f"  ✗ {name}  {detail}")
    print("═" * 64)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
