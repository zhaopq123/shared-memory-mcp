# -*- coding: utf-8 -*-
"""治理四件套（v2 · S-11）—— **只出建议，绝不自动动手**。

━━━ 这个模块的红线 ━━━

里面**没有任何删除/改写函数**，一个都没有。原因不是"暂时没写"，是设计：

自动清理是最容易把人的东西弄丢的一类功能 —— 它在你没看着的时候执行，
出错了也不会有报错弹给你。所以治理只做三件事：

  1. **把状况算清楚**（容量、增长率、重复率、低引用候选）
  2. **把候选列出来**（哪几条像重复、哪几条可能过期了）
  3. **给建议动作**（"建议走取代链"），由人或 agent 显式执行

换句话说：**这里产出的是"报告"，不是"动作"**。
代码评审时如果发现这个文件里出现了 unlink / remove / 写库调用，
那就是有人越过了红线。

━━━ 四项各自的口径（写在注释里，省得以后重新猜）━━━

· ``duplicate_rate``   —— 落在**重复簇**里的条目占比（簇 ≥2 条才算）
· ``growth_30d``       —— 近 30 天新建条数（按 created 字段，不是 git 时间）
· ``low_reference``    —— 被召回次数 ≤ 阈值的条目（计数落 ``state/refs.json``）
· ``expired``          —— valid-until 已过 / 已被取代的条目
"""

from __future__ import annotations

import difflib
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .knowledge import CST, expiry_state

#: 标题相似度阈值（0~1）。0.82 是实测出来的：
#: 再高会漏掉"同一件事换个说法"，再低会把"XX 与 YY 的对比"这类
#: 本来该分开的条目聚到一起（误报比漏报烦人得多）。
DUP_TITLE_THRESHOLD = 0.82
#: 召回次数 ≤ 这个值算"低引用候选"（默认 0：一次没被召回过的）
LOW_REF_MAX = 0
#: 低引用的豁免期（天）：刚写的条目引用次数当然是 0，不该被点名
LOW_REF_GRACE_DAYS = 30


def _meta_of(obj: Any) -> dict[str, Any]:
    """取 meta。

    ⚠️ 两种输入都要能吃：**Note 对象**（meta 在 .meta 里）与
    **面板用的 dict**（字段已扁平化，time/claim 这些就在顶层）。
    只支持一种的话，面板和 MCP 就会各算一套口径 —— 两边的"重复率"
    不一样时，用户不知道该信哪个。
    """
    if isinstance(obj, dict):
        return obj
    m = getattr(obj, "meta", None)
    return dict(m) if isinstance(m, dict) else {}


def _title_of(obj: Any) -> str:
    return str(obj.get("title", "") if isinstance(obj, dict) else getattr(obj, "title", "") or "")


def _id_of(obj: Any) -> str:
    return str(obj.get("id", "") if isinstance(obj, dict) else getattr(obj, "id", "") or "")


def _norm_title(s: str) -> str:
    """标题归一化（比较用）：去空白与常见标点，全角转半角。"""
    t = str(s or "").strip().lower()
    t = re.sub(r"[\s\-_·:：,，。.、/\\|()（）\[\]【】「」\"'`]+", "", t)
    return t


def _age_days(note: Any) -> float:
    """条目存活天数（按 created）。解析不出来给一个很大的值（当作老条目）。"""
    raw = str(_meta_of(note).get("created", "") or "")
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if not m:
        return 1e6
    try:
        d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return 1e6
    return max(0.0, (datetime.now() - d).total_seconds() / 86400.0)


