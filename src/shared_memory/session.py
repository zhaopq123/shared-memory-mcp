"""会话生命周期登记（v2 · S-2 防串台核心）。

━━━ 解决什么问题 ━━━

共享记忆跨会话、跨 agent 可见 —— 于是会**串台**：b 会话（在查天气）
搜天搜地，能把 ax 任务（在做重构）的**过程态**捞出来，误当现行口径。

光靠"默认只搜长期记忆"不够：任务**正在进行时**的上下文恰恰在过程态里，
收工前 agent 自己必须能搜到。所以范围得按会话动态收窄。

解法 = 会话显式登记 + 记忆按 scope 分层：

  · 开工 ``memory_session_start(task, space)`` → 登记会话 + 返回上下文包
  · 期间检索默认范围 = 长期记忆 + **本会话绑定任务域**
  · 收工 ``memory_session_end(outcome, summary)`` → 任务记忆归类：
    可复用的升 long-term，过程态标 archived（不删，可考古）
  · 未登记 = 没有会话上下文 → 任务域记忆不进默认检索

━━━ 存储 ━━━

``<home>/state/sessions.json``：**非 git** 目录（与 digest-cursors 同款，
见 digest.py 的说明）。结构：

    {
      "<user>/<task>": {
        "session_id": "<user>/<task>",
        "user": "...", "task": "...",
        "space": "...", "visibility": "private|space|shared",
        "status": "active|ended",
        "started": "<iso>", "updated": "<iso>",
        "ended": "<iso>", "outcome": "...",
        "cursor": {...}          # 调用方可存任意进度标记
      }
    }

幂等键 = ``(user, task)`` —— 同一任务重复 start 不重复登记，
只刷新 ``updated``。这样"任务"天然唯一，扫库也能按 task 名定位。

━━━ 并发 ━━━

原子写（tmp 名带 pid + 单调计数 + ``os.replace``）+ 全局文件锁 ——
与 digest 游标完全同款，理由见 digest.py：多会话/多连接同时 start/end，
不加锁会丢更新、撞 tmp 名、save 撞别的读句柄（Windows 实锤）。
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from .errors import ErrorCode, ToolError
from .vault import now_iso

logger = logging.getLogger(__name__)

#: 会话可见性三档（v2 D-2）。S-2 只登记不强制，S-10 才接可见性强制。
VISIBILITIES: tuple[str, ...] = ("private", "space", "shared")
#: 会话状态
STATUS_ACTIVE = "active"
STATUS_ENDED = "ended"

_FILE_LOCK = threading.Lock()
_TMP_SEQ = itertools.count()


def _sessions_path(home: Path) -> Path:
    return Path(home) / "state" / "sessions.json"


def task_scope(task: str) -> str:
    """任务名 → 生效域字符串（``task:<名>``）。"""
    return f"task:{task}"


def session_id(user: str, task: str) -> str:
    """会话幂等键：``<user>/<task>``（同任务=同会话，重复 start 幂等）。"""
    return f"{user}/{task}"


# ══════════════════════ 存储 ══════════════════════


def _read_file(home: Path | str) -> dict[str, dict[str, Any]]:
    """**不加锁**读（调用方要么已持锁，要么走公开的 ``load``）。"""
    try:
        raw = json.loads(_sessions_path(Path(home)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}


def _write_file(home: Path | str, data: dict[str, dict[str, Any]]) -> None:
    """**不加锁**整表原子写。

    会话表**小**（每活跃会话一条），整表替换的代价可忽略；
    与 digest 游标不同，这里没有"每用户条目"的多写者竞争面
    —— 但仍要防 tmp 名互踩，故带 pid + 计数。
    """
    path = _sessions_path(Path(home))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{next(_TMP_SEQ)}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load(home: Path | str) -> dict[str, dict[str, Any]]:
    """读会话表（加锁）。文件不存在/损坏 → 空表（视作没有任何会话）。"""
    with _FILE_LOCK:
        return _read_file(home)



# ══════════════════════ 校验 ══════════════════════


def _check_task(task: str) -> str:
    t = (task or "").strip()
    if not t:
        raise ToolError(
            ErrorCode.INVALID_ARGUMENT,
            "task 不能为空",
            hint="给这次会话起个任务名（如『共享记忆v2开发』），同任务重复调用幂等",
        )
    return t


def _check_visibility(visibility: str) -> str:
    v = (visibility or "private").strip() or "private"
    if v not in VISIBILITIES:
        raise ToolError(
            ErrorCode.INVALID_ARGUMENT,
            f"visibility 不合法：{visibility}",
            hint="合法值：" + " / ".join(VISIBILITIES) + "（默认 private）",
        )
    return v


# ══════════════════════ 生命周期 ══════════════════════


def start(
    home: Path | str,
    user: str,
    task: str,
    *,
    space: str = "",
    visibility: str = "private",
) -> dict[str, Any]:
    """登记（或复用）一个会话。返回该会话条目。

    **幂等**：同一 ``(user, task)`` 重复调用只刷新 ``updated`` /
    ``space`` / ``visibility``，不会重复登记，也不重置 ``started``
    （这样"这个任务是什么时候开的"始终是首次开工时间）。

    若同名会话此前已 ``ended``，重新 start 会**重开**它
    （新 ``started``，清 ``outcome``，回到 active）—— 同一任务分两段做
    是常见情形，不该被当成新任务。
    """
    t = _check_task(task)
    v = _check_visibility(visibility)
    sid = session_id(user, t)
    now = now_iso()

    with _FILE_LOCK:
        data = _read_file(home)
        entry = data.get(sid)
        if entry is None:
            entry = {
                "session_id": sid,
                "user": user,
                "task": t,
                "space": (space or "").strip(),
                "visibility": v,
                "status": STATUS_ACTIVE,
                "started": now,
                "updated": now,
                "cursor": {},
            }
        else:
            # 复用：不重置 started；ended 则重开
            if entry.get("status") == STATUS_ENDED:
                entry["status"] = STATUS_ACTIVE
                entry["started"] = now
                entry.pop("ended", None)
                entry.pop("outcome", None)
            entry["space"] = (space or "").strip() or entry.get("space", "")
            entry["visibility"] = v
            entry["updated"] = now
            entry.setdefault("cursor", {})
        data[sid] = entry
        _write_file(home, data)
    return dict(entry)


def end(
    home: Path | str,
    user: str,
    task: str,
    *,
    outcome: str = "",
    summary: str = "",
) -> dict[str, Any] | None:
    """收尾一个会话（标记 ended）。返回更新后的条目；找不到返回 ``None``。"""
    t = _check_task(task)
    sid = session_id(user, t)
    now = now_iso()
    with _FILE_LOCK:
        data = _read_file(home)
        entry = data.get(sid)
        if entry is None:
            return None
        entry["status"] = STATUS_ENDED
        entry["ended"] = now
        entry["updated"] = now
        if outcome:
            entry["outcome"] = outcome
        if summary:
            entry["summary"] = summary
        data[sid] = entry
        _write_file(home, data)
    return dict(entry)


def touch(
    home: Path | str, user: str, task: str, *, cursor: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """刷新 updated（可顺带更新 cursor 进度标记）。找不到返回 ``None``。"""
    sid = session_id(user, task)
    now = now_iso()
    with _FILE_LOCK:
        data = _read_file(home)
        entry = data.get(sid)
        if entry is None:
            return None
        entry["updated"] = now
        if cursor is not None:
            entry["cursor"] = dict(cursor)
        data[sid] = entry
        _write_file(home, data)
    return dict(entry)


# ══════════════════════ 查询 ══════════════════════


def get(home: Path | str, user: str, task: str) -> dict[str, Any] | None:
    """按 (user, task) 取一条会话；不存在返回 ``None``。"""
    return load(home).get(session_id(user, task))


def for_user(home: Path | str, user: str) -> list[dict[str, Any]]:
    """该用户的全部会话（含已结束），最近更新在前。"""
    items = [e for e in load(home).values() if e.get("user") == user]
    items.sort(key=lambda e: str(e.get("updated", "")), reverse=True)
    return items


def active(home: Path | str, user: str) -> list[dict[str, Any]]:
    """该用户**进行中**的会话（按最近更新排序）。

    正常一次只有一个；多个说明开了多个任务没关 ——
    检索会把这些任务的 scope **全部**纳入默认范围（不能丢过程态）。
    """
    return [e for e in for_user(home, user) if e.get("status") == STATUS_ACTIVE]


def active_tasks(home: Path | str, user: str) -> list[str]:
    """该用户进行中的任务名列表（去重、保序）。"""
    out: list[str] = []
    for e in active(home, user):
        t = str(e.get("task", ""))
        if t and t not in out:
            out.append(t)
    return out
