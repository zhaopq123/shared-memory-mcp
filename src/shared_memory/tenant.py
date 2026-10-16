"""租户层 —— 用户、资源库、权限解析。

━━━ 这一层回答三个问题 ━━━

  1. **你是谁**（认证）—— 由 token 反查出用户
  2. **你能读哪些库**（授权）—— 由用户身份算出库集合
  3. **你能写哪些**（写权限）—— 逐库判定

━━━ 核心设计：权限集合不出服务端 ━━━

调用方（agent）**不能说"我要搜哪些库"** —— 只要能说，它就能越权。

正确模型：
    token → 用户 → 可读库集合 → 逐个库搜（没权限的库**根本不打开**）

而不是"搜全部再过滤"——那样过滤逻辑一旦漏一处（分页、计数、高亮），
就泄露了。**没权限就物理拿不到，比"拿到了再藏起来"可靠一个量级。**

━━━ 资源库的三种归属 ━━━

  · ``company``         公司级 —— 全员可读；只有 admin 能写
  · ``group:<组名>``    小组级 —— 组员可读写（默认）
  · ``user:<用户名>``   个人级 —— 只有本人

权限判定因此是**纯函数**（用户 + 库归属 → 能不能读写），不需要维护
一张巨大的权限表。"新人只读公司库"就是给他一个能读 company 的 token 而已。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: scrypt 参数。选这套的理由：
#:   · n=2**14（16384）—— 交互式登录的常用强度，单次哈希约几十毫秒
#:   · r=8, p=1 —— scrypt 的常规配置
#:   · 显式存参数而不是写死常量 —— 将来提高强度时，老密码仍能验证
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32


# ══════════════════════ 用户 ══════════════════════


@dataclass
class User:
    """一个用户（人）。

    ``groups`` 是他所属的组（可以有多个）—— 决定他能读哪些 group 库。
    ``role`` 是 ``admin`` / ``member``：admin 能写公司库、能管用户。
    """

    name: str
    #: 形如 ``scrypt$n$r$p$salt_hex$hash_hex``；空串表示"未设密码"
    password: str = ""
    role: str = "member"          # admin | member
    groups: list[str] = field(default_factory=list)
    display: str = ""
    created: float = field(default_factory=time.time)
    disabled: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    # ── 密码 ──

    @staticmethod
    def hash_password(plain: str) -> str:
        """把明文密码hash成可存储的字符串。

        为什么用 scrypt 而不是 sha256：sha256 是**为速度设计**的，
        显卡每秒能算几十亿次，密码字典攻击成本极低。
        scrypt 是**为抗暴力设计**的（内存硬），同样的显卡每秒只能算几千次。
        """
        salt = secrets.token_bytes(16)
        dk = hashlib.scrypt(
            plain.encode("utf-8"),
            salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN,
        )
        return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${dk.hex()}"

    def check_password(self, plain: str) -> bool:
        """校验密码（常数时间比较）。"""
        if not self.password or not plain:
            return False
        try:
            algo, n, r, p, salt_hex, hash_hex = self.password.split("$")
            if algo != "scrypt":
                return False
            dk = hashlib.scrypt(
                plain.encode("utf-8"),
                salt=bytes.fromhex(salt_hex),
                n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(hash_hex)),
            )
            return hmac.compare_digest(dk.hex(), hash_hex)
        except (ValueError, TypeError):
            return False


# ══════════════════════ 资源库 ══════════════════════


@dataclass
class Library:
    """一个资源库（记忆库）。

    ━━━ 为什么归属是一个字符串而不是权限表 ━━━

    ``owner`` 只有三种形态：
      · ``company``        全员可读、admin 可写
      · ``group:<组名>``   该组成员可读写
      · ``user:<用户名>``  仅本人

    这样「某某能不能读这个库」是一次**纯计算**，不需要查询权限表 ——
    权限表是错的来源（会漏、会不一致），而计算不会。

    ━━━ ``created_by``：为什么需要它（实测踩出的缺陷）━━━

    最初只按 ``owner`` 判断"谁能改共享范围"，于是：

        bob 建库（owner=user:bob）→ 共享给组（owner=group:研发）
        → bob 想收回 → 检查发现 owner 不是 user:bob → **拒绝**

    **结果：一旦共享出去就收不回来**（只有管理员能救）。

    这是设计缺陷：共享必须**可逆**，否则用户不敢共享。

    所以单独记 ``created_by``：它回答"**这个库是谁的**"，
    而 ``owner`` 回答"**现在谁能读**"。两件事，两个字段。
    """

    id: str
    label: str = ""
    owner: str = "company"
    #: 是否允许成员写入（false = 只读库，比如公司规范）
    writable_by_members: bool = True
    #: 对 Agent 隐身（人可用，agent 不可见）
    hidden_from_agents: bool = False
    created: float = field(default_factory=time.time)
    #: 创建者用户名。**不受 owner 变化影响** ——
    #: 它决定"谁有权改这个库的共享范围"（见 can_manage_lib）。
    created_by: str = ""

    @property
    def kind(self) -> str:
        return self.owner.split(":", 1)[0]

    @property
    def owner_name(self) -> str:
        parts = self.owner.split(":", 1)
        return parts[1] if len(parts) > 1 else ""


# ══════════════════════ 权限判定（纯函数） ══════════════════════


def can_read(user: User, lib: Library) -> bool:
    """判断用户能否**读**某个库。

    这是整个权限系统的核心 —— 它必须是纯函数、无副作用、易审计。
    任何"先查表再判断"的实现都会引入不一致的风险。

    ━━━ 管理员的边界（一个有意识的取舍）━━━

    管理员能读所有公司库与组库，但**读不了他人的个人库**。

    理由：个人库的意义就在私密。如果管理员能读，用户就不会往里放
    真私密的东西，这个功能就废了。管理员真正需要的是"管理能力"
    （删库、限容量、看统计），不是"读内容"。
    """
    if user.disabled:
        return False
    if lib.kind == "company":
        return True                      # 公司级：全员可读
    if lib.kind == "group":
        # 组员 + 管理员。管理员能进任意组（管理场景需要）。
        return lib.owner_name in user.groups or user.is_admin
    if lib.kind == "user":
        # ⚠️ 注意这里**没有** admin 特例 —— 是刻意的，见 docstring
        return lib.owner_name == user.name
    return False


def can_write(user: User, lib: Library) -> bool:
    """判断用户能否**写**某个库。

    ━━━ 公司库为什么默认只读 ━━━

    它是所有新人看到的第一份材料。让十几个人随手写会让它烂掉 ——
    这是"防污染"而非"防泄密"。要放开请把 ``writable_by_members`` 置 true。
    """
    if user.disabled:
        return False
    if not can_read(user, lib):
        return False                     # 读都读不了，当然不能写
    if lib.kind == "company":
        # 管理员始终能写（管理场景需要）
        return bool(lib.writable_by_members) or user.is_admin
    return bool(lib.writable_by_members)


def visible_libraries(user: User, libs: list[Library]) -> list[Library]:
    """算出一个用户**能读**的全部库。

    ⚠️ 这个返回值**永远不出服务端** —— 它只用于服务端内部循环搜索。
    不返回给调用方、不接受调用方输入。这是防越权的关键约束。
    """
    return [lib for lib in libs if can_read(user, lib)]


def writable_libraries(user: User, libs: list[Library]) -> list[Library]:
    """能写的库（``memory_write`` 只能落在这些库里）。"""
    return [lib for lib in libs if can_write(user, lib)]


# ══════════════════════ 持久化 ══════════════════════


class TenantStore:
    """用户与库的持久化。

    ━━━ 为什么存文件而不是数据库 ━━━

    与记忆本身同一个理由：**纯文本、可读、可手改、可 git 版本化**。
    一个几十人或几十库的规模，一个 JSON 文件完全够用，
    而它带来的好处是"配置出问题时有救"（能直接看、直接改）。

    存储位置：``<home>/tenants.json``，权限 600（含密码哈希）。
    """

    def __init__(self, home: Path | str) -> None:
        self.home = Path(home).expanduser().resolve()
        self.path = self.home / "tenants.json"
        self.users: dict[str, User] = {}
        self.libs: dict[str, Library] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # 配置坏了不该让服务起不来 —— 但必须显式报出来，
            # 否则会变成"所有人都突然没权限了"这种难查的问题。
            logger.error("租户配置读取失败（将使用空配置）: %s", exc)
            return
        for name, d in (raw.get("users") or {}).items():
            try:
                self.users[name] = User(**d)
            except TypeError as exc:
                logger.warning("跳过损坏的用户条目 %s: %s", name, exc)
        for lid, d in (raw.get("libs") or {}).items():
            try:
                self.libs[lid] = Library(**d)
            except TypeError as exc:
                logger.warning("跳过损坏的库条目 %s: %s", lid, exc)

    def save(self) -> None:
        """原子写（临时文件 + rename），避免写一半崩掉导致配置损坏。"""
        self.home.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "users": {k: asdict(v) for k, v in self.users.items()},
            "libs": {k: asdict(v) for k, v in self.libs.items()},
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    # ── 初始化 ──

    def ensure_bootstrap(self, *, admin_name: str = "admin") -> tuple[User, str] | None:
        """首次运行时创建管理员与默认库。

        Returns:
            ``(用户, 明文密码)`` —— 仅在**新建**时返回密码，之后只存哈希。
            调用方负责只显示一次。已经初始化过则返回 ``None``。
        """
        if self.users:
            return None
        password = secrets.token_urlsafe(12)
        admin = User(
            name=admin_name,
            password=User.hash_password(password),
            role="admin",
            display="管理员",
        )
        self.users[admin_name] = admin
        # 默认建三个库：公司 / 一个示例小组 / 管理员的个人库
        self.libs["company"] = Library(
            id="company", label="公司", owner="company", writable_by_members=False,
        )
        self.libs[f"user-{admin_name}"] = Library(
            id=f"user-{admin_name}", label=f"{admin_name} 的个人库",
            owner=f"user:{admin_name}",
        )
        self.save()
        return admin, password

    # ── 查询 ──

    def user(self, name: str) -> User | None:
        return self.users.get((name or "").strip())

    def lib(self, lid: str) -> Library | None:
        return self.libs.get((lid or "").strip())

    def all_libs(self) -> list[Library]:
        return list(self.libs.values())

    # ── 变更 ──

    def add_user(
        self, name: str, password: str, *, role: str = "member",
        groups: list[str] | None = None, display: str = "",
    ) -> tuple[User | None, str]:
        """新增用户。返回 ``(用户, 明文密码)``；密码留空则随机生成。"""
        name = (name or "").strip()
        if not name:
            return None, ""
        if name in self.users:
            return None, "用户已存在"
        plain = password or secrets.token_urlsafe(12)
        u = User(
            name=name,
            password=User.hash_password(plain),
            role=role if role in ("admin", "member") else "member",
            groups=list(groups or []),
            display=display or name,
        )
        self.users[name] = u
        # 每个用户自动有自己的个人库
        pid = f"user-{name}"
        self.libs.setdefault(pid, Library(
            id=pid, label=f"{u.display} 的个人库", owner=f"user:{name}",
        ))
        self.save()
        return u, plain

    def remove_user(self, name: str) -> bool:
        """删用户（同时删他的个人库登记，但不删文件）。

        ━━━ ⚠️ 调用方**必须**同时吊销他的 token ━━━

        删用户不吊销 token 是个**安全隐患**：token 还在「有效」列表里，
        持有它的人仍能发起请求。虽然权限计算时查不到用户会拒绝，
        但那个状态本身是错的 —— 而且日志里会显示"未知用户"，
        排查时容易误判成"权限系统坏了"。

        本方法不直接吊销，因为 token 存在 ``Auth`` 里（另一层）——
        跨层调用会让依赖关系混乱。正确做法是调用方（``TenantHub``
        或面板）在删用户后调 ``auth.revoke_token(name)``。

        见 ``tests/test_tenant_lifecycle.py`` 的回归测试。
        """
        if name not in self.users:
            return False
        del self.users[name]
        self.libs.pop(f"user-{name}", None)
        self.save()
        return True

    def add_lib(
        self, lid: str, *, label: str = "", owner: str = "company",
        writable_by_members: bool = True, hidden_from_agents: bool = False,
        created_by: str = "",
    ) -> Library | None:
        """建库。

        Args:
            created_by: 创建者。它决定"谁有权改共享范围" —— 与 owner
                （谁能读）是两件事，见 ``Library`` 的 docstring。
        """
        lid = (lid or "").strip()
        if not lid or lid in self.libs:
            return None
        lib = Library(
            id=lid, label=label or lid, owner=owner,
            writable_by_members=writable_by_members,
            hidden_from_agents=hidden_from_agents,
            created_by=created_by,
        )
        self.libs[lid] = lib
        self.save()
        return lib

    def set_password(self, name: str, plain: str) -> bool:
        u = self.users.get(name)
        if u is None or not plain:
            return False
        u.password = User.hash_password(plain)
        self.save()
        return True
