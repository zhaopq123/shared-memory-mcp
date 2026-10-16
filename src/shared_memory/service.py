"""Service 层 —— 业务逻辑，不碰协议、不碰并发原语。

━━━ 为什么要有这一层 ━━━

把「写要排队、读不排队」这条规则**收敛在一个地方**，MCP 层（stdio 与
http 两个入口）只做参数透传。这样两个传输方式的行为**不可能不一致** ——
如果各自实现一遍，早晚会漂移（一边排队一边不排，就是数据覆盖）。

同理，git 提交策略也只在这里实现一次。

━━━ 为什么提交要防抖 ━━━

每次写都 commit 会产生大量噪音提交（一个 agent 连续记 5 条 = 5 个提交），
日志很快变得不可读。这里用「静默 N 秒后提交」—— 与 dsh-tiddlywiki 的
自动提交同一思路。

提交同样走队列：`git add` 会改 `.git/index`，与文件写入并发会互相干扰
（`.git/index.lock` 冲突）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from .config import Config, apply_rules_on_write
from .errors import ErrorCode, ToolError
from .knowledge import (
    build_backlinks,
    build_view,
    extract_links,
    list_views,
    parse_date,
)
from .queue import WriteQueue
from .vault import SCOPE_LONG_TERM, Note, Vault, now_iso

logger = logging.getLogger(__name__)

#: 写入静默多久后自动提交（秒）。设 0 表示每次写都提交。
DEFAULT_COMMIT_DEBOUNCE = float(os.environ.get("SHARED_MEMORY_COMMIT_DEBOUNCE", "8"))


class SharedMemoryService:
    """共享记忆服务。

    一个实例 = 一份 vault + 一个写队列。**所有传输入口必须共用同一个实例**，
    否则队列就不共享了，"多 agent 不覆盖"的保证随之失效。
    """

    def __init__(
        self,
        vault: Vault,
        *,
        queue: WriteQueue | None = None,
        commit_debounce: float = DEFAULT_COMMIT_DEBOUNCE,
        config: Config | None = None,
    ) -> None:
        """
        Args:
            vault: 存储层。
            queue: 写队列。默认新建一个（测试时可注入以便观测）。
            commit_debounce: 自动提交的静默秒数；0 = 每次写都提交。
            config: 配置视图。默认基于 vault 根目录构造。
        """
        self.vault = vault
        self.queue = queue or WriteQueue()
        self.commit_debounce = commit_debounce
        self.config = config or Config(vault.root)
        #: 是否有未提交的改动。防抖提交靠它决定要不要真的跑 git。
        self._dirty = False
        #: 待执行的防抖任务。新写入会取消它并重新计时。
        self._committer: asyncio.Task[None] | None = None
        self._started_at = now_iso()
        #: git 提交的作者名。由 ``set_author`` 设置（每次请求）。
        #:
        #: 为什么是显式字段而不是 contextvar：service 不只被 HTTP 调用
        #: （面板、脚本、测试也直接用），那些路径没有中间件注入身份。
        #: 用一个显式字段，所有路径行为一致。
        self._author: str = ""

    def set_author(self, name: str) -> None:
        """设置 git 提交的作者。

        多租户下**每次请求都要设置** —— 否则后面所有人的提交都会用
        上一个请求留下的作者名（那比没有作者更糟：是错的信息）。
        """
        self._author = (name or "").strip()

    def welcome(self) -> str:
        """服务端对所有接入 agent 说的话（instructions）。

        内容来自 vault 里的 ``_config/instructions.md``，面板可改。
        文件缺失时用内置默认值（带"不幻觉 + 记忆纪律"那套）。
        """
        return self.config.instructions()

    # ══════════════════════ 生命周期 ══════════════════════

    def start(self) -> None:
        """启动服务。

        ⚠️ 本方法可能被**没有事件循环**的同步上下文调用
        （HTTP 入口在 `uvicorn.run` 之前就会调它）。所以它只做
        「不依赖事件循环」的事：标记启动、初始化 git 仓库。
        写者循环由队列自己在第一次 `submit()` 时惰性创建。
        """
        self.queue.start()
        if self.vault.auto_commit:
            self.vault.init_repo()

    async def stop(self) -> None:
        """优雅停机。

        顺序要紧：
          1. **先提交未落盘的改动** —— 否则最后几条记忆只存在于工作树里，
             虽然文件在磁盘上，但没有 git 记录，容易被后续误操作带走
          2. 取消待执行的防抖任务（避免停机后又冒出一个提交）
          3. 停止写队列（等在途任务收尾）
        """
        try:
            await self._flush_commit(reason="服务停止")
        except Exception as exc:  # noqa: BLE001
            # 停机时的提交失败**不该阻止停机** —— 文件已经写入磁盘了，
            # 只是少了 git 记录。宁可留下工作树，也不能卡住进程退出。
            logger.warning("停止时提交失败: %s", exc)
        if self._committer is not None:
            self._committer.cancel()
            self._committer = None
        await self.queue.stop()

    # ══════════════════════ 防抖提交 ══════════════════════

    def _touch_commit(self) -> None:
        """标记有改动，安排一次防抖提交。

        重复调用会**重置计时**（取消上一个任务）—— 这正是"静默 N 秒"
        的语义：只要还在写，就不提交，等安静下来再一起提交。
        """
        self._dirty = True
        if self.commit_debounce <= 0:
            return  # 关闭防抖：由调用方直接 flush
        if self._committer is not None and not self._committer.done():
            self._committer.cancel()
        self._committer = asyncio.create_task(self._debounced_commit())

    async def _debounced_commit(self) -> None:
        """等静默期结束后提交。被取消 = 又有新写入，重新计时（正常路径）。"""
        try:
            await asyncio.sleep(self.commit_debounce)
        except asyncio.CancelledError:
            return  # 有新写入，这次不提交 —— 不是错误
        if not self._dirty:
            return
        try:
            await self._flush_commit(reason="自动提交")
        except Exception as exc:  # noqa: BLE001
            # 提交失败不该让写入方看到错误 —— 内容已经在磁盘上了。
            # 记日志即可，等下一次写入会再尝试。
            logger.warning("防抖提交失败: %s", exc)

    async def _flush_commit(self, *, reason: str = "提交") -> dict[str, Any]:
        """立即提交（排队执行，避免与文件写入抢 git index）。

        注意：提交**走同一个队列**。这是刻意的 —— 队列保证串行，
        也就保证了 `git add` 不会与正在进行的写入交错。

        ━━━ 提交信息里带作者 ━━━

        多租户下"谁写的"必须能在 git 历史里查出来。以前固定用
        ``shared-memory`` 这个通用身份，结果十几个人写的提交长得一样，
        追溯时只能靠 frontmatter 里的 author 字段 —— 但那是文件内容，
        删了就没了。

        git 的 author 是**元数据**，跟着提交走，删不掉也改不了
        （改的话 git 会记录那次修改）。所以这里显式传入作者。

        ━━━ 作者从哪来 ━━━

        ``self._author``（由 ``set_author`` 设置），**不是** contextvar。

        为什么不用 contextvar：那是 HTTP 层的传递机制。但 service 也会被
        面板、脚本、测试直接调用 —— 那些路径没有中间件，身份就是空的。
        用一个显式字段更直接，也不会出现"同样的操作，走 HTTP 就有作者、
        走别的就没有"的不一致。
        """
        if not self._dirty:
            return {"committed": False, "message": "无改动"}

        author = self._author

        def _do() -> dict[str, Any]:
            # 先清标记再提交：若提交失败，下一次写入会重新置位。
            # 反过来（先提交后清标记）会在异常时留下永久脏标记。
            self._dirty = False
            return self.vault.commit(f"memory: {reason}", author=author)

        result = await self.queue.submit(_do)
        if result.get("committed"):
            logger.info("已提交: %s", result.get("commit"))
        return result

    # ══════════════════════ 写（走队列）══════════════════════

    async def write(
        self,
        *,
        title: str,
        body: str,
        tags: list[str] | None = None,
        author: str = "",
        workspace: str = "",
        mode: str = "upsert",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """写入一条记忆（排队 → 落盘 → 按需提交）。

        ━━━ 这里体现「闭环」的两个要点 ━━━

        1. **写入前按规则处理**（apply_rules_on_write）：
           自动推断 type/claim、自动补工作区标签、校验长度。
           不合规**不硬拒**，而是补全 + 在返回里提醒 ——
           硬拒会让 agent 反复换写法重试，浪费轮次。

        2. **返回里带上 `notes`**：告诉 agent"我给你补了什么"。
           不打断它，但让它知道数据被规范化了。

        Args:
            workspace: 工作区/项目名。会写进 frontmatter 的 ``workspace``
                字段并自动追加 ``ws/<name>`` 标签（与 dsh-tiddlywiki 同口径）。
                留空 = 跨项目/基础设施类记忆。

        Returns:
            结果字典，额外带：
              · ``committed`` —— 本次是否触发了提交
              · ``notes``     —— 规则补全/提醒列表（可能为空）
        """
        if mode not in ("upsert", "create", "append"):
            raise ToolError(
                ErrorCode.INVALID_ARGUMENT,
                f"mode 只能是 upsert / create / append，收到 {mode!r}",
            )
        if not (title or "").strip():
            raise ToolError(ErrorCode.INVALID_ARGUMENT, "title 不能为空")

        # ── 时效字段校验（方案 4）──
        # 在进队列**之前**校验：坏日期不该占用写队列，更不该落盘后
        # 才在 expiring 视图里表现为"永不过期"（parse_date 宽容 =
        # 解析不了就当没有 —— 那会让 agent 以为设了有效期实际没有）。
        for key in ("valid-until", "review-after"):
            v = (extra or {}).get(key)
            if v not in (None, "") and parse_date(str(v)) is None:
                raise ToolError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"{key} 不是合法日期：{v!r}",
                    hint="用 YYYY-MM-DD 或完整 ISO 格式（如 2026-12-31）",
                )
        # claim 三值校验（方案 9 的写端）：推断是兜底，显式标注必须
        # 可信 —— 拼错一个字母就会变成一个永远过滤不掉的幽灵值。
        _cl = (extra or {}).get("claim")
        if _cl not in (None, "") and str(_cl).strip() not in (
            "fact", "inference", "proposal",
        ):
            raise ToolError(
                ErrorCode.INVALID_ARGUMENT,
                f"claim 只能是 fact / inference / proposal，收到 {_cl!r}",
                hint="拿不准就留空让服务端推断；proposal 适合'我建议…'",
            )
        # superseded-by：**空串是合法值**（upsert 覆盖语义 = 清除标记），
        # 但纯空白串一定是手误 —— 拒。
        sb = (extra or {}).get("superseded-by")
        if sb is not None and str(sb) != "" and not str(sb).strip():
            raise ToolError(
                ErrorCode.INVALID_ARGUMENT,
                "superseded-by 不能是纯空白",
                hint="要标记被取代就填新记忆的标题；要清除标记传空字符串",
            )

        # ── 按规则处理（在读配置时就完成，不进队列：纯函数，无副作用）──
        rules = self.config.rules()
        tags_in = [t.strip() for t in (tags or []) if str(t).strip()]
        if workspace:
            wtag = f"ws/{workspace}"
            if wtag not in tags_in:
                tags_in.append(wtag)
        extra_in = dict(extra or {})
        tags_fixed, extra_fixed, rule_notes = apply_rules_on_write(
            title=title,
            body=body,
            tags=tags_in,
            workspace=workspace,
            extra=extra_in,
            rules=rules,
        )
        # 标题可能被规则截断 —— 用处理后的值
        title_fixed = (
            title[: rules["max_title_chars"]].rstrip()
            if rules.get("max_title_chars") and len(title) > rules["max_title_chars"]
            else title
        )

        def _do() -> dict[str, Any]:
            return self.vault.put(
                title=title_fixed,
                body=body,
                tags=tags_fixed,
                author=author,
                workspace=workspace,
                mode=mode,
                extra=extra_fixed,
            )

        result = await self.queue.submit(_do)
        self._touch_commit()
        # 关闭防抖时，_touch_commit 不会安排任务，所以要立刻提交
        if self.commit_debounce <= 0:
            commit = await self._flush_commit(reason=f"写入：{title_fixed}")
            result["committed"] = bool(commit.get("committed"))
            result["commit_pending"] = False
        else:
            # ⚠️ 防抖模式下 committed=False 是**正常状态，不是失败**。
            # 说清楚的必要性：agent 若以为失败就重试，会写出重复内容，
            # 而每次重试都重置防抖计时 → 提交永远不发生。
            # 所以额外给一个 commit_pending 标志把语义讲明。
            result["committed"] = False
            result["commit_pending"] = True
        if rule_notes:
            result["notes"] = rule_notes

        # ── 写时关联提示（方案 10）──
        # "先拉再推"不强制（决策 1），但服务端把"拉"的时机还给你：
        # 写入响应里带上最可能相关的既有记忆，agent 自己决定
        # 忽略 / 改 create / 补 superseded_by / 合并。
        # 成本：一次 all_notes()（有缓存，近似零成本）+ O(n) 标题相似度。
        related = self._related_notes(
            title_fixed, tags_fixed, workspace,
            exclude_id=str(result.get("id", "")),
        )
        if related:
            result["related"] = related
        return result

    def _related_notes(
        self,
        title: str,
        tags: list[str] | None,
        workspace: str,
        *,
        exclude_id: str = "",
        limit: int = 3,
    ) -> list[dict[str, str]]:
        """找与刚写的记忆最相关的既有记忆（方案 10）。

        打分：标题相似度（主）+ 共享标签 + 同工作区加分。
        阈值：标题相似度 >= 0.3 或共享标签 >= 2 才算相关 ——
        宁缺毋滥，返回一堆弱相关等于没返回。
        """
        from difflib import SequenceMatcher

        tags_l = {t.lower() for t in (tags or [])}
        title_l = (title or "").lower()
        scored: list[tuple[float, Note]] = []
        for n in self.vault.all_notes():
            if exclude_id and n.id == exclude_id:
                continue
            other_title = (n.title or "").lower()
            sim = SequenceMatcher(None, title_l, other_title).ratio()
            shared = len(tags_l & {t.lower() for t in (n.tags or [])})
            ws_bonus = 5.0 if (workspace and n.workspace == workspace) else 0.0
            if sim >= 0.3 or shared >= 2:
                scored.append((sim * 100 + shared * 10 + ws_bonus, n))
        scored.sort(key=lambda x: -x[0])
        return [{"id": n.id, "title": n.title} for _, n in scored[:limit]]

    async def delete(self, ident: str) -> dict[str, Any]:
        """软删除一条记忆（排队）。"""

        def _do() -> dict[str, Any]:
            return self.vault.delete(ident)

        result = await self.queue.submit(_do)
        self._touch_commit()
        if self.commit_debounce <= 0:
            await self._flush_commit(reason=f"删除：{ident}")
        return result

    async def restore(self, filename: str) -> dict[str, Any]:
        """从回收站恢复（排队）。"""

        def _do() -> dict[str, Any]:
            return self.vault.restore(filename)

        result = await self.queue.submit(_do)
        self._touch_commit()
        if self.commit_debounce <= 0:
            await self._flush_commit(reason=f"恢复：{filename}")
        return result

    async def commit_now(self, message: str = "手动提交") -> dict[str, Any]:
        """立刻提交（不等防抖）。用于 `memory_stats commit_now=True`。"""
        result = await self._flush_commit(reason=message)
        return {"ok": True, **result}

    # ══════════════════════ 读（不排队）══════════════════════
    # 读没有竞争，走并发路径。让读排队会把「一个 agent 写」
    # 放大成「所有 agent 都卡住」。

    def search(
        self,
        query: str = "",
        *,
        tags: list[str] | None = None,
        workspace: str = "",
        limit: int = 20,
        offset: int = 0,
        since: str = "",
        until: str = "",
        sort: str = "relevance",
        claim: str = "",
        status: str = "",
        scope: str = SCOPE_LONG_TERM,
    ) -> dict[str, Any]:
        """检索记忆。逻辑全在 vault.search（见那里的详细说明）。

        相比 vault.search 只多一件事：调用方没显式传 ``status`` 时，
        读配置里的 ``search_default_status``（默认 ``active``，见决策 7）——
        老部署想恢复"含过期"的行为，改配置即可，不用改客户端。
        """
        status_eff = (status or "").strip() or str(
            self.config.settings().get("search_default_status", "active") or "active"
        )
        out = self.vault.search(
            query,
            tags=tags,
            workspace=workspace,
            limit=limit,
            offset=offset,
            since=since,
            until=until,
            sort=sort,
            claim=claim,
            status=status_eff,
            scope=scope,
        )
        out["hint"] = "用 memory_get 按 title 或 id 读全文"
        return out

    def get(self, ident: str) -> dict[str, Any]:
        """读一条记忆全文。"""
        note: Note = self.vault.get(ident)
        return {"ok": True, "note": note.to_dict()}

    def list_notes(
        self, *, limit: int = 50, tag: str = "", workspace: str = ""
    ) -> dict[str, Any]:
        """列举记忆（最近更新的在前），可按标签或工作区过滤。

        与 search 的区别：**不过滤关键词**，用于"看看库里都有什么"。
        """
        notes = self.vault.all_notes()
        if tag:
            want = tag.strip().lower()
            notes = [n for n in notes if want in {t.lower() for t in n.tags}]
        if workspace:
            ws = workspace.strip()
            notes = (
                [n for n in notes if not n.workspace]
                if ws == "__none__"
                else [n for n in notes if n.workspace == ws]
            )
        notes.sort(key=lambda n: (n.updated or n.created), reverse=True)
        total = len(notes)
        lim = max(1, min(limit, 200))
        return {
            "ok": True,
            "total": total,
            "showing": min(total, lim),
            "notes": [n.to_dict(with_body=False) for n in notes[:lim]],
        }

    def stats(self) -> dict[str, Any]:
        """库况 + 写队列状况 + 运行时长。

        顺带做两件治理小事（方案 5b/5c）：
          · 回收站懒清理（``trash_gc``）—— 超期/超量的 trash 物理清除，
            git 全历史是兜底；阈值走配置，0 = 关闭；
          · 配额告警（``quota_warning``）—— 单库条数到阈值只提醒，
            **不硬拒**（共享库拒写会挡住别人）。
        """
        settings = self.config.settings()

        # 先清理再统计 —— 让 trash 计数反映清理后的真实值
        ttl_days = int(settings.get("trash_ttl_days", 30) or 0)
        max_files = int(settings.get("trash_max_files", 200) or 0)
        trash_gc = (
            self.vault.gc_trash(max_age_days=ttl_days, max_files=max_files)
            if (ttl_days or max_files) else None
        )

        s = self.vault.stats()
        if trash_gc is not None:
            s["trash_gc"] = trash_gc
        warn_at = int(settings.get("quota_warn_notes", 500) or 0)
        if warn_at and s.get("notes", 0) >= warn_at:
            s["quota_warning"] = (
                f"单库已达 {s['notes']} 条（告警阈值 {warn_at}）—— "
                "建议归档低引用旧记忆（memory_view kind=projects 先看分布）"
            )
        return {
            "ok": True,
            **s,
            "write_queue": self.queue.stats(),
            "started_at": self._started_at,
            "uptime_seconds": self._uptime(),
        }

    def health(self) -> dict[str, Any]:
        """健康检查 —— 给监控/守护脚本用。

        与 stats 的区别：**只返回判定所需的最小信息**，且有明确的 ok 布尔。
        判定标准（任一不满足则 ok=False）：
          · 写队列的写者循环*该在的时候*活着
          · notes 目录可写（用一次真实写入验证，不是猜）
          · 队列积压未达到容量上限

        ━━━ 为什么「写者没起来」不等于故障（重要）━━━

        写者循环是**惰性创建**的 —— 服务启动时不会创建它，第一次
        ``submit()`` 才创建（因为 ``start()`` 可能在无事件循环的同步
        上下文里被调用，那时 create_task 会抛错）。

        所以「一个刚启动、还没收到任何写入的服务」里 worker 必然是 None。
        那**不是故障**，是正常状态。

        判定条件因此写成：**有过写入记录（processed>0 或 pending>0）
        但 worker 却没了** → 才是真的故障（写者崩了，之后的写入会永远挂着）。

        实测踩过：不加这个区分时，健康检查会对一个完全正常的服务报
        "写者循环未运行" —— 误报会让监控失去信任。
        """
        q = self.queue.stats()
        problems: list[str] = []

        used = q["processed"] > 0 or q["pending"] > 0 or q["outstanding"] > 0
        if used and not q["worker_alive"]:
            # 处理过任务却没了 worker = 写者崩了，后续写入会永远挂在队列里
            problems.append("写者循环已停止（服务需要重启）")
        if q["pending"] >= q["max_depth"]:
            problems.append(f"写入队列已满（{q['pending']}/{q['max_depth']}）")

        # 探测可写性：真的写一个文件再删掉。
        #
        # ⚠️ 探测位置必须是 **notes 目录**（写入实际落点），不是 vault 根目录。
        # 根目录可写但 notes 不可写是完全可能的（notes 被换成了文件、
        # 被单独挂载成只读、权限位不同），那种情况下探测根目录会给出
        # 假的"健康"，而所有写入其实都会失败。实测踩过。
        #
        # 为什么不用 os.access：在容器/只读挂载/权限位异常的场合，
        # access() 的结论经常与真实写入结果不一致 —— 必须实测。
        probe = self.vault.notes_dir / ".health-probe"
        try:
            self.vault.notes_dir.mkdir(parents=True, exist_ok=True)
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            problems.append(f"vault 不可写：{exc}")

        s = self.vault.stats()
        return {
            "ok": not problems,
            "problems": problems,
            "notes": s["notes"],
            "unreadable": s["unreadable"],
            "queue": q,
            "uptime_seconds": self._uptime(),
        }

    def git_log(self, limit: int = 10) -> dict[str, Any]:
        """最近 git 提交。"""
        return {"ok": True, "commits": self.vault.log(max(1, min(limit, 50)))}

    # ══════════════════════ 体系层：视图 / 引用 / 配置 ══════════════════════

    def view(self, kind: str, **params: Any) -> dict[str, Any]:
        """算一个视图（决策记录/踩坑/待办/时效告警/...）。

        **实时计算，不查索引** —— 索引会过时，过时的索引比没有索引更糟。
        """
        return build_view(kind, self.vault.all_notes(), params=params)

    def views(self) -> dict[str, Any]:
        """列出全部可用视图（目录）。"""
        return list_views()

    def backlinks(self, title: str) -> dict[str, Any]:
        """谁引用了这条记忆（反向链接）。

        ━━━ 为什么这个能力重要 ━━━
        它是把碎片连成网的关键：从任意一点能摸到相关上下文，
        而不是"搜到一条就只看到这一条"。
        """
        notes = self.vault.all_notes()
        back = build_backlinks(notes)
        outgoing = extract_links(
            (self.vault.get(title).body or "") if self._exists(title) else "",
            {n.title for n in notes},
        )
        return {
            "ok": True,
            "title": title,
            "backlinks": back.get(title, []),
            "outgoing": outgoing,
        }

    def _exists(self, title: str) -> bool:
        try:
            self.vault.resolve(title)
            return True
        except ToolError:
            return False

    def config_snapshot(self) -> dict[str, Any]:
        """当前配置（面板用）。"""
        return self.config.snapshot()

    async def update_config(
        self,
        *,
        instructions: str | None = None,
        rules: dict[str, Any] | None = None,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """改配置（面板用）—— **走同一个写队列**。

        ━━━ 为什么配置写也必须排队（漏洞 6）━━━

        配置也是文件。面板和 agent 同时改会互相覆盖 ——
        跟记忆写入是同一类问题，没理由用两套办法。
        所以这里复用 service 的写队列，而不是直接写文件。

        ━━━ 保存前校验（漏洞 9）━━━

        配置改错会让**行为异常**，而症状往往离配置很远
        （"为什么视图是空的" ← 其实是 infer_type 写成了字符串）。
        所以先校验，不合法就拒绝并说清是哪一项 ——
        把问题挡在写盘之前，比事后排查便宜得多。
        """
        writes: list[tuple[str, str, str]] = []  # (文件名, 内容, commit 说明)

        if rules is not None:
            problems = self.config.validate_rules(rules)
            if problems:
                raise ToolError(
                    ErrorCode.INVALID_ARGUMENT,
                    "规则配置不合法：" + "；".join(problems),
                    hint="修好这些项再保存",
                )
            writes.append(
                ("rules.json", json.dumps(rules, ensure_ascii=False, indent=2), "规则")
            )

        if settings is not None:
            problems = self.config.validate_settings(settings)
            if problems:
                raise ToolError(
                    ErrorCode.INVALID_ARGUMENT,
                    "运行参数不合法：" + "；".join(problems),
                    hint="修好这些项再保存",
                )
            writes.append(
                (
                    "settings.json",
                    json.dumps(settings, ensure_ascii=False, indent=2),
                    "参数",
                )
            )

        if instructions is not None:
            writes.append(("instructions.md", instructions, "指令"))

        if not writes:
            return {"ok": True, "changed": []}

        def _do() -> list[str]:
            self.config.dir.mkdir(parents=True, exist_ok=True)
            changed = []
            for name, content, _label in writes:
                target = self.config.dir / name
                target.write_text(content, encoding="utf-8")
                changed.append(name)
            return changed

        changed = await self.queue.submit(_do)
        self._touch_commit()
        if self.commit_debounce <= 0:
            await self._flush_commit(reason="配置变更")
        return {
            "ok": True,
            "changed": changed,
            "note": "配置已保存并立即生效（指令变更需客户端重连才会重新拉取）",
        }

    def trash(self) -> dict[str, Any]:
        """回收站内容。"""
        items = self.vault.trash()
        return {"ok": True, "count": len(items), "items": items}

    def export_markdown(self, *, include_body: bool = True) -> dict[str, Any]:
        """把整库导出为一个 Markdown 文档。

        用途：在**没有 MCP 的场合**使用这批记忆 —— 贴进对话、写进文档、
        离线归档。这也是"纯文本存储"白拿到的一个能力。
        """
        text = self.vault.export_markdown(include_body=include_body)
        return {
            "ok": True,
            "chars": len(text),
            "markdown": text,
        }

    # ══════════════════════ 导入 / 导出（方案 14）═════════════════════

    def export_files(self, *, workspace: str = "", out_dir: str | Path, lib_id: str = "default") -> dict[str, Any]:
        """导出本库（某工作区的）记忆文件为 bundle 部件。读路径，不进队列。"""
        return self.vault.export_files(workspace=workspace, out_dir=out_dir, lib_id=lib_id)

    async def import_bundle(
        self,
        bundle: str | Path,
        *,
        on_conflict: str = "skip",
        workspace: str = "",
    ) -> dict[str, Any]:
        """把 bundle 导入本库（**走写队列** —— 导入不过是批量 memory_write）。

        先在队列外预检（plan_import）：坏 bundle 不占用队列、不写半截。
        """
        if on_conflict not in ("skip", "update", "rename"):
            raise ToolError(
                ErrorCode.INVALID_ARGUMENT,
                f"on_conflict 只能是 skip / update / rename，收到 {on_conflict!r}",
                hint="skip=跳过已存在（默认，不覆盖）；update=整篇覆盖；rename=另存",
            )
        plan = self.vault.plan_import(bundle, workspace=workspace)
        if not plan["entries"]:
            return {
                "ok": False,
                "imported": 0, "skipped": 0, "updated": 0, "renamed": 0,
                "errors": plan["errors"] or ["bundle 里没有可导入的条目"],
            }

        def _do() -> dict[str, Any]:
            return self.vault.import_bundle(
                bundle, on_conflict=on_conflict, workspace=workspace
            )

        result = await self.queue.submit(_do)
        self._touch_commit()
        if self.commit_debounce <= 0:
            await self._flush_commit(reason="导入记忆")
        result["workspace"] = plan["workspace"]
        return result

    def _uptime(self) -> int:
        """运行秒数（解析失败返回 0，不让监控因此报错）。"""
        from .vault import parse_iso
        from datetime import datetime

        started = parse_iso(self._started_at)
        if started is None:
            return 0
        return int((datetime.now(started.tzinfo) - started).total_seconds())

    def build_welcome(self) -> str:
        """服务端写给 agent 的「先查再答」指令。

        ━━━ 这是召回层的唯一抓手 ━━━

        MCP **只提供工具，不会自动被调用**。所以「agent 该在什么时候去查记忆」
        这件事，只能靠服务端把指令写进它能读到的地方：

          · FastMCP 的 ``instructions``（本方法）—— 支持该字段的客户端会展示
          · 每个工具的 docstring —— 所有客户端都能看到

        不做这一步的话，用户会遇到"我明明存了，它却说不知道" ——
        而存储层其实一直是好的，问题出在没有任何机制让 agent 想到去查。
        """
        return (
            "这是一个**跨 agent 共享的长期记忆库**（所有接入的 agent 共用一份，无隔离）。\n\n"
            "使用约定：\n"
            "1. **动手前先检索** —— 调 memory_search 查有没有相关记忆，别重复造轮子；\n"
            "2. **有结论就写回** —— 调 memory_write 记下结论/踩坑/口径，供其他 agent 复用；\n"
            "3. 记忆是纯 Markdown 文件 + git 版本化，可放心写入，误删有回收站与历史。\n\n"
            "没有「我的记忆」和「别人的记忆」之分 —— 你写的就是所有人能读到的。"
        )


def default_vault_root() -> Path:
    """vault 默认位置。可用 ``SHARED_MEMORY_ROOT`` 覆盖。"""
    env = os.environ.get("SHARED_MEMORY_ROOT")
    if env:
        return Path(env)
    return Path.home() / "shared-memory"
