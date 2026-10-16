"""多租户 MCP 工具面 —— 身份由服务端解析，调用方**无法**指定权限范围。

━━━ 最重要的一条设计约束 ━━━

**没有任何工具参数能让调用方扩大可见范围。**

  · 没有 ``libs=[...]`` 参数 —— 那是越权面
  · 没有 ``scope=company`` 参数 —— 同上
  · ``lib`` 参数只用来**收窄**（在你有权限的集合里选一个），不能扩大

身份来自凭据（token → 用户），与请求内容无关。这样"伪造参数越权"
在结构上就不可能。

━━━ 工具数量控制 ━━━

之前 11 个工具已经偏多（实测结论：工具越多，agent 选对的概率越低）。
多租户只新增 **2 个**：

  · ``memory_whoami`` —— 我是谁、能看哪些库（**开工先调它**）
  · ``memory_libs``   —— 库清单与配额

其余工具通过**新增可选参数 ``lib``** 复用，而不是变成 ``memory_write_team``
这种组合爆炸。

2026-10-12 优化再新增 **2 个**（共 14 个）：

  · ``memory_export`` —— 以工作空间为单位导出 bundle
  · ``memory_import`` —— 导入 bundle（走队列，冲突三策略）

━━━ 个人库写入的默认 ━━━

``memory_write`` 不传 ``lib`` 时落**自己的个人库** —— 写是最容易污染
别人的操作，默认落自己名下最安全。
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

from .errors import ErrorCode, ToolError
from .knowledge import list_views
from .tenancy import TenantHub
from . import session as _session
from . import governance as _governance
from .vault import SCOPE_LONG_TERM

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: 读路径卸载到线程池的默认线程数。
#:
#: 读操作 = 文件 IO（glob/stat/read_text），本质是同步阻塞调用。
#: 直接在事件循环里跑，会让**一个慢读把所有并发请求一起卡住**。
#:
#: 实测（本机 40 并发压测，未卸载时）：
#:   · 读 p95 = 2194ms，soak p99 = 19665ms
#: 卸载后长尾基本消失。
#:
#: 为什么是 8：读操作以磁盘 IO 为主，线程数超过 CPU 核数仍有收益
#:（IO 等待时线程可让出）；但再高对单块磁盘只会增加上下文切换。
#:
#: ━━━ 但这个「8」必须改成自适应 ━━━
#:
#: 这个服务实际跑在**手机**上（vivo / proot Ubuntu），核数与开发机差得多。
#: 实测教训（2026-10-13，生产压测）：写死 8 时 **20 并发读 p95 = 5796ms**，
#: 而**单请求实测只有 50~90ms** —— 说明不是单请求慢，
#: 是 8 个读线程 + 事件循环 + git 子进程在 8 核上**互相争抢**，把并发度做死了。
def _pool_size() -> int:
    """按 CPU 核数算读池大小：``核数 - 2``，下限 2、上限 8。"""
    try:
        cores = os.cpu_count() or 4
    except Exception:  # noqa: BLE001
        cores = 4
    # 减 2：给事件循环、git 子进程（fork 出来也要 CPU）、OS 的 IO 线程留余量
    return max(2, min(8, cores - 2))


_READ_POOL_SIZE = _pool_size()
_read_pool: Any = None


def _get_read_pool() -> Any:
    """惰性创建读线程池（进程内单例）。

    ⚠️ 必须**全局共享**而不是每个 Tools 实例一个：
    每个库（tenant vault）各自建池会让线程数随库数量线性膨胀。
    """
    global _read_pool
    if _read_pool is None:
        _read_pool = ThreadPoolExecutor(
            max_workers=_READ_POOL_SIZE, thread_name_prefix="shm-read"
        )
        logger.info("读线程池：%d 个线程（CPU %d 核）", _READ_POOL_SIZE, os.cpu_count() or 0)
    return _read_pool


class TenantTools:
    """多租户工具实现。

    与 ``MemoryTools`` 的差别：
      · 每个方法第一个参数是 **user_name**（由鉴权层注入，不是调用方传的）
      · 所有操作经过 ``TenantHub``，权限检查在那一层集中做
    """

    def __init__(self, hub: TenantHub) -> None:
        self.hub = hub

    @staticmethod
    async def _io(fn: Callable[[], T]) -> T:
        """把**同步的读操作**丢进线程池执行。

        ━━━ 为什么读也要卸载 ━━━

        ``hub.search`` / ``hub.view`` / ``hub.list_notes`` 这些是同步函数，
        内部做 glob / stat / read_text。在 asyncio 里直接调用会**阻塞事件循环**
        —— 期间服务端不响应任何其他请求。

        实测（本机 40 并发）：未卸载时读请求 p95 = 2194ms、soak p99 = 19665ms；
        卸载后长尾基本消失。

        ⚠️ **写路径不这么做** —— 写必须经 queue 串行（那是正确性要求，
        不是性能问题）。见 queue.py。
        """
        return await asyncio.get_running_loop().run_in_executor(
            _get_read_pool(), functools.partial(fn)
        )

    def _err(self, exc: Exception, action: str) -> dict[str, Any]:
        if isinstance(exc, ToolError):
            return exc.to_dict()
        logger.exception("%s 失败", action)
        return ToolError(
            ErrorCode.INTERNAL_ERROR,
            f"{action}失败：{type(exc).__name__}",
            reason=str(exc)[:300],
        ).to_dict()

    # ══════════════════════ 身份 ══════════════════════

    async def memory_whoami(self, user_name: str) -> dict[str, Any]:
        """我是谁、能看哪些库（**开工先调它**）。

        ━━━ 为什么这是多租户下第一个该调的工具 ━━━

        它告诉你：
          · 你的身份（用户名、角色、所属组）
          · 你能读的**每一个库**及其归属（公司/小组/个人）
          · 其中哪些你能写

        知道这些之后，后续操作才不会"我以为我能写"或
        "我不知道这个库是公司的还是别人的"。

        角色说明：
          · ``admin`` —— 能管用户与库；能写公司库
          · ``member`` —— 能读公司库、读写自己组的库与个人库

        返回的库列表**只有你有权限的那些** —— 所以它不构成泄露
        （知道自己有什么权限是应该的）。
        """
        try:
            return self.hub.whoami(user_name)
        except Exception as exc:  # noqa: BLE001
            return self._err(exc, "查询身份")

    async def memory_libs(self, user_name: str) -> dict[str, Any]:
        """列出你可读的库（含每个库的规模与写入队列状况）。

        与 ``memory_whoami`` 的区别：这个更侧重"每个库现在什么状况"
        （多少条、有没有积压），whoami 侧重"我是谁、我有什么权限"。

        ⚠️ 只统计你有权限的库 —— 全局统计本身就是泄露。
        """
        try:
            return await self._io(lambda: self.hub.stats(user_name))
        except Exception as exc:  # noqa: BLE001
            return self._err(exc, "列举库")

    # ══════════════════════ 会话生命周期（v2 S-2）══════════════════════

    def _space_lib_id(self, user_name: str, space: str) -> str:
        """把 ``space`` 解析成**可读**的 lib id；解析不到返回空串（跨库）。

        容错而非报错：会话的 space 是"在哪个空间做这件事"，写错/无权限
        不该让整个 session_start 失败 —— 退化为跨库范围并如实告知。
        """
        s = (space or "").strip()
        if not s:
            return ""
        try:
            self.hub.tenant.vault_for(user_name, s)  # 无权限/不存在 → 抛
            return s
        except ToolError:
            return ""

    def _open_tasks(self, user_name: str) -> list[str]:
        """别人显式开放给这个身份的会话任务名（v2 S-10.3）。

        "同空间"由会话登记的 ``space`` 判定 —— 判不出就不开放（收紧方向
        必须显式）。库里那些**没开会话**的 ``task:`` 存量条目不受这里影响，
        它们没有会话档位可依，仍按 share 档位与库权限决定可见性。
        """
        return _session.open_tasks(
            self.hub.home,
            user_name,
            same_space=lambda s: bool(self._space_lib_id(user_name, s)),
        )

    def _task_claimed_by_other(self, user_name: str, task: str) -> bool:
        """这个任务域正被**别人的 private 会话**占着吗。"""
        for entry in _session.load(self.hub.home).values():
            if entry.get("status") != "active":
                continue
            if str(entry.get("task", "")) != task:
                continue
            if entry.get("user") == user_name:
                return False          # 自己的会话，永远能读写自己的过程态
            if str(entry.get("visibility") or "private") != "private":
                return False          # 已开放
            return True
        return False

    def _scope_readable(self, user_name: str, sc: str) -> bool:
        """显式指定的 scope 允不允许读（v2 S-10.5：越权读**返回空**）。

        不回 SCOPE_DENIED 是刻意的：报错会告诉调用方"这个任务域存在、
        里面有东西不给你"，而存在性本身就是串台要防的东西。
        写侧才用 SCOPE_DENIED（写失败必须说清原因，见
        :meth:`_resolve_write_scope`）。
        """
        s = (sc or "").strip()
        if not s.startswith("task:") or len(s) <= len("task:"):
            return True
        task = s[len("task:"):]
        if task in _session.active_tasks(self.hub.home, user_name):
            return True
        return not self._task_claimed_by_other(user_name, task)

    def _effective_scopes(self, user_name: str, scope: str) -> list[str]:
        """把 memory_search 的 scope 参数解析成**实际要检索的 scope 列表**。

        · 显式传了 → 就那一个（``all`` / ``long-term`` / ``task:xx`` / ``archived``），
          但别人的 private 任务域会被**剔掉**（返回空结果，不报错）
        · 没传（默认）→ ``long-term`` + 当前活跃会话绑定的任务域
          + **别人显式开放（visibility=space/shared）的进行中会话任务域**。
          这是防串台的核心：private 的过程态只在"正在做这个任务的人"的
          默认范围里，别人的会话摸不到。
        """
        s = (scope or "").strip()
        if s:
            return [s] if self._scope_readable(user_name, s) else []
        out: list[str] = [SCOPE_LONG_TERM]
        for t in _session.active_tasks(self.hub.home, user_name):
            out.append(_session.task_scope(t))
        for t in self._open_tasks(user_name):
            sc = _session.task_scope(t)
            if sc not in out:
                out.append(sc)
        return out

    def _resolve_write_scope(self, user_name: str, scope: str) -> str:
        """把 memory_write 的 scope 参数解析成落盘的 scope 字符串。

        · 空 → 返回空串（交给 vault 缺省 long-term）
        · ``task`` / ``current`` → 当前活跃会话的任务域（无会话则报错）
        · 其他（``task:名`` / ``long-term`` / ``archived``）→ 原样交给 vault 校验
        """
        s = (scope or "").strip()
        if not s:
            return ""
        if s in ("task", "current"):
            tasks = _session.active_tasks(self.hub.home, user_name)
            if not tasks:
                raise ToolError(
                    ErrorCode.SESSION_NOT_STARTED,
                    "scope=task 需要先开一个会话",
                    hint="先调 memory_session_start(task=...) 再写过程态记忆",
                )
            return _session.task_scope(tasks[0])
        if s.startswith("task:") and len(s) > len("task:"):
            # 写别人的任务域 = 往别人的工作上下文里塞东西，必须显式授权。
            # 这里用 SCOPE_DENIED 而不是静默丢弃：写操作失败了调用方
            # 一定要知道是"没权限"而不是"参数错"，否则它会改参数重试。
            task = s[len("task:"):]
            if self._task_claimed_by_other(user_name, task):
                raise ToolError(
                    ErrorCode.SCOPE_DENIED,
                    f"任务域「{task}」属于别人的会话，且未开放",
                    hint="要让别人写，请对方 memory_session_start 时把 "
                         "visibility 切成 space/shared；否则写自己的任务域或 long-term",
                )
        return s

    async def memory_session_start(
        self,
        user_name: str,
        task: str,
        space: str = "",
        visibility: str = "private",
        project: str = "",
    ) -> dict[str, Any]:
        """开工：登记会话 + 返回**结构化上下文包**（三段）。

        ━━━ 为什么要有"会话"这个概念 ━━━

        共享记忆跨会话可见，不登记就会**串台**：b 会话搜天搜地，
        把 ax 任务的过程态捞出来当现行口径。登记之后，检索范围
        默认收窄到「长期记忆 + 本会话任务域」，别人的过程态摸不到。

        ━━━ 返回的三段 ━━━

        1. ``constraints`` —— **约束清单**（type=constraint，常驻）。
           硬约束不该靠"想起来去搜"，开工就摆在面前。
        2. ``task_notes`` —— 这个任务**此前**留下的记忆（scope=task:<task>）。
           同一任务分两段做时，上一段的过程态在这里接续。
        3. ``space_brief`` —— 空间长期摘要（workflow/constraint 优先，
           其余按更新时间），让你知道"这个空间里已经定过什么"。

        幂等：同 ``(user, task)`` 重复调用不重复登记，只刷新时间。

        ``project`` 是**归属标识**（项目路径或名字）：只登记进会话条目，
        让面板/摘要知道"这个任务属于哪个项目"。接入配置由客户端的
        ``scripts/connect.py`` 写 ``.mcp.json`` —— 服务端**不碰客户机
        文件系统**（跨机器时物理上够不着，同机时那也是越权动作）。

        调用时机：**动手之前**。收工对应 ``memory_session_end``。
        """
        try:
            lib = self._space_lib_id(user_name, space)
            entry = await self._io(
                lambda: _session.start(
                    self.hub.home, user_name, task,
                    space=space, visibility=visibility, project=project,
                )
            )

            def _collect() -> dict[str, Any]:
                kw: dict[str, Any] = {"lib_id": lib} if lib else {}

                # ① 该任务此前的过程态（含已归档 → status=all 才看得到全貌）
                task_items = self.hub.search(
                    user_name, "", status="all",
                    scope=_session.task_scope(task), limit=50, **kw,
                ).get("results", [])

                # ② 空间长期记忆（workflow/constraint 优先）
                long_items = self.hub.search(
                    user_name, "", scope=SCOPE_LONG_TERM,
                    limit=100, sort="updated", **kw,
                ).get("results", [])
                prio = {"constraint": 0, "workflow": 1, "lesson": 2, "habit": 3}
                long_items.sort(
                    key=lambda x: (prio.get(str(x.get("type", "")), 9), "")
                )
                constraints = [
                    x for x in long_items if x.get("type") == "constraint"
                ]
                return {
                    "constraints": constraints[:20],
                    "task_notes": task_items,
                    "space_brief": long_items[:8],
                }

            pkg = await self._io(_collect)
            return {
                "ok": True,
                "action": "started",
                "session": {
                    "session_id": entry.get("session_id", ""),
                    "task": entry.get("task", task),
                    "space": entry.get("space", ""),
                    "visibility": entry.get("visibility", visibility),
                    "started": entry.get("started", ""),
                },
                # 三段上下文包
                "constraints": pkg["constraints"],
                "task_notes": pkg["task_notes"],
                "space_brief": pkg["space_brief"],
                "counts": {
                    "constraints": len(pkg["constraints"]),
                    "task_notes": len(pkg["task_notes"]),
                    "space_brief": len(pkg["space_brief"]),
                },
                "hint": "约束清单要当硬约束遵守；写完过程态用 memory_write(scope=\"task\")；"
                        "收工调 memory_session_end。",
            }
        except Exception as exc:  # noqa: BLE001
            return self._err(exc, "开工登记会话")

    async def memory_session_end(
        self,
        user_name: str,
        task: str = "",
        outcome: str = "",
        summary: str = "",
    ) -> dict[str, Any]:
        """收工：把任务记忆**归类收尾**（留 / 归档，都不删）。

        ━━━ 归类规则（确定性，非猜测）━━━

        遍历本任务域（``scope=task:<task>``）的记忆，按 **type** 分类：

        · ``workflow`` / ``constraint`` / ``habit`` / ``lesson``
          → 升 ``long-term``（**可复用的东西最重要** —— 这是它们的归宿）
        · 其余（``fact`` / ``task`` / 未标注）→ 标 ``archived``
          （**不删**：过程态退出默认检索，但考古时 ``scope="archived"`` 仍可查）

        ``summary`` 非空时会额外写一条会话小结（type=fact，long-term），
        让"这个任务最后得到了什么"留在库里，而不是随聊天记录蒸发。

        没传 ``task`` 时自动取当前唯一活跃会话。
        """
        try:
            t = (task or "").strip()
            if not t:
                tasks = _session.active_tasks(self.hub.home, user_name)
                if len(tasks) == 1:
                    t = tasks[0]
                else:
                    raise ToolError(
                        ErrorCode.INVALID_ARGUMENT,
                        "看不出要结束哪个会话",
                        hint=f"活跃会话：{tasks or '无'}；请显式传 task",
                    )
            entry = _session.get(self.hub.home, user_name, t)
            if entry is None:
                raise ToolError(
                    ErrorCode.SESSION_NOT_STARTED,
                    f"没有这个会话：{t}",
                    hint="先 memory_session_start(task=...)",
                )
            lib = self._space_lib_id(user_name, str(entry.get("space", "")))
            kw: dict[str, Any] = {"lib_id": lib} if lib else {}

            items = await self._io(
                lambda: self.hub.search(
                    user_name, "", status="all",
                    scope=_session.task_scope(t), limit=200, **kw,
                ).get("results", [])
            )

            reusable = ("workflow", "constraint", "habit", "lesson")
            promoted: list[str] = []
            archived: list[str] = []
            errors: list[dict[str, str]] = []
            for item in items:
                title = str(item.get("title", ""))
                note_lib = str(item.get("_lib", "") or lib)
                typ = str(item.get("type", "") or "fact")
                new_scope = SCOPE_LONG_TERM if typ in reusable else "archived"
                try:
                    # 只改 scope，正文原样带过去（vault.put 更新分支按标题 upsert）
                    got = self.hub.get(user_name, title, lib_id=note_lib)
                    body = str((got.get("note") or {}).get("body", ""))
                    await self.hub.write(
                        user_name, title=title, body=body, lib_id=note_lib,
                        extra={"scope": new_scope},
                    )
                    (promoted if new_scope == SCOPE_LONG_TERM else archived).append(title)
                except Exception as exc:  # noqa: BLE001
                    errors.append({"title": title, "error": str(exc)[:160]})

            written_summary = ""
            if summary.strip():
                written_summary = f"会话小结：{t}"
                await self.hub.write(
                    user_name,
                    title=written_summary,
                    body=(
                        f"**任务**：{t}\n\n"
                        f"**结果**：{outcome or '（未标注）'}\n\n"
                        f"{summary.strip()}\n"
                    ),
                    lib_id=lib,
                    extra={"type": "fact", "claim": "fact", "scope": SCOPE_LONG_TERM},
                )

            await self._io(
                lambda: _session.end(
                    self.hub.home, user_name, t, outcome=outcome, summary=summary
                )
            )
            return {
                "ok": True,
                "action": "ended",
                "task": t,
                "promoted": promoted,
                "archived": archived,
                "summary_note": written_summary,
                "errors": errors,
                "counts": {"promoted": len(promoted), "archived": len(archived)},
                "hint": "过程态已退出默认检索（archived 仍可用 scope=\"archived\" 考古）。",
            }
        except Exception as exc:  # noqa: BLE001
            return self._err(exc, "收工结束会话")


    # ══════════════════════ 读 ══════════════════════

    async def memory_search(
        self,
        user_name: str,
        query: str = "",
        tags: list[str] | None = None,
        workspace: str = "",
        lib: str = "",
        limit: int = 20,
        offset: int = 0,
        since: str = "",
        until: str = "",
        sort: str = "relevance",
        claim: str = "",
        status: str = "",
        scope: str = "",
    ) -> dict[str, Any]:
        """检索共享记忆（**开工前先调这个**）。

        默认**跨你有权限的全部库**搜索，结果里每条带 ``_lib`` 标出来源。

        参数：
          · query     关键词，空格分隔（多词 AND）
          · tags      标签过滤（全部命中）
          · workspace 工作区过滤
          · **lib**   只搜某一个库。**只能收窄，不能扩大** ——
                      传一个你没权限的库会返回 403，而不是拿到内容。
          · limit / offset / since / until / sort  同前
          · **claim** ``fact`` / ``inference`` / ``proposal``，空 = 不限。
                      问"现行口径"用 ``fact``；看到 ``inference`` 该多留心眼。
          · **status** ``active``（默认）只返回现行口径，排除已被取代/
                      已过期的；``all`` = 含旧代的考古模式。
                      问"现在该怎么做"就用默认，别让过期口径误导你。
          · **scope** 生效域（v2 防串台）。**不传（默认）= 长期记忆 +
                      当前会话绑定的任务域** —— 任务过程态只在"正在做这个
                      任务的人"的默认范围里，别人的会话摸不到。
                      ``all`` = 全都要（考古）；``task:xx`` 只要那个任务域；
                      ``archived`` 只要已归档的过程态；``long-term`` 只要长期。
                      想跨任务查别人的过程态，必须显式给 ``scope``。

        ⚠️ 返回的 ``_lib_label`` 告诉你结果来自哪个库（公司 / 某小组 /
        某人个人库）—— 这决定你该不该采信、该不该先确认。

        找不到时不要立刻放弃：换短关键词，或用 tags/lib 单独看。
        """
        try:
            scopes = self._effective_scopes(user_name, scope)

            def _one(sc: str, lim: int, off: int) -> dict[str, Any]:
                return self.hub.search(
                    user_name, query, lib_id=lib, tags=tags, workspace=workspace,
                    limit=lim, offset=off, since=since, until=until, sort=sort,
                    claim=claim, status=status, scope=sc,
                )

            if len(scopes) == 1:
                return await self._io(lambda: _one(scopes[0], limit, offset))

            # 多 scope（默认：长期记忆 + 本会话任务域）→ 各搜一遍再合并。
            # 子查询 offset 一律 0，合并后统一 offset/limit 切片 ——
            # 否则每个子查询都扣一次 offset，结果会整体前移（错位）。
            parts = await self._io(lambda: [_one(sc, limit, 0) for sc in scopes])
            merged: list[dict[str, Any]] = []
            seen: set[tuple[str, str]] = set()
            total = 0
            per_lib: list[dict[str, Any]] = []
            for r in parts:
                for item in r.get("results", []):
                    key = (str(item.get("_lib", "")), str(item.get("id", "")))
                    if key in seen:
                        continue
                    seen.add(key)
                    merged.append(item)
                total += int(r.get("total", 0) or 0)
                per_lib.extend(r.get("libraries", []) or [])
            merged.sort(
                key=lambda x: (x.get("updated") or x.get("created") or ""),
                reverse=True,
            )
            off = max(0, offset)
            lim = max(1, min(limit, 100))
            page = merged[: off + lim][off:]

            # v2（S-11.4）：引用计数 —— 本次召回命中了哪些条目。
            # 落在 state/（非 git）。**旁路统计，失败不影响召回**：
            # 统计坏了只是"低引用候选"不准，而召回失败是功能不可用。
            _governance.bump_refs(
                self.hub.home,
                [str(n.get("id", "")) for n in page if n.get("id")],
            )
            return {
                "ok": True,
                "query": query,
                "total": total,
                "count": len(page),
                "offset": off,
                "scopes": scopes,
                "libraries": per_lib,
                "hint": "结果来自多个库/生效域，看 _lib_label 与 scope 判断来源。",
                "results": page,
            }
        except Exception as exc:  # noqa: BLE001
            return self._err(exc, "检索")

    async def memory_get(self, user_name: str, ident: str, lib: str = "") -> dict[str, Any]:
        """按标题或 id 读一条记忆的全文。

        不传 ``lib`` 时，**只在你可读的库里**找 —— 找不到就是找不到，
        不会去读无权限的库（那是越权）。

        传 ``lib`` 可精确定位（同名条目在不同库里可能都有）。
        """
        try:
            return await self._io(lambda: self.hub.get(user_name, ident, lib_id=lib))
        except Exception as exc:  # noqa: BLE001
            return self._err(exc, "读取")

    async def memory_list(
        self, user_name: str, limit: int = 50, tag: str = "",
        workspace: str = "", lib: str = "",
    ) -> dict[str, Any]:
        """列出记忆（最近更新在前），跨库合并。

        适合「看看我能看到的范围里都有什么」。结果不含正文。
        """
        try:
            return await self._io(
                lambda: self.hub.list_notes(
                    user_name, limit=limit, lib_id=lib, tag=tag, workspace=workspace,
                )
            )
        except Exception as exc:  # noqa: BLE001
            return self._err(exc, "列举")

    # ══════════════════════ 写 ══════════════════════

    async def memory_write(
        self,
        user_name: str,
        title: str,
        body: str,
        tags: list[str] | None = None,
        workspace: str = "",
        lib: str = "",
        mode: str = "upsert",
        author: str = "",
        claim: str = "",
        valid_until: str = "",
        review_after: str = "",
        superseded_by: str = "",
        scope: str = "",
        share: str = "",
    ) -> dict[str, Any]:
        """写入一条共享记忆（**有结论 / 踩坑 / 口径就该写回**）。

        ━━━ 写到哪个库 ━━━

        不传 ``lib`` → **写进你自己的个人库**（默认最安全）。

        要往共享库写必须**显式**传 ``lib``：
          · ``lib="company"``     公司库（**默认只读**，member 会拿到 403）
          · ``lib="group-xxx"``   你们组的库（组员可写）

        为什么默认落个人库：写是最容易污染别人的操作。
        默认落自己名下，永远不会误改公司规范或同事的东西。

        ━━━ 内容要求 ━━━

        你写的内容**权限范围内的人都能读到**，所以写成别人能直接用的形式：
        **背景（为什么）、结论（是什么）、依据（凭什么）**。

        · title     标题（按标题去重，同标题 = 同一条）
        · body      正文，Markdown
        · tags      标签，如 ["部署", "vivo"]
        · workspace 项目/工作区名
        · **lib**   写到哪个库（不传 = 个人库）
        · mode      upsert / create / append
        · author    可选，谁写的（仅溯源，**不用于权限**）
        · **claim** 这条记忆的性质：``fact``（事实）/ ``inference``
                    （推断）/ ``proposal``（建议）。**反幻觉的结构性
                    落点** —— 别人（和未来的你）搜到它时知道该用
                    什么姿势采信。不传则服务端按规则推断
        · **valid_until**    硬过期日期（``YYYY-MM-DD`` 或 ISO）。
                              到点后 search 默认不再返回它，expiring 视图告警
        · **review_after**   该复查日期。到点后 expiring 视图提醒复查
        · **superseded_by**  本记忆被哪条取代了（填**新记忆的标题**）。
                              "取代而非删除"的抓手：旧条标上它即退出现行口径，
                              但原文仍在，后人知道"这条路走过、不通"
        · **share**   共享档位（v2 S-10）：``mine`` 仅自己 / ``space`` 同空间
                      / ``shared`` 已授权跨空间。不传 = 继承空间默认档
                      （``_config/space.json`` 的 ``default_share``）。
                      ``mine`` 会**用鉴权身份填 author**，因此不接受
                      代录（填了别人的 author 直接报错）—— 否则写出的
                      条目对写者自己不可见。

        ━━━ 时效字段的覆盖规则 ━━━

        upsert 更新已有记忆时，这三个字段**显式传才覆盖**，不传保留原值 ——
        避免"只想改正文"却把有效期信息清掉。

        什么时候该写：搞清根因 / 踩了别人也会踩的坑 / 定下口径 /
        发现"看起来是但其实不是"的事实。

        不需要写：能从代码直接读到的、临时中间状态、只对当前这句话
        有意义的上下文。

        写入是串行的（先进先出队列），并发不覆盖、不死锁。
        """
        # 时效字段走已有的 extra 通道进 frontmatter（机制早已存在，
        # 这里只是把它们暴露成工具参数 —— 方案 4）。
        extra: dict[str, Any] = {}
        if claim:
            extra["claim"] = claim
        if valid_until:
            extra["valid-until"] = valid_until
        if review_after:
            extra["review-after"] = review_after
        if superseded_by:
            extra["superseded-by"] = superseded_by
        try:
            # v2（S-2）scope：空=缺省 long-term；"task"/"current"=当前会话任务域；
            # 也接受显式 "task:名"/"archived"。放在 try 内 —— 解析失败（如
            # scope=task 但没开会话）要走错误信封，不能裸抛。
            resolved_scope = self._resolve_write_scope(user_name, scope)
            if resolved_scope:
                extra["scope"] = resolved_scope

            # v2（S-10.2）share：空 = 继承空间默认档（读侧解析，不落盘）。
            # 非法值由 vault.validate_v2_fields 拦下（INVALID_ARGUMENT）。
            sh = (share or "").strip()
            if sh:
                extra["share"] = sh
            if sh == "mine":
                if author and author != user_name:
                    # 「仅自己」+ 作者填别人 = 写出一条**你自己再也读不到**的条目。
                    # 不拦的话它写得成功、搜不到、列不出，只剩一个 git 提交当证据 ——
                    # 这种"成功的黑洞"比报错难查十倍。
                    raise ToolError(
                        ErrorCode.INVALID_ARGUMENT,
                        "share=mine 的条目作者必须是调用者本人",
                        hint="代录别人的口径请改用 share=space（或留空继承空间默认档）",
                    )
                author = user_name
            return await self.hub.write(
                user_name,
                title=title, body=body, tags=tags, workspace=workspace,
                lib_id=lib, mode=mode, author=author, extra=extra or None,
            )
        except Exception as exc:  # noqa: BLE001
            return self._err(exc, "写入")

    async def memory_delete(
        self, user_name: str, ident: str, confirm: bool = False, lib: str = ""
    ) -> dict[str, Any]:
        """删除一条记忆（**软删除**，移入回收站，可恢复）。

        需要 ``confirm=True`` —— 防误删的一道闸。

        不传 ``lib`` 时在**你可写的库**里找它。找不到说明它不在你的
        写权限范围内（可能只读，或者是别人的）。
        """
        if not confirm:
            return ToolError(
                ErrorCode.GOV_CONFIRM_REQUIRED,
                f"确认要删除「{ident}」吗？确认后请带 confirm=True 再调一次",
                hint="这是软删除：会移入 .trash 保留，可在面板的回收站页恢复",
            ).to_dict()
        try:
            return await self.hub.delete(user_name, ident, lib_id=lib)
        except Exception as exc:  # noqa: BLE001
            return self._err(exc, "删除")

    async def memory_restore(
        self, user_name: str, filename: str, lib: str
    ) -> dict[str, Any]:
        """从回收站恢复一条被软删除的记忆。

        ``lib`` 必填 —— 回收站是**库内**的概念，恢复必须指明哪个库。
        """
        try:
            return await self.hub.restore(user_name, filename, lib_id=lib)
        except Exception as exc:  # noqa: BLE001
            return self._err(exc, "恢复")

    # ══════════════════════ 体系 ══════════════════════

    async def memory_view(
        self, user_name: str, kind: str = "", lib: str = "",
        workspace: str = "", tag: str = "", limit: int = 200,
        include_superseded: bool = False,
    ) -> dict[str, Any]:
        """看「视图」—— 按规则实时算出来的入口（**不知道搜什么时用它**）。

        可用 kind：
          · ``overview``    总览（类型分布、时效告警统计）
          · ``decisions``   决策记录 —— 定过的口径与取舍
          · ``lessons``     踩坑清单 —— 避免重复踩
          · ``todos``       待办
          · ``expiring``    时效告警 —— 已过期/该复查/已被取代
          · ``inferences``  未核实的推断 —— 引用前当心
          · ``projects``    按项目
          · ``recent``      最近更新
          · 不填 → 返回视图目录

        · **include_superseded** 默认 ``False``：已被取代
          （``superseded-by`` 非空）的条目不进普通视图 —— 现行口径视图
          里混着"已走不通的旧路"会误导人。要考古显式传 ``True``
          （``expiring`` 视图恒含它们，它本来就是管这个的）。

        跨库时**逐库算再合并**（不能先合并全库再算 —— 那会把无权库的
        内容算进统计，泄露规模）。
        """
        try:
            if not (kind or "").strip():
                # 目录是静态清单（不依赖任何库的数据），直接返回，
                # 不走 tenant.readable —— 无权库时也能拿到目录。
                return list_views()
            return await self._io(
                lambda: self.hub.view(
                    user_name, kind, lib_id=lib,
                    workspace=workspace, tag=tag, limit=limit,
                    include_superseded=include_superseded,
                )
            )
        except Exception as exc:  # noqa: BLE001
            return self._err(exc, "视图")

    async def memory_backlinks(
        self, user_name: str, title: str, lib: str = ""
    ) -> dict[str, Any]:
        """查一条记忆的**引用关系**：谁引用了它、它引用了谁。

        引用是自动识别的（正文里的 ``[[标题]]`` + 提到已存在的标题），
        不需要维护关系表。

        ⚠️ 只在**同库内**统计 —— 跨库引用会泄露"另一库存在某个标题"，
        那本身就是信息泄露。
        """
        try:
            return await self._io(lambda: self.hub.backlinks(user_name, title, lib_id=lib))
        except Exception as exc:  # noqa: BLE001
            return self._err(exc, "查引用")

    # ══════════════════════ 工作空间（共享控制）══════════════════════

    async def memory_workspaces(self, user_name: str) -> dict[str, Any]:
        """列出**工作空间**（= 库）及其共享范围（**管理时用**）。

        ━━━ 为什么这个和 memory_libs 分开 ━━━

        ``memory_libs`` 侧重"库有多少条、队列堵不堵"（运行状况）；
        这个侧重"**这个工作空间共享给谁**"（权限状况），
        并带上 ``can_manage``（你能不能改）与可选的共享范围。

        每个库带：
          · ``share_label`` —— 共享范围的人类可读说明
            （"仅 admin 自己" / "小组：研发" / "公司（全员可读）"）
          · ``readable`` / ``writable`` —— 你能不能读/写
          · ``can_manage`` —— **你能不能改它的共享范围**

        什么时候用：
          · 想确认"我这条记忆会不会被别人看到"
          · 准备写敏感内容前，先看落哪个工作空间合适
          · 想共享某个工作空间给组里
        """
        try:
            return self.hub.workspace.list_for(user_name)
        except Exception as exc:  # noqa: BLE001
            return self._err(exc, "列举工作空间")

    async def memory_workspace_create(
        self, user_name: str, workspace: str, share: str = "", label: str = ""
    ) -> dict[str, Any]:
        """为一个**工作空间**建独立的记忆库（决定它共享给谁）。

        ━━━ 为什么要「一个工作空间一个库」━━━

        共享范围必须绑在**库**上（物理隔离），不能绑在条目标签上：

        · 库级隔离 —— 没权限的人**根本打不开那个库**
        · 条目标签 —— 得"搜完再过滤"，漏一处（分页/计数/高亮）就泄露

        所以「这个工作空间不共享」= 它的库 owner 是 ``user:你``。

        Args:
            workspace: 工作空间名（中文可以，会安全化后作目录名）。
            share: 共享范围，留空 = **仅自己**（最安全的默认）：
              · ``user:<你的用户名>``  仅自己（不共享）
              · ``group:<组名>``       组内可读（你必须在那个组里）
              · ``company``            全员可读（**仅管理员能设**）
            label: 显示名，默认用工作空间名。

        建完后写入时传 ``lib="<返回的 lib>"`` 即落进这个工作空间。
        """
        try:
            return self.hub.workspace.create(
                user_name, workspace=workspace, share=share, label=label
            )
        except Exception as exc:  # noqa: BLE001
            return self._err(exc, "建工作空间")

    async def memory_workspace_share(
        self, user_name: str, lib: str, share: str
    ) -> dict[str, Any]:
        """改一个工作空间（库）的**共享范围** —— 逐步放开或收回。

        ━━━ 这个操作的性质：它能决定"谁能看到内容" ━━━

        所以只有**库所有者**或**管理员**能做。别人能改的话，
        就能把你私人的东西改成公开 —— 那是泄露，不是配置。

        典型用法（逐步放开）：
          1. 先 ``share="user:我"`` —— 自己先写、先整理
          2. 觉得可以给组里看了 → ``share="group:研发"``
          3. 变成通用规范 → ``share="company"``（管理员）

        **收回也一样**（``company`` → ``user:我``），立即生效：
        没权限的人物理上打不开那个库了。

        ⚠️ 改成 ``company`` 时库会**自动变成只读**（成员不能写）——
        公司库是所有新人看到的第一份材料，让所有人随手写会让它烂掉。
        """
        try:
            return self.hub.workspace.share(user_name, lib=lib, share=share)
        except Exception as exc:  # noqa: BLE001
            return self._err(exc, "改共享范围")

    # ══════════════════════ 运维 ══════════════════════

    async def memory_commit(
        self, user_name: str, lib: str = "", message: str = ""
    ) -> dict[str, Any]:
        """把库里的改动**手动提交**进 git（v2 · S-8 之后这是常规通道）。

        ━━━ 模型变了，别再按"防抖会自动提交"理解 ━━━

        v1 是"每次写都提交（防抖 8 秒）"，结果是历史被"改个错别字"淹没，
        真想 diff 的那条找不到。v2 改成**按需**：

          · 普通写入 **不碰 git** —— 防手滑靠库内 `.history/` 改前快照
          · 标了 ``versioned: true`` 的长文 → 进共享版本库，每次一提交
          · 删除/恢复 → 立刻提交本库（破坏性动作必须留痕）
          · **其余想进版本库时，就是这个工具**

        所以什么时候需要它：
          1. **要把一段时间的成果固化成一个版本**（做完一个阶段、给别人看）
          2. **要确认改动真的入库了** —— 排查"写了没生效"
          3. **要写一条有意义的提交说明**（默认说明是模板）

        ``lib`` 不传则提交**你所有可写的库**（一次全提）。
        传 ``lib`` 只提那一个（需要写权限）。

        返回 ``committed=false`` + ``message="无改动"`` **不是失败**，
        是"工作树本来就干净"。
        """
        try:
            if lib:
                self.hub.tenant.vault_for(user_name, lib, need_write=True)
                r = await self.hub.service_for(lib).commit_now(
                    message or f"手动提交：{user_name}"
                )
                r["_lib"] = lib
                return r
            # 不传库 → 把所有可写库都提一遍
            libs = self.hub.tenant.writable(user_name)
            results = []
            for lib_obj in libs:
                try:
                    r = await self.hub.service_for(lib_obj.id).commit_now(
                        message or f"手动提交：{user_name}"
                    )
                    results.append({"lib": lib_obj.id, **r})
                except Exception as exc:  # noqa: BLE001
                    results.append({"lib": lib_obj.id, "committed": False,
                                    "message": str(exc)[:120]})
            committed = sum(1 for r in results if r.get("committed"))
            return {
                "ok": True,
                "committed_count": committed,
                "lib_count": len(results),
                "results": results,
                "message": f"已提交 {committed}/{len(results)} 个库",
            }
        except Exception as exc:  # noqa: BLE001
            return self._err(exc, "提交")

    async def memory_snapshot(
        self, user_name: str, *, out_dir: str = "", with_state: bool = False
    ) -> dict[str, Any]:
        """整目录快照（v2 S-8.4）—— **这是唯一的"能把数据捞回来"的动作**。

        ⚠️ 别把它和 ``memory_export`` 混：export 导的是**条目内容 bundle**
        （可跨库导入、按工作区过滤），快照打的是**磁盘现状**
        （`libs/` + `_versioned/` + `tenants.json`，含 frontmatter、回收站、
        改前快照这些 export 不带的东西）。要恢复一台机器用快照。

        ``with_state=True`` 才连 `state/`（令牌/主密钥/PIN）一起打 ——
        默认不给，因为那个包等于一袋凭据。
        """
        try:
            r = self.hub.snapshot_home(out_dir=out_dir, with_state=with_state)
            r["_actor"] = user_name
            return r
        except Exception as exc:  # noqa: BLE001
            return self._err(exc, "快照")

    async def memory_restore(
        self, user_name: str, bundle: str, *, confirm: bool = False
    ) -> dict[str, Any]:
        """从快照恢复整目录。``confirm=False`` 只返回预览、不动数据。

        恢复前**自动**给当前状态打一个快照（``pre_restore_snapshot``）——
        恢复本身是可回退的动作，这才是"敢恢复"的前提。

        ⚠️ 只限 admin：这条动作的覆盖面是**整个目录**（所有人的库 +
        身份表），不是"我这份记忆"。member 拿不到它，也不该拿到。
        """
        user = self.hub.store.user(user_name)
        if user is None or not user.is_admin:
            return {
                "ok": False,
                "code": ErrorCode.SCOPE_DENIED,
                "message": "整目录恢复只允许 admin 身份调用",
                "hint": "member 请用 memory_backup(action=\"export\") 导自己的条目",
            }
        try:
            r = self.hub.restore_home(bundle, confirm=confirm)
            r["_actor"] = user_name
            return r
        except Exception as exc:  # noqa: BLE001
            return self._err(exc, "恢复")

    async def memory_versions(
        self, user_name: str, ident: str, *, lib: str = "", sha: str = ""
    ) -> dict[str, Any]:
        """一条记忆的历史：git 版本序列（仅 versioned）+ 改前快照。

        ━━━ 什么时候用它 ━━━
        "这条被我改坏了，上一版是什么" —— 就调它，不用先判断有没有 versioned。

        · ``versioned=true`` → ``versions`` 是共享版本库里的提交序列，
          并附最近一版（或 ``sha`` 指定那版）的 diff
        · ``versioned=false`` → ``versions`` 为空并说明"未启用版本控制"，
          但 ``snapshots``/``previous_body`` 仍给出库里的改前快照 ——
          防手滑那层对每条记忆都开着，覆盖是不可逆的，所以这里必须给得出内容
        """
        try:
            return self.hub.versions(user_name, ident, lib_id=lib, sha=sha)
        except Exception as exc:  # noqa: BLE001
            return self._err(exc, "版本历史")

    async def memory_stats(
        self, user_name: str, lib: str = "", commit_now: bool = False,
        with_commits: bool = False,
    ) -> dict[str, Any]:
        """看库况 —— 排查「写了没生效」「是不是卡住了」时先看这个。

        · 不传 ``lib`` → 列出**你可读的全部库**及其条数与队列积压
        · 传 ``lib``   → 那个库的详细状况
        · ``commit_now=True`` → 顺便立刻提交（等价于调 memory_commit）
        · ``with_commits=True`` → 附带 git 提交历史（**默认不带**：
          每次要 fork git 子进程，约 60ms；核心字段不依赖它）

        ⚠️ 只统计你有权限的库。全局统计（含无权库的条数）本身就是泄露。

        ━━━ 返回里的两个关键字段 ━━━
        · ``write_queue.pending`` —— 还有多少写没落盘。非 0 且长时间不变 = 卡了
        · ``git.dirty``           —— 有没有改了没提交的。写完想确认是否已入库看这个
        """
        if commit_now:
            return await self.memory_commit(user_name, lib)
        try:
            return await self._io(
                lambda: self.hub.stats(user_name, lib_id=lib, with_commits=with_commits)
            )
        except Exception as exc:  # noqa: BLE001
            return self._err(exc, "统计")

    # ══════════════════════ 导入 / 导出（方案 14） ══════════════════════

    async def memory_export(
        self, user_name: str, workspace: str = "", lib: str = "", out_dir: str = "",
    ) -> dict[str, Any]:
        """以**工作空间**为单位导出记忆 bundle（字节级保留，可无损再导入）。

        ━━━ 什么时候用它 ━━━

          · 项目交接 / 换机器 / 备份：把一个项目的记忆整包带走
          · 跨环境同步：导出 → 传到另一台 → import

        · **workspace** 导出粒度。传项目名只导该项目；不传 = 全部
        · **lib**      只导一个库（需读权限）；不传 = 你有权读的每个库
                      各导一份、合并进一个 bundle（多库 bundle：
                      ``libs/<库>/notes/*.md`` + 顶层 manifest）
        · **out_dir**  产物目录，默认 ``<home>/exports/``

        bundle 结构（``shared-memory-bundle/1``）：

            manifest.json            # schema 版本 + 每条 sha256 + 来源库
            libs/<库id>/notes/*.md   # 字节级原样拷贝（不重新序列化）

        ⚠️ 导出**不进 vault 主目录**（那里只放记忆本身），也不会被
        git 当成记忆提交。
        """
        try:
            return await self._io(
                lambda: self.hub.export(
                    user_name, workspace=workspace, lib_id=lib, out_dir=out_dir,
                )
            )
        except Exception as exc:  # noqa: BLE001
            return self._err(exc, "导出")

    async def memory_import(
        self, user_name: str, bundle: str, target_lib: str = "",
        workspace: str = "", on_conflict: str = "skip",
    ) -> dict[str, Any]:
        """导入 memory_export 产出的 bundle（写路径，走队列串行）。

        · **bundle**      bundle 目录路径（含 manifest.json 那个）
        · **target_lib**  导入到哪个库，需**写权限**。不传 = 你的个人库
                          （与 memory_write 的默认最小污染面一致）
        · **workspace**   **过滤器**不是改写器：只导入该工作区的条目；
                          不传则用 bundle manifest 里记的值
        · **on_conflict** 目标库已有同名/同 id 条目时：
                          · ``skip``（默认）不覆盖，保守
                          · ``update`` 覆盖 —— bundle 里的版本为准
                          · ``rename`` 都保留，冲突的自动改名

        ⚠️ 导入**不重写内容** —— 字节级还原是这条链路的意义
        （manifest 里有 sha256，导入前后可校验）。

        导入前会做预检（schema 版本、路径白名单防穿越、逐条清单），
        预检失败不会写坏目标库。
        """
        if on_conflict not in ("skip", "update", "rename"):
            return ToolError(
                ErrorCode.INVALID_ARGUMENT,
                f"on_conflict 只支持 skip/update/rename，收到：{on_conflict!r}",
            ).to_dict()
        try:
            return await self.hub.import_bundle(
                user_name, bundle,
                target_lib=target_lib, workspace=workspace, on_conflict=on_conflict,
            )
        except Exception as exc:  # noqa: BLE001
            return self._err(exc, "导入")
