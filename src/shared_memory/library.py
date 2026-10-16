"""多库管理 —— GitHub 式的「多库 + 统一入口」。

━━━ 为什么是多库而不是「库内分层」━━━

一个常见做法是：一个库，每条记忆带 ``scope: personal/team/company`` 字段，
检索时按权限过滤。**那是难且容易漏的路**：

  · 每次检索都要过滤 —— 漏一处（分页、计数、高亮片段）就泄露
  · 过滤是"加法式"的：要记得对每个输出都做
  · 权限和内容混在一起，审计困难

GitHub 的答案是**多库**：

  · 权限绑在**库**上，不落在条目上
  · 没权限的库**根本不打开** —— 物理上拿不到，而不是"拿到了再藏起来"
  · 隔离天然、实现简单、审计容易

━━━ 跨库搜索怎么保证不越权 ━━━

**权限集合不出服务端，也不接受调用方输入。**

    token → 用户 → 可读库集合 → 逐个库搜（没权限的不打开）→ 合并 + 标来源

调用方**不能传 "我要搜哪些库"** —— 只要能传，就能越权。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .config import Config
from .errors import ErrorCode, ToolError
from .tenant import Library, TenantStore, User, can_write, visible_libraries
from .vault import VERSIONED_DIRNAME, Vault, now_iso, share_visible

logger = logging.getLogger(__name__)


class LibrarySet:
    """一组资源库 —— 每个库一个目录、一个 git 仓库、一份 Vault。

    ━━━ 惰性打开 ━━━

    库按需创建 Vault（首次访问时）。理由：可能有几十个库，
    但一次请求只碰得到少数几个；全部预先打开会浪费句柄和内存。

    惰性同时保证了**没权限的库不会被打开** —— 连目录都不会碰。
    """

    def __init__(self, home: Path | str, store: TenantStore) -> None:
        self.home = Path(home).expanduser().resolve()
        self.store = store
        #: {库 id: Vault}
        self._vaults: dict[str, Vault] = {}
        self.libs_dir = self.home / "libs"
        self.libs_dir.mkdir(parents=True, exist_ok=True)

    def path_of(self, lib: Library) -> Path:
        """库的目录。

        用库 id 作为目录名（已在 TenantStore 里做过合法性校验：
        只允许字母数字连字符下划线，所以不存在路径穿越）。
        """
        return self.libs_dir / lib.id

    def vault(self, lib: Library) -> Vault:
        """取库的 Vault（惰性创建 + 初始化 git）。

        ⚠️ 调用方必须**先确认有权限**再调这里 —— 本方法不做权限检查。
        权限检查集中在 Tenant 层（见 `Tenant.vault_for`），
        这样"检查哪些库"只有一处实现，不会漏。

        v2（S-8.2）：所有库共用**一个** `_versioned` 版本库，
        目录放在租户根而不是 `libs/` 里 —— 放 libs/ 会被当成一个库。
        """
        v = self._vaults.get(lib.id)
        if v is None:
            path = self.path_of(lib)
            keep = 3
            try:
                keep = int(Config(path).settings().get("history_keep", 3))
            except Exception:  # noqa: BLE001 - 配置读不出来就用缺省，别挡住开库
                pass
            v = Vault(
                path,
                versioned_dir=self.home / VERSIONED_DIRNAME,
                history_keep=keep,
                lib_id=lib.id,
            )
            v.init_repo()
            self._vaults[lib.id] = v
            logger.info("已打开库 %s（%s）", lib.id, lib.owner)
        return v

    def close_all(self) -> None:
        self._vaults.clear()


class Tenant:
    """把一个请求（已解析出用户）映射到他能用的库。

    ━━━ 这是权限的唯一入口 ━━━

    所有需要访问记忆的操作都必须经过这里的 ``vault_for`` /
    ``libs_for`` —— 它们是**唯一**算权限的地方。

    这样"某个操作忘了检查权限"这个最常见的漏洞类型就不会发生：
    因为除了这里，没有别的地方能拿到 Vault。
    """

    def __init__(self, home: Path | str, store: TenantStore, libs: LibrarySet) -> None:
        self.home = Path(home).expanduser().resolve()
        self.store = store
        self.libs = libs

    # ────────────────────── 权限计算 ──────────────────────

    def user(self, name: str) -> User:
        u = self.store.user(name)
        if u is None:
            # ⚠️ 这里必须用 NOT_FOUND 而不是 INVALID_ARGUMENT：
            # 用户不存在是"查不到"而不是"参数格式错"。
            # 而且调用方（工具层）按 code 决定要不要重试 ——
            # NOT_FOUND 明确表示"别重试了"。
            #
            # 曾经这里写的是 INVALID_REQUEST（枚举里根本没这个名字）——
            # 结果是**用户被删后访问任何库都抛 AttributeError**（500 而不是 403）。
            # 实测被测试抓到。
            raise ToolError(
                ErrorCode.NOT_FOUND,
                f"未知用户：{name}",
                hint="用户不存在或已被删除",
            )
        return u

    def readable(self, user_name: str) -> list[Library]:
        """该用户**可读**的库集合。

        ⚠️ 返回值只在服务端内部使用，**不返回给调用方**。
        """
        u = self.user(user_name)
        return visible_libraries(u, self.store.all_libs())

    def writable(self, user_name: str) -> list[Library]:
        """该用户**可写**的库集合。"""
        u = self.user(user_name)
        return [
            lib for lib in self.store.all_libs()
            if can_write(u, lib)
        ]

    def vault_for(self, user_name: str, lib_id: str, *, need_write: bool = False) -> Vault:
        """取一个库的 Vault，**顺带做权限检查**。

        这是唯一的取库入口 —— 有权限检查就不会被绕过。

        Args:
            need_write: True 时额外要求写权限。

        Raises:
            ToolError(FORBIDDEN): 无权限。
        """
        user = self.user(user_name)
        lib = self.store.lib(lib_id)
        if lib is None:
            raise ToolError(
                ErrorCode.NOT_FOUND,
                f"库不存在：{lib_id}",
                hint="用 memory_libs 看有哪些库",
            )

        allowed = self.readable(user_name)
        if lib.id not in {x.id for x in allowed}:
            # ⚠️ 错误信息**不透露库是否存在的内容细节** ——
            # 只说"无权访问"，避免把库的存在性/规模泄露给无权限的人。
            raise ToolError(
                ErrorCode.FORBIDDEN,
                f"无权访问库「{lib.label or lib.id}」",
                hint=f"你是 {user.name}（组：{', '.join(user.groups) or '无'}）",
            )

        if need_write and not can_write(user, lib):
            # 区分"读都没权限"和"只读库"—— 后者的提示要说明是设计如此
            if lib.kind == "company" and not lib.writable_by_members:
                raise ToolError(
                    ErrorCode.FORBIDDEN,
                    f"库「{lib.label or lib.id}」是只读的",
                    hint="公司级库默认只读（防污染）。需要修改请联系管理员。",
                )
            raise ToolError(
                ErrorCode.FORBIDDEN,
                f"你对库「{lib.label or lib.id}」只有读权限",
                hint="该库未开放成员写入",
            )

        return self.libs.vault(lib)

    def default_write_lib(self, user_name: str) -> Library:
        """用户不指定库时，写到哪里？

        ━━━ 默认写自己的个人库 ━━━

        理由：写是最容易"污染别人"的操作。默认落自己名下，
        永远不会误改公司规范或同事的东西。要往上写必须**显式指定库**。
        """
        u = self.user(user_name)
        own = self.store.lib(f"user-{u.name}")
        if own is not None and can_write(u, own):
            return own
        # 兜底：第一个能写的库
        w = self.writable(user_name)
        if not w:
            raise ToolError(
                ErrorCode.FORBIDDEN,
                "你没有可写的库",
                hint="联系管理员为你分配一个库",
            )
        return w[0]

    # ────────────────────── 跨库操作 ──────────────────────

    def shared_incoming(self, user_name: str) -> list[Library]:
        """经**目标空间授权**可以跨空间读取的源库（v2 S-10.2）。

        ━━━ 规则 ━━━

        我可读的每个库，其 ``_config/space.json`` 的
        ``accept_shared_from`` 列出的**源库** —— 源库里档位为 ``shared``
        的条目，才允许进入我的检索面。

        方向是**接收方 opt-in**（决策 D-2 的"目标空间授权"）：
        如果我可读的库里没人写"我接受某某空间的共享"，那这里就是空的，
        任何别的库都不会被打开。反过来，源空间单方面把条目设成
        ``shared`` **不构成**我能读到它的理由。

        ⚠️ 这是系统里**唯一**一条"打开我没有读权限的库"的路径，
        而且打开时固定 ``cross_space=True`` —— 该模式下只可能返回
        ``shared`` 档位的条目，``mine``/``space`` 在 vault 层就被挡。
        权限判定仍然只有一处（这里 + :meth:`vault.share_visible`），
        新增跨库入口时**必须**带上 cross_space，别调 ``vault()`` 后裸搜。
        """
        mine = {lib.id for lib in self.readable(user_name)}
        wanted: set[str] = set()
        for lib in self.readable(user_name):
            # 只读我**有权限**的库的配置，不去碰别人空间目录里的文件
            wanted.update(Config(self.libs.path_of(lib)).accept_shared_from())

        out: list[Library] = []
        for lib_id in sorted(wanted - mine):
            lib = self.store.lib(lib_id)
            if lib is not None:
                out.append(lib)
        return out

    def search_all(
        self,
        user_name: str,
        query: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """在**用户可读的全部库**里搜索，合并结果并标出来源。

        ━━━ 安全要点（这是整个系统最容易出漏洞的地方）━━━

        1. **调用方不能指定要搜哪些库** —— 没有任何参数能影响这个集合
        2. **逐库搜，没权限的库根本不打开**（不是"搜了再过滤"）。
           v2 有一个**受控例外**：:meth:`shared_incoming` —— 我可读的库
           显式 ``accept_shared_from`` 过的源库会以 ``cross_space=True``
           打开，那个模式下 vault 只可能返回 ``shared`` 档位条目。
           除这条之外，新增跨库入口都不得绕过 :meth:`readable`。
        3. **计数只算有权限的** —— 否则"共 42 条"会泄露无权库的规模
        4. 结果**标上来库** —— 让用户能判断该采信哪个
        5. **``viewer`` 由鉴权结果注入，不接受调用方传**（v2 S-10.2）——
           能自己填身份就等于没有共享档位

        第 3 条容易被忽略：如果先全搜再过滤，"总数"往往已经算错了，
        而那个数字本身就是信息泄露。
        """
        # 身份覆盖而不是 setdefault：调用方（含测试与内部通道）传进来的
        # viewer 一律作废 —— 它是权限判定，不是检索偏好。
        kw = dict(kwargs)
        kw["viewer"] = user_name

        libs = self.readable(user_name)
        merged: list[dict[str, Any]] = []
        total = 0
        per_lib: list[dict[str, Any]] = []

        for lib in libs:
            try:
                v = self.libs.vault(lib)
                r = v.search(query, **kw)
            except Exception as exc:  # noqa: BLE001
                # 单个库出问题不该让整个搜索失败 —— 但要说出来
                logger.warning("库 %s 搜索失败: %s", lib.id, exc)
                per_lib.append({"lib": lib.id, "error": str(exc)[:120]})
                continue

            for item in r.get("results", []):
                item["_lib"] = lib.id
                item["_lib_label"] = lib.label or lib.id
                item["_lib_owner"] = lib.owner
                merged.append(item)
            total += r.get("total", 0)
            per_lib.append({"lib": lib.id, "label": lib.label or lib.id,
                            "count": r.get("total", 0)})

        # 经目标空间授权的跨空间共享面（v2 S-10.2）：只可能出 shared 条目。
        for lib in self.shared_incoming(user_name):
            try:
                v = self.libs.vault(lib)
                r = v.search(query, cross_space=True, **kw)
            except Exception as exc:  # noqa: BLE001
                logger.warning("共享源库 %s 搜索失败: %s", lib.id, exc)
                continue

            items = r.get("results", [])
            for item in items:
                item["_lib"] = lib.id
                item["_lib_label"] = lib.label or lib.id
                item["_lib_owner"] = lib.owner
                item["_shared_incoming"] = True   # 告诉调用方"这是跨空间捞来的"
                merged.append(item)
            total += r.get("total", 0)
            if items:
                per_lib.append({"lib": lib.id, "label": lib.label or lib.id,
                                "count": r.get("total", 0), "shared": True})

        # 按相关度（vault.search 已排序，但跨库需要重新合并排序）：
        # 用"最近更新"作次级键，保证结果稳定
        merged.sort(key=lambda x: (x.get("updated") or x.get("created") or ""), reverse=True)

        return {
            "ok": True,
            "query": query,
            "total": total,
            "count": len(merged),
            "libraries": per_lib,
            # 提示调用方"这些结果来自多个库"——它需要知道该采信哪个
            "hint": "结果来自多个库，看 _lib_label 判断来源与可信度",
            "results": merged[: kwargs.get("limit", 20)],
        }

    def note_for_user(self, user_name: str) -> dict[str, Any]:
        """当前用户的身份与可见范围（面板/工具用它显示"我是谁、能看什么"）。

        这里**可以**返回库列表 —— 因为只返回他自己有权限的那些，
        不构成泄露（他知道自己有什么权限是应该的）。
        """
        u = self.user(user_name)
        readable = self.readable(user_name)
        writable = {x.id for x in self.writable(user_name)}
        return {
            "ok": True,
            "user": {
                "name": u.name,
                "display": u.display or u.name,
                "role": u.role,
                "groups": list(u.groups),
                "is_admin": u.is_admin,
            },
            "libraries": [
                {
                    "id": lib.id,
                    "label": lib.label or lib.id,
                    "owner": lib.owner,
                    "kind": lib.kind,
                    "writable": lib.id in writable,
                }
                for lib in readable
            ],
        }
