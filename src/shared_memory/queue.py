"""单写者 FIFO 队列 —— 并发的唯一入口。

━━━ 为什么是这个设计 ━━━

需求是「多 agent 不隔离、共享同一份记忆、共同操作同一个工作空间」。
不隔离就意味着**并发写同一个 vault**，而并发写会静默互相覆盖。

这里选的做法不是加锁，是**排队**：所有写请求进一个 FIFO 队列，
由一个写者循环串行执行。由此获得：

  · 不会互相覆盖 —— 严格串行，后到的等前一个落盘
  · 不会死锁 —— 队列本身无锁，谈不上死锁
  · 不会无限等待 —— 有容量上限，满了立刻拒绝（QUEUE_FULL）
  · 崩溃可诊断 —— 未执行的请求明确失败，不会假装成功

━━━ 关键约定：ack 在落盘之后 ━━━

``submit()`` 返回的 future 在**写入真正完成之后**才 resolve。
绝不允许「入队即返回成功」—— 那样进程一崩，agent 以为写成功了，
实际什么都没落盘，而它会基于这个假事实继续往下做。
错误会以「记忆莫名其妙丢失」的形式出现在很远的地方，极难定位。

━━━ 读为什么不进队列 ━━━

读不改状态，没有竞争。让读也排队会把「一个 agent 写」变成
「所有 agent 都卡住」—— 那是把局部问题放大成全局故障。

━━━ 事件循环的坑（实测）━━━

``start()`` 会在**没有运行中事件循环**的同步上下文里被调用
（HTTP 入口在 ``uvicorn.run`` 之前就会调它）。在那里 ``create_task``
会抛 ``RuntimeError: no running event loop``，服务直接起不来。

所以 ``start()`` 只置标记，**写者循环在第一次 ``submit()`` 时惰性创建**
（那时必然在事件循环里）。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from .errors import ErrorCode, ToolError

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: 队列容量上限（含正在执行的那个）。
#:
#: 为什么要有上限：没有上限的话，写入速度超过消费速度时内存会无限增长，
#: 最终被 OOM 杀掉 —— 那会**牵连所有 agent**（服务是共享的）。
#: 有限的队列 + 明确拒绝，比无限队列 + 静默崩溃好得多。
#:
#: 256 的取值依据：单次写入是几毫秒级（写文件 + 偶尔 git），
#: 256 个积压意味着约 1~2 秒的排队延迟 —— 已经足够让调用方感知到"忙"，
#: 又不至于因为瞬时并发（几十个 agent 同时写）而误拒。
DEFAULT_MAX_DEPTH = 256


class WriteQueue:
    """串行执行写操作的 FIFO 队列（单写者模型）。

    线程/任务安全说明：所有方法都只在**同一个事件循环**里被调用
    （这是 asyncio 的前提）。不需要额外加锁 —— 队列本身就是同步点。
    """

    def __init__(self, *, max_depth: int = DEFAULT_MAX_DEPTH) -> None:
        """
        Args:
            max_depth: 容量上限（含在办任务）。超过时 `submit()` 抛 QUEUE_FULL。
        """
        # 元素是 (可调用对象, 结果 future) 或 None（毒丸，用于停机）
        self._queue: asyncio.Queue[
            tuple[Callable[[], Awaitable[Any]], asyncio.Future[Any]] | None
        ] = asyncio.Queue()
        self._max_depth = max_depth
        self._worker: asyncio.Task[None] | None = None

        # 可观测性计数（队列积压会让 agent 变慢，要能查出来）
        self._processed = 0
        self._rejected = 0

        # ⚠️ 已提交但**尚未完成**的任务数（含正在执行的那个）。
        #
        # 为什么不能只用 qsize() 判断容量：写者 `get()` 之后队列长度立刻减一，
        # 但那个任务还在跑。只数 qsize() 会让「1 个在办 + max_depth 个排队」
        # 被算成「未满」——于是超额的任务不是被拒绝，而是**无限等待**，
        # 把「满了明确拒绝」的承诺变成静默挂起。
        #
        # 实测症状：第三个并发写永远不返回，测试挂死。
        self._outstanding = 0

        #: start() 是否被调用过（不代表写者已创建）
        self._started = False

    # ══════════════════════ 生命周期 ══════════════════════

    def start(self) -> None:
        """标记队列可用。**不创建 asyncio 任务。**

        ⚠️ 这里刻意不 ``create_task``：``start()`` 会在**没有运行中事件循环**的
        同步上下文里被调用（HTTP 服务的 ``build_service()`` 就在 uvicorn.run
        之前调用它）。在那里创建任务会抛 ``RuntimeError: no running event
        loop``，服务直接起不来 —— 实测踩过。

        写者循环改为在第一次 ``submit()`` 时惰性创建（那时必然在事件循环里）。
        """
        self._started = True

    def _ensure_worker(self) -> None:
        """惰性创建写者循环。**只在事件循环内调用。**"""
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="shared-memory-writer")
            logger.info("写者循环已启动")

    async def stop(self, *, timeout: float = 10.0) -> None:
        """停止写者循环，等在途任务收尾。

        用「毒丸」（往队列放一个 None）而不是 `task.cancel()`：
        cancel 会在**任意 await 点**打断正在执行的写入，可能留下半写状态。
        毒丸让写者跑完当前任务、处理完已排队的任务后自然退出。

        Args:
            timeout: 等待收尾的秒数。超时后强制 cancel（避免停机卡住）。
        """
        if self._worker is None:
            return
        await self._queue.put(None)  # 毒丸
        try:
            await asyncio.wait_for(self._worker, timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            logger.warning("写者循环未在 %ss 内退出，强制取消", timeout)
            self._worker.cancel()
        self._worker = None

    # ══════════════════════ 提交 ══════════════════════

    async def submit(self, fn: Callable[[], Awaitable[T]]) -> T:
        """把一次写操作排进队列，**等它真正完成后**才返回。

        Args:
            fn: 无参可调用对象。可以是同步函数（返回普通值）或异步函数 ——
                两种都支持（见 ``_run`` 里的说明）。

        Returns:
            fn 的返回值。

        Raises:
            ToolError(QUEUE_FULL): 队列满。**立刻返回，不排队等待** ——
                宁可让调用方看到明确的"忙"，也不要让请求无限堆积。
            Exception: fn 自身抛出的异常会原样透传（并保证队列继续工作）。
        """
        if not self._started:
            self.start()
        self._ensure_worker()

        # 容量检查用 outstanding 而不是 qsize（原因见 __init__ 的注释）
        if self._outstanding >= self._max_depth:
            self._rejected += 1
            raise ToolError(
                ErrorCode.QUEUE_FULL,
                f"写入队列已满（深度 {self._max_depth}），请稍后重试",
                hint="稍等片刻重试；若持续出现，说明写入量超过了单写者吞吐",
            )

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[T] = loop.create_future()

        # ⚠️ 先加计数再入队：反过来的话，写者可能在这两步之间取走任务并完成，
        # 那时减计数会把一个不相干的任务名额一起减掉（计数漂移）。
        self._outstanding += 1
        await self._queue.put((fn, fut))
        try:
            return await fut
        finally:
            # 完成、异常、取消都要减 —— 否则一次取消会永久少一个名额，
            # 队列容量会随着取消次数增加而"缩水"，最终永久拒绝所有写入。
            self._outstanding -= 1

    # ══════════════════════ 写者循环 ══════════════════════

    async def _run(self) -> None:
        """串行执行：一次一个，严格先进先出。"""
        while True:
            try:
                item = await self._queue.get()
            except asyncio.CancelledError:
                break

            if item is None:  # 毒丸 → 正常停机
                self._queue.task_done()
                break

            fn, fut = item
            try:
                # ━━━ 为什么整个调用要丢进线程池 ━━━
                #
                # 队列里跑的是**文件 IO 与 git 子进程**（subprocess.run），
                # 单次 60~900ms。这些都是同步阻塞调用，直接在事件循环里跑
                # 会把整个服务卡住 —— 一个客户端写记忆，其余所有请求排队等它。
                #
                # 实测（本机 40 并发 + 20s soak）：不丢线程池时
                #   · 读请求 p95 = 2194ms
                #   · soak p99 = 19665ms（**19.6 秒**的鬼长尾）
                #
                # 而**串行语义不变** —— 队列的 FIFO 与「单写者」保证
                # 靠的是这里一次只 await 一个任务，不是「在哪个线程」。
                #
                # ⚠️ 不能用 `inspect.iscoroutinefunction(fn)` 分流：
                # `lambda: asyncio.sleep(0)` 是普通函数却返回协程，
                # 判断会走错分支，把协程丢进线程池变成「未执行的协程对象」。
                # 所以统一在线程里调用，再用 isawaitable 兜住异步结果
                # （这正是原实现容忍两种可调用对象的方式）。
                result = await asyncio.to_thread(fn)
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:  # noqa: BLE001
                # 单个任务失败**不能让写者退出** —— 否则一次业务异常
                # 就会让整个记忆服务永久失效（所有后续写入都挂在队列里）。
                # 把异常回给调用方，循环继续。
                if not fut.done():
                    fut.set_exception(exc)
                else:
                    logger.warning("任务已取消，但其异常无法送达: %r", exc)
            else:
                if not fut.done():
                    fut.set_result(result)
            finally:
                self._processed += 1
                self._queue.task_done()

        logger.info("写者循环已退出（已处理 %d 条）", self._processed)

    # ══════════════════════ 可观测性 ══════════════════════

    def stats(self) -> dict[str, Any]:
        """队列状态快照。用于 ``memory_stats`` 与 ``/health``。

        排查「写了没生效」时先看这里：
          · ``pending`` 持续很大 → 写入积压，服务消费不过来
          · ``worker_alive=false`` → 写者死了，所有写入都会永远挂着
          · ``rejected`` 在涨 → 有调用方被容量上限拒绝（该扩容了）
        """
        return {
            "pending": self._queue.qsize(),
            "outstanding": self._outstanding,
            "max_depth": self._max_depth,
            "processed": self._processed,
            "rejected": self._rejected,
            "worker_alive": self._worker is not None and not self._worker.done(),
        }

    @property
    def max_depth(self) -> int:
        """容量上限（供健康检查判定积压是否触顶）。"""
        return self._max_depth
