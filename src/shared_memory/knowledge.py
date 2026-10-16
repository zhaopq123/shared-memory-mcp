"""体系层 —— 把「一堆记忆」变成「有结构的知识」。

━━━ 这一层解决什么 ━━━

没有它的时候：记忆是**扁平列表**，靠关键词搜，搜到什么算什么。
问题不是"搜不到"，是：

  · **不知道搜什么** —— 没有入口，只能漫无目的翻
  · **过期了还在被当真** —— 一条失效的部署方法比没有更危险
  · **看不到关系** —— 只知道碎片，不知道推理链

所以这一层做三件事：

  1. **时效** —— 让记忆知道什么时候该退休（valid-until / review-after / superseded-by）
  2. **引用** —— 让记忆之间连起来（related + 正文里的自动识别）
  3. **视图** —— 按规则实时算出来的入口（决策/踩坑/待办/过期/项目）

━━━ 关键设计：视图是「查询」不是「存储」 ━━━

不生成索引文件。理由：索引会过时，而**过时的索引比没有索引更糟**
（它会漏掉新内容，且没人知道它漏了）。视图每次实时算 —— 慢一点，但永远对。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .errors import ErrorCode, ToolError

CST = timezone(timedelta(hours=8))

#: 记忆的类型（type 字段）。不做强校验 —— 允许用户自定义类型，
#: 但内置的这几种在视图里有特殊待遇。
KNOWN_TYPES = ("note", "decision", "lesson", "todo", "ref",
               # v2 六型（S-1）；旧值保留可读
               "workflow", "constraint", "habit", "fact", "task")

#: 陈述性质（claim 字段）—— 这是"不幻觉"的**结构性**落点。
#:
#: 为什么必须有它：提示词是软约束，拦不住已经生成的幻觉。
#: 而 claim 是**写进文件**的标记 —— 后续 agent 读到时能分辨
#: "这是实测的"还是"这是某人猜的"，从而决定该不该采信。
CLAIM_KINDS = ("fact", "inference", "proposal")

#: claim 的显示前缀（读取时标出来，让人/agent 一眼看到）
CLAIM_MARK = {
    "fact": "✓ 事实",
    "inference": "? 推断",
    "proposal": "◇ 建议",
}


# ══════════════════════ 时效 ══════════════════════


def parse_date(value: str) -> datetime | None:
    """解析日期/时间。支持 ``2026-10-12`` 与完整 ISO 两种写法。

    宽容是刻意的：时效字段是**人手写**的（面板里填），
    写错格式不该让整条记忆失效 —— 解析不了就当"没有时效"。
    """
    if not value:
        return None
    v = str(value).strip()
    if not v:
        return None
    try:
        # 只有日期时按当天 00:00 解释
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            dt = datetime.fromisoformat(v).replace(tzinfo=CST)
        else:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=CST)
        return dt
    except (ValueError, TypeError):
        return None


def expiry_state(meta: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """判断一条记忆的时效状态。

    Returns:
        ``{"state": "active"|"expired"|"review"|"superseded", "detail": str}``

        · ``superseded`` —— 已被取代（有 superseded-by），最高优先级：
          即使日期没过期，它也不该再被当作现行口径
        · ``expired`` —— valid-until 已过（硬过期）
        · ``review`` —— review-after 已到，该复查（软提醒）
        · ``active`` —— 正常
    """
    now = now or datetime.now(CST)

    superseded_by = str(meta.get("superseded-by", "") or "").strip()
    if superseded_by:
        return {"state": "superseded", "detail": f"已被「{superseded_by}」取代"}

    valid_until = parse_date(str(meta.get("valid-until", "") or ""))
    if valid_until and now > valid_until:
        return {
            "state": "expired",
            "detail": f"已于 {valid_until.date().isoformat()} 过期",
        }

    review_after = parse_date(str(meta.get("review-after", "") or ""))
    if review_after and now > review_after:
        return {
            "state": "review",
            "detail": f"建议复查（{review_after.date().isoformat()} 之后未复查）",
        }

    return {"state": "active", "detail": ""}


# ══════════════════════ 引用 ══════════════════════


def extract_links(body: str, all_titles: set[str]) -> list[str]:
    """从正文里找出引用了哪些其他记忆。

    ━━━ 为什么要自动识别（而不是只靠手填 related）━━━

    手填 related 的下场跟元数据一样：**没人填**。
    而人在写正文时本来就会提到别的记忆的标题（"见 XXX"、"参考 YYY"）——
    那就已经是一条引用了，只是没人把它变成结构。

    识别两种写法：
      · wiki 式：``[[标题]]``（与 dsh-tiddlywiki 同口径）
      · 裸提及：正文里出现**已存在的记忆标题**（精确匹配，长度 >= 4）

    长度阈值的理由：太短的标题（如"配置"）会满篇误命中，
    那不是引用，是巧合。

    Args:
        body: 正文。
        all_titles: 库里现有记忆的标题集合。

    Returns:
        被引用的标题列表（去重，保持出现顺序）。
    """
    found: list[str] = []
    seen: set[str] = set()

    # ① wiki 式显式链接 —— 优先，且可以不局限于"已存在"（允许先写链接后建条目）
    for m in re.finditer(r"\[\[([^\[\]]+?)\]\]", body):
        target = m.group(1).split("|")[-1].strip()
        if target and target not in seen:
            seen.add(target)
            found.append(target)

    # ② 裸提及已存在的标题
    #
    # ⚠️ **性能：裸提及这一段是 O(标题数 × 正文长度)** ━━━
    #
    # `build_backlinks` 会对**每一条**记忆调用本函数，所以整体是 O(n²)。
    # 实测：500 条 80ms → 2000 条 1058ms（涨 13 倍，不是 4 倍）。
    #
    # ⚠️ 这里**不能**改成「遍历正文的每个位置去查标题表」——
    #    那样每条正文都要跑一次 Python 层循环，比现在的 C 层
    #    `str.__contains__` 慢 5 倍以上（实测 500 条：78ms → 418ms）。
    #
    # 真正的解法在**调用方** ``build_backlinks``：把「逐条调 extract_links」
    # 改成「先建标题 → 位置倒排索引，一次扫完全部正文」。
    # 本函数保持 O(标题数) 的朴素实现，因为它对**单条**调用是最快的
    # （C 层 substring search，且标题集通常不大）。
    #
    # ⚠️ set 遍历顺序不保证稳定，所以这里的输出顺序本就非确定；
    # 调用方（build_backlinks）不依赖顺序，只用「是否命中」。
    for title in all_titles:
        t = title.strip()
        if len(t) < 4 or t in seen:
            continue
        if t in body:
            seen.add(t)
            found.append(t)

    return found


def build_backlinks(notes: list[Any]) -> dict[str, list[str]]:
    """算出反向引用：哪些记忆引用了目标。

    用于面板的「谁引用了它」区块 —— 这是把碎片连成网的关键：
    顺着双向链接能从任意一点走进相关上下文。

    ⚠️ **性能：从 O(n²) 降到接近 O(n·m)** ━━━

    原实现对每条记忆调一次 ``extract_links``，而后者内部要遍历
    **全部标题**做 ``title in body`` —— 整体 O(n² × |body|)。
    实测（本机中文正文）：500 条 80ms → 2000 条 1058ms。

    优化：**先建「标题 → 标题 id」表，再把每条正文一次性扫完**。
    做法是「按标题首字符分桶 + 只比对该位置可能开头的标题」：

      · 每条正文扫一遍，桶把候选压到「同首字符的标题」
      · 仍比 n² 少一个量级（真实语料里同首字符标题很少）

    ⚠️ **但这里刻意用 Python 层循环**，与 ``extract_links`` 的选择相反 ——
    因为**规模不同**：
      · ``extract_links`` 通常只对**一条**正文调用，标题集小 → C 层 `in` 最快
      · ``build_backlinks`` 对**全部**正文调用 → 必须消除「每条都遍历全部标题」

    这是两种规模下的相反取舍，不是前后矛盾。
    """
    titles = {n.title for n in notes}
    if not titles:
        return {}

    # ── 1. wiki 式链接：一次性正则扫描全部正文 ──
    # 这一半本来就是 O(总正文长度)，不需要优化。
    back: dict[str, list[str]] = {}
    for n in notes:
        for target in re.findall(r"\[\[([^\[\]]+?)\]\]", n.body or ""):
            tgt = target.split("|")[-1].strip()
            if not tgt or tgt == n.title:
                continue
            lst = back.setdefault(tgt, [])
            if n.title not in lst:
                lst.append(n.title)

    # ── 2. 裸提及：先建首字符桶，再逐条正文扫 ──
    by_first: dict[str, list[str]] = {}
    for t in titles:
        s = t.strip()
        if len(s) < 4:
            continue  # 长度阈值，与 extract_links 一致
        by_first.setdefault(s[0], []).append(s)

    if not by_first:
        return back

    for n in notes:
        body = n.body or ""
        if not body:
            continue
        found: set[str] = set()
        i = 0
        n_chars = len(body)
        while i < n_chars:
            bucket = by_first.get(body[i])
            if bucket:
                for t in bucket:
                    # 标题 ≥4 字；若它已在正文里更早的位置出现过，
                    # 这里也会命中 —— 用 found 去重即可
                    if body.startswith(t, i):
                        found.add(t)
                        i += len(t) - 1
                        break
            i += 1
        for tgt in found:
            if tgt == n.title:
                continue
            lst = back.setdefault(tgt, [])
            if n.title not in lst:
                lst.append(n.title)

    return back


# ══════════════════════ 视图 ══════════════════════

#: 内置视图定义。``kind`` 是机器名，``label`` 是给人看的。
VIEWS: dict[str, dict[str, str]] = {
    "overview": {"label": "总览", "desc": "库的整体状况：条数、类型分布、时效告警"},
    "decisions": {"label": "决策记录", "desc": "定过的口径与取舍（type=fact/constraint，兼容旧 decision）"},
    "lessons": {"label": "踩坑清单", "desc": "踩过的坑，避免重复（type=lesson）"},
    "todos": {"label": "待办", "desc": "未完成的事（type=task，兼容旧 todo / status=todo）"},
    "expiring": {"label": "时效告警", "desc": "已过期 / 该复查 / 已被取代"},
    "inferences": {"label": "未核实的推断", "desc": "claim=inference 的条目 —— 引用前需当心"},
    "projects": {"label": "按项目", "desc": "每个工作区下的记忆"},
    "recent": {"label": "最近更新", "desc": "按时间倒序"},
}


def build_view(
    kind: str,
    notes: list[Any],
    *,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """按类型算出一个视图。

    ━━━ 为什么是函数而不是存储 ━━━

    视图每次实时算。不生成索引文件 —— 索引会过时，
    而**过时的索引比没有索引更糟**（会漏内容，且没人知道它漏了）。

    Args:
        kind: 视图类型（VIEWS 的键）。
        notes: 全部记忆。
        params: 额外参数。
            · ``tag``              主题过滤（已有）
            · ``workspace``        项目过滤（projects 视图用，已有）
            · ``limit``            条数上限（已有）
            · ``include_superseded``  false（默认）时**折叠取代链**：
              同一条 superseded-by 链只显示最新一代（方案 5a）。
              expiring 视图不受影响（它的职责就是展示这些问题条目）。

    Returns:
        ``{"ok", "kind", "label", "desc", "total", "items", "stats"?}``
    """
    params = params or {}
    kind = (kind or "").strip().lower()

    if kind not in VIEWS:
        raise ToolError(
            ErrorCode.INVALID_ARGUMENT,
            f"未知视图：{kind}",
            hint="可用视图：" + "、".join(VIEWS.keys()),
        )

    def brief(n: Any) -> dict[str, Any]:
        """视图里的条目摘要（带时效与类型标注）。"""
        d = n.to_dict(with_body=False)
        st = expiry_state(getattr(n, "meta", {}) or {})
        if st["state"] != "active":
            d["expiry"] = st
        t = (getattr(n, "meta", {}) or {}).get("type", "")
        if t:
            d["type"] = t
        c = (getattr(n, "meta", {}) or {}).get("claim", "")
        if c:
            d["claim"] = c
        return d

    def by_updated(items: list[Any]) -> list[Any]:
        return sorted(items, key=lambda n: (n.updated or n.created), reverse=True)

    # ── 各视图的筛选逻辑 ──

    if kind == "overview":
        types: dict[str, int] = {}
        claims: dict[str, int] = {}
        expiry_counts = {"expired": 0, "review": 0, "superseded": 0}
        workspaces: dict[str, int] = {}
        for n in notes:
            meta = getattr(n, "meta", {}) or {}
            t = meta.get("type", "note")
            types[t] = types.get(t, 0) + 1
            c = meta.get("claim", "")
            if c:
                claims[c] = claims.get(c, 0) + 1
            st = expiry_state(meta)
            if st["state"] in expiry_counts:
                expiry_counts[st["state"]] += 1
            ws = getattr(n, "workspace", "") or "(无工作区)"
            workspaces[ws] = workspaces.get(ws, 0) + 1
        return {
            "ok": True,
            "kind": kind,
            "label": VIEWS[kind]["label"],
            "desc": VIEWS[kind]["desc"],
            "total": len(notes),
            "stats": {
                "types": sorted(types.items(), key=lambda kv: -kv[1]),
                "claims": sorted(claims.items(), key=lambda kv: -kv[1]),
                "expiry": expiry_counts,
                "workspaces": sorted(workspaces.items(), key=lambda kv: -kv[1]),
            },
            "items": [brief(n) for n in by_updated(notes)[:20]],
        }

    def type_filter(t: str) -> list[Any]:
        return [
            n for n in notes
            if (getattr(n, "meta", {}) or {}).get("type") == t
        ]

    if kind == "decisions":
        # v2（S-1）：新写入推六型后，口径类记忆落在 fact/constraint；
        # 旧数据的 decision 仍要能读到（兼容不迁移）。
        items = [
            n for n in notes
            if (getattr(n, "meta", {}) or {}).get("type") in
            ("decision", "fact", "constraint")
        ]
    elif kind == "lessons":
        items = type_filter("lesson")
    elif kind == "todos":
        # 待办写法：type=task（v2）/ type=todo（旧）/ status=todo（更通用）
        items = [
            n for n in notes
            if (getattr(n, "meta", {}) or {}).get("type") in ("todo", "task")
            or (getattr(n, "meta", {}) or {}).get("status") == "todo"
        ]
    elif kind == "expiring":
        items = [
            n for n in notes
            if expiry_state(getattr(n, "meta", {}) or {})["state"] != "active"
        ]
        # 时效告警按严重度排：被取代 > 过期 > 该复查
        order = {"superseded": 0, "expired": 1, "review": 2}
        items = sorted(
            items,
            key=lambda n: order.get(
                expiry_state(getattr(n, "meta", {}) or {})["state"], 9
            ),
        )
    elif kind == "inferences":
        items = [
            n for n in notes
            if (getattr(n, "meta", {}) or {}).get("claim") == "inference"
        ]
    elif kind == "projects":
        ws = str(params.get("workspace", "") or "").strip()
        if ws:
            items = [n for n in notes if (getattr(n, "workspace", "") or "") == ws]
        else:
            items = notes  # 不带参数时列全部，前端按工作区分组
    elif kind == "recent":
        items = by_updated(notes)
    else:  # 理论上到不了（上面已校验），保险起见
        items = by_updated(notes)

    if kind != "expiring":
        items = by_updated(items)

    # ── 取代链折叠（方案 5a）──
    # A --superseded-by--> B --superseded-by--> C：旧代条目全部带
    # superseded-by 指针，最新代（C）不带。所以"折叠"= 在普通视图里
    # 排除带指针的旧代 —— 纯展示过滤，存储不动。
    # overview 不折（它是总览，计数含旧代才有意义）；expiring 不折
    # （它的职责就是把这些问题条目摆出来）。
    if kind not in ("expiring", "overview") and not params.get("include_superseded"):
        items = [
            n for n in items
            if not str((getattr(n, "meta", {}) or {}).get("superseded-by", "") or "").strip()
        ]

    # tag 视图：params 里给 tag 时收窄（用于"主题页"）
    tag = str(params.get("tag", "") or "").strip().lower()
    if tag:
        items = [n for n in items if tag in {t.lower() for t in (n.tags or [])}]

    lim = int(params.get("limit", 200) or 200)
    return {
        "ok": True,
        "kind": kind,
        "label": VIEWS[kind]["label"],
        "desc": VIEWS[kind]["desc"],
        "total": len(items),
        "items": [brief(n) for n in items[:lim]],
    }


def list_views() -> dict[str, Any]:
    """列出所有可用视图（给面板与 agent 当目录用）。"""
    return {
        "ok": True,
        "views": [
            {"kind": k, "label": v["label"], "desc": v["desc"]}
            for k, v in VIEWS.items()
        ],
    }
