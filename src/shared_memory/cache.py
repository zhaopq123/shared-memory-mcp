"""读缓存 —— 给 vault 的热路径加一层可失效的内存缓存。

━━━ 为什么需要这一层 ━━━

基线测量（500 条记忆，本机）::

    all_notes         126 ms
    search            137 ms
    build_backlinks   192 ms
    get_one           208 ms
    纯 parse_note      0.007 ms/条

**97% 的时间花在文件 IO 上，解析本身可以忽略。**
而一次 ``memory_view`` 会触发 ``all_notes()`` 一次 + 每个视图函数再读一次 ——
面板刷新一次要重复扫库十几次。

所以缓存的目标很明确：**让「没变的文件」不必重读重解析**。

━━━ 为什么用 (mtime_ns, size) 做失效键 ━━━

不缓存「整个库的快照」，而是**逐文件**缓存。这样：

  · 改一条只有那一条失效，不会全库失效
  · 多个 vault / 多租户可以各自持有缓存，互不干扰
  · 进程重启后自然重建，无需持久化

``mtime_ns`` 选纳秒精度而不是秒 —— 秒级精度在「同一秒内改两次」时会漏检。
加上 ``size`` 做二次确认：极端情况下（某些文件系统 mtime 粗）size 能兜住。

⚠️ **已知边界**：如果外部程序把文件内容改成完全相同的大小、
且文件系统没有更新 mtime，缓存不会失效。这在实践中几乎不发生
（任何写入都会更新 mtime），但值得记录。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, TypeVar

T = TypeVar("T")


@dataclass
class _Entry:
    """一条缓存记录。"""

    stamp: tuple[int, int]
    value: Any


@dataclass
class NoteCache:
    """逐文件的「解析结果 + stamp」缓存。

    ``by_path`` 的 key 是**绝对路径字符串**，value 是 ``(stamp, Note)``。
    ``Note`` 是纯数据对象（vault.py 明确保证它不持有文件句柄），
    所以共享同一个实例是安全的 —— 但调用方若要修改，必须自己先 copy。

    另外缓存 ``stamp``（不只是 Note）：``_read_note`` 每次要先 stat 拿
    ``(mtime_ns, size)`` 才能判断命中。实测 500 个文件全量 stat 要
    ~47ms，是热路径最大的一笔开销。所以把 stamp 也留着，
    由上层在「目录没变」时直接取用，连 stat 都不用调。
    """

    by_path: dict[str, _Entry] = field(default_factory=dict)
    stamps: dict[str, tuple[int, int]] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)
    #: 上次「全量校验 stamp」的 monotonic 时刻。
    #: 见 ``NoteCache.should_revalidate`` —— 缓存有 TTL 兜底，
    #: 保证「外部改了文件但目录 mtime 没变」这种边角情况最终会被发现。
    _checked_at: float = field(default_factory=time.monotonic)
    revalidate_every: float = 5.0

    def should_revalidate(self) -> bool:
        """是否到了做一次全量 stamp 校验的时候（默认每 5s 一次）。

        为什么需要：``_read_note`` 命中 stamp 缓存时**不 stat**（性能考虑）。
        如果有人绕过 MCP 直接改文件内容，且文件系统没有更新 mtime，
        缓存会一直返回旧内容。这个 TTL 保证最坏 ``revalidate_every``
        秒内一定会重新 stat 一遍，把外部改动吸进来。

        这是「性能 vs 极端一致」的折中：正常写路径（走 invalidate_cache）
        永远是即时的；这条 TTL 只兜底「绕过服务的手工改文件」。
        """
        now = time.monotonic()
        if (now - self._checked_at) >= self.revalidate_every:
            self._checked_at = now
            return True
        return False

    def touch_checked(self) -> None:
        self._checked_at = time.monotonic()

    def stamp_of(self, p: Path) -> tuple[int, int] | None:
        """取缓存的 stamp；没有就 None（调用方需自行 stat）。"""
        with self.lock:
            return self.stamps.get(str(p))

    def set_stamp(self, p: Path, stamp: tuple[int, int]) -> None:
        with self.lock:
            self.stamps[str(p)] = stamp

    def get(self, p: Path, stamp: tuple[int, int]) -> Any | None:
        """命中且未过期就返回缓存值，否则 None。"""
        with self.lock:
            e = self.by_path.get(str(p))
            if e is not None and e.stamp == stamp:
                return e.value
            return None

    def put(self, p: Path, stamp: tuple[int, int], value: Any) -> None:
        with self.lock:
            k = str(p)
            self.by_path[k] = _Entry(stamp=stamp, value=value)
            self.stamps[k] = stamp

    def drop(self, p: Path) -> None:
        """某个文件变了/被删 → 丢掉它的缓存。"""
        with self.lock:
            k = str(p)
            self.by_path.pop(k, None)
            self.stamps.pop(k, None)

    def clear(self) -> None:
        with self.lock:
            self.by_path.clear()
            self.stamps.clear()

    def prune_missing(self) -> int:
        """清掉已被删除的文件的缓存，返回清理条数。

        没有这一条的话，长期运行的服务会因为软删除不断累积僵尸条目
        —— 文件没了，缓存还留着，白占内存。
        """
        with self.lock:
            dead = [k for k in self.by_path if not Path(k).exists()]
            for k in dead:
                del self.by_path[k]
                self.stamps.pop(k, None)
            return len(dead)

    def __len__(self) -> int:
        with self.lock:
            return len(self.by_path)


class TTLCache(Generic[T]):
    """带 TTL 的整体缓存 —— 用于「整块重算才划算」的东西。

    典型用途：``git log``（要 fork 一个子进程，开销固定且不小）。
    提交是有明确时间戳的短时间操作，用 TTL 兜住就够了 ——
    但**写入路径必须显式失效**，不能只靠 TTL 自然过期。
    """

    def __init__(self, ttl: float = 2.0) -> None:
        self.ttl = ttl
        self._data: T | None = None
        self._at: float = 0.0
        self._lock = threading.RLock()
        import time as _time

        self._now = _time.monotonic

    def get(self) -> T | None:
        """取缓存值；已过期或从未设置则返回 None。"""
        with self._lock:
            if self._data is not None and (self._now() - self._at) < self.ttl:
                return self._data
            return None

    def set(self, value: T) -> None:
        with self._lock:
            self._data = value
            self._at = self._now()

    def invalidate(self) -> None:
        with self._lock:
            self._data = None
            self._at = 0.0
