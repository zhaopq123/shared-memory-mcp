"""启动变更摘要（方案 11）+ 自动存取注入层（v2 S-12.1）。

━━━ 解决什么问题 ━━━

之前的流程里，agent 每次开工都是「冷启动」：不知道上次开会之后
库里多了什么、什么该复查了。要么它主动全库翻一遍（浪费），
要么它根本不知道有变化（更糟 —— 拿着过期口径干活）。

MCP 的 ``initialize`` 响应里有个 ``instructions`` 字段，客户端会
把它并进系统提示。fastmcp 4.1.0 有 ``on_initialize`` 中间件钩子，
可以在响应出去前**追加一段** —— 这就是本模块。

━━━ 三段（S-12.1，口径来自设计文档 D-7/D-9）━━━

1. **长期摘要**（``format_digest``）：你不在时库里变了什么。
   没变就是空串 —— 不刷屏。
2. **约束清单**（``constraint_manifest``）：``type=constraint`` 的条目。
   ⚠️ **这一段与游标无关，每次握手必达**。硬约束的宿命就是"每次都要
   知道"，靠 agent 想起来搜 = 迟早出事（D-9-1）。所以首次连接、
   零变更连接都得带上它。
3. **当前任务上下文包**（``session_context``）：正在做哪些任务 +
   ``type=workflow`` 的清单**置顶**带回（D-9-2：工作流不用搜，
   开工就在面前）。

━━━ 三条设计约束 ━━━

1. **零噪音**：自游标以来什么都没变 → 第 1 段为空，不碰 instructions。
   稳态下会话启动只多"约束 + 工作流"那点固定成本。
2. **失败静默**：任何异常都不能影响 initialize（中间件里整体兜底）
   —— 这是 nice-to-have，不是关键路径。
3. **游标按用户**：``<home>/state/digest-cursors.json``，
   ``{user_name: iso_ts}``。原子写（tmp + os.replace）。

━━━ 语义边界（诚实声明）━━━

摘要 = 「你离开期间**变更过**的东西」，含新写的、改过的、
以及**刚进入** review/expired 状态的。它**不重复播报**长期
处于告警态但没人动过的条目 —— 那会每次会话都刷屏；
查当前全部告警用 ``memory_view(kind="expiring")``，那才是它的职责。

时间比较用 ``>=``（秒级精度下同秒写入不能漏）+ ``seen`` 记账
（播报过的 note id 不再播）—— 两者合起来是**精确一次**：
不漏报、不重复。详见游标存储的说明。

⚠️ **注入层必须过共享档位闸**（``TenantHub.note_visible``）：
digest 是服务端**主动**塞进提示词的，调用方没索取就能看到内容 ——
漏过滤就不是"多看一条"，是**主动泄露**别人的 ``mine`` 条目。
扫描范围与工具面同口径（只碰 ``readable`` 的库）。

━━━ 成本 ━━━

每次 initialize 一遍可读库的 ``all_notes()``。读路径有 NoteCache
（mtime 缓存），重复扫描的边际成本接近零。三段都截断
（每库 top3 + 告警 top3 + 约束 12 条 + 工作流 6 条），不随数据量膨胀。
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from .knowledge import expiry_state
from .session import active_tasks, task_scope
from .vault import SCOPE_LONG_TERM, now_iso

logger = logging.getLogger(__name__)

#: 每库最多列几条"最近更新"标题（防 instructions 膨胀）
_MAX_TITLES_PER_LIB = 3
#: 时效告警最多列几条
_MAX_ALERTS = 3
#: 硬约束清单最多列几条（超出时报名字并给出取全的方法，见 constraint_manifest）
_MAX_CONSTRAINTS = 12
#: 工作流清单最多列几条
_MAX_WORKFLOWS = 6
#: seen 记账上限（note id 很短，1000 条撑爆不了游标文件）
_SEEN_CAP = 1000


# ══════════════════════ 游标存储 ══════════════════════
#
# 游标条目格式（v2）：
#     {"since": "<iso>", "seen": ["<note_id>", ...]}
#
# ━━━ 为什么需要 seen 记账（2026-10-13 补）━━━
#
# note 的 updated 是**秒级精度**。光靠 ``updated >= since`` 无法区分
# 「游标建立前一刻写入的基线条目」和「游标建立后同秒写入的新条目」——
# 前者会被重复播报，后者可能被漏掉。
#
# 时间精度不够，就靠记账：播报过的 note id 进 seen，之后不再播。
# 代价是游标文件变大（有 _SEEN_CAP 兜底），换来**精确一次**语义。


def _cursor_path(home: Path) -> Path:
    return home / "state" / "digest-cursors.json"


# ══════════════════════ 并发控制 ══════════════════════
#
# build_suffix 是「读游标 → 算摘要 → 写游标」的 read-modify-write。
# 并发 initialize（多用户同时连，或同用户多会话）下必须防三种 race
# （全部是 2026-10-13 压测/单测实锤，不是假想）：
#
#   ① **同一 tmp 文件名互踩** —— 所有写者共用 `xxx.tmp`，
#      Windows 上后写的 replace 报 WinError 32。
#      解法：tmp 名带 pid + 单调计数，全局唯一。
#   ② **save 撞别的用户的 load** —— 所有用户共用同一个
#      cursors.json，u1 的 os.replace 会撞上 u2 正开着的读句柄
#      （WinError 5）。每用户锁管不了这个 —— 文件是共享的。
#      解法：`_FILE_LOCK` 包住全部文件 IO（微秒级，无感）。
#   ③ **read-modify-write 丢更新** —— 同用户两个并发连接都读到
#      旧游标，后保存的覆盖先保存的。解法：**每用户一把锁**
#      （不是全局锁 —— 那会把所有用户的摘要计算串行化）。
#
# 跨进程（多实例共享 home）不在锁的覆盖范围内 —— 部署模型是
# 单写者进程；真要多实例得换文件锁，那时应重新评估整个设计。

_LOCKS_GUARD = threading.Lock()
_USER_LOCKS: dict[str, threading.Lock] = {}
_TMP_SEQ = itertools.count()
#: 游标**文件** IO 的全局锁。为什么需要它（每用户锁不够）：
#: 所有用户共用同一个 cursors.json —— u1 的 save（os.replace）
#: 会撞上 u2 的 load（read_text 正开着文件），Windows 上报
#: WinError 5/32（2026-10-13 压测 + 单测双实锤）。
#: 文件 IO 是微秒级，全局串行无感；真正的计算（扫描）在锁外。
_FILE_LOCK = threading.Lock()


def _user_lock(user_name: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _USER_LOCKS.get(user_name)
        if lock is None:
            lock = threading.Lock()
            _USER_LOCKS[user_name] = lock
        return lock


def load_cursors(home: Path) -> dict[str, Any]:
    """读游标表。文件不存在/损坏 → 空表（视作全体首次连接）。

    兼容 v1（值是纯 ISO 字符串）—— 升级不丢基线。
    """
    try:
        with _FILE_LOCK:
            raw = json.loads(_cursor_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, Any] = {}
    for k, v in (raw or {}).items():
        if isinstance(v, str):
            out[k] = {"since": v, "seen": []}
        elif isinstance(v, dict):
            out[k] = {"since": str(v.get("since", "")),
                      "seen": list(v.get("seen") or [])}
    return out


def update_cursor(home: Path, user_name: str, entry: dict[str, Any]) -> None:
    """只更新**一个用户**的游标条目（读-改-写全程在文件锁内）。

    ━━━ 为什么不能"保存整个表"（2026-10-13 单测实锤的深层竞态）━━━

    u0 和 u5 同时连接，各自 load 到旧表 → 各自算完 → 各自保存
    **整个表**。后保存的把先保存的用户条目**整个抹掉** ——
    u0 的基线凭空消失，它下一次连接什么都看不到。
    每用户锁管不了这个：锁是按用户分的，表是共享的。

    解法：保存时在锁内**重读最新表**，只覆盖自己那条。
    tmp 名带 pid + 计数（固定名在并发下踩 WinError 32，压测实锤）。
    """
    path = _cursor_path(home)
    try:
        with _FILE_LOCK:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = {}
            if not isinstance(raw, dict):
                raw = {}
            raw[user_name] = entry
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(
                f"{path.name}.{os.getpid()}.{next(_TMP_SEQ)}.tmp"
            )
            tmp.write_text(
                json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            os.replace(tmp, path)
    except OSError as exc:
        logger.warning("游标写入失败（不影响摘要返回）: %s", exc)


# ══════════════════════ 摘要计算 ══════════════════════


def _visible_scopes(hub: Any, user_name: str) -> set[str]:
    """该用户**默认可见**的生效域集合（S-3.2）。

    ``long-term`` + 他自己的活跃任务域。别人的任务域、归档区都不在内
    —— 摘要是"你不在时发生了什么"，不该播报你看不到的东西。
    """
    scopes: set[str] = {SCOPE_LONG_TERM}
    try:
        for t in active_tasks(hub.home, user_name):
            scopes.add(task_scope(t))
    except Exception as exc:  # noqa: BLE001
        logger.warning("活跃会话读取失败（摘要按 long-term 处理）: %s", exc)
    return scopes


def compute(
    hub: Any, user_name: str, since: str, seen: frozenset[str] | set[str] = frozenset()
) -> dict[str, Any]:
    """算 ``user_name`` 自 ``since`` 以来的变更摘要。

    只扫**该用户可读的库**（越权面为零 —— 与其它工具同一约束）。
    告警只报「本次变更中刚进入 review/expired 的」—— 长期告警态
    但无人动过的条目不重复播报（见模块 docstring 的语义边界）。
    ``seen`` 里的 note id 跳过（精确一次播报，见游标存储的说明）。
    """
    changed: list[dict[str, Any]] = []   # 每库：条数 + top 标题
    alerts: list[dict[str, str]] = []    # 本次变更里该复查/已过期的
    new_ids: list[str] = []              # 本次播报过的 id（回填 seen）
    total_changed = 0

    # v2（S-3.2）：摘要只汇**这个人默认可见的生效域** ——
    # long-term + 他自己的活跃任务域。别人的任务过程态（task:<别人>）
    # 与已归档条目都不播报标题：它们是**私域**，进别人的摘要就是泄露。
    scopes = _visible_scopes(hub, user_name)

    for lib in hub.tenant.readable(user_name):
        svc = hub.service_for(lib.id)
        notes = svc.vault.all_notes()
        lib_changed: list[tuple[str, str]] = []
        for n in notes:
            meta = n.meta or {}
            if str(getattr(n, "scope", SCOPE_LONG_TERM) or SCOPE_LONG_TERM) not in scopes:
                continue
            # ⚠️ 共享档位闸（S-12.1）：digest 是**主动注入**，别人的
            # ``mine`` 条目连"标题出现在提示词里"都不能有。
            if not hub.note_visible(user_name, n.to_dict(with_body=False), lib.id):
                continue
            updated = str(meta.get("updated", ""))
            # >= 而非 >：秒级精度下同秒写入不能漏（见语义边界）；
            # 同秒边界带来的重复由 seen 记账消除。
            if not (updated and since and updated >= since):
                continue
            nid = str(getattr(n, "id", "") or "")
            if nid and nid in seen:
                continue
            title = str(meta.get("title", ""))
            lib_changed.append((updated, title))
            total_changed += 1
            if nid:
                new_ids.append(nid)
            st = expiry_state(meta)["state"]
            if st in ("review", "expired"):
                alerts.append({
                    "title": title,
                    "state": st,
                    "lib": lib.label or lib.id,
                })
        if lib_changed:
            lib_changed.sort(reverse=True)
            changed.append({
                "lib": lib.label or lib.id,
                "count": len(lib_changed),
                "titles": [t for _, t in lib_changed[:_MAX_TITLES_PER_LIB]],
            })

    alerts.sort(key=lambda a: (a["state"] != "expired", a["title"]))
    return {
        "since": since,
        "total_changed": total_changed,
        "changed": changed,
        "alerts": alerts[:_MAX_ALERTS],
        "new_ids": new_ids,
    }


def format_digest(d: dict[str, Any]) -> str:
    """把摘要压成一段紧凑文本（空变更 → 空串）。"""
    if not d.get("total_changed") and not d.get("alerts"):
        return ""
    since_short = str(d.get("since", ""))[5:16].replace("T", " ")
    lines = [f"── 记忆库变更（自 {since_short}）──"]
    for c in d.get("changed", []):
        titles = "、".join(f"「{t}」" for t in c.get("titles", []))
        more = c["count"] - len(c.get("titles", []))
        suffix = f" 等 {more} 条" if more > 0 else ""
        lines.append(f"· {c['lib']}：{c['count']} 条更新（{titles}{suffix}）")
    for a in d.get("alerts", []):
        mark = "已过期" if a["state"] == "expired" else "该复查"
        lines.append(f"· ⚠ {a['lib']}「{a['title']}」{mark}")
    return "\n".join(lines)


def _all_note_ids(hub: Any, user_name: str) -> list[str]:
    """该用户可读范围内全部 note id（建基线时 seeding seen 用）。

    v2：与 compute 同口径，只收默认可见的生效域 —— 否则 seen 会被
    看不见的 id 灌满（无功能影响，但让"记账"语义变脏）。
    S-12.1：同样过共享档位闸，与 compute 逐条对齐（两边口径不一致时，
    别人隐藏的条目会在下一次连接被当成"新变更"播报出去）。
    """
    ids: list[str] = []
    scopes = _visible_scopes(hub, user_name)
    for lib in hub.tenant.readable(user_name):
        for n in hub.service_for(lib.id).vault.all_notes():
            if str(getattr(n, "scope", SCOPE_LONG_TERM) or SCOPE_LONG_TERM) not in scopes:
                continue
            if not hub.note_visible(user_name, n.to_dict(with_body=False), lib.id):
                continue
            nid = str(getattr(n, "id", "") or "")
            if nid:
                ids.append(nid)
    return ids


def _reusable(hub: Any, user_name: str) -> dict[str, list[dict[str, str]]]:
    """扫出可见范围内的「可复用资产」：``constraint`` 与 ``workflow`` 两型。

    D-9 把这两型当**资产**特殊对待：硬约束每次会话常驻（不靠搜），
    工作流随任务上下文包置顶带回。所以注入层要能**一次性**把它们捞全。

    与 compute 同两条闸：生效域（自己的任务域 + 长期）与共享档位。
    按更新时间倒序 —— 约束之间没有优先级，但"最近定下的"通常更现行。
    """
    out: dict[str, list[dict[str, str]]] = {"constraint": [], "workflow": []}
    scopes = _visible_scopes(hub, user_name)
    for lib in hub.tenant.readable(user_name):
        for n in hub.service_for(lib.id).vault.all_notes():
            kind = str(getattr(n, "type", "") or "")
            if kind not in out:
                continue
            if str(getattr(n, "scope", SCOPE_LONG_TERM) or SCOPE_LONG_TERM) not in scopes:
                continue
            if not hub.note_visible(user_name, n.to_dict(with_body=False), lib.id):
                continue
            out[kind].append({
                "title": str(n.title or ""),
                "lib": lib.label or lib.id,
                "updated": str(n.updated or ""),
            })
    for items in out.values():
        items.sort(key=lambda x: x["updated"], reverse=True)
    return out


def _asset_lines(items: list[dict[str, str]], cap: int, tag: str) -> list[str]:
    """列标题（只给标题 + 来源库）—— 全文走两段式读取（D-8）。"""
    lines = []
    for x in items[:cap]:
        lines.append(f"· [{tag}] {x['title']}（{x['lib']}）")
    more = len(items) - min(len(items), cap)
    if more > 0:
        lines.append(
            f"· …另有 {more} 条，取全："
            f"memory_recall(tags=[\"{tag}\"], scope=\"all\", limit=100)"
        )
    return lines


def constraint_manifest(hub: Any, user_name: str) -> str:
    """「硬约束」常驻清单（S-12.1 第二段）。没有约束条目时返回空串。

    ⚠️ 调用方**不能**把它当"有变更才带上"的可选段落 ——
    约束的价值恰恰在于零变更、首次连接时也在场。
    """
    items = _reusable(hub, user_name)["constraint"]
    if not items:
        return ""
    lines = ["── 硬约束（每次会话常驻 · 必须遵守，不用搜）──"]
    lines += _asset_lines(items, _MAX_CONSTRAINTS, "constraint")
    return "\n".join(lines)


def session_context(hub: Any, user_name: str, home: Path) -> str:
    """当前任务上下文包（S-12.1 第三段）：工作流置顶 + 活跃任务。

    v2 S-2.5：让 agent 一握手就知道"你正在做 X，相关记忆 N 条"——
    任务上下文不靠它自己想起来（Q7「会自动存取记忆吗」的读半边）。
    N 只算该任务域（``scope=task:<task>``）的条数，轻量。

    v2 S-12.1：按 D-9-2 把 ``workflow`` 型条目**置顶**带进同一个包 ——
    "怎么做"这类资产和"正在做什么"是同一件事的两半，分开两段反而
    要 agent 自己拼。两个都空才返回空串。
    """
    try:
        flows = _reusable(hub, user_name)["workflow"]
        flow_lines: list[str] = []
        if flows:
            flow_lines = ["── 当前任务上下文 ──", "· 工作流（按它走）："]
            for x in _asset_lines(flows, _MAX_WORKFLOWS, "workflow"):
                flow_lines.append("  " + x)
        tasks = active_tasks(home, user_name)
        if tasks:
            if not flow_lines:
                flow_lines = ["── 当前任务上下文 ──"]
            parts: list[str] = []
            for t in tasks:
                n = 0
                try:
                    r = hub.search(
                        user_name, "", status="all",
                        scope=task_scope(t), limit=1,
                    )
                    n = int(r.get("total", 0) or 0)
                except Exception:  # noqa: BLE001
                    n = 0
                parts.append(f"「{t}」（相关记忆 {n} 条）")
            flow_lines.append("· 你正在做：" + "、".join(parts))
            flow_lines.append(
                "· 过程态记忆只在你的默认检索范围里；**收工调 "
                "memory_session_end** 把它们归类归档（可复用的升长期）。"
            )
        return "\n".join(flow_lines) if flow_lines else ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("任务上下文计算失败（已忽略）: %s", exc)
        return ""


def build_suffix(hub: Any, user_name: str, home: Path) -> tuple[str, dict[str, str]]:
    """算三段注入内容并推进游标。返回 ``(instructions 追加段, 新游标表)``。

    段序按设计文档：**长期摘要 → 硬约束 → 当前任务上下文**。
    其中**只有第 2 段是无条件的**（D-9-1"每次都要知道"）——
    首次连接（还没游标）也要带上它，那恰恰是最需要约束的一刻。

    任何异常都吞掉返回空串 —— 摘要是 nice-to-have，
    绝不能把 initialize 搞挂。
    """
    try:
        # 每用户锁：读-算-写对同一用户原子（防同用户双连接交错）。
        # 跨用户条目丢失由 update_cursor 的锁内合并解决（见那里）。
        with _user_lock(user_name):
            cursors = load_cursors(home)
            entry = cursors.get(user_name) or {"since": "", "seen": []}
            since = str(entry.get("since", ""))
            seen = list(entry.get("seen") or [])
            now = now_iso()
            # 常驻两段：与游标无关，每次都算
            cons = constraint_manifest(hub, user_name)
            sess = session_context(hub, user_name, home)
            if not since:
                # 首次连接：建基线 + 现存条目全数记进 seen。
                # seeding 是必须的 —— 否则基线条目若与游标同秒，
                # 下次连接会被 ``updated >= since`` 误判成"新变更"。
                new_entry = {
                    "since": now,
                    "seen": _all_note_ids(hub, user_name)[-_SEEN_CAP:],
                }
                update_cursor(home, user_name, new_entry)
                combined = "\n\n".join(x for x in (cons, sess) if x)
                return combined, load_cursors(home)
            d = compute(hub, user_name, since, set(seen))
            suffix = format_digest(d)
            combined = "\n\n".join(x for x in (suffix, cons, sess) if x)
            # 推进游标 + 记账：本次播报过的 id 进 seen（精确一次）
            merged = list(dict.fromkeys([*seen, *d.get("new_ids", [])]))
            update_cursor(home, user_name,
                          {"since": now, "seen": merged[-_SEEN_CAP:]})
            return combined, load_cursors(home)
    except Exception as exc:  # noqa: BLE001
        logger.warning("变更摘要计算失败（已忽略）: %s", exc)
        return "", load_cursors(home)


# ══════════════════════ 中间件 ══════════════════


def make_digest_middleware(hub: Any, home: Path) -> Any:
    """构造 on_initialize 中间件（fastmcp 4.1.0）。

    身份从 contextvar 取（鉴权层在 MCP 之前注入）——
    取不到（无鉴权探活等）就静默跳过。
    """
    from fastmcp.server.middleware import Middleware

    class _DigestMiddleware(Middleware):
        async def on_initialize(self, context: Any, call_next: Any) -> Any:
            result = await call_next(context)
            try:
                from .request_ctx import require_user

                try:
                    user_name = require_user()
                except Exception:  # noqa: BLE001
                    return result  # 无身份（探活/内部请求）→ 不播报
                # 卸载到线程池：摘要含全库扫描，不能占着事件循环
                #（initialize 高峰期别的会话也在过这个循环）。
                import asyncio

                suffix, _ = await asyncio.to_thread(
                    build_suffix, hub, user_name, home
                )
                if suffix:
                    # instructions 为空也要注入：约束清单是"每次必达"的，
                    # 不能因为某个客户端/配置把 instructions 清空就失效。
                    base = getattr(result, "instructions", None) or ""
                    result.instructions = f"{base}\n\n{suffix}" if base else suffix
            except Exception as exc:  # noqa: BLE001
                logger.warning("digest 中间件失败（已忽略）: %s", exc)
            return result

    return _DigestMiddleware()
