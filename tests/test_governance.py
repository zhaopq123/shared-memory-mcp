"""治理四件套（v2 · S-11）单测。

━━━ 这里测的都是"只读"的东西 ━━━

治理模块**不许有删除路径**（红线在 test_panel_contract 里守）。所以这里
测的是"算得对不对、留不留得住"，而不是"删得干不干净"。

三件事：
  1. 重复簇检测：宁可多聚（误报可），不可漏（漏报不可）—— 漏报等于
     用户永远不知道有两条在讲同一件事
  2. 容量指标可复现：同样的输入永远同样的输出（否则"指标"没法被信任）
  3. 引用计数落盘且重启不丢（它是"低引用候选"的唯一依据）
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shared_memory.governance import (  # noqa: E402
    bump_refs,
    capacity_report,
    duplicate_clusters,
    load_refs,
)


def _n(i: int, title: str, tags: list[str] | None = None, **meta):
    d = {"id": str(i), "title": title, "tags": tags or []}
    d.update(meta)
    return d


# ══════════════════════ 重复簇 ══════════════════════


def test_clusters_same_topic_different_wording() -> None:
    """同一件事换个说法 → 必须聚到一起（漏报是最糟的）。"""
    notes = [
        _n(1, "共享记忆 MCP 部署步骤", ["共享记忆"]),
        _n(2, "共享记忆MCP-部署步骤", ["共享记忆"]),
        _n(3, "咖啡冲煮水温", ["咖啡"]),
    ]
    clusters = duplicate_clusters(notes)
    assert len(clusters) == 1, f"应该只有一簇，实际 {len(clusters)}"
    assert set(clusters[0]["ids"]) == {"1", "2"}
    assert "取代链" in clusters[0]["suggestion"], "建议必须是取代而不是删除"


def test_no_cluster_for_unrelated_titles() -> None:
    """完全无关的标题不该被聚到一起（误报会让体检变成噪音）。"""
    notes = [_n(1, "咖啡冲煮水温"), _n(2, "网文第二卷大纲"), _n(3, "服务器重启流程")]
    assert duplicate_clusters(notes) == []


def test_short_titles_are_skipped() -> None:
    """极短标题不比相似度 —— "待办"vs"代办"聚一起没有意义。"""
    assert duplicate_clusters([_n(1, "待办"), _n(2, "代办")]) == []


def test_capacity_report_is_reproducible() -> None:
    """同样的输入 → 同样的指标（"指标不可复现"等于没有指标）。"""
    notes = [_n(1, "甲", ["x"]), _n(2, "甲", ["x"]), _n(3, "乙")]
    a = capacity_report(notes, {})
    b = capacity_report(notes, {})
    for k in ("total", "duplicate_rate", "low_reference_count", "duplicate_clusters"):
        assert a[k] == b[k], f"{k} 两次结果不一致"


def test_capacity_report_counts_duplicates() -> None:
    notes = [_n(1, "甲甲甲", ["x"]), _n(2, "甲甲甲", ["x"]), _n(3, "乙")]
    r = capacity_report(notes, {})
    assert r["total"] == 3
    assert r["duplicate_clusters"] == 1
    assert r["duplicate_rate"] == round(2 / 3, 3)
    assert "不自动删除" in r["note"], "报告必须自带「只建议」的声明"


# ══════════════════════ 引用计数 ══════════════════════


def test_refs_survive_restart(tmp_path: Path) -> None:
    """引用计数落在 state/ 且**重启（重新读文件）不丢**。"""
    bump_refs(tmp_path, ["a", "b", "a"])
    refs = load_refs(tmp_path)
    assert refs["a"] == 2 and refs["b"] == 1
    # 模拟重启：重新从盘上读
    assert load_refs(tmp_path) == refs


def test_refs_bump_never_raises_on_bad_home() -> None:
    """统计是**旁路**：坏路径也不能抛 —— 不然召回会因为统计失败而失败。"""
    bump_refs("/proc/definitely-not-writable/shm", ["a"])
    bump_refs("", [])
    # 走到这里没抛就算过
