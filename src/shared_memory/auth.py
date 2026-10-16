"""鉴权 —— 凭据校验、身份解析、token 生命周期、限速。

━━━ 与 tenant.py 的分工 ━━━

  · ``tenant.py`` —— **你是谁、你能看哪些库**（用户与权限模型）
  · ``auth.py``   —— **如何证明你是你**（凭据），并把它解析成用户

所以这里的核心产出不是"通过/拒绝"，而是：**这个请求代表哪个用户**。

━━━ 为什么 token 必须绑定用户（而不是一个共享密钥）━━━

一个不绑人的 token 只能回答"你有权限吗"，回答不了"**你代表谁**"。
而多租户下后者更重要：

  同事的 agent 用共享 token 连上来 → 它读到了你的个人库 ——
  因为它不知道自己代表谁，系统也无法判断该给它看什么。

所以每个 token 都绑定一个**所有者**。这同时也让吊销变精确：
某人离职 → 吊销他的全部 token；某台机器丢了 → 只吊销那一把。

━━━ 危险默认必须消除 ━━━

**绑非 loopback 地址时，没有可用凭据就拒绝启动。**

以前是"没配 token 就无鉴权运行，只打 warning" —— 那等于默认裸奔。
用户在公司网里起服务，同事扫到 IP 就能读写全部记忆。
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

COOKIE_NAME = "shm_session"

#: 一次性登录票据有效期（秒）
LOGIN_TICKET_TTL = 120

#: 失败限速
FAIL_WINDOW_SECONDS = 300
#: 窗口内允许的失败次数。
#:
#: ━━━ 为什么从 20 提到 50（实测踩过）━━━
#: 一次普通的安全审计（穷举 20+ 个端点）就能把 20 打满，
#: 于是后续请求全 429 —— **包括带正确凭据的**。
#: 安全机制反而造成可用性故障，这是最不该出现的那种失败。
#:
#: 50 仍然足够防爆破：在线猜 64 位 hex token 的成功率是 1/16^64，
#: 猜几千次也不改变什么。真正要防的是"慢速持续试"（每秒一次试一小时），
#: 而 50 次/5 分钟 ≈ 每秒 0.17 次，早在那之前就被挡下。
FAIL_MAX_ATTEMPTS = 50
FAIL_BACKOFF_SECONDS = 30

#: 主密钥轮换的**宽限窗口**（秒）：旧密钥在这段时间内仍可用 ——
#: 让正在跑的请求把手上的活干完，而不是集体 401（见 rotate_master_key）。
MASTER_KEY_GRACE_SECONDS = 60
#: PIN 连续错几次锁（第 6 次被拒）。
PIN_MAX_ATTEMPTS = 5
#: PIN 锁定时长（秒）：到时**自动解除**，不需要管理员解锁。
PIN_LOCK_SECONDS = 600


# ══════════════════════ 绑定地址 ══════════════════════


def is_loopback_bind(host: str) -> bool:
    """绑定地址是否只对本机可见。这是「要不要强制鉴权」的唯一依据。

    为什么不用"内网/外网"：那个区分不可靠也不可判定 ——
    ``0.0.0.0`` 在公司网里就是"同事都能连"，在家庭路由后面才是"只有自己家"。

    而**绑定地址是确定的**：``127.0.0.1`` 由操作系统保证只有本机可连。

    ⚠️ **空串按"可达"处理**（返回 False）。理由：空串在 uvicorn 的
    语义里是"绑所有网卡"，而它在配置缺失/拼写错误时也会出现 ——
    保守判成可达，比误判成 loopback 安全。
    """
    h = (host or "").strip()
    if h == "localhost":
        return True
    if not h:
        return False                     # 空串 = 绑所有网卡，按可达处理
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        # 主机名无法保证只有本机可连 → 保守按可达
        return False


def is_publicly_reachable(host: str) -> bool:
    return not is_loopback_bind(host)


# ══════════════════════ 凭据 ══════════════════════


@dataclass
class Token:
    """一把 API token —— **绑定到用户**。

    ``label`` 用来区分设备/用途（"笔记本的 Cursor"、"手机"），
    这样泄露时能知道是哪一把、能单独吊销。
    """

    token: str
    user: str                     # 所有者用户名
    label: str = ""
    scope: str = "readwrite"      # readwrite | read
    created: float = field(default_factory=time.time)
    revoked_at: float | None = None
    last_used: float = 0.0
    #: 服务端生成/面板发放的凭据才由 tokens.json 管理。
    #: 构造 Auth 时显式注入的 token 默认视为服务管理 token；
    #: env token 与浏览器 session 必须由各自入口显式标记 managed=False。
    managed: bool = True

    @property
    def active(self) -> bool:
        return self.revoked_at is None

    def matches(self, candidate: str) -> bool:
        """常数时间比较，避免时序侧信道。

        ━━━ ⚠️ 必须编码成 bytes 再比（实测踩过）━━━

        ``hmac.compare_digest`` 对 **str** 只接受 ASCII —— 传中文会抛：

            TypeError: comparing strings with non-ASCII characters is not supported

        而 token 的**标签**恰恰常是中文（"笔记本"、"主账号"），
        ``revoke_token`` 会拿标签来调本方法 → 一按中文标签吊销就崩。

        解法：先 encode 成 bytes。bytes 比较没有字符集限制，
        而且常数时间的性质不变。
        """
        if not candidate or not self.token:
            return False
        try:
            return hmac.compare_digest(
                self.token.encode("utf-8"), candidate.encode("utf-8")
            )
        except (AttributeError, TypeError):
            # 非字符串（None、数字等）—— 当作不匹配，不抛
            return False

    @property
    def fingerprint(self) -> str:
        t = self.token
        return f"{t[:4]}…{t[-4:]}" if len(t) >= 8 else "…"


class Auth:
    """鉴权器：把凭据解析成身份，并管理生命周期。

    一个实例覆盖**所有**端点（面板 / API / MCP）——
    "有的端点有鉴权、有的没有"本身就是漏洞。
    """

    def __init__(
        self,
        *,
        bind_host: str,
        tokens: list[Token] | None = None,
        store: Any = None,
    ) -> None:
        self.bind_host = bind_host
        self._tokens: list[Token] = list(tokens or [])
        #: TenantStore（用户与库）。面板登录要用它校验密码。
        self.store = store
        #: 一次性登录票据 {ticket: (过期, user, source)}
        self._tickets: dict[str, tuple[float, str, str]] = {}
        #: 失败计数 {来源IP: [时间戳]}
        self._failures: dict[str, list[float]] = {}
        self.required = is_publicly_reachable(bind_host)
        #: token 持久化文件。None = 不持久化（内存态，重启即失效）。
        self.persist_path: Path | None = None
        #: 管理 token 生命周期的线程安全锁（面板并发创建/吊销时串行）。
        self._token_lock = threading.RLock()
        #: 外部注入 token 的持久化吊销墓碑：删掉环境配置后不再保留 active token，
        #: 但若同一 token 后续重新注入，也不能因重启而复活。
        self._revoked_external: dict[str, dict[str, Any]] = {}
        #: v2（S-6）：主密钥与 PIN 的落盘位置（由 attach_* 挂载）。
        self._master_path: Path | None = None
        self._pin_path: Path | None = None
        #: PIN 失败时间戳（内存态）。**不落盘**是有意的：重启即解锁，
        #: 而重启服务需要主机权限 —— 那把钥匙比 PIN 强得多。
        self._pin_failures: list[float] = []
        #: 两个 state 文件的 mtime 缓存（见 _load_state_file）。
        self._master_cache: tuple[Any, dict[str, Any]] | None = None
        self._pin_cache: tuple[Any, dict[str, Any]] | None = None

    # ────────────────────── token 持久化 ──────────────────────

    def attach_persistence(self, path: str | Path) -> int:
        """挂上持久化文件：先加载已有 token，之后增删都写回。

        ━━━ 为什么必须有（实测踩过）━━━

        面板发的 token 原来只存内存 —— 服务一重启（升级、重启、崩溃拉起）
        **全部失效**。症状对用户是"昨天还好好的，今天所有 agent 都 401"，
        而且新 token 只打印在启动日志里，没人盯着看。

        面板的 token 管理带 label（"笔记本的 Cursor"）和吊销 —— 那套语义
        本身就预设了凭据是**长期存在**的。不落盘等于自相矛盾。

        ━━━ 放哪 ━━━

        调用方传 ``<home>/state/tokens.json`` —— ``state/`` 是**非 git** 目录
        （digest 游标也在那），不会被任何库的 git 提交带走。
        **绝不能放进 libs/** —— 那是会 commit 的 vault，等于把密钥写进 git 历史。

        Returns:
            加载到的 token 条数（0 = 首次运行）。
        """
        self.persist_path = Path(path)
        if not self.persist_path.exists():
            return 0
        try:
            raw = json.loads(self.persist_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("schema") != "shared-memory-tokens/1":
                raise ValueError("不支持的 token 文件 schema；预期 shared-memory-tokens/1")
            stored_tokens = raw.get("tokens", [])
            stored_tombstones = raw.get("revoked_external", [])
            if not isinstance(stored_tokens, list):
                raise ValueError("tokens 应为数组")
            if not isinstance(stored_tombstones, list):
                raise ValueError("revoked_external 应为数组")

            def required_text(item: dict[str, Any], key: str) -> str:
                value = item.get(key)
                if not isinstance(value, str) or not value:
                    raise ValueError(f"{key} 必须是非空字符串")
                return value

            def optional_text(item: dict[str, Any], key: str, default: str = "") -> str:
                value = item.get(key, default)
                if not isinstance(value, str):
                    raise ValueError(f"{key} 必须是字符串")
                return value

            def finite_number(value: Any, key: str) -> float:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"{key} 必须是有限数字")
                try:
                    number = float(value)
                except OverflowError as exc:
                    raise ValueError(f"{key} 必须是有限数字") from exc
                if not math.isfinite(number):
                    raise ValueError(f"{key} 必须是有限数字")
                return number

            decoded_tokens: list[Token] = []
            seen_tokens: set[str] = set()
            for index, item in enumerate(stored_tokens):
                if not isinstance(item, dict):
                    raise ValueError(f"tokens[{index}] 必须是对象")
                token_value = required_text(item, "token")
                user = required_text(item, "user")
                label = optional_text(item, "label")
                scope = optional_text(item, "scope", "readwrite")
                if scope not in ("read", "readwrite"):
                    raise ValueError(f"tokens[{index}].scope 非法")
                created = finite_number(item.get("created", time.time()), "created")
                last_used = finite_number(item.get("last_used", 0.0), "last_used")
                revoked_raw = item.get("revoked_at")
                revoked_at = (
                    None if revoked_raw is None
                    else finite_number(revoked_raw, "revoked_at")
                )
                managed = item.get("managed", True)
                if not isinstance(managed, bool):
                    raise ValueError(f"tokens[{index}].managed 必须是布尔值")
                if token_value in seen_tokens:
                    raise ValueError(f"tokens 中 token 重复：tokens[{index}]")
                seen_tokens.add(token_value)
                decoded_tokens.append(Token(
                    token=token_value,
                    user=user,
                    label=label,
                    scope=scope,
                    created=created,
                    revoked_at=revoked_at,
                    last_used=last_used,
                    managed=managed,
                ))

            decoded_tombstones: dict[str, dict[str, Any]] = {}
            for index, item in enumerate(stored_tombstones):
                if not isinstance(item, dict):
                    raise ValueError(f"revoked_external[{index}] 必须是对象")
                token_value = required_text(item, "token")
                user = required_text(item, "user")
                label = optional_text(item, "label")
                scope = optional_text(item, "scope", "readwrite")
                if scope not in ("read", "readwrite"):
                    raise ValueError(f"revoked_external[{index}].scope 非法")
                if "revoked_at" not in item:
                    raise ValueError(f"revoked_external[{index}].revoked_at 缺失")
                revoked_at = finite_number(item["revoked_at"], "revoked_at")
                if token_value in decoded_tombstones:
                    raise ValueError(f"revoked_external 中 token 重复：{token_value[:4]}…")
                if token_value in seen_tokens:
                    raise ValueError("同一个 token 同时出现在 tokens 与 revoked_external")
                decoded_tombstones[token_value] = {
                    "token": token_value, "user": user, "label": label,
                    "scope": scope, "revoked_at": revoked_at,
                }
        except (OSError, ValueError, TypeError) as exc:
            # 失败关闭：绝不跳过坏行后继续启动。否则坏掉的吊销墓碑会让环境
            # token 复活，或坏 managed 标记会把 token 错当长期凭据加载。
            # 整份文件先完整解码，成功后才合并状态，避免部分加载。
            logger.critical("token 文件不可安全读取（%s）：%s", self.persist_path, exc)
            raise RuntimeError(
                f"凭据文件无法安全读取：{self.persist_path}。"
                f"服务已拒绝启动，未加载任何 token，也未改写文件。原因：{exc}。"
                "请先备份并修复该文件，或在确认不再需要旧凭据后手动移走它。"
            ) from exc

        loaded = 0
        with self._token_lock:
            for tok in decoded_tokens:
                existing = next((t for t in self._tokens if t.token == tok.token), None)
                if tok.managed:
                    if existing is not None:
                        # 环境配置的用户/权限信息优先；持久化吊销状态不能被冲掉。
                        if tok.revoked_at is not None:
                            existing.revoked_at = tok.revoked_at
                        continue
                    self._tokens.append(tok)
                    loaded += 1
                elif tok.revoked_at is not None:
                    # 兼容旧布局中的 external token 吊销墓碑，但不保留 active 凭据。
                    decoded_tombstones[tok.token] = {
                        "token": tok.token, "user": tok.user, "label": tok.label,
                        "scope": tok.scope, "revoked_at": tok.revoked_at,
                    }
                    if existing is not None:
                        existing.revoked_at = tok.revoked_at

            for token_value, tombstone in decoded_tombstones.items():
                self._revoked_external[token_value] = tombstone
                existing = next((t for t in self._tokens if t.token == token_value), None)
                if existing is not None:
                    existing.revoked_at = tombstone["revoked_at"]

        if loaded:
            logger.info("从 %s 加载 %d 把 token", self.persist_path, loaded)
        return loaded

    def _persist(self) -> bool:
        """原子写回 token 快照。失败返回 False，调用方必须回滚本次变更。

        ⚠️ **只在增删时调用**，不在 ``resolve()``（每次请求都跑）里调用 ——
        ``last_used`` 频繁变化，每次请求都落盘会变成写风暴。

        POSIX 使用 0600；Windows 没有 chmod 等价权限模型，文件继承父目录
        ACL。因此部署时必须限制 ``<home>/state`` 的 NTFS ACL。
        """
        if self.persist_path is None:
            return True  # 纯内存测试/嵌入模式

        with self._token_lock:
            payload = {
                "schema": "shared-memory-tokens/1",
                "note": "含明文凭据，切勿提交进 git / 分享 / 截图。",
                "tokens": [
                    {
                        "token": t.token,
                        "user": t.user,
                        "label": t.label,
                        "scope": t.scope,
                        "created": t.created,
                        "revoked_at": t.revoked_at,
                        "last_used": t.last_used,
                        "managed": True,
                    }
                    for t in self._tokens if t.managed
                ],
                # 外部注入 token 只保留吊销墓碑，不存 active 凭据。
                # 如果管理员后来重新注入同一 token，attach 时会再次强制吊销。
                "revoked_external": list(self._revoked_external.values()),
            }
            tmp_path: Path | None = None
            try:
                self.persist_path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", newline="\n",
                    dir=self.persist_path.parent,
                    prefix=f".{self.persist_path.name}.", suffix=".tmp",
                    delete=False,
                ) as tmp:
                    tmp_path = Path(tmp.name)
                    tmp.write(json.dumps(payload, ensure_ascii=False, indent=2))
                    tmp.flush()
                    os.fsync(tmp.fileno())
                # POSIX mode is restrictive; on Windows, inherit parent ACL.
                if os.name != "nt":
                    os.chmod(tmp_path, 0o600)
                os.replace(tmp_path, self.persist_path)
                return True
            except OSError as exc:
                logger.error("token 持久化失败（%s）：%s", self.persist_path, exc)
                if tmp_path is not None:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                return False

    # ────────────────────── 启动自检 ──────────────────────

    def preflight(self) -> tuple[bool, str]:
        """返回 ``(是否允许启动, 说明)``。

        **拒绝启动**而不是打警告：警告会被忽略（日志刷过去就没了），
        而服务照样裸奔 —— 用户以为"我起好了"，同事那边已经能读全部记忆。
        """
        if not self.required:
            return True, (
                f"绑定 {self.bind_host}（仅本机可达），未强制鉴权。"
            )
        active = [t for t in self._tokens if t.active]
        has_users = self.store is not None and bool(getattr(self.store, "users", None))
        if not active and not has_users:
            return False, (
                f"绑定 {self.bind_host} —— **网络上其他机器可以连到本服务**，\n"
                "但没有任何可用凭据 —— 拒绝启动。\n\n"
                "这在公司网/共享 WiFi 下等于把全部记忆公开给同网段所有人。\n\n"
                "解法（任选）：\n"
                "  1. 配一把 token：\n"
                "       SHARED_MEMORY_TOKEN=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \\n')\n"
                "  2. 让服务自动初始化管理员（首次启动会打印一次密码）\n"
                "  3. 只允许本机访问：把 HOST 改成 127.0.0.1\n\n"
                "（确实要在无鉴权内网跑，必须显式设 SHARED_MEMORY_ALLOW_INSECURE=1）"
            )
        return True, f"绑定 {self.bind_host}，已配置 {len(active)} 把 token"

    def allow_insecure(self) -> bool:
        """用户是否显式接受"无鉴权内网运行"。

        保留这个开关（而不是硬禁止）：有些场景确实需要（隔离测试网、
        临时演示），硬禁止会逼用户去改代码绕过 —— 那更糟。
        但必须**显式、留痕**。
        """
        return os.environ.get("SHARED_MEMORY_ALLOW_INSECURE", "").strip() == "1"

    # ────────────────────── token 生命周期 ──────────────────────

    def add_token(
        self, *, user: str, label: str = "", scope: str = "readwrite",
        token: str | None = None,
    ) -> Token:
        """发一把绑定到用户的 token。不传 token 则随机生成。

        返回的 Token 对象含明文 —— 调用方负责只显示一次。
        """
        tok = Token(
            token=token or secrets.token_hex(32),
            user=user,
            label=label,
            scope=scope if scope in ("read", "readwrite") else "readwrite",
            managed=True,
        )
        with self._token_lock:
            self._tokens.append(tok)
            if not self._persist():
                self._tokens.remove(tok)
                raise OSError(
                    f"token 已生成但无法持久化到 {self.persist_path}；"
                    "未发放凭据，请检查目录权限/磁盘空间后重试"
                )
        logger.info("已发放 token（user=%s, label=%s, scope=%s）", user, label, tok.scope)
        return tok

    def revoke_token(self, fingerprint_or_label: str) -> int:
        """按指纹或 label 吊销。返回吊销数量（0 表示没找到）。

        支持"吊销某人的全部" —— 离职场景常用。
        """
        key = (fingerprint_or_label or "").strip()
        n = 0
        changed: list[tuple[Token, float | None]] = []
        prior_tombstones: dict[str, dict[str, Any] | None] = {}
        with self._token_lock:
            for t in self._tokens:
                if not t.active:
                    continue
                if t.matches(key) or t.label == key or t.user == key:
                    changed.append((t, t.revoked_at))
                    if not t.managed:
                        prior_tombstones[t.token] = self._revoked_external.get(t.token)
                    t.revoked_at = time.time()
                    if not t.managed:
                        self._revoked_external[t.token] = {
                            "token": t.token, "user": t.user, "label": t.label,
                            "scope": t.scope, "revoked_at": t.revoked_at,
                        }
                    n += 1
            if n and not self._persist():
                for t, prior in changed:
                    t.revoked_at = prior
                for token_value, previous in prior_tombstones.items():
                    if previous is None:
                        self._revoked_external.pop(token_value, None)
                    else:
                        self._revoked_external[token_value] = previous
                raise OSError(
                    f"无法持久化吊销状态到 {self.persist_path}；"
                    "本次吊销已回滚，凭据仍有效。请修复存储后重试"
                )
        if n:
            logger.warning("已吊销 %d 把 token（匹配 %s）", n, key)
        return n

    def list_tokens(self) -> list[dict[str, Any]]:
        """列出 token 元信息。**永不返回明文**。

        显示指纹而不是值：完整的 token 一旦出现在浏览器/日志/截图里，
        就等于又泄露了一次（这正是之前踩过的坑）。
        """
        with self._token_lock:
            return [
                {
                    "fingerprint": t.fingerprint,
                    "user": t.user,
                    "label": t.label,
                    "scope": t.scope,
                    "active": t.active,
                    "created": t.created,
                    "last_used": t.last_used,
                }
                for t in self._tokens
            ]

    def rotate_token(self, target: str) -> tuple[str, str] | None:
        """轮换（吊销旧的 + 发新的同 label）。返回 ``(label, 新明文)``。

        轮换是一个事务：持久化失败时恢复旧 token、移除新 token，
        不制造"当前已吊销、重启后又复活"的半成状态。
        """
        with self._token_lock:
            old = next((
                t for t in self._tokens
                if t.active and (
                    t.matches(target) or t.label == target or t.fingerprint == target
                )
            ), None)
            if old is None:
                return None
            if not old.managed:
                raise ValueError(
                    "这把 token 来自环境配置或临时会话，服务无法安全轮换它。"
                    "环境 token 请更新 SHARED_MEMORY_TOKEN/SHARED_MEMORY_TOKEN_FILE；"
                    "浏览器会话请重新登录"
                )

            prior_revoked_at = old.revoked_at
            prior_tombstone = self._revoked_external.get(old.token)
            old.revoked_at = time.time()
            if not old.managed:
                self._revoked_external[old.token] = {
                    "token": old.token, "user": old.user, "label": old.label,
                    "scope": old.scope, "revoked_at": old.revoked_at,
                }
            new = Token(
                token=secrets.token_hex(32),
                user=old.user,
                label=old.label,
                scope=old.scope,
                managed=old.managed,
            )
            self._tokens.append(new)
            if not self._persist():
                self._tokens.remove(new)
                old.revoked_at = prior_revoked_at
                if prior_tombstone is None:
                    self._revoked_external.pop(old.token, None)
                else:
                    self._revoked_external[old.token] = prior_tombstone
                raise OSError(
                    f"无法持久化 token 轮换到 {self.persist_path}；"
                    "旧 token 已恢复，新 token 未发放"
                )
            logger.info("已轮换 token（user=%s, label=%s）", new.user, new.label)
            return new.label, new.token

    # ────────────────────── 校验 → 身份 ──────────────────────

    def resolve(self, candidate: str) -> tuple[str, str]:
        """把凭据解析成 ``(用户名, scope)``。失败返回 ``("", "")``。

        ━━━ 这是整个鉴权层的核心产出 ━━━

        不是"通过/拒绝"，而是 **"这个请求代表哪个用户"**。
        有了身份，租户层才能算出他能看哪些库。
        """
        if not candidate:
            return "", ""
        # v2（S-6.2）：先试**主密钥**（单密钥模型）。
        # 放在 token 循环之前 —— 主人那把钥匙最常用，且它不该被
        # 任何 token 的优先级遮住。旧 token 仍然有效（过渡期），
        # 直到 S-6.4 的迁移把它们退役。
        if self.master_key_enabled() and self.verify_master(candidate):
            return self.master_key_user(), "readwrite"
        with self._token_lock:
            for t in self._tokens:
                if t.active and t.matches(candidate):
                    t.last_used = time.time()
                    return t.user, t.scope
        return "", ""

    def scope_allows(self, scope: str, action: str) -> bool:
        """scope 是否允许某个动作。"""
        if action == "read":
            return scope in ("read", "readwrite")
        return scope == "readwrite"

    # ────────────────────── 限速 ──────────────────────

    def note_failure(self, source: str) -> None:
        """记一次失败。用于限速判定。

        ⚠️ ``source`` 为空时**不计** —— 见 ``throttled`` 的说明：
        没有来源信息的失败（探针、内部检查）不参与限速，
        否则健康检查会把自己锁死。
        """
        if not source:
            return
        now = time.time()
        bucket = self._failures.setdefault(source, [])
        bucket.append(now)
        cutoff = now - FAIL_WINDOW_SECONDS
        self._failures[source] = [t for t in bucket if t > cutoff]

    def throttled(self, source: str) -> tuple[bool, float]:
        """``(是否限速, 需等待秒数)``。

        ━━━ 为什么探针类请求不计入（实测踩过）━━━

        冒烟测试用无凭据的 ``/favicon.ico`` 探测服务就绪，
        每次探测都 401、都记一次失败 —— 20 次后触发限速，
        于是**后续所有请求（包括带正确凭据的）全被 429**。

        这不是测试的错，是设计缺陷：**没有来源信息的失败不该计入**。

        真实世界里会打无凭据请求的东西很多：
          · 容器健康探针（k8s livenessProbe）
          · 守护脚本的存活检查
          · 监控系统
          · 负载均衡的心跳

        它们会把**共享出口 IP**（或本机 IP）拖进限速名单，
        然后真正用户被误伤 —— 典型的"安全机制造成可用性故障"。

        所以：**只对带来源的请求限速**。上面那些场景大多没有
        可归因的来源，不该影响真人。

        限速本身仍有必要：32 字节随机 token 虽然枚举不动，
        但"用户自己设的弱密码"（面板登录）能 —— 所以登录路径
        始终计入。
        """
        if not source:
            return False, 0.0
        now = time.time()
        cutoff = now - FAIL_WINDOW_SECONDS
        recent = [t for t in self._failures.get(source, []) if t > cutoff]
        if len(recent) <= FAIL_MAX_ATTEMPTS:
            return False, 0.0
        return True, float(FAIL_BACKOFF_SECONDS)

    # ────────────────────── 登录票据 ──────────────────────

    def issue_ticket(self, user: str, *, source: str = "") -> str:
        """签发一次性登录票据（用于「URL 换 cookie」）。

        URL 会进浏览器历史、书签、Referer、代理日志、截图。
        票据则是：一次性、短命、只换 cookie 不是凭据本身 ——
        即使泄露，可利用窗口也极小。
        """
        ticket = secrets.token_urlsafe(24)
        self._tickets[ticket] = (time.time() + LOGIN_TICKET_TTL, user, source)
        now = time.time()
        # 顺手清理过期票据：字典无限增长也是内存 DoS 面
        self._tickets = {k: v for k, v in self._tickets.items() if v[0] > now}
        return ticket

    def redeem_ticket(self, ticket: str) -> str:
        """兑换票据（**用掉即失效**）。返回用户名，失败返回空串。"""
        rec = self._tickets.pop(ticket, None)
        if rec is None:
            return ""
        expires, user, _src = rec
        if time.time() > expires:
            return ""
        return user

    def make_session(self, user: str) -> str:
        """生成会话 cookie 值，登记为**临时凭据**（绑定同一用户）。

        会话与长期 token 分开：会话可被重启清空（用户重新登录），
        长期 token 不变（已接入的 MCP 客户端不受影响）。
        """
        sess = secrets.token_urlsafe(32)
        self._tokens.append(Token(
            token=sess, user=user, label=f"session:{user[:8]}", scope="readwrite",
            managed=False,
        ))
        return sess

    def user_of(self, token_value: str) -> str:
        """从凭据值反查用户名（面板用于显示当前登录者）。"""
        user, _ = self.resolve(token_value)
        return user

    # ══════════════════════ 主密钥 + PIN（v2 · S-6）══════════════════════
    #
    # ━━━ 为什么从"每设备一把 token"简化为"一把主密钥 + 一个面板 PIN" ━━━
    #
    # 旧模型的问题不是不安全，是**用不起来**：每接一台设备要签发一把 token、
    # 起个 label、记住哪把是哪台；换机器要重新签发；忘了哪把泄露还得翻列表。
    # 实测里用户的原话是"mcp 为什么要手动配置？？"—— **门槛本身就是缺陷**。
    #
    # 新模型两个凭据，各管一摊：
    #   · **主密钥**（MCP / agent）—— 一把，只存哈希，可轮换（旧的有宽限）
    #   · **PIN**（面板 / 人）—— 4~8 位数字，错 5 次锁 10 分钟，可关
    #
    # 旧 token **过渡期继续有效**（S-6.2 要求保留 Token 类一版兼容）；
    # 把 admin token 真正退役是 S-6.4 的迁移，那一步要用户确认。
    #
    # ━━━ 三条不可让步 ━━━
    #
    # 1. **只存哈希**：盘上从不出现明文（`state/` 已是非 git 目录，仍只存哈希）。
    # 2. **hmac.compare_digest**：定时安全比较，不泄露前缀匹配信息。
    # 3. **轮换有宽限**：正在跑的请求不该因为一次轮换集体掉线。

    # ────────── 主密钥 ──────────

    def attach_master_key(self, path: str | Path) -> bool:
        """挂上主密钥文件（``<home>/state/master-key.json``）。

        Returns:
            是否**已存在**主密钥（服务启动时用它决定要不要打印"首次设置"提示）。
        """
        self._master_path = Path(path)
        self._master_cache = None      # 换路径 → 旧缓存作废
        return self.master_key_enabled()

    def master_key_enabled(self) -> bool:
        return bool(self._load_master().get("hash"))

    def master_key_user(self) -> str:
        """主密钥**代表谁** —— 文件里记的 owner；缺省退回第一个 admin。"""
        owner = str(self._load_master().get("owner", "") or "")
        if owner:
            return owner
        store = self.store
        if store is not None:
            users = getattr(store, "users", {}) or {}
            if "admin" in users:
                return "admin"
            for name, u in users.items():
                if getattr(u, "is_admin", False):
                    return name
        return ""

    @staticmethod
    def _hash_secret(plain: str) -> str:
        """凭据哈希（带域前缀，避免和别处的 sha256 撞语义）。"""
        return hashlib.sha256(("shm-v2:" + (plain or "")).encode("utf-8")).hexdigest()

    def set_master_key(self, plain: str = "") -> str:
        """设置主密钥（``plain`` 留空则**生成**一把）。返回明文。

        ⚠️ 明文只在返回值里出现**这一次** —— 盘上只有哈希，丢了只能轮换。
        """
        key = (plain or "").strip() or secrets.token_urlsafe(32)
        data = self._load_master()
        data.update({
            "hash": self._hash_secret(key),
            "owner": str(data.get("owner", "") or "") or self.master_key_user(),
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        })
        data.pop("prev", None)
        self._write_master(data)
        return key

    def verify_master(self, candidate: str) -> bool:
        """校验主密钥（**含轮换宽限期内的旧密钥**）。"""
        if not candidate:
            return False
        data = self._load_master()
        got = self._hash_secret(candidate)
        want = str(data.get("hash", "") or "")
        if want and hmac.compare_digest(want, got):
            return True
        prev = data.get("prev") or {}
        prev_hash = str(prev.get("hash", "") or "")
        if prev_hash and float(prev.get("until", 0) or 0) > time.time():
            return hmac.compare_digest(prev_hash, got)
        return False

    def is_rotated_out(self, candidate: str) -> bool:
        """这把密钥是不是**刚轮换掉的旧密钥**（宽限期已过）。

        为什么要单独判：客户端拿到 401 时只有两种正确反应 ——
        "密钥错了，去查配置"和"密钥被轮换掉了，去拿新的"。
        不分开的的话，主人轮换完密钥，agent 只会报"未授权"，
        排查方向直接跑偏到"是不是我 token 写错了"。
        """
        if not candidate:
            return False
        prev = (self._load_master().get("prev") or {})
        prev_hash = str(prev.get("hash", "") or "")
        if not prev_hash:
            return False
        if float(prev.get("until", 0) or 0) > time.time():
            return False        # 还在宽限期内，verify_master 会放行
        return hmac.compare_digest(prev_hash, self._hash_secret(candidate))

    def rotate_master_key(self) -> str:
        """轮换主密钥：返回新密钥明文；旧密钥在**宽限期**内仍可用。

        ━━━ 为什么要宽限 ━━━

        轮换的那一刻，**正在跑**的请求可能还攥着旧密钥（重试、长连接、
        多进程 agent）。一刀切会让它们全部 401 —— 用户看到的是
        "刚点了轮换，agent 全掉线"。宽限窗口让它们把手上这步干完。
        """
        old = self._load_master()
        new_key = secrets.token_urlsafe(32)
        data: dict[str, Any] = {
            "hash": self._hash_secret(new_key),
            "owner": str(old.get("owner", "") or "") or self.master_key_user(),
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        if old.get("hash"):
            data["prev"] = {
                "hash": old["hash"],
                "until": time.time() + MASTER_KEY_GRACE_SECONDS,
            }
        self._write_master(data)
        return new_key

    def revoke_master_key(self) -> bool:
        """退役主密钥（迁移收尾 / 关停用）。返回是否真有东西被删。"""
        p = self._master_path
        if p is None or not p.exists():
            return False
        p.unlink()
        self._master_cache = None      # 删了文件就必须清缓存，否则"还以为有密钥"
        return True

    def _load_state_file(self, path: Path | None, cache_attr: str) -> dict[str, Any]:
        """读一个小 state 文件，带 **mtime 缓存**。

        ━━━ 为什么需要缓存 ━━━

        ``resolve()`` 每次凭据解析都要问一次主密钥（enabled / verify / owner
        各一次）。服务跑在**手机**上，每个 MCP 请求多三次 read+parse 是白花的。
        用 mtime 判定：文件没动就用内存里的那份；我们自己的写操作会顺手
        更新缓存（见 ``_write_state_file``），所以缓存在单写者模型下恒新鲜。

        外部手工编辑文件也能被 mtime 变化发现 —— 这正是用 mtime 而不是
        纯内存标志的原因。
        """
        if path is None:
            return {}
        try:
            st = path.stat()
        except OSError:
            return {}
        key = (str(path), st.st_mtime_ns, st.st_size)
        cache = getattr(self, cache_attr, None)
        if cache is not None and cache[0] == key:
            return cache[1]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        setattr(self, cache_attr, (key, data))
        return data

    def _load_master(self) -> dict[str, Any]:
        return self._load_state_file(self._master_path, "_master_cache")

    def _write_master(self, data: dict[str, Any]) -> None:
        self._write_state_file(self._master_path, data)

    # ────────── 面板 PIN ──────────

    def attach_pin(self, path: str | Path) -> bool:
        """挂上 PIN 文件（``<home>/state/pin.json``）。返回是否已设 PIN。"""
        self._pin_path = Path(path)
        self._pin_cache = None         # 换路径 → 旧缓存作废
        return self.pin_enabled()

    def pin_enabled(self) -> bool:
        return bool(self._load_pin().get("hash"))

    def set_pin(self, pin: str) -> None:
        """设置 PIN（4~8 位数字）。

        Raises:
            ValueError: 位数/字符不合法（消息直说要求，别让人猜）。
        """
        p = (pin or "").strip()
        if not p.isdigit() or not (4 <= len(p) <= 8):
            raise ValueError("PIN 必须是 4~8 位数字")
        self._write_pin({
            "hash": self._hash_secret(p),
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        })
        self._pin_failures.clear()

    def disable_pin(self) -> bool:
        """关掉 PIN（面板免登录）。返回是否真有 PIN 被关。

        ⚠️ 服务绑定在**公开可达**地址时，``panel_open()`` 仍然为假 ——
        关 PIN 不等于把面板敞开给公网（见 ``panel_open``）。
        """
        p = self._pin_path
        if p is None or not p.exists():
            return False
        p.unlink()
        self._pin_cache = None         # 同上：删了文件必须清缓存
        self._pin_failures.clear()
        return True

    def verify_pin(self, pin: str) -> bool:
        """校验 PIN。**错 5 次锁 10 分钟**，到时自解（内存态，重启解除）。"""
        locked, _ = self.pin_locked()
        if locked:
            return False
        want = str(self._load_pin().get("hash", "") or "")
        if not want:
            return False
        if hmac.compare_digest(want, self._hash_secret(pin or "")):
            self._pin_failures.clear()
            return True
        self._pin_failures.append(time.time())
        return False

    def pin_locked(self) -> tuple[bool, float]:
        """``(是否锁定, 还需等几秒)``。窗口外的旧失败自动过期。"""
        now = time.time()
        self._pin_failures = [
            t for t in self._pin_failures if now - t < PIN_LOCK_SECONDS
        ]
        if len(self._pin_failures) < PIN_MAX_ATTEMPTS:
            return False, 0.0
        wait = PIN_LOCK_SECONDS - (now - self._pin_failures[0])
        return True, max(0.0, wait)

    def panel_open(self) -> bool:
        """面板是否**免登录直进**。

        ⚠️ 只在**未公开可达**（loopback / 纯内网绑定）**且**没设 PIN 时为真。
        公开绑定时永远要凭据 —— "图省事关掉 PIN" 不能变成
        "把面板敞给公网"。这条是安全底线，不提供开关。
        """
        return (not self.required) and (not self.pin_enabled())

    def _load_pin(self) -> dict[str, Any]:
        return self._load_state_file(self._pin_path, "_pin_cache")

    def _write_pin(self, data: dict[str, Any]) -> None:
        self._write_state_file(self._pin_path, data)

    def _write_state_file(self, path: Path | None, data: dict[str, Any]) -> None:
        """原子写一个 state 文件（tmp 带 pid + os.replace），并**刷新缓存**。

        与 token 持久化同款：崩溃/并发下不留半个 JSON。
        写完就更新 mtime 缓存 —— 否则下一次读会白跑一趟 stat+read
        （我们刚写的内容本来就是最新的）。
        """
        if path is None:
            raise RuntimeError("未挂载状态文件（先调 attach_master_key / attach_pin）")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        os.replace(tmp, path)
        cache_attr = "_master_cache" if path is self._master_path else "_pin_cache"
        try:
            st = path.stat()
            setattr(self, cache_attr, ((str(path), st.st_mtime_ns, st.st_size), data))
        except OSError:  # pragma: no cover - 刚写完就 stat 不到属异常
            setattr(self, cache_attr, None)