def duplicate_clusters(
    notes: list[Any], *, threshold: float = DUP_TITLE_THRESHOLD
) -> list[dict[str, Any]]:
    """把"像重复的"聚成簇。

    ━━━ 判据为什么是标题 + 标签，而不是正文相似度 ━━━

    正文相似度在"同一件事写了两遍"和"同一主题的两条不同结论"之间分不清 ——
    后者恰恰**不该**被合并（两条结论可以同时成立）。标题才是"我想记录的是
    不是同一个东西"的最强信号，标签是它的佐证。

    Returns:
        ``[{"reason", "titles", "ids"}]``，按簇内条目数倒序。**空列表是正常的**。
    """
    items = []
    for n in notes:
        title = _title_of(n)
        key = _norm_title(title)
        if len(key) < 2:
            continue      # 太短的标题相似度没有意义（"待办"vs"代办"）
        tags = frozenset(str(t) for t in (_meta_of(n).get("tags", []) or []))
        items.append({"id": _id_of(n), "title": title, "key": key, "tags": tags})

    used: set[int] = set()
    clusters: list[dict[str, Any]] = []
    for i, a in enumerate(items):
        if i in used:
            continue
        group = [a]
        for j in range(i + 1, len(items)):
            if j in used:
                continue
            b = items[j]
            ratio = difflib.SequenceMatcher(None, a["key"], b["key"]).ratio()
            shared = a["tags"] & b["tags"]
            # 标题高度相似，或标题中等相似且标签有交叠
            if ratio >= threshold or (ratio >= 0.62 and shared):
                group.append(b)
                used.add(j)
        if len(group) > 1:
            used.add(i)
            clusters.append({
                "reason": "标题高度相似" if len(group) == 2 else f"{len(group)} 条标题相似",
                "titles": [g["title"] for g in group],
                "ids": [g["id"] for g in group],
                "suggestion": "建议走取代链（superseded-by）而不是删除",
            })
    clusters.sort(key=lambda c: len(c["ids"]), reverse=True)
    return clusters


def capacity_report(
    notes: list[Any], refs: dict[str, int], *, now: datetime | None = None
) -> dict[str, Any]:
    """容量仪表（口径见模块头注释 —— 指标必须可复现）。"""
    # 必须带时区：expiry_state 里的 valid-until / review-after 解析出来都是 aware，
    # naive 的 datetime.now() 一比就 TypeError（线上 /api/governance 500 的根因）。
    now = now or datetime.now(CST)
    total = len(notes)
    recent = [n for n in notes if _age_days(n) <= 30]

    clusters = duplicate_clusters(notes)
    dup_ids = {i for c in clusters for i in c["ids"]}

    expired = []
    superseded = []
    for n in notes:
        st = expiry_state(_meta_of(n), now=now)["state"]
        if st == "expired":
            expired.append(_title_of(n))
        elif st == "superseded":
            superseded.append(_title_of(n))

    low = [
        _title_of(n)
        for n in notes
        if refs.get(_id_of(n), 0) <= LOW_REF_MAX
        and _age_days(n) > LOW_REF_GRACE_DAYS
    ]

    return {
        "total": total,
        "growth_30d": len(recent),
        "duplicate_clusters": len(clusters),
        "duplicate_rate": round(len(dup_ids) / total, 3) if total else 0.0,
        "expired_count": len(expired),
        "superseded_count": len(superseded),
        "low_reference_count": len(low),
        "low_reference_sample": low[:10],
        "expired_sample": expired[:10],
        "duplicates": clusters[:10],
        "note": "治理只出建议，不自动删除或改写任何条目。",
    }


# ────────────────────── 引用计数（S-11.4）──────────────────────
#
# 落 state/（**非 git** 目录）：这是运行时统计，不该进版本库 ——
# 它会随每次召回变化，提交进去等于每次召回都产生一个 commit。


def refs_path(home: Path | str) -> Path:
    return Path(home) / "state" / "refs.json"


def load_refs(home: Path | str) -> dict[str, int]:
    p = refs_path(home)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): int(v) for k, v in data.items() if isinstance(v, (int, float))}


def bump_refs(home: Path | str, ids: list[str]) -> None:
    """召回命中一次就 +1（原子写，重启不丢）。**失败不抛** —— 统计坏了不该影响召回。"""
    if not ids:
        return
    try:
        data = load_refs(home)
        for i in ids:
            data[str(i)] = int(data.get(str(i), 0)) + 1
        p = refs_path(home)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
        # 上限保护：条目多起来后这个文件不该无限长
        if len(data) > 20000:
            keep = dict(sorted(data.items(), key=lambda kv: kv[1], reverse=True)[:10000])
            tmp2 = p.with_name(f"{p.name}.{os.getpid()}.tmp")
            tmp2.write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp2, p)
    except Exception:  # noqa: BLE001 - 统计是旁路，绝不能因为它让召回失败
        pass


__all__ = [
    "DUP_TITLE_THRESHOLD",
    "LOW_REF_GRACE_DAYS",
    "LOW_REF_MAX",
    "bump_refs",
    "capacity_report",
    "duplicate_clusters",
    "load_refs",
    "refs_path",
]
