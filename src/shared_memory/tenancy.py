"""多租户服务层 —— 把「身份 → 权限 → 库」这条链串起来。

━━━ 位置与职责 ━━━

    auth.py      凭据 → **你是谁**
    tenant.py    用户/库模型 + 权限纯函数（能读哪个库）
    library.py   多库集合 + 权限入口（唯一的取库路径）
    service.py   单库的业务逻辑（写/读/搜索/视图）
    tenancy.py   **本文件** —— 把上面几层组合成"一个请求的完整处理"

━━━ 本文件存在的理由 ━━━

单库的 ``SharedMemoryService`` 不需要知道"用户"这个概念（它只操作
一个 vault）。而多租户需要在它外面包一层：

  1. 从身份算出可读/可写库集合
  2. 逐库调用单库服务（**没权限的库根本不打开**）
  3. 合并结果并标出来源
  4. 把身份信息透传给日志/审计

这样"权限"只有一处实现（``library.Tenant``），单库逻辑保持纯净
—— 它既能被多租户用，也能被独立测试。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from .auth import Auth
from .errors import ErrorCode, ToolError
from .library import LibrarySet, Tenant
from .service import SharedMemoryService
from .tenant import TenantStore, User
from .workspace import WorkspaceManager

logger = logging.getLogger(__name__)


def _view_meta(kind: str) -> tuple[str, str]:
    """视图的 (label, desc)。

    ━━━ 为什么复用 knowledge.VIEWS 而不是在这里重新定义 ━━━

    VIEWS 已经是单一数据源（knowledge.py 里的 dict）。在这里另写一份
    就会出现"两处不一致"的风险 —— 而实测已经踩过类似的坑：
    视图标题空白，就是因为跨库分支漏了这两个字段。

    从源头取，就杜绝了"改了源头忘了改这里"。
    """
    from .knowledge import VIEWS
    v = VIEWS.get(kind) or {}
    return v.get("label", kind), v.get("desc", "")


class TenantHub:
    """多租户中枢 —— 所有业务操作的**唯一入口**。

    ━━━ 为什么强调"唯一" ━━━

    权限漏洞最常见的形式不是"判断逻辑写错了"，而是**某个操作忘了判断**。

    所以设计上让"拿到 Vault"只有一条路：``self.tenant.vault_for(user, lib, need_write)``。
    除此之外任何地方都拿不到 Vault —— 忘了判断在结构上就做不到。
    """

    def __init__(self, home: Path | str) -> None:
        self.home = Path(home).expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True)

        # 用户与库的登记（JSON，可手改）
        self.store = TenantStore(self.home)
        # 库集合（每个库一个目录 + git）
        self.libs = LibrarySet(self.home, self.store)
        # 权限入口
        self.tenant = Tenant(self.home, self.store, self.libs)
        # 工作空间（= 库）的创建与共享范围管理。
        # 与 tenant 分开：tenant 管"能不能访问"，workspace 管"共享给谁" ——
        # 两件事的权限规则不同（前者按归属算，后者只允许所有者/管理员改）。
        self.workspace = WorkspaceManager(self.store, self.tenant)
        # 单库服务缓存（每个库一个 service，复用其写队列）
        self._services: dict[str, SharedMemoryService] = {}
        #: 测试用开关：True 时所有库都关掉防抖（每次写立刻提交）。
        #: 生产环境保持 False —— 防抖是为了避免"每写一条就 fork 一次 git"。
        self.fast_commit = False

    # ────────────────────── 生命周期 ──────────────────────

    def bootstrap(self) -> dict[str, Any] | None:
        """首次运行：建管理员 + 默认库。

        Returns:
            ``{"user": …, "password": …}``（**仅此一次**返回明文密码），
            或 ``None``（已初始化过）。
        """
        result = self.store.ensure_bootstrap()
        if result is None:
            return None
        user, password = result
        return {
            "user": user.name,
            "password": password,
            "note": "这是首次启动生成的初始密码，**只显示这一次**。"
                    "请立刻记下并在面板里修改。",
        }

    def _new_service(self, vault: Any) -> SharedMemoryService:
        """按当前配置（含 fast_commit）建一个新 service 并启动。"""
        svc = SharedMemoryService(
            vault, commit_debounce=0 if self.fast_commit else None
        )
        svc.start()
        return svc

    def service_for(self, lib_id: str, *, commit_debounce: float | None = None) -> SharedMemoryService:
        """取某库的单库服务（惰性创建，复用写队列）。

        每个库一个独立队列是刻意的：库之间不共享写入，
        所以一个库被大量写入不会阻塞另一个库的读。

        Args:
            commit_debounce: 覆盖默认防抖秒数。不传则看 ``fast_commit``：
                · ``fast_commit=True`` → 0（每次写立刻提交，测试用）
                · ``False``            → 用 SharedMemoryService 的默认值
        """
        svc = self._services.get(lib_id)
        if svc is None:
            lib = self.store.lib(lib_id)
            if lib is None:
                raise ToolError(ErrorCode.NOT_FOUND, f"库不存在：{lib_id}")
            vault = self.libs.vault(lib)
            if commit_debounce is None and self.fast_commit:
                commit_debounce = 0
            svc = SharedMemoryService(vault, commit_debounce=commit_debounce) \
                if commit_debounce is not None else SharedMemoryService(vault)
            svc.start()
            self._services[lib_id] = svc
        return svc

    async def close(self) -> None:
        for svc in self._services.values():
            try:
                await svc.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("停止库服务失败: %s", exc)
        self._services.clear()

    # ────────────────────── 身份 ──────────────────────

    def whoami(self, user_name: str) -> dict[str, Any]:
        """当前身份与可见范围。面板与 `memory_whoami` 用它。"""
        return self.tenant.note_for_user(user_name)

    # ────────────────────── 写 ──────────────────────

    async def write(
        self,
        user_name: str,
        *,
        title: str,
        body: str,
        lib_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """写入一条记忆。

        ``lib_id`` 为空时**落到用户自己的个人库** —— 写是最容易污染别人的
        操作，默认落自己名下，要往共享库写必须显式指定。

        ⚠️ 这里的实现细节值得说明：**先做权限检查，再用检查通过的那个
        vault 构造/复用 service**。而不是"先取 service 再检查" ——
        那样 service 可能持有另一个库的 vault（缓存未命中时）。
        """
        if lib_id:
            target = lib_id
        else:
            target = self.tenant.default_write_lib(user_name).id

        # vault_for 带写权限检查 —— 这是唯一的权威判定
        vault = self.tenant.vault_for(user_name, target, need_write=True)

        # 用**检查通过的那个** vault 取 service。
        #
        # ⚠️ 必须走 service_for()（而不是自己 new）—— 第一版在这里直接
        # `SharedMemoryService(vault)`，结果绕过了 fast_commit 与缓存，
        # 测试全失败。**统一入口**是这类 bug 的唯一防线。
        svc = self.service_for(target)
        if svc.vault is not vault:  # pragma: no cover - 缓存与库应一一对应
            svc = self._new_service(vault)

        # ⚠️ 每次写入都设置作者 —— 服务实例是**跨请求复用**的，
        # 不设置的话会沿用上一个请求的作者，那是错误的信息
        # （比"没有作者"更糟：会误导追溯）。
        svc.set_author(user_name)

        result = await svc.write(title=title, body=body, **kwargs)
        lib = self.store.lib(target)
        result["_lib"] = target
        result["_lib_label"] = (lib.label if lib else target)
        return result

    async def delete(self, user_name: str, ident: str, *, lib_id: str = "") -> dict[str, Any]:
        """删除（软删除）。需要 write 权限。"""
        lib_id = lib_id or self._find_lib_of(user_name, ident)
        self.tenant.vault_for(user_name, lib_id, need_write=True)
        return await self.service_for(lib_id).delete(ident)

    async def restore(self, user_name: str, filename: str, *, lib_id: str) -> dict[str, Any]:
        self.tenant.vault_for(user_name, lib_id, need_write=True)
        return await self.service_for(lib_id).restore(filename)

    # ────────────────────── 读 ──────────────────────

    def search(
        self, user_name: str, query: str = "", *, lib_id: str = "", **kwargs: Any
    ) -> dict[str, Any]:
        """搜索。

        · 给了 ``lib_id`` → 只搜那个库（**仍要过权限检查**）
        · 没给 → **跨全部可读库搜**，结果带来源标注
        """
        if lib_id:
            self.tenant.vault_for(user_name, lib_id)      # 无权限会抛
            r = self.service_for(lib_id).search(query, viewer=user_name, **kwargs)
            for item in r.get("results", []):
                lib = self.store.lib(lib_id)
                item["_lib"] = lib_id
                item["_lib_label"] = (lib.label if lib else lib_id)
            return r
        return self.tenant.search_all(user_name, query, **kwargs)

    def _note_visible(self, user_name: str, note: dict, lib_id: str,
                      *, cross_space: bool = False) -> bool:
        """这条记忆对该身份可见吗（v2 S-10.2 的读端闸）。

        ``note`` 是 ``Note.to_dict()`` 的产物 —— ``share``/``author``
        为空时不输出，所以这里一律 ``.get(...) or ""``。
        """
        from .vault import share_visible

        svc = self.service_for(lib_id)
        return share_visible(
            str(note.get("share") or ""),
            str(note.get("author") or ""),
            user_name,
            space_default=svc.config.default_share(),
            cross_space=cross_space,
        )

    @staticmethod
    def _not_found(ident: str) -> ToolError:
        """与 :meth:`vault.Vault.resolve` 的 NOT_FOUND 逐字一致的构造器。

        两处文案必须由同一个函数产生 —— 抄两遍必然漂移，而漂移的后果
        不是"文案不好看"，是**错误体变成存在性探针**。
        """
        return ToolError(
            ErrorCode.NOT_FOUND,
            f"找不到记忆：{ident}",
            hint="用 memory_list 或 memory_search 查看现有记忆的标题",
        )

    def get(self, user_name: str, ident: str, *, lib_id: str = "") -> dict[str, Any]:
        """读一条记忆全文。

        没给库时**只在该用户可读的库里找** —— 找不到也没关系，
        关键是**不会去读无权限的库**（那是越权）。

        ━━━ 越权读的统一口径（v2 S-10.5）━━━

        档位不允许（别人的 ``mine``）与"根本没这条"**返回同一个
        NOT_FOUND**。不能回 FORBIDDEN —— 那等于告诉调用方
        "这个标题存在、内容不给你"，把存在性泄露出去了。
        """
        if lib_id:
            self.tenant.vault_for(user_name, lib_id)
            # 库里真没有这条 → vault 抛 NOT_FOUND，原样透出去（不要包装：
            # 包装出来的文案与下面"被档位挡住"的那份不一致就会变成探针）
            r = self.service_for(lib_id).get(ident)
            if not self._note_visible(user_name, r["note"], lib_id):
                # ⚠️ 必须与"库里真没有这条"**逐字一样**（含 hint）。
                # 写一句"这条被共享档位挡住了"看起来更贴心，实际是把
                # 错误体变成了存在性探针：调用方换个标题一比就分出两种情况。
                raise self._not_found(ident)
            r["note"]["_lib"] = lib_id
            return r

        for lib in self.tenant.readable(user_name):
            try:
                r = self.service_for(lib.id).get(ident)
            except ToolError as exc:
                if exc.code != ErrorCode.NOT_FOUND:
                    raise
                continue
            if not self._note_visible(user_name, r["note"], lib.id):
                continue
            r["note"]["_lib"] = lib.id
            r["note"]["_lib_label"] = lib.label or lib.id
            return r

        # 经目标空间授权的跨空间共享面：只放行 shared 档位
        for lib in self.tenant.shared_incoming(user_name):
            try:
                r = self.service_for(lib.id).get(ident)
            except ToolError as exc:
                if exc.code != ErrorCode.NOT_FOUND:
                    raise
                continue
            if not self._note_visible(user_name, r["note"], lib.id, cross_space=True):
                continue
            r["note"]["_lib"] = lib.id
            r["note"]["_lib_label"] = lib.label or lib.id
            r["note"]["_shared_incoming"] = True
            return r

        raise ToolError(
            ErrorCode.NOT_FOUND,
            f"在你可读的库里找不到：{ident}",
            hint="用 memory_search 确认标题，或换一个库试试",
        )

    def list_notes(
        self, user_name: str, *, limit: int = 50, lib_id: str = "", **kwargs: Any
    ) -> dict[str, Any]:
        """列举。跨库时合并并按时间排序。

        ``viewer`` 与检索同闸（v2 S-10.2）：面板左栏与 agent 列举
        走这条路，档位判定必须在同一处生效，不能只给检索加闸。
        """
        if lib_id:
            self.tenant.vault_for(user_name, lib_id)
            return self.service_for(lib_id).list_notes(
                limit=limit, viewer=user_name, **kwargs
            )

        merged: list[dict[str, Any]] = []
        for lib in self.tenant.readable(user_name):
            r = self.service_for(lib.id).list_notes(limit=200, viewer=user_name, **kwargs)
            for item in r.get("notes", []):
                item["_lib"] = lib.id
                item["_lib_label"] = lib.label or lib.id
                merged.append(item)
        merged.sort(key=lambda x: (x.get("updated") or x.get("created") or ""), reverse=True)
        return {
            "ok": True,
            "total": len(merged),
            "showing": min(len(merged), limit),
            "notes": merged[:limit],
        }

    # ────────────────────── 体系：视图 ──────────────────────

    def view(self, user_name: str, kind: str, *, lib_id: str = "", **params: Any) -> dict[str, Any]:
        """视图。跨库时**合并各库视图的结果**。

        ⚠️ 视图内部要读全量笔记来算统计 —— 跨库时必须逐库算再合并，
        不能"先合并全库再算"（那会把无权限库的内容算进统计里，泄露规模）。
        """
        if lib_id:
            self.tenant.vault_for(user_name, lib_id)
            return self.service_for(lib_id).view(kind, viewer=user_name, **params)

        libs = self.tenant.readable(user_name)
        if not libs:
            return {"ok": True, "kind": kind, "total": 0, "items": [],
                    "note": "你没有可读的库"}

        merged_items: list[dict[str, Any]] = []
        merged_stats: dict[str, Any] = {}
        merged_total = 0
        type_counts: dict[str, int] = {}
        expiry_counts = {"expired": 0, "review": 0, "superseded": 0}
        ws_counts: dict[str, int] = {}

        for lib in libs:
            r = self.service_for(lib.id).view(kind, viewer=user_name, **params)
            merged_total += r.get("total", 0)
            for item in r.get("items", []):
                item["_lib"] = lib.id
                item["_lib_label"] = lib.label or lib.id
                merged_items.append(item)
            # 统计逐库累加（而不是合并后重算）
            st = r.get("stats") or {}
            for t, n in st.get("types", []):
                type_counts[t] = type_counts.get(t, 0) + n
            for k2, n in (st.get("expiry") or {}).items():
                if k2 in expiry_counts:
                    expiry_counts[k2] += n
            for w, n in st.get("workspaces", []):
                ws_counts[w] = ws_counts.get(w, 0) + n

        if kind == "overview":
            merged_stats = {
                "types": sorted(type_counts.items(), key=lambda kv: -kv[1]),
                "expiry": expiry_counts,
                "workspaces": sorted(ws_counts.items(), key=lambda kv: -kv[1]),
            }

        merged_items.sort(key=lambda x: (x.get("updated") or x.get("created") or ""), reverse=True)
        # ⚠️ label / desc 必须带上 —— 前端 loadKnowledge 用它们渲染
        # 视图的标题与副说明（见 panel.js 的 r.label / r.desc）。
        #
        # 漏掉的后果**不是报错**，而是标题区域变成空白
        # （`<h2 class="sechead"></h2>`）。用户看到的是「一片空」，
        # 而不是「哪里错了」—— 这类静默缺陷只能靠真浏览器发现。
        _lbl, _desc = _view_meta(kind)
        return {
            "ok": True,
            "kind": kind,
            "label": _lbl,
            "desc": _desc,
            "total": merged_total,
            "items": merged_items[: params.get("limit", 200)],
            **({"stats": merged_stats} if merged_stats else {}),
        }

    def backlinks(self, user_name: str, title: str, *, lib_id: str = "") -> dict[str, Any]:
        if lib_id:
            self.tenant.vault_for(user_name, lib_id)
            return self.service_for(lib_id).backlinks(title)
        # 跨库：**合并所有可读库**的 backlinks，不能停在第一个库 ——
        # service.backlinks 对查不到的标题返回空信封而不抛错，原实现在
        # 第一个库（company）就 return，个人库里的引用永远看不见
        # （2026-10-13 部署实测抓到的 bug）。
        # 引用是库内关系；outgoing 取标题**实际所在库**的计算结果。
        merged: list[str] = []
        outgoing: list[str] = []
        found_lib = ""
        for lib in self.tenant.readable(user_name):
            try:
                svc = self.service_for(lib.id)
                r = svc.backlinks(title)
            except ToolError:
                continue
            for b in r.get("backlinks", []):
                if b not in merged:
                    merged.append(b)
            if not found_lib and svc._exists(title):
                found_lib = lib.id
                outgoing = r.get("outgoing", [])
        return {
            "ok": True,
            "title": title,
            "backlinks": merged,
            "outgoing": outgoing,
            **({"lib": found_lib} if found_lib else {}),
        }

    # ────────────────────── 导入 / 导出（方案 14） ──────────────────────

    def export(
        self, user_name: str, *, workspace: str = "", lib_id: str = "", out_dir: str = ""
    ) -> dict[str, Any]:
        """以**工作空间**为单位导出记忆 bundle（字节级保留，可无损再导入）。

        · 传 ``lib_id`` → 只导那一个库（需读权限）
        · 不传 → **你有权读的每个库**各导一份，合并进一个 bundle
          （多库 bundle 结构：``libs/<库>/notes/*.md`` + 顶层 manifest）

        产物落在 vault 之外（默认 ``<home>/exports/``）—— vault 主目录
        保持纯净（约束 5），也不会被 git 当成记忆提交。

        ⚠️ 同步方法（内部全是文件拷贝，没有 await 点）—— 工具层用
        ``_io`` 卸载到读线程池，别在这里包 async。
        """
        from .vault import now_iso

        if lib_id:
            self.tenant.vault_for(user_name, lib_id)  # 读权限检查
            libs = [lib_id]
        else:
            libs = [lib.id for lib in self.tenant.readable(user_name)]
        if not libs:
            return {"ok": False, "error": "no_readable_libs",
                    "message": "你没有可读的库，无从导出"}

        ws = (workspace or "").strip()
        root = Path(out_dir).expanduser() if out_dir else (self.home / "exports")
        stamp = now_iso().replace(":", "").replace("-", "")[:15]
        safe_ws = re.sub(r'[<>:"/\\|?*\x00-\x1f\s]', "-", ws) or "all"
        bundle_dir = root / f"{safe_ws}-{stamp}"
        bundle_dir.mkdir(parents=True, exist_ok=True)

        all_entries: list[dict[str, Any]] = []
        lib_summaries: list[dict[str, Any]] = []
        total_bytes = 0
        for lid in libs:
            svc = self.service_for(lid)
            part = svc.export_files(workspace=ws, out_dir=bundle_dir, lib_id=lid)
            all_entries.extend(part["entries"])
            total_bytes += part["bytes"]
            lib_obj = self.store.lib(lid)
            lib_summaries.append({
                "id": lid, "label": (lib_obj.label if lib_obj else lid),
                "count": part["count"],
            })

        manifest = {
            "schema": "shared-memory-bundle/1",
            "exported_at": now_iso(),
            "exported_by": user_name,
            "workspace": ws,
            "libs": lib_summaries,
            "entries": all_entries,
        }
        (bundle_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        result: dict[str, Any] = {
            "ok": True,
            "bundle": str(bundle_dir),
            "workspace": ws,
            "lib_count": len(lib_summaries),
            "count": len(all_entries),
            "bytes": total_bytes,
            "libs": lib_summaries,
        }

        # 导出了库却一条没进 —— 最可能的原因是 workspace 参数理解错了。
        # 实测踩过：把**库 id**（如 user-admin）传给 workspace，得到
        # "3 个库 0 条记忆"的静默空结果，用户以为导出坏了。
        # workspace 是**记忆条目上的项目标签**，不是库名。
        if ws and all_entries == [] and lib_summaries:
            result["hint"] = (
                f"workspace={ws!r} 没有匹配到任何记忆 —— 注意它是**条目上的"
                "项目标签**（memory_write 的 workspace 参数），不是库名。"
                "要按库导出请用 lib=<库id>（如 lib=\"user-admin\"）。"
            )
        return result

    async def import_bundle(
        self,
        user_name: str,
        bundle: str,
        *,
        target_lib: str = "",
        workspace: str = "",
        on_conflict: str = "skip",
    ) -> dict[str, Any]:
        """把 bundle 导入**一个目标库**（默认你的个人库）。

        · 目标库需**写权限**；不传 ``target_lib`` → 你的个人库
          （写入默认最小污染面，与 memory_write 一致）
        · ``workspace`` 是**过滤器**不是改写器：只导入该工作区的条目；
          不传则用 bundle manifest 里记的值。导入**不 Rewrite 原内容** ——
          字节级还原是这条链路的意义（决策 8）
        · ``on_conflict``：skip（默认，不覆盖）/ update / rename
        """
        target = target_lib or self.tenant.default_write_lib(user_name).id
        self.tenant.vault_for(user_name, target, need_write=True)
        svc = self.service_for(target)
        result = await svc.import_bundle(
            bundle, on_conflict=on_conflict, workspace=workspace
        )
        lib = self.store.lib(target)
        result["_lib"] = target
        result["_lib_label"] = (lib.label if lib else target)
        return result

    # ────────────────────── 运维 ──────────────────────

    def stats(self, user_name: str, *, lib_id: str = "", with_commits: bool = False) -> dict[str, Any]:
        """库况。跨库时给出每个库的概要。

        ⚠️ **只统计用户可读的库** —— 全局统计（含无权库）本身就是泄露。

        ━━━ git 历史为什么改成按需（with_commits）━━━

        实测每次 ``git log`` 要 fork 子进程（约 60ms，与数据量无关），
        而 stats 的核心用途（队列积压、条数、dirty 状态）**不依赖**它。
        默认不带 → stats 从 ~0.35s 降到 ~0.1s 量级；
        面板/排查场景显式传 ``with_commits=True`` 拿完整数据。
        """
        if lib_id:
            self.tenant.vault_for(user_name, lib_id)
            s = self.service_for(lib_id).stats()
            s["_lib"] = lib_id
            # 补 git 历史（service.stats() 不含它 —— 那是 git_log 的职责）
            if with_commits:
                s["commits"] = self.service_for(lib_id).git_log(20)["commits"]
            return s

        out = []
        total = 0
        for lib in self.tenant.readable(user_name):
            s = self.service_for(lib.id).stats()
            entry = {
                "lib": lib.id,
                "label": lib.label or lib.id,
                "owner": lib.owner,
                "notes": s.get("notes", 0),
                "queue": s.get("write_queue", {}).get("pending", 0),
            }
            # 每个库带几条最近提交，让面板能显示"谁在什么时候改了什么"
            if with_commits:
                entry["commits"] = self.service_for(lib.id).git_log(5)["commits"]
            out.append(entry)
            total += s.get("notes", 0)
        return {
            "ok": True,
            "user": user_name,
            "lib_count": len(out),
            "notes_total": total,
            "libraries": out,
        }

    def health(self, user_name: str = "") -> dict[str, Any]:
        """跨库健康检查 —— 给 ``/health`` 端点用（监控/守护/容器探针）。

        ━━━ 与 stats 的区别 ━━━

        stats 回答"库里有什么"（给人看）；health 回答"**服务还能不能用**"
        （给程序判定），所以：
          · 有明确的 ``ok`` 布尔与 ``problems`` 列表
          · 用**真实写入**探测可写性，不靠 os.access 猜
          · 区分"写者还没创建"与"写者崩了"（前者不是故障）

        逐库调用 ``SharedMemoryService.health()`` 再合并 —— 判定逻辑
        只有一份，不在 HTTP 层重写（重写必然与真实判定漂移）。

        ⚠️ ``user_name`` 为空 = 汇总**全部库**。这条路径只用于
        未启用鉴权的 loopback 探活（``auth.required=False``）——
        那种部署下"只有本机可达"，聚合结果不构成泄露。绑公网时
        ``/health`` 仍要求凭据，走的是一定带 user 的路径。
        """
        if user_name:
            libs = [lib.id for lib in self.tenant.readable(user_name)]
        else:
            libs = [lib.id for lib in self.store.all_libs()]

        problems: list[str] = []
        notes = 0
        unreadable = 0
        pending = 0
        outstanding = 0
        rejected = 0
        processed = 0
        max_depth = 0
        uptime = 0
        for lid in libs:
            h = self.service_for(lid).health()
            notes += h.get("notes", 0)
            unreadable += h.get("unreadable", 0)
            q = h.get("queue", {})
            pending += q.get("pending", 0)
            outstanding += q.get("outstanding", 0)
            rejected += q.get("rejected", 0)
            processed += q.get("processed", 0)
            max_depth += q.get("max_depth", 0)
            uptime = max(uptime, h.get("uptime_seconds", 0))
            if not h.get("ok"):
                # 带上库名 —— 多库部署里"哪个库坏了"是排查第一问
                problems.extend(f"[{lid}] {p}" for p in h.get("problems", []))

        return {
            "ok": not problems,
            "problems": problems,
            "user": user_name,
            "libs": len(libs),
            "notes": notes,
            "unreadable": unreadable,
            # F-4：探针只要一个数就能判"积压了没有"，不必钻进嵌套对象。
            # 与 queue.pending 同一个来源（不在这里二次计算，避免两处真相）。
            "queue_depth": pending,
            "queue": {
                "pending": pending,
                "outstanding": outstanding,
                "max_depth": max_depth,
                "processed": processed,
                "rejected": rejected,
            },
            "uptime_seconds": uptime,
        }

    # ────────────────────── 用户生命周期 ──────────────────────

    def remove_user(self, name: str, *, auth: Any = None) -> dict[str, Any]:
        """删用户 —— **连带吊销他的全部 token**。

        ━━━ 为什么必须一起做 ━━━

        删用户不吊销 token 是个安全隐患：token 还在「有效」列表里，
        持有它的人仍能发起请求。虽然权限计算时查不到用户会拒绝，
        但那个状态本身是错的（列表里挂着一把"有效但无人"的凭据），
        日志里也会显示"未知用户"，排查时容易误判成权限系统坏了。

        这正是实测踩出来的：清掉测试用户后，面板的 token 列表里
        还挂着两把属于已删用户的 token。

        Args:
            name: 要删的用户名。
            auth: 鉴权器（用来吊销 token）。不传则只删用户 ——
                调用方要清楚自己在做什么。
        """
        if name not in self.store.users:
            return {"ok": False, "message": f"用户不存在：{name}"}

        revoked = 0
        if auth is not None:
            revoked = auth.revoke_token(name)

        # 个人库：**不删目录**（里面可能有真实记忆），只移除登记
        had_lib = f"user-{name}" in self.store.libs
        self.store.remove_user(name)

        logger.warning("已删除用户 %s（吊销 %d 把 token，移除个人库登记=%s）",
                       name, revoked, had_lib)
        return {
            "ok": True,
            "user": name,
            "revoked_tokens": revoked,
            "personal_lib_unregistered": had_lib,
            "note": "个人库的**文件保留**（可能有真实记忆）—— 只是不再登记。"
                    "要彻底删除请手工处理目录。",
        }

    # ────────────────────── 辅助 ──────────────────────

    def _find_lib_of(self, user_name: str, ident: str) -> str:
        """在可读库里找 ident 属于哪个库。

        ⚠️ 只搜**可读**的库 —— 找不到就说明确实不在你可及的范围内。
        """
        for lib in self.tenant.readable(user_name):
            try:
                self.service_for(lib.id).get(ident)
                return lib.id
            except ToolError:
                continue
        raise ToolError(
            ErrorCode.NOT_FOUND,
            f"在你可读的库里找不到：{ident}",
            hint="用 memory_search 确认标题；跨库操作要显式传 lib",
        )


def default_home() -> Path:
    """多租户模式的数据根目录。

    布局：
        <home>/
          ├── tenants.json      用户与库登记（含密码哈希，权限 600）
          ├── libs/
          │     ├── company/    每个库一个目录 + 一个 git 仓库
          │     ├── user-admin/
          │     └── group-xxx/
    """
    env = os.environ.get("SHARED_MEMORY_HOME")
    if env:
        return Path(env)
    return Path.home() / "shared-memory"
