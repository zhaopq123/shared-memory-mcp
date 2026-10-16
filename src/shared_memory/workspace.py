"""工作空间 —— 把「工作空间 = 一个库」这个约定变成明确概念。

━━━ 为什么需要这一层 ━━━

之前"工作空间"只是记忆上的一个**标签**（`workspace` 字段），
它**不控制任何权限**：

    workspace: 乱七八糟     ← 只是文字
    lib: user-admin        ← 这才是权限边界

于是"这个工作空间不共享"做不到 —— 能读 `user-admin` 的人
能看到里面**所有**工作空间的记忆。

━━━ 解法：工作空间 → 库 ━━━

一个工作空间一个库。共享范围 = 库的 `owner`：

    owner="user:admin"    → 只有我能读（不共享）
    owner="group:研发"    → 组内可读
    owner="company"       → 全员可读

改共享范围 = 改 `owner` 一个字段，**不用搬数据**。
`user:admin` → `group:研发` → `company` 就是逐步放开的过程。

━━━ 为什么不做「库内按工作空间过滤」━━━

那是我们明确否决过的路：

  · 每次检索都要按 ACL 过滤 —— 漏一处（分页、计数、高亮）就泄露
  · 过滤是"加法式"的：要记得对每个输出都做
  · 而"一个工作空间一个库"是"减法式"的：**没权限的库根本不打开**

GitHub 用的是后者（多库 + 库级权限），我们跟它一致。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from .errors import ErrorCode, ToolError
from .tenant import Library, User, can_read, can_write

logger = logging.getLogger(__name__)

#: 工作空间库的 id 前缀。
#:
#: 用前缀而不是另开一个类型，理由：库已经是权限单位了，
#: 工作空间库就是"归属某个工作空间"的库 —— 不需要新概念。
WS_PREFIX = "ws-"

#: 工作空间名 → 库 id 时允许的字符。
#: 中文要保留（用户的工作空间名就是中文），所以不用 \w（那个不含中文标点）。
_SAFE = re.compile(r"[^\w\u4e00-\u9fff-]+")


def ws_lib_id(workspace: str) -> str:
    """工作空间名 → 库 id。

    ━━━ 为什么要转义 ━━━

    库 id 会变成**目录名**。工作空间名是用户随便起的（可能有空格、
    斜杠、点），直接当目录名会有两个问题：
      · 路径穿越（`../` 之类）
      · 跨平台不合法（Windows 不允许 `:` `*` `?` 等）

    所以做白名单化：只留字母数字、下划线、中文、连字符。
    这跟 `vault._slug` 同口径。
    """
    slug = _SAFE.sub("-", (workspace or "").strip())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return WS_PREFIX + (slug or "unnamed")


def lib_id_to_ws(lib_id: str) -> str:
    """库 id → 工作空间名（去掉前缀）。

    注意：这是**有损**的（原名里的特殊字符已被替换），
    所以只用于显示兜底。真实名应读库的 label。
    """
    return lib_id[len(WS_PREFIX):] if lib_id.startswith(WS_PREFIX) else lib_id


@dataclass
class ShareTarget:
    """一个可选的共享范围（给面板下拉用）。"""

    owner: str
    label: str
    desc: str


def share_targets(user: User, libs: list[Library]) -> list[ShareTarget]:
    """列出当前用户**能设置**的共享范围。

    ━━━ 为什么按用户算而不是写死 ━━━

    因为"能共享给谁"取决于他属于哪些组：
    一个不属于任何组的人，没有"共享给组"这个选项（那会建出一个
    谁都读不到的库 —— 看起来成功，实际是个黑洞）。

    这个原则叫**不提供会导致无效结果的选项**。
    """
    out = [
        ShareTarget(
            owner=f"user:{user.name}",
            label="仅自己",
            desc="不共享。只有你能读，管理员也读不到。",
        )
    ]
    for g in sorted(set(user.groups or [])):
        out.append(ShareTarget(
            owner=f"group:{g}",
            label=f"小组：{g}",
            desc=f"{g} 组的成员都能读。",
        ))
    # 只有管理员能设成公司级（否则 member 就能把内容推给全公司）
    if user.is_admin:
        out.append(ShareTarget(
            owner="company",
            label="公司",
            desc="全员可读。注意：公司库默认只读，成员不能写。",
        ))
    return out


def can_manage_lib(user: User, lib: Library) -> bool:
    """判断用户能否改这个库的归属（共享范围）。

    ━━━ 规则 ━━━

      · 管理员：能改任何库
      · **创建者**：能改自己建的（决定要不要共享出去、要不要收回）
      · 个人库的所有者：能改（兼容手工建的库 —— 它们没有 created_by）
      · 其他人：不能（否则有人能把你私人的东西改成公开）

    ━━━ 为什么看 created_by 而不是 owner（实测踩出的缺陷）━━━

    最初只看 ``owner``，于是：

        bob 建库（owner=user:bob）→ 共享给组（owner=group:研发）
        → bob 想收回 → owner 不是 user:bob 了 → **拒绝**

    **一旦共享出去就收不回来。** 这是设计缺陷：共享必须可逆，
    否则用户不敢共享。

    ``created_by`` 回答"这个库是谁的"（不变），
    ``owner`` 回答"现在谁能读"（会变）—— 两件事，两个字段。
    """
    if user.disabled:
        return False
    if user.is_admin:
        return True
    # 创建者永远能管自己的库（即使已经共享出去）
    if lib.created_by and lib.created_by == user.name:
        return True
    # 兼容：手工建的库没有 created_by，退回按 owner 判断
    if not lib.created_by and lib.kind == "user" and lib.owner_name == user.name:
        return True
    return False


class WorkspaceManager:
    """工作空间（库）的创建与共享范围管理。

    这是**唯一**能改库归属的地方 —— 与 `Tenant.vault_for` 是
    "唯一能取库的地方"同一思路：关键操作只有一个入口，才不会漏检查。
    """

    def __init__(self, store: Any, tenant: Any) -> None:
        self.store = store
        self.tenant = tenant

    # ────────────────────── 查询 ──────────────────────

    def list_for(self, user_name: str) -> dict[str, Any]:
        """列出当前用户可见的工作空间及其共享状态。

        返回里带 `can_manage` —— 面板据此决定"改共享范围"按钮是否可点。
        **前端不自己算权限**（那会出现"前端说能改、后端拒绝"的不一致）。
        """
        user = self.tenant.user(user_name)
        items = []
        for lib in self.store.all_libs():
            readable = can_read(user, lib)
            items.append({
                "lib": lib.id,
                "label": lib.label or lib.id,
                "owner": lib.owner,
                "kind": lib.kind,
                "owner_name": lib.owner_name,
                "writable": can_write(user, lib),
                "readable": readable,
                "can_manage": can_manage_lib(user, lib),
                "is_workspace": lib.id.startswith(WS_PREFIX),
                # 共享范围的人类可读说明
                "share_label": _share_label(lib),
            })
        # 可管理的排前面（面板上用户最关心的就是自己能动的东西）
        items.sort(key=lambda x: (not x["can_manage"], not x["readable"], x["label"]))
        return {
            "ok": True,
            "user": user_name,
            "count": len(items),
            "libraries": items,
            "share_targets": [
                {"owner": t.owner, "label": t.label, "desc": t.desc}
                for t in share_targets(user, self.store.all_libs())
            ],
        }

    # ────────────────────── 变更 ──────────────────────

    def create(
        self,
        user_name: str,
        *,
        workspace: str,
        share: str = "",
        label: str = "",
    ) -> dict[str, Any]:
        """为一个工作空间建库。

        Args:
            workspace: 工作空间名（中文可以）。
            share: 共享范围（``user:<名>`` / ``group:<组>` / ``company``）。
                留空 = 仅自己（最安全的默认值）。
            label: 显示名，默认用工作空间名。
        """
        user = self.tenant.user(user_name)
        ws = (workspace or "").strip()
        if not ws:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, "工作空间名不能为空")

        lid = ws_lib_id(ws)
        if self.store.lib(lid) is not None:
            raise ToolError(
                ErrorCode.CONFLICT,
                f"工作空间「{ws}」的库已存在（{lid}）",
                hint="用 memory_workspace_share 改它的共享范围，或换一个名字",
            )

        # 默认：仅自己。这是最安全的默认值 —— 共享要显式选。
        owner = (share or f"user:{user.name}").strip()
        self._validate_share(user, owner)

        lib = self.store.add_lib(
            lid,
            label=label or ws,
            owner=owner,
            writable_by_members=(owner == "company") is False,
            # ⚠️ 记录创建者 —— 它决定"谁能改共享范围"。
            # 不记的话，一旦共享给组，原主人就收不回来了（实测踩过）。
            created_by=user.name,
        )
        if lib is None:
            raise ToolError(ErrorCode.INTERNAL_ERROR, "建库失败")
        logger.info("用户 %s 建了工作空间库 %s（share=%s）", user_name, lid, owner)
        return {
            "ok": True,
            "lib": lib.id,
            "workspace": ws,
            "label": lib.label,
            "owner": lib.owner,
            "created_by": lib.created_by,
            "share_label": _share_label(lib),
            "note": "库已创建。记忆写入时传 lib=" + lib.id + " 即落进这个工作空间。",
        }

    def share(
        self, user_name: str, *, lib: str, share: str
    ) -> dict[str, Any]:
        """改一个库的共享范围。

        ━━━ 这个操作的性质 ━━━

        它**能决定泄露**（把私密的改成公开），所以：
          · 只有库所有者或管理员能做（`can_manage_lib`）
          · 变更入 git（可追溯谁在什么时候放开的）
          · 返回里明确说清"现在谁能读了"
        """
        user = self.tenant.user(user_name)
        target = self.store.lib(lib)
        if target is None:
            raise ToolError(ErrorCode.NOT_FOUND, f"库不存在：{lib}")

        if not can_manage_lib(user, target):
            raise ToolError(
                ErrorCode.FORBIDDEN,
                f"你不能改「{target.label or target.id}」的共享范围",
                hint="只有库所有者或管理员能改 —— "
                     "因为改归属等于决定谁能看到内容",
            )

        owner = (share or "").strip()
        self._validate_share(user, owner)

        before = _share_label(target)
        target.owner = owner
        # 公司库默认只读（防污染）；其它归属允许成员写
        if owner == "company":
            target.writable_by_members = False
        else:
            target.writable_by_members = True
        self.store.save()

        logger.warning(
            "用户 %s 改了 %s 的共享范围：%s → %s",
            user_name, target.id, before, _share_label(target),
        )
        return {
            "ok": True,
            "lib": target.id,
            "before": before,
            "after": _share_label(target),
            "owner": target.owner,
            "writable_by_members": target.writable_by_members,
            "note": "已生效。没权限的人现在读不到了（物理上打不开这个库）。",
        }

    def _validate_share(self, user: User, owner: str) -> None:
        """校验共享范围合法且当前用户有权设置。

        ━━━ 为什么不能随便填 ━━━

        如果允许任意 owner 字符串：
          · 打错一个字母 → 建出一个谁都读不到的库（黑洞，看起来成功）
          · 或者更糟：写成一个不存在的组名 → 以为共享了实际没有

        所以必须**白名单校验**：只能是 user:<自己> / group:<自己的组> / company。
        """
        allowed = {t.owner for t in share_targets(user, self.store.all_libs())}
        if owner not in allowed:
            raise ToolError(
                ErrorCode.INVALID_ARGUMENT,
                f"无效的共享范围：{owner}",
                hint="可用值：" + "、".join(sorted(allowed)),
            )


def _share_label(lib: Library) -> str:
    """共享范围的人类可读说明。"""
    if lib.owner == "company":
        return "公司（全员可读）"
    if lib.kind == "group":
        return f"小组：{lib.owner_name}"
    if lib.kind == "user":
        return f"仅 {lib.owner_name} 自己"
    return lib.owner
