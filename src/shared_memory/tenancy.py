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
from .vault import VERSIONED_DIRNAME
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
        #: 测试用开关：True 时配置/导入类改动的防抖降到 0（改动立刻提交）。
        #: ⚠️ v2 S-8 之后它**不再影响普通写入** —— 写入本来就不产生提交了，
        #: 所以别再把它理解成"每次写都提交"。
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

    async def set_share(self, user_name: str, ident: str, share: str,
                        *, lib_id: str = "") -> dict[str, Any]:
        """改一条记忆的共享档位 —— **走 write 同一条内部通道**，不是直改文件。

        ━━━ 为什么绕这一大圈 ━━━

        最省事的做法是"读出来、改一个字段、写回去"的三段式手工补丁。
        但那条路绕过写队列（并发覆盖）、绕过字段校验（坏档位进库）、
        绕过改前快照与版本库 —— 而档位是**权限字段**，恰恰最不能走旁门。
        面板上的徽标点击就走这里，和 agent 调 ``memory_remember`` 是同一条路。

        两道闸都在，且都不是这里新发明的：
          · 读闸 ``get()``：**看不见的条目也改不了**，别人的 ``mine`` 与
            "根本没这条"同样是 NOT_FOUND（不因为"只是改个档位"就放水）
          · 写闸 ``write()`` → ``vault_for(need_write=True)``：能读不能写的库
            （比如成员眼中的公司库）改档位会被拒 —— 该拒，档位改了就是改内容
        """
        r = self.get(user_name, ident, lib_id=lib_id)
        note = r["note"]
        return await self.write(
            user_name,
            title=str(note.get("title") or ""),
            body=str(note.get("body") or ""),
            lib_id=lib_id or str(note.get("_lib") or ""),
            extra={"share": share},
        )

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

    def note_visible(self, user_name: str, note: dict, lib_id: str,
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
            if not self.note_visible(user_name, r["note"], lib_id):
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
            if not self.note_visible(user_name, r["note"], lib.id):
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
            if not self.note_visible(user_name, r["note"], lib.id, cross_space=True):
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

    def versions(
        self,
        user_name: str,
        ident: str,
        *,
        lib_id: str = "",
        sha: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        """一条记忆的**版本历史 + 最近一版 diff**（v2 · S-8.3）。只读。

        ━━━ 为什么不报错，而是老实说"没启用版本控制" ━━━

        S-8 之后只有标了 ``versioned`` 的长文才进 git 版本库，普通条目没有
        版本序列 —— 但**这不等于"改坏了没法退"**：每条记忆都有 `.history/`
        改前快照（防手滑层，与 versioned 无关）。所以这里两种数据都给：
        `versions`（git 里，可能有）与 `snapshots`（盘上，人人有）。
        让"我改坏了"这种提问总能拿到一个可用的答案，比维持概念整洁重要。

        权限与读一条记忆同闸：**直接复用 get()**，不自己拼"找库 + 判档位"。
        不是省事 —— 自己拼的话，"库里真没有"与"有但档位不给看"很容易
        写成两句不同的文案，那两句差异就是一个存在性探针（S-10.5 的教训）。
        """
        r = self.get(user_name, ident, lib_id=lib_id)
        note = r["note"]
        lib_id = str(note.get("_lib") or lib_id)

        vault = self.service_for(lib_id).vault
        snaps = vault.list_history(ident)
        out: dict[str, Any] = {
            "ok": True,
            "ident": ident,
            "title": note.get("title", ""),
            "lib": lib_id,
            "versioned": bool(note.get("versioned")),
            "snapshots": snaps,
        }
        if snaps:
            # 只给最近一版正文：够用（"改坏了想退回上一版"要的就是它），
            # 且不会把整个快照目录灌进上下文。
            out["previous_body"] = vault.newest_history_body(ident)[:4000]
        if not out["versioned"]:
            out["versions"] = []
            out["message"] = (
                "未启用版本控制（这条没标 versioned，不进 git 版本库）；"
                + ("下面是改前快照" if snaps else "而且还没有过覆盖，没有快照")
            )
            return out

        vr = vault.version_repo
        if vr is None:  # pragma: no cover - 生产必配，独立模式才会走到
            out["versions"] = []
            out["message"] = "服务端没有启用共享版本库"
            return out
        name = vault.resolve(ident).name
        h = vr.history(vault.lib_id, name, limit=limit)
        out["versions"] = h.get("versions", [])
        if h.get("message"):
            out["message"] = h["message"]
        if out["versions"]:
            d = vr.diff(vault.lib_id, name, sha or out["versions"][0]["sha"])
            out["diff"] = d.get("diff", "") if d.get("ok") else ""
            if not d.get("ok"):
                out["diff_error"] = d.get("message", "")
        return out

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

    # ────────────────────── 全量快照 / 恢复（v2 S-8.4）──────────────────────

    #: 快照必含的目录（数据本体）。`exports` 刻意不含：那是产物目录，
    #: 含进来会让"备份的备份"无限套娃，还会把上一次快照再打一遍。
    SNAPSHOT_DIRS = ("libs", VERSIONED_DIRNAME)
    SNAPSHOT_FILES = ("tenants.json",)

    def snapshot_home(
        self, *, out_dir: str = "", with_state: bool = False
    ) -> dict[str, Any]:
        """打包整个租户根目录（**这才是 v2 的备份机制**）。

        ━━━ 为什么备份不再靠 git ━━━

        v1 把 git 当备份：每次写都提交，于是"有历史"和"能恢复"被混成
        一件事。S-8 之后普通写入不产生提交，git 里只剩 versioned 长文 ——
        拿它当备份会**静默漏掉绝大多数记忆**。所以备份明确成另一条路：
        tar 整个目录（`libs/` + `_versioned/` + `tenants.json`）。

        Args:
            with_state: 是否连 `state/`（令牌、主密钥、PIN）一起打。
                默认 **False**：那个 tar 里是凭据，落到 exports/ 就成了
                一个等着被误传的密钥包。要备份凭据请显式传 True，
                返回值里也会提醒你这个包按机密对待。

        Returns:
            ``{"ok", "path", "bytes", "members", "with_state", "warnings"}``
        """
        import tarfile
        import time as _time

        # 文件名必须**唯一**：restore 会先给当前状态打一份快照再解包，
        # 两次快照极易落在同一秒。原先只精确到秒 → 恢复前的快照直接覆盖
        # 掉正在恢复的那个包，解出来的东西半新半旧，函数还一路返回 ok。
        # 加微秒 + 存在即递增，**永不覆盖已有备份**。
        stamp = _time.strftime("%Y%m%d-%H%M%S")
        base = Path(out_dir).expanduser() if out_dir else (self.home / "exports")
        base.mkdir(parents=True, exist_ok=True)
        name = f"shm-snapshot-{stamp}-{_time.time_ns() % 1_000_000:06d}"
        out = base / f"{name}.tar.gz"
        seq = 1
        while out.exists():
            out = base / f"{name}-{seq}.tar.gz"
            seq += 1

        warnings: list[str] = []
        if with_state:
            warnings.append(
                "本包含 state/（令牌 / 主密钥 / PIN）—— 按机密对待，"
                "不要发到共享盘或聊天里"
            )
        members = 0
        with tarfile.open(out, "w:gz") as tf:
            for d in self.SNAPSHOT_DIRS:
                p = self.home / d
                if p.is_dir():
                    before = members
                    for f in p.rglob("*"):
                        if f.is_file():
                            tf.add(f, arcname=str(f.relative_to(self.home)), recursive=False)
                            members += 1
                    if members == before:
                        warnings.append(f"{d}/ 是空的（这个库还没数据）")
            for f in self.SNAPSHOT_FILES:
                p = self.home / f
                if p.is_file():
                    tf.add(p, arcname=f, recursive=False)
                    members += 1
            if with_state:
                st = self.home / "state"
                if st.is_dir():
                    for f in st.rglob("*"):
                        if f.is_file():
                            tf.add(f, arcname=str(f.relative_to(self.home)), recursive=False)
                            members += 1
        try:
            # 快照可能含凭据 → 权限收到 600，别做成同机可读
            out.chmod(0o600)
        except OSError:
            pass
        bytes_total = out.stat().st_size

        # ⚠️ 打完必须**真解一遍**：实测里"备份文件存在但打不开"是常见事故
        # （打包时进程被 kill、磁盘满）。只报"存在"不报"可读"的备份工具
        # 会让人带着一个废包去恢复。
        try:
            with tarfile.open(out, "r:gz") as tf:
                back = len([m for m in tf.getmembers() if m.isfile()])
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "code": ErrorCode.STORAGE_ERROR,
                "message": f"快照写出来但读不回（这个包不可用）：{exc}",
                "path": str(out),
            }
        if back != members:
            warnings.append(f"回读清点不一致：写入 {members}，读到 {back} —— 别用这个包")

        return {
            "ok": True,
            "action": "snapshot",
            "path": str(out),
            "bytes": bytes_total,
            "members": members,
            "with_state": with_state,
            "warnings": warnings,
        }

    def restore_home(self, bundle: str, *, confirm: bool = False) -> dict[str, Any]:
        """从快照 tar 恢复整个租户根目录（**明示覆盖 + 恢复前自动快照**）。

        两道闸都是必需的：
          1. ``confirm=True`` —— 恢复是整目录覆盖，正在写的记忆会没
          2. 覆盖前先给当前状态打一个快照 —— 恢复错了还能退回恢复前

        ⚠️ 解包必须防 **tar slip**：包里若有 `../../` 或绝对路径的成员，
        直接 extractall 等于让一个 tar 文件写到目录外。这里逐成员校验
        并拒整个包（不是"跳过坏成员"—— 一个畸形包通常意味着包被改过，
        那种情况下"部分恢复"比不恢复更糟）。
        """
        import tarfile

        src = Path(bundle).expanduser()
        if not src.is_file():
            raise ToolError(
                ErrorCode.NOT_FOUND,
                f"找不到快照文件：{bundle}",
                hint="用 memory_backup(action=\"snapshot\") 生成；路径要给到 .tar.gz 文件本身",
            )
        if not confirm:
            return {
                "ok": False,
                "code": ErrorCode.GOV_CONFIRM_REQUIRED,
                "message": "恢复会**覆盖整个记忆目录**，必须显式 confirm=True",
                "preview": {
                    "bundle": str(src),
                    "bytes": src.stat().st_size,
                    "will_replace": list(self.SNAPSHOT_DIRS) + list(self.SNAPSHOT_FILES),
                },
                "hint": "先看 preview 确认要覆盖的是哪台机器的哪个目录",
            }

        # 恢复前自动快照当前状态（必须先做：这一步同样往 exports/ 写包）
        pre = self.snapshot_home(with_state=True)
        if not pre.get("ok"):
            raise ToolError(
                ErrorCode.STORAGE_ERROR,
                f"恢复前的自动快照没做成，为安全起见不执行恢复：{pre.get('message')}",
            )

        # ━━━ 先解到暂存区，清点无误再换目录 ━━━
        #
        # 为什么不"边解包边覆盖"：那样第一条失败就把库留在"一半新一半旧"
        # 的状态，而这种状态没人能收拾（连"现在到底恢复了什么"都说不清）。
        # 也是在这里避开了 git 对象文件**只读**（444）的坑：直接覆盖
        # `.git/objects/**` 会 PermissionError，必须先解到别处、
        # 再把旧目录整体移走（见 _make_writable）。
        import shutil
        import time as _time

        staging = self.home / ".restore-staging" / _time.strftime("%Y%m%d-%H%M%S")
        staging.mkdir(parents=True, exist_ok=True)
        try:
            with tarfile.open(src, "r:gz") as tf:
                # ⚠️ members 必须**在真正解包的这个句柄里现取现用**。
                # 复用上一次 open 留下的成员对象是按旧文件偏移读的：包在这
                # 期间被覆盖（同秒快照曾经重名）就会把别的成员内容写进目标
                # 文件 —— tenants.json 变成一段 zlib 压缩的 git object，而
                # 恢复过程一路报 ok。2026-10-29 实测踩到。
                members = [m for m in tf.getmembers() if m.isfile()]
                bad: list[str] = []
                for m in members:
                    name = m.name
                    if name.startswith("/") or ".." in Path(name).parts:
                        bad.append(name)
                if bad:
                    raise ToolError(
                        ErrorCode.INVALID_ARGUMENT,
                        f"快照里有越界路径，整包拒绝：{bad[:3]}",
                        hint="这个包可能被手工改过或损坏 —— 换一个包，别强行恢复",
                    )
                counted = len(members)
                # 解释器自己的第二道防护：filter="data" 会拒硬链接/符号链接/
                # 特殊文件；版本没有这个参数时（<3.11.4）退回只用上面那道校验。
                try:
                    tf.extractall(staging, members=members, filter="data")
                except TypeError:  # pragma: no cover - 老版本 Python
                    tf.extractall(staging, members=members)
            got = sum(1 for p in staging.rglob("*") if p.is_file())
            if got != counted:
                raise ToolError(
                    ErrorCode.STORAGE_ERROR,
                    f"解包不完整（应有 {counted}，实得 {got}），不执行覆盖",
                    hint=f"包 {src} 可能损坏；当前库**没有被改动**，换一个包再试",
                )

            for entry in sorted(staging.iterdir()):
                live = self.home / entry.name
                if entry.is_dir():
                    self._swap_dir(entry, live)
                else:
                    self._replace_file(entry, live)
        finally:
            root = self.home / ".restore-staging"
            if root.exists():
                for d in root.iterdir():
                    self._make_writable(d)
                    shutil.rmtree(d, ignore_errors=True)

        # 恢复完**必须重启服务**：tenants.json 是启动时载入内存的，
        # 直接跑的话内存里还是旧身份表，会出现"文件已恢复但登录不上"。
        return {
            "ok": True,
            "action": "restore",
            "restored": counted,
            "pre_restore_snapshot": pre["path"],
            "message": (
                f"已恢复 {counted} 个文件；恢复前的状态存进了 "
                f"{pre['path']}。⚠️ 重启服务后 tenants.json 才会重新载入 —— "
                "不重启会出现「文件已恢复但身份还是旧的」"
            ),
        }

    @staticmethod
    def _make_writable(path: Path) -> None:
        """把一棵树全部加上写位。

        ⚠️ 必需，不是洁癖：git 的对象文件是**只读 444**，
        直接覆盖或 ``shutil.rmtree`` 都会 ``PermissionError`` ——
        而 Windows 上连"先删后写"都救不了（只读文件删不掉）。
        恢复一个 git 仓库形态的目录，绕不开这一步。
        """
        import stat

        if not path.exists():
            return
        for p in [path, *path.rglob("*")]:
            try:
                p.chmod(stat.S_IRUSR | stat.S_IWUSR | (stat.S_IXUSR if p.is_dir() else 0))
            except OSError:
                pass  # 单个改不动不该中断整棵树的准备，后面真会失败的会自己抛

    def _swap_dir(self, src: Path, dst: Path) -> None:
        """用暂存目录整个替换线上目录（先让两边都可写，再搬）。"""
        import shutil

        self._make_writable(dst)
        if dst.exists():
            shutil.rmtree(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))

    def _replace_file(self, src: Path, dst: Path) -> None:
        """用一个文件替换线上文件（tenants.json 这类）。"""
        import shutil

        self._make_writable(dst.parent)
        if dst.exists():
            dst.unlink()
        shutil.move(str(src), str(dst))

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
