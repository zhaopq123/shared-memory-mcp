"""Vault —— 共享记忆的存储层：一个 git 仓库 + 一堆 Markdown 文件。

━━━ 为什么是「文件 + git」而不是数据库 ━━━

参考 dsh-tiddlywiki 的内核：

  · **一条记忆 = 一个 .md 文件** —— 人能直接读、能手改、能 grep
  · **整个 vault 是 git 仓库** —— 版本化、可回滚、可 diff、可异地备份
  · **故障时纯文本也能抢救** —— 不依赖任何程序就能读懂内容

数据库在这三件事上都更差：不可读、迁移要工具、坏了就全坏了。
而并发问题这里由 queue.py 的单写者队列解决，不需要靠数据库事务。

━━━ 文件格式 ━━━

每个记忆是一个 Markdown 文件，YAML frontmatter 存元数据：

    ---
    id: mem-20261012-163000-a1b2
    title: 记忆标题
    tags: [tag1, tag2]
    created: 2026-10-12T16:30:00+08:00
    updated: 2026-10-12T16:30:00+08:00
    author: dsh            # 谁写的（可选，仅作溯源，不用于隔离）
    ---

    正文……

文件名 = ``<safe-title>.md``，标题重名时追加 id 后缀避免互相覆盖。

━━━ 职责边界（重要）━━━

本类**不做并发控制**。它假设调用方（queue.py 的单写者）已经保证了串行。
这样拆分的好处：

  · 存储层可以独立单元测试，不需要起事件循环
  · 并发策略只有一处实现，不会在两层之间漂移
  · 将来若要换掉并发策略（比如换多进程模型），存储层一行不用改
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .cache import NoteCache, TTLCache
from .config import SHARE_LEVELS, Config
from .errors import ErrorCode, ToolError
from .knowledge import expiry_state

logger = logging.getLogger(__name__)

#: 本项目的"本地时间"固定用 UTC+8。
#:
#: 为什么硬编码而不是取系统时区：记忆库可能被多台机器（不同时区）写入，
#: 时间戳必须**可比**。固定偏移让「谁先写的」在任何机器上读都一致。
#: 代价是不跟随夏令时 —— 对+8 区没有夏令时，所以这个代价是零。
CST = timezone(timedelta(hours=8))

#: log() 实际向 git 请求的条数（缓存粒度）。
#: 一律按这个上限取，再按调用方的 limit 切片 —— 避免小 limit 的请求
#: 用一份短结果覆盖掉长缓存（实测会导致缓存永不命中）。
_LOG_CACHE_MAX = 50

#: 文件名里不允许出现的字符。
#: 取的是 Windows/Android/Linux 三者的**保守并集** —— 因为 vault 可能被
#: 同步到任意平台（今天就在 Windows 写、在 Android 读）。
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: 连续空白压成单个短横（文件名里带空格容易被 shell 脚本坑）
_DASHES = re.compile(r"\s+")

#: frontmatter 定界符
_FM = "---"

#: 文件名主干长度上限。留出空间给 `--<id>` 后缀和 `.md`，
#: 避免超过 ext4(255) / NTFS(255) / Android 的路径长度限制。
_MAX_SLUG = 80

# ══════════════════ v2 概念字段（S-1 落地）══════════════════
#: 记忆六型（v2 产品重构设计 §4.3）。type 缺省 fact。
#: 与 v1 的 note/decision/lesson/todo/ref 的关系：
#: decision→fact（或按内容判 constraint）、lesson→lesson、
#: todo→task、ref→fact；旧值仍可读，新写入只认六型。
NOTE_TYPES: tuple[str, ...] = (
    "workflow", "constraint", "habit", "fact", "lesson", "task",
)

#: scope 三层（v2 防串台核心）：long-term 默认可见；
#: task:<名> 仅当前会话；archived 默认不可见。
SCOPE_LONG_TERM = "long-term"
SCOPE_ARCHIVED = "archived"


def valid_type(value: str) -> bool:
    """type 是否是六型之一。"""
    return value in NOTE_TYPES


def valid_scope(value: str) -> bool:
    """scope 是否是三层之一（task: 后必须跟非空任务名）。"""
    if value in (SCOPE_LONG_TERM, SCOPE_ARCHIVED):
        return True
    return value.startswith("task:") and len(value) > len("task:")


# ══════════════════ 共享档位可见性（v2 · S-10.2，决策 D-3）══════════════════


def effective_share(share: str, space_default: str = "space") -> str:
    """条目档位：显式值优先，空则继承空间默认。

    继承发生在**读侧**而不是写侧，是刻意的：存量条目没有 share 字段，
    写侧继承要给几千条历史数据补 frontmatter（一次大 diff、git 历史噪音），
    而读侧继承零成本、改空间默认值还能立刻作用到全部存量。
    """
    s = (share or "").strip()
    return s if s in SHARE_LEVELS else (space_default or "space")


def share_visible(
    share: str,
    author: str,
    viewer: str,
    *,
    space_default: str = "space",
    cross_space: bool = False,
) -> bool:
    """这条记忆对这个身份可见吗。

    ━━━ 三档语义 ━━━

    · ``mine``   仅作者。**无作者的存量条目按 space 处理** ——
      把"不知道谁写的"判成"谁都看不见"，会让作者自己点开一条他刚设成
      私密的记忆却发现它消失了，那是 bug 级困惑；
      而这批条目本来就在同空间里躺了很久，突然全隐藏反而是行为回退。
    · ``space``  同空间（同库）可见 —— 库的读权限就是这道闸，
      由 :mod:`tenancy` 把守，这里不再判第二遍。
    · ``shared`` 跨空间可见，但**只在目标空间授权后**才进入跨空间检索面
      （``cross_space=True`` 的那条路）。同空间内它与 space 等价。

    Args:
        share: 条目上的显式档位（可为空 = 继承）。
        author: 条目作者。
        viewer: 当前身份；**空串 = 不做共享过滤**（内部通道：面板治理、
            迁移脚本、管理员导出 —— 它们本来就该看全貌）。
        space_default: 所在空间缺省档位。
        cross_space: 这次访问是不是"经授权跨空间捞进来的"。
            True 时**只放行 ``shared``**，其它一概不看 ——
            这是跨空间通道的唯一存在理由，别让它变成第二个全库入口。
    """
    if not viewer:
        return True
    eff = effective_share(share, space_default)
    if cross_space:
        return eff == "shared"
    if eff == "mine":
        return (not author) or author == viewer
    return True


def validate_v2_fields(extra: dict[str, Any]) -> None:
    """显式传入的 v2 概念字段必须合法（S-1.5）。

    缺省值由 put() 保证合法，这里只拦"显式传错"——
    传错不拦，脏数据会悄悄躺进 frontmatter，等检索时才对不上。
    """
    t = str(extra.get("type") or "").strip()
    if t and not valid_type(t):
        raise ToolError(
            ErrorCode.INVALID_TYPE,
            f"未知的记忆类型：{t}",
            hint="合法类型（六型）：" + "、".join(NOTE_TYPES),
        )
    sc = str(extra.get("scope") or "").strip()
    if sc and not valid_scope(sc):
        raise ToolError(
            ErrorCode.INVALID_ARGUMENT,
            f"scope 格式不对：{sc}",
            hint="合法值：long-term / task:<任务名> / archived",
        )
    sh = str(extra.get("share") or "").strip()
    if sh and sh not in SHARE_LEVELS:
        raise ToolError(
            ErrorCode.INVALID_ARGUMENT,
            f"share 格式不对：{sh}",
            hint="合法值：mine / space / shared（留空 = 继承空间默认）",
        )


def now_iso() -> str:
    """当前时间，ISO 格式带 +08:00 偏移（人类可读且可排序）。"""
    return datetime.now(CST).replace(microsecond=0).isoformat()


def parse_iso(value: str) -> datetime | None:
    """解析时间戳；失败返回 None（不抛）。

    为什么宽容：frontmatter 是**用户可以手改**的 —— 手滑写错格式
    不该让整条记忆无法读取。解析失败的条目在时间过滤里视为"无时间"。
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    # 无时区的值按本地(CST)解释，否则无法与其他条目比较
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt


def _slug(title: str) -> str:
    """把标题变成安全的文件名主干。

    中文**保留原样** —— 不转拼音也不转义。文件名可读是这套设计的卖点之一：
    `ls vault/notes/` 就能看懂库里有什么，不需要打开文件。

    只把真正危险的字符换掉，空白压成短横。
    """
    s = unicodedata.normalize("NFC", title).strip()
    s = _UNSAFE.sub("", s)
    s = _DASHES.sub("-", s)
    s = s.strip(".-")  # 前后导的点和横线：`.` 开头会变隐藏文件
    if len(s) > _MAX_SLUG:
        s = s[:_MAX_SLUG].rstrip(".-")
    return s or "untitled"


def _new_id() -> str:
    """生成记忆 id：``mem-<时间>-<4位随机>``。

    为什么带随机后缀：同一秒内并发写多条时，仅靠时间戳会撞 id。
    随机部分只有 2 字节，够用且保持 id 短（id 会出现在 URL 与工具返回值里）。
    """
    import secrets

    ts = datetime.now(CST).strftime("%Y%m%d-%H%M%S")
    return f"mem-{ts}-{secrets.token_hex(2)}"


def _stamp() -> str:
    """回收站用的文件时间戳：``<日期>-<时间>-<随机>``。

    **随机后缀是必需的**，不是装饰：纯秒级时间戳在同一秒内删除两次同名
    记忆时会撞名，`Path.rename` 会抛 FileExistsError。
    实测踩过（测试 ``test_delete_same_title_twice_keeps_both`` 就是为它写的）。
    """
    import secrets

    ts = datetime.now(CST).strftime("%Y%m%d-%H%M%S")
    return f"{ts}-{secrets.token_hex(2)}"


def dump_frontmatter(meta: dict[str, Any]) -> str:
    """把元数据字典序列化成 frontmatter 文本块。

    刻意**不引入 PyYAML**：vault 要能在任何机器上用纯文本工具打开，
    格式越简单越好。只支持两种值形态：
      · 标量      → ``key: value``
      · 字符串列表 → ``key: [a, b]``

    已知字段按固定顺序输出（id/title/tags/created/updated/author），
    其余键追加在后 —— 这样**同一批元数据永远生成同一份文本**，
    git diff 里不会出现无意义的行序变化。
    """
    known = (
        "id",
        "title",
        "tags",
        "created",
        "updated",
        "author",
        "workspace",
        # 体系字段：按可读顺序排列，让 frontmatter 一眼能扫完
        "type",
        "status",
        "claim",
        "valid-until",
        "review-after",
        "superseded-by",
        "related",
        # v2 概念字段（S-1）：scope/versioned/share
        "scope",
        "versioned",
        "share",
    )
    lines = [_FM]

    def emit(k: str, v: Any) -> None:
        if v in (None, "", []):
            return
        if isinstance(v, list):
            lines.append(f"{k}: [{', '.join(str(x) for x in v)}]")
        else:
            lines.append(f"{k}: {v}")

    for k in known:
        if k in meta:
            emit(k, meta[k])
    # 其余自定义字段（保留 agent 额外写入的键，不丢数据）
    for k, v in meta.items():
        if k not in known:
            emit(k, v)

    lines.append(_FM)
    return "\n".join(lines)


def parse_note(raw: str) -> tuple[dict[str, Any], str]:
    """拆出 frontmatter 与正文。

    返回值：``(元数据字典, 正文)``。没有 frontmatter 时返回 ``({}, 原文)``
    —— 这样**用户手写的纯 Markdown 文件也能被库里识别**，
    不需要先补一层元数据才能用。
    """
    if not raw.startswith(_FM):
        return {}, raw
    end = raw.find(f"\n{_FM}", len(_FM))
    if end == -1:
        # 只有开头有 --- 没有结尾：当作没有 frontmatter，全文当正文。
        # 不报错是刻意的 —— 半截的元数据块不该让内容读不出来。
        return {}, raw
    head = raw[len(_FM) : end].strip("\n")
    body = raw[end + len(_FM) + 1 :].lstrip("\n")

    meta: dict[str, Any] = {}
    for line in head.splitlines():
        if not line.strip() or ":" not in line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if v.startswith("[") and v.endswith("]"):
            inner = v[1:-1].strip()
            meta[k] = [x.strip() for x in inner.split(",") if x.strip()] if inner else []
        else:
            meta[k] = v
    return meta, body


@dataclass
class Note:
    """一条记忆。

    这是个**纯数据对象**（不持有文件句柄），所以可以安全地在异步代码里
    传递而不必担心生命周期。``path`` 是 vault 相对路径，用于溯源与外部工具定位。

    ``meta`` 是**完整的 frontmatter 字典**（含自定义字段）。为什么单独存一份
    而不是把所有字段都做成属性：自定义字段是开放的（用户/agent 可以写任意键），
    做成固定属性每加一个字段就要改三处（模型、序列化、解析）。
    ``meta`` 是那条逃生通道 —— 体系层（视图/时效）从它读扩展字段。
    """

    id: str
    title: str
    body: str
    tags: list[str] = field(default_factory=list)
    created: str = ""
    updated: str = ""
    author: str = ""
    workspace: str = ""
    path: str = ""
    #: 完整 frontmatter（含 type / status / claim / valid-until / related 等）
    meta: dict[str, Any] = field(default_factory=dict)

    # ── v2 概念字段（S-1）──
    # 都走 meta 不加 dataclass 字段：序列化/兼容旧文件的成本为零，
    # 缺省值即默认语义（旧数据没标过的按"结论/长期/无版本/继承空间"读）。

    @property
    def type(self) -> str:
        """记忆类型（六型之一）。缺省 ``fact``。"""
        return str((self.meta or {}).get("type") or "fact")

    @property
    def scope(self) -> str:
        """生效域：``long-term`` / ``task:<名>`` / ``archived``。缺省 long-term。"""
        return str((self.meta or {}).get("scope") or SCOPE_LONG_TERM)

    @property
    def versioned(self) -> bool:
        """是否进版本库。缺省 ``False``（默认无 git）。"""
        return bool((self.meta or {}).get("versioned"))

    @property
    def share(self) -> str:
        """共享档位：``mine`` / ``space`` / ``shared``。空 = 继承空间默认。"""
        return str((self.meta or {}).get("share") or "")

    def to_dict(self, *, with_body: bool = True, share_default: str = "") -> dict[str, Any]:
        """转成可 JSON 序列化的字典。

        ``with_body=False`` 用于列表/搜索结果 —— **正文可能很长**，
        列 200 条时全带上会把返回体撑爆（也会浪费 agent 的上下文预算）。
        只给摘要，要全文再调 memory_get。

        ``share_default`` 给了（非空）就额外输出 ``share_effective``：
        面板徽标要显示**实际生效档位**，而条目的 ``share`` 可能是空的
        （继承空间默认）。让前端再解析一遍继承规则等于把口径抄第二遍 ——
        继承只有一处定义（:func:`effective_share`）。

        体系字段（type/status/claim/时效/引用）会被提升到顶层 ——
        agent 读摘要时就能看到"这是推断"、"这条已过期"，
        不必再读全文才知道。
        """
        d: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "tags": list(self.tags),
            "created": self.created,
            "updated": self.updated,
        }
        if self.author:
            d["author"] = self.author
        if self.workspace:
            d["workspace"] = self.workspace

        # ── 体系字段提升到顶层 ──
        # 空值不输出（避免每个响应多一堆无信息字段）
        for key in (
            "type",
            "status",
            "claim",
            "valid-until",
            "review-after",
            "superseded-by",
            "related",
        ):
            v = (self.meta or {}).get(key)
            if v not in (None, "", []):
                d[key] = v

        # v2（S-2/S-10）：scope/share/versioned 也提升到顶层。
        # scope 只在**非缺省**（long-term）时输出 —— 缺省是最常见情形，
        # 每条都带一个 scope: long-term 是纯噪音；而 task:/archived
        # 是**需要一眼看见**的（决定这条能不能被默认检索到）。
        sc = self.scope
        if sc and sc != SCOPE_LONG_TERM:
            d["scope"] = sc
        sh = self.share
        if sh:
            d["share"] = sh
        if share_default:
            d["share_effective"] = effective_share(sh, share_default)
        if self.versioned:
            d["versioned"] = True

        if self.path:
            d["path"] = self.path
        if with_body:
            d["body"] = self.body
        return d


class Vault:
    """一个 git 仓库形态的共享记忆库。

    ⚠️ 本类**不做并发控制** —— 它假设调用方（queue.py 的单写者）
    已经保证了串行。这样存储层可以独立测试，也不承担它不该承担的职责。
    """

    def __init__(
        self,
        root: str | Path,
        *,
        auto_commit: bool = True,
        git_timeout: int = 30,
    ) -> None:
        """
        Args:
            root: vault 根目录。不存在会被创建。
            auto_commit: 是否启用 git 版本化（关闭后 commit() 变成空操作）。
            git_timeout: 单条 git 命令的超时秒数。手机上 git 偶尔会卡，
                设超时可以避免整条写入链路被一个卡住的 git 挂死。
        """
        self.root = Path(root).expanduser().resolve()
        self.notes_dir = self.root / "notes"
        self.trash_dir = self.root / ".trash"
        self.auto_commit = auto_commit
        self.git_timeout = git_timeout
        self.notes_dir.mkdir(parents=True, exist_ok=True)

        # ── 读缓存（性能层）──
        # 见 cache.py 的 docstring：基线 500 条时 all_notes 要 126ms，
        # 其中 97% 是文件 IO。有了它，未变更的文件不再重读重解析。
        self._cache = NoteCache()
        # git log 要 fork 子进程，开销固定且不小；纯 TTL 缓存 + 写时失效。
        self._log_cache: TTLCache[list[dict[str, str]]] = TTLCache(ttl=2.0)
        # 「是不是 git 仓库」的结论 —— 同样要 fork，用 TTL 缓存。
        self._is_repo_cache: TTLCache[bool] = TTLCache(ttl=5.0)
        # 标题/id 索引（惰性构建，靠目录签名判断是否过期）
        self._lookup_cache: tuple[dict[str, Path], dict[str, list[tuple[str, Path]]]] | None = None
        self._lookup_stamp: tuple[int, int] = (-1, -1)

    # ══════════════════════ 缓存维护 ══════════════════════

    def invalidate_cache(self, path: Path | None = None) -> None:
        """让缓存失效。

        **所有写路径都必须调它** —— 这是缓存正确性的唯一保障。
        ``path`` 为 None 表示全量失效（结构性变化：新增/删除文件）。

        索引缓存统一全量清：判断某个标题是否需要精确失效很难
        （可能因 slug 改名而路径变了），而重建索引只是一次遍历，
        宁可多重建也不返回错数据。
        """
        if path is None:
            self._cache.clear()
        else:
            self._cache.drop(Path(path))
        self._lookup_cache = None
        self._lookup_stamp = (-1, -1)
        self._log_cache.invalidate()


    # ══════════════════════ git ══════════════════════

    def _git(self, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        """跑一条 git 命令。失败不抛（由调用方决定要不要在意）。

        ⚠️ ``encoding="utf-8"`` + ``errors="replace"`` 是**必需**的：
        不指定时 subprocess 用**系统区域编码**解码（Windows 上是 GBK），
        而 git 输出是 UTF-8 —— 中文提交信息会让解码抛 UnicodeDecodeError，
        把一次已经成功的提交变成异常。实测踩过。

        ``check=False`` 是刻意的：git 的「非零退出」在很多场景下是正常结果
        （比如 `diff --cached --quiet` 用退出码表示"有/无改动"），
        统一抛异常会让调用方难以区分「失败」和「有内容」。
        """
        # ⚠️ GIT_CEILING_DIRECTORIES 是**必需**的，不是加固：
        # `git -C <dir>` 默认会向上遍历找 .git。当记忆库放在一个本身是
        # git 仓库的目录里（生产 /opt/shared-memory-mcp 就是代码仓库），
        # 记忆的 add/commit 会落进**代码仓库** —— 代码改动被
        # "memory: 自动提交" 卷走，而记忆自己反而没有版本化。
        # 设成库目录本身 = 禁止再往上找，git 只认这个目录及其子目录。
        git_env = dict(os.environ)
        git_env["GIT_CEILING_DIRECTORIES"] = str(self.root)
        try:
            return subprocess.run(
                ["git", "-C", str(self.root), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=check,
                timeout=self.git_timeout,
                env=git_env,
            )
        except subprocess.TimeoutExpired:
            # 超时也返回一个 CompletedProcess，而不是抛 —— 保持"不抛"的契约，
            # 让上层用 returncode 判断（timeout 用 -1 表示，不会与 git 的退出码冲突）
            logger.warning("git %s 超时（%ss）", " ".join(args), self.git_timeout)
            return subprocess.CompletedProcess(
                args=["git", *args], returncode=-1, stdout="", stderr="timeout"
            )
        except FileNotFoundError:
            # 目标机没装 git —— 记忆服务**不应该因此起不来**
            logger.error("找不到 git 可执行文件；版本化功能不可用")
            return subprocess.CompletedProcess(
                args=["git", *args], returncode=-1, stdout="", stderr="git not found"
            )

    def is_repo(self) -> bool:
        """这个目录是不是 git 工作树。

        ⚠️ 每条 ``git`` 子进程 ≈ 60ms，而 ``stats()``（面板每次刷新都调）
        会用到它。用 ``.git`` 目录存在性做**快速判定**——它不 fork：
        仓库一定有 ``.git``（工作树是文件、裸仓库是目录，两种都存在）。
        真的没有 ``.git`` 时才退回跑 git 确认（兼容 worktree 指针等边缘情况）。

        这样常态路径零 fork，只有「真的不是仓库」才付 60ms ——
        而那通常发生在初始化之前，一次性成本。
        """
        cached = self._is_repo_cache.get()
        if cached is not None:
            return cached
        # ⚠️ 不能只看 `.git` 存在：那个 .git 可能是空壳/失效的，
        # 也可能是**上级仓库**被 git 向上遍历"看到"的。
        # 唯一可靠的判定：确认仓库顶层就是本目录。
        # （加上 _git 的 GIT_CEILING_DIRECTORIES 后，git 不会再越界）
        r = self._git("rev-parse", "--show-toplevel")
        result = False
        if r.returncode == 0 and r.stdout.strip():
            try:
                toplevel = Path(r.stdout.strip()).resolve()
                result = toplevel == self.root.resolve()
            except OSError:
                result = False
        self._is_repo_cache.set(result)
        return result

    def init_repo(self) -> None:
        """首次初始化：git init + .gitignore + README + 首次提交。

        幂等：已经是仓库时直接返回。

        ⚠️ 显式设 user.name/user.email：容器/CI 里常常没有全局 git 身份，
        不设的话 `git commit` 会失败，而错误信息（"Please tell me who you are"）
        对使用者来说毫无指向性。
        """
        if self.is_repo():
            return
        self._git("init", "-b", "main")
        self._git("config", "user.name", "shared-memory")
        self._git("config", "user.email", "shared-memory@local")
        # 仓库刚建出来：把「不是仓库」的旧结论清掉
        self._is_repo_cache.invalidate()

        # 运行时产物不该进版本库
        (self.root / ".gitignore").write_text(
            "# 运行时产物，不该进版本库\n"
            "*.tmp\n"
            "*.lock\n"
            ".DS_Store\n"
            "Thumbs.db\n",
            encoding="utf-8",
        )
        (self.root / "README.md").write_text(
            "# 共享记忆库（Shared Memory Vault）\n\n"
            "这个目录是多个 AI agent 共享的记忆存储。\n\n"
            "- `notes/` —— 每条记忆一个 Markdown 文件，可直接阅读和编辑\n"
            "- `.trash/` —— 软删除的回收站，可手工恢复\n"
            "- 整个目录是 git 仓库，改动会自动提交，可 diff、可回滚\n"
            "- 由 shared-memory-mcp 服务管理写入，人也可以直接改文件\n\n"
            "> 直接用文本编辑器改文件是**支持的用法** —— "
            "服务读的是文件内容，不是缓存。\n",
            encoding="utf-8",
        )
        self._git("add", "-A")
        self._git("commit", "-m", "chore: 初始化共享记忆库")
        logger.info("已初始化 vault: %s", self.root)

    def commit(self, message: str, author: str = "") -> dict[str, Any]:
        """提交当前改动。

        **没有变化时不算失败** —— 返回 ``committed=False`` 而不是抛异常。
        为什么：自动提交是防抖触发的，很可能在"这段时间没人写"时被调用，
        那是正常情况，不该报错。

        ━━━ author 参数（多租户的关键）━━━

        用 ``git -c user.name=… -c user.email=…`` **单次覆盖**，
        而不是改仓库级 config。理由：
          · 仓库级 config 是全局的，会被下一次别人的提交覆盖
          · 单次覆盖只影响这一条提交，天然并发安全
          · 不改用户的 git 配置（那个是他们的环境，不是我们的）

        这样 git 历史里能看到"这条是 bob 写的"，而 frontmatter 里的
        author 字段可以随手删 —— **删了 git 还记着**。

        ⚠️ **性能：git 子进程是这里的主要成本**（实测单次 fork ≈ 60ms）━━

        原实现要 fork **5 次**：``is_repo`` + ``add`` + ``diff --cached`` +
        ``commit`` + ``rev-parse --short HEAD`` —— 光启动进程就 300ms。

        优化后只 fork **2 次**：
          1. ``commit -a``（``-a`` 自动 stage 已跟踪文件的修改，
             但新增文件仍需 ``add``）
          2. 拿 sha 用 ``git log -1 --format=%h`` 与 log 复用同一套解析

        而「有没有改动」不再单独 ``diff`` 判断 —— 直接看 commit 的返回：
        git 在无改动时返回非 0 且 stderr 含 ``nothing to commit``，
        这与原来的 ``committed=False, message="无改动"`` 语义一致。

        ⚠️ 保留 ``add -A``：新增记忆文件必须被 stage，``-a`` 不管未跟踪文件。

        Args:
            message: 提交说明。
            author: 作者名。空则用仓库默认身份。

        Returns:
            ``{"committed": bool, "message": str, "commit": str?, "author": str?}``
        """
        # 先 add（含新增文件），再一次 commit —— 合并掉原来的 diff 探测
        self._git("add", "-A")

        if author:
            # 邮箱用 @local 后缀 —— 不伪造外部邮箱（那会让
            # "看起来像来自某公司"造成误解），又满足 git 的格式要求
            r = self._git(
                "-c", f"user.name={author}",
                "-c", f"user.email={author}@shared-memory.local",
                "commit", "-m", message,
            )
        else:
            r = self._git("commit", "-m", message)

        if r.returncode != 0:
            err = (r.stderr or r.stdout).strip()[:300]
            # 无改动是**正常情况**（防抖提交在没人写时也会触发），不算失败
            if "nothing to commit" in err or "no changes added" in err \
                    or "working tree clean" in err:
                return {"committed": False, "message": "无改动"}
            if "not a git repository" in err:
                return {"committed": False, "message": "不是 git 仓库"}
            return {"committed": False, "message": err}

        # 提交成功 → log 缓存必须失效（否则面板会显示旧的提交列表）
        self._log_cache.invalidate()
        # sha 用 rev-parse 拿（commit 本身的输出里没有短 sha）
        sha = self._git("rev-parse", "--short", "HEAD").stdout.strip()
        out: dict[str, Any] = {"committed": True, "message": message, "commit": sha}
        if author:
            out["author"] = author
        return out

    def log(self, limit: int = 10) -> list[dict[str, str]]:
        """最近若干条提交。用在面板与 memory_stats 里。

        输出里带 ``author`` —— 多租户下面板必须能看出"这条是谁提交的"，
        否则十几个人改过的库看起来像一个人在改。

        ⚠️ **性能：这里的每个 git 子进程都是真金白银的开销** ━━━

        实测（本机，单次）：``git log`` ≈ 60ms，``git rev-parse`` ≈ 60ms，
        ``git rev-parse --is-inside-work-tree`` ≈ 60ms。
        也就是说一次「判断是否仓库 + 取 HEAD + 取 log」要 fork **3 次** = 180ms。

        早期版本想用「比对 HEAD 哈希」来判断缓存是否过期 ——
        但那需要先跑 ``rev-parse``，**等于为了省一次 git log 而多付一次 git**，
        实测缓存命中率是 0，纯属白做功。

        现在的做法：
          · 纯 TTL 缓存（2s）—— 面板刷新和 stats 在 2 秒内复用
          · **所有 commit() 成功后显式失效** —— 保证新提交立即可见

        所以正确性不依赖时钟，而是依赖「写路径必定失效」这一条不变式；
        TTL 只兜底「绕过服务直接跑 git commit」这种罕见情况。
        """
        cap = max(1, min(limit, 50))
        cached = self._log_cache.get()
        # 命中条件（二者之一即可）：
        #   a) 缓存条数 >= cap —— 足够满足本次请求
        #   b) 缓存条数 < _LOG_CACHE_MAX —— 说明当初取不满、**历史已到底**，
        #      这份缓存就是全量，对任何 cap 都是完整的
        #
        # ⚠️ 只有 (a) 会让「新建库（历史仅几条）」永远不命中 ——
        #   实测这是最常见的场景，却每次都要 fork git（22ms），
        #   白白浪费。（b) 才是这个场景的正解。
        if cached is not None and (
            len(cached) >= cap or len(cached) < _LOG_CACHE_MAX
        ):
            return cached[:cap]

        if not self.is_repo():
            return []
        # 始终按 _LOG_CACHE_MAX 取满再缓存，然后按调用方的 limit 切片。
        #
        # 为什么不按 cap 取再缓存：limit=3 的请求会把一份 3 条的结果
        # 写进缓存，之后 limit=50 的请求因「缓存只有 3 条 < 50」而失效，
        # 于是每次 limit=50 都要重新 fork —— 缓存形同虚设（实测踩过）。
        r = self._git(
            "log", f"-{_LOG_CACHE_MAX}",
            "--format=%h|%ci|%an|%s",
        )
        out: list[dict[str, str]] = []
        for line in r.stdout.splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                out.append({
                    "commit": parts[0], "date": parts[1],
                    "author": parts[2], "subject": parts[3],
                })
            elif len(parts) == 3:  # 兼容旧格式（无 author 的历史）
                out.append({
                    "commit": parts[0], "date": parts[1],
                    "author": "shared-memory", "subject": parts[2],
                })
        # 缓存条件：**取不满 _LOG_CACHE_MAX = 历史已到底**，
        # 此时这份结果对任何 cap 都是完整的，必须缓存
        #（新建库最常见的就是这种，实测早期版本因此永不命中）。
        # 取满了 50 条则不缓存 —— 下次可能已有新提交，且 cap 可能更大。
        if len(out) < _LOG_CACHE_MAX:
            self._log_cache.set(list(out))
        return out[:cap]

    # ══════════════════════ 文件定位 ══════════════════════

    def _iter_files(self):
        """遍历 notes 目录下所有 .md 文件（按名排序，保证结果稳定）。"""
        return sorted(self.notes_dir.glob("*.md"))

    def _build_lookup(self) -> tuple[dict[str, Path], dict[str, list[tuple[str, Path]]]]:
        """一次遍历，同时构建 **id 索引** 和 **标题索引**。

        返回 ``(by_id, by_title)``：
          · ``by_id``    —— note_id → Path
          · ``by_title`` —— title → [(updated, Path), ...]（同标题可能多条）

        为什么值得单独抽出来：``resolve()`` 原本要先扫一遍找 id、
        再扫一遍找标题 —— **两次全库扫描**，而且每次都重读重解析。
        合成一次（且走解析缓存）后只扫一遍。

        索引本身也缓存：``_lookup_stamp`` 记录构建时的 notes 目录签名，
        没变就直接复用；变了（增删文件会改目录 mtime）就重建。
        """
        stamp = self._dir_stamp()
        if self._lookup_cache is not None and self._lookup_stamp == stamp:
            return self._lookup_cache

        by_id: dict[str, Path] = {}
        by_title: dict[str, list[tuple[str, Path]]] = {}
        for p in self._iter_files():
            try:
                note = self._read_note(p)  # 走缓存，不重复 IO
            except (OSError, UnicodeDecodeError):
                continue  # 坏文件跳过，不让一条烂数据阻断整次查找
            if note.id:
                by_id[note.id] = p
            by_title.setdefault(note.title.strip(), []).append(
                (note.updated or note.created or "", p)
            )

        self._lookup_cache = (by_id, by_title)
        self._lookup_stamp = stamp
        return self._lookup_cache

    def _dir_stamp(self) -> tuple[int, int]:
        """notes 目录的 (mtime_ns, .md 条数) —— 判据「文件集合有没有变」。

        目录 mtime 会在增删/改名时更新，能捕获结构性变化；
        配合逐文件缓存的 mtime，覆盖「内容变」与「文件增删」两类失效。

        ⚠️ **只 stat 目录本身，不遍历**。早期版本在这里再做一次 glob 数文件，
        实测那一步就占 2.2ms —— 而全量文件 stat 要 47ms，
        在热路径上做这种全量遍历是纯粹的浪费。目录 mtime 已经够用：
        任何增删改名都会更新它。
        """
        try:
            return (self.notes_dir.stat().st_mtime_ns, 0)
        except OSError:
            return (0, 0)

    def _find_by_title(self, title: str) -> Path | None:
        """按**标题**（而非文件名）找文件。

        标题是逻辑标识，文件名是物理落点（可能被 _slug 改过、
        可能带 `--id` 后缀）—— 标题才是稳定标识。

        性能：改走共用标题索引，不再自己 O(n) 扫全库。
        """
        target = title.strip()
        _, by_title = self._build_lookup()
        cands = by_title.get(target)
        if not cands:
            return None
        # 同标题多条 → 取最近更新的（与 resolve 语义一致）
        return max(cands, key=lambda kv: kv[0])[1]

    def _find_by_id(self, note_id: str) -> Path | None:
        """按 id 找文件。id 是创建时生成的，之后不变。"""
        by_id, _ = self._build_lookup()
        return by_id.get(note_id.strip())

    def resolve(self, ident: str) -> Path:
        """把「标题或 id」解析成文件路径。找不到抛 NOT_FOUND。

        先查 id 再查标题：id 格式固定（``mem-...``）不会与人类标题混淆，
        且 id 查询是精确匹配，命中更快也更不容易误伤。

        ⚠️ **标题不保证唯一**：软删除后重建同名记忆、或恢复一条同名记忆，
        都会让库里存在多条同标题条目。此时按标题查找返回**最近更新的那条**
        （而不是文件系统顺序里的第一条 —— 那会让结果随文件重命名而变）。
        需要精确定位时请用 id。
        """
        ident = (ident or "").strip()
        if not ident:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, "标题/id 不能为空")
        by_id, by_title = self._build_lookup()
        p = by_id.get(ident)
        if p is not None:
            return p
        # 标题可能有多条 → 取最近更新的那条，保证结果稳定可预期
        cands = by_title.get(ident)
        if cands:
            return max(cands, key=lambda kv: kv[0])[1]
        raise ToolError(
            ErrorCode.NOT_FOUND,
            f"找不到记忆：{ident}",
            hint="用 memory_list 或 memory_search 查看现有记忆的标题",
        )

    def _unique_path(self, title: str, note_id: str, *, force_new: bool = False) -> Path:
        """决定一条记忆写到哪个文件名。

        三种情况：
          1. 目标文件名不存在         → 直接用
          2. 存在但**是同一条记忆**   → 用它（更新语义）
          3. 存在但是**另一条**       → 加 `--<id>` 后缀

        情况 3 是防数据丢失的关键：两个不同的标题 slug 后可能撞名
        （比如 "部署 A" 和 "部署/A" 都可能 slug 成 "部署-A"），
        不加后缀会**静默覆盖**其中一条。

        Args:
            force_new: **强制使用一个全新的文件名**，绝不返回已存在的路径。
                ``restore()`` 必须用它 —— 恢复的语义是"把这条加回来"，
                而"加回来"时如果已有同名记忆，复用它等于**覆盖别人的内容**。
                实测踩过：不设 force_new 时 restore 会撞上已存在的文件，
                在 Windows 上直接抛 FileExistsError。
        """
        base = _slug(title)

        def fresh() -> Path:
            """一个**肯定不存在**的路径。

            用 note_id 但带存在性检查：restore 场景下 note_id 是原条目的 id
            （不会变），所以同一份回收站文件被恢复两次会得到同一个候选路径 ——
            那种情况下必须再退让一步，否则 rename 会覆盖或抛 FileExistsError。
            """
            p = self.notes_dir / f"{base}--{note_id}.md"
            if not p.exists():
                return p
            # 极罕见：连带 id 的名字也占了 → 再挂一个随机短码
            import secrets

            return self.notes_dir / f"{base}--{note_id}-{secrets.token_hex(2)}.md"

        p = self.notes_dir / f"{base}.md"
        if force_new:
            return p if not p.exists() else fresh()
        if not p.exists():
            return p
        try:
            meta, _ = parse_note(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            return fresh()
        if str(meta.get("title", "")).strip() == title.strip():
            return p  # 情况 2：同一条记忆，允许更新
        return fresh()  # 情况 3：不同记忆撞名

    # ══════════════════════ 读 ══════════════════════

    def _read_note(self, p: Path) -> Note:
        """把一个文件读成 Note 对象。

        带缓存：``(mtime_ns, size)`` 未变则直接复用上次解析结果。

        ⚠️ **一个刻意的正确性/性能权衡** ━━━

        朴素做法是每次都 ``stat()`` 再比 stamp。但实测 500 个文件全量
        stat 要 ~47ms，是 ``all_notes`` 的最大开销（比解析贵 6000 倍）。

        所以这里分两条路：
          · **命中 stamp 缓存** → 直接比对缓存里的 stamp，零 IO
          · **未命中**（首次见到这个文件）→ 才 stat

        代价：**如果外部程序改了文件内容但不更新 mtime**，缓存不会察觉。
        实践中这不会发生 —— 任何写操作都会更新 mtime_ns。
        而且写入路径（put/delete/restore）都显式调 ``invalidate_cache``，
        所以「通过 MCP 写」这条主路径一定是最新的。

        「人用编辑器直接改文件」这条旁路仍能被发现，因为编辑器会更新 mtime，
        下次该文件的 stamp 缓存因内容变化而在**目录重建时**被刷新
        （见 ``_notes_snapshot``）。
        """
        cached_stamp = self._cache.stamp_of(p)
        if cached_stamp is not None:
            cached = self._cache.get(p, cached_stamp)
            if cached is not None:
                return cached
        else:
            try:
                st = p.stat()
            except OSError:
                raise
            cached_stamp = (st.st_mtime_ns, st.st_size)
            cached = self._cache.get(p, cached_stamp)
            if cached is not None:
                return cached

        assert cached_stamp is not None
        meta, body = parse_note(p.read_text(encoding="utf-8"))
        note = Note(
            id=str(meta.get("id", "")),
            title=str(meta.get("title", p.stem)),
            body=body,
            tags=list(meta.get("tags", []) or []),
            created=str(meta.get("created", "")),
            updated=str(meta.get("updated", "")),
            author=str(meta.get("author", "")),
            workspace=str(meta.get("workspace", "")),
            path=str(p.relative_to(self.root)),
            # 完整 frontmatter：体系层（视图/时效/引用）要读扩展字段。
            # 深拷贝一份避免与调用方的 meta 互相影响。
            meta=dict(meta),
        )
        self._cache.put(p, cached_stamp, note)
        return note

    def all_notes(self) -> list[Note]:
        """读出全部记忆。

        **读不了的单个文件会被跳过**（而不是让整个操作失败）——
        记忆库是长期资产，一条坏数据不该让整库不可用。
        这些文件会出现在 stats 的 unreadable 计数里，便于排查。

        性能：逐文件走 (mtime_ns,size) 缓存，未变更的文件不重读。
        每隔 ``revalidate_every`` 秒做一次全量 stamp 校验，
        以便吸进「绕过服务被外部修改」的内容（见 cache.py 的说明）。
        """
        self._maybe_revalidate()
        notes: list[Note] = []
        for p in self._iter_files():
            try:
                notes.append(self._read_note(p))
            except (OSError, UnicodeDecodeError) as exc:
                logger.warning("跳过无法读取的记忆文件 %s: %s", p.name, exc)
                continue
        return notes

    def _maybe_revalidate(self) -> None:
        """到点就做一次全量 stamp 刷新：把每个缓存文件的 mtime 重新读一遍。

        命中后对应缓存条目会被更新/失效，从而让「外部改文件」能被吸进来。
        频率由 ``NoteCache.revalidate_every`` 控制（默认 5s），
        远低于常态查询频率，所以摊薄成本可忽略；
        但它保证了缓存不会「无限期返回陈旧内容」。
        """
        if not self._cache.should_revalidate():
            return
        for p in self._iter_files():
            try:
                st = p.stat()
            except OSError:
                self._cache.drop(p)  # 文件没了
                continue
            fresh = (st.st_mtime_ns, st.st_size)
            old = self._cache.stamp_of(p)
            if old is None:
                self._cache.set_stamp(p, fresh)
            elif old != fresh:
                # 内容变了 → 让 _read_note 下次重新解析
                self._cache.drop(p)

    def get(self, ident: str) -> Note:
        """按标题或 id 读一条记忆（含全文）。"""
        return self._read_note(self.resolve(ident))

    def exists(self, title: str) -> bool:
        """标题是否已存在（供 create 模式预检）。"""
        return self._find_by_title(title) is not None

    # ══════════════════════ 写 ══════════════════════
    # 以下方法都会被 queue 包起来串行调用 —— 见模块 docstring 的职责边界

    def put(
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
        """写入一条记忆。

        Args:
            title: 标题，**按标题去重**（同标题 = 同一条记忆）。
            body: 正文，Markdown。
            tags: 标签列表。``upsert`` 模式下若不传则**保留原有标签**
                （避免只想改正文却把标签清空）。
            author: 谁写的。仅作溯源，**不用于隔离**。
            workspace: 这条记忆属于哪个工作区/项目。
                ━━━ 为什么需要它 ━━━
                共享记忆库是**跨项目**的：同一个库里有 A 项目的踩坑、
                B 项目的口径、以及跟项目无关的基础设施笔记。没有工作区维度，
                检索时它们会混在一起 —— 在 A 项目里搜「部署」，
                可能捞到 B 项目的部署笔记，而 agent 无从分辨。

                约定（与 dsh-tiddlywiki 插件一致）：
                  · 存进 frontmatter 的 ``workspace`` 字段
                  · **自动追加标签** ``ws/<workspace>``，这样按标签过滤
                    也能命中，且不需要理解额外参数
                  · 留空表示"跨项目/基础设施"类记忆（完全合法，
                    比如部署档案、工具配置这类跟项目无关的内容）
            mode:
              · ``upsert``（默认）有则更新，无则新建
              · ``create``  只新建，已存在则报 CONFLICT
              · ``append``  追加到正文末尾，保留已有内容
            extra: 额外写入 frontmatter 的自定义字段。

        Returns:
            ``{"ok", "action", "id", "title", "path", "workspace"}``
        """
        title = (title or "").strip()
        if not title:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, "title 不能为空")
        body = body or ""
        ts = now_iso()
        workspace = (workspace or "").strip()
        tags = [t.strip() for t in (tags or []) if str(t).strip()]
        # 自动补工作区标签（与 tiddlywiki 插件同口径）。
        # 只在调用方没显式给过时加，避免重复。
        if workspace:
            wtag = f"ws/{workspace}"
            if wtag not in tags:
                tags.append(wtag)

        existing = self._find_by_title(title)

        if existing is not None:
            if mode == "create":
                raise ToolError(
                    ErrorCode.CONFLICT,
                    f"记忆已存在：{title}",
                    hint="换个标题，或用 mode=upsert 更新它",
                )
            try:
                meta, old_body = parse_note(existing.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                meta, old_body = {}, ""
            new_body = (
                f"{old_body.rstrip()}\n\n{body}".strip() if mode == "append" else body
            )
            meta.update({"title": title, "updated": ts})
            # v2（S-1）：缺省补值 + 显式值校验。
            # setdefault 而非覆盖——更新时不能把显式标过的类型/域改掉。
            if extra:
                validate_v2_fields(extra)
            meta.setdefault("type", "fact")
            meta.setdefault("scope", SCOPE_LONG_TERM)
            # 保留 created：它记录的是这条记忆**最初**被写下的时间，
            # 更新时不该被覆盖（否则"最近创建"这类排序会失真）
            if "created" not in meta:
                meta["created"] = ts
            if tags:
                meta["tags"] = tags
            if author:
                meta["author"] = author
            # 工作区：只在显式传了才改。不传时**保留原值** ——
            # 否则一次"只改正文"的更新会把它从原项目里摘出去。
            if workspace:
                meta["workspace"] = workspace
            if extra:
                meta.update(extra)
            existing.write_text(
                f"{dump_frontmatter(meta)}\n\n{new_body}\n", encoding="utf-8"
            )
            # 写后必须立刻可读 —— read-your-writes 是硬要求
            self.invalidate_cache(existing)
            return {
                "ok": True,
                "action": "updated",
                "id": meta.get("id", ""),
                "title": title,
                "workspace": str(meta.get("workspace", "")),
                "path": str(existing.relative_to(self.root)),
            }

        note_id = _new_id()
        path = self._unique_path(title, note_id)
        if extra:
            validate_v2_fields(extra)
        meta: dict[str, Any] = {
            "id": note_id,
            "title": title,
            "tags": tags,
            "created": ts,
            "updated": ts,
            # v2（S-1）缺省：不填也有值——agent 赶时间不会记得传。
            # versioned/share 刻意不写缺省：一个噪音字段换不来信息，
            # 读取端按缺省语义处理（False / 继承空间）。
            "type": "fact",
            "scope": SCOPE_LONG_TERM,
        }
        if author:
            meta["author"] = author
        if workspace:
            meta["workspace"] = workspace
        if extra:
            meta.update(extra)
        path.write_text(f"{dump_frontmatter(meta)}\n\n{body}\n", encoding="utf-8")
        self.invalidate_cache(path)
        return {
            "ok": True,
            "action": "created",
            "id": note_id,
            "title": title,
            "workspace": workspace,
            "path": str(path.relative_to(self.root)),
        }

    def delete(self, ident: str) -> dict[str, Any]:
        """**软删除**：移到 ``.trash/`` 保留可恢复。

        刻意不物理删除 —— 记忆是长期资产，误删的代价远大于多占一点空间。

        回收站文件名 = ``<原名>--<时间戳>-<随机>.md``。
        **随机后缀是必需的**：时间戳只精确到秒，同一秒内删两次同名记忆
        会撞文件名并抛 FileExistsError（实测踩过）。
        随机后缀还顺带保证了「同一条被删多次，每次都有独立的可恢复副本」。

        要真删：vault 是普通 git 仓库，直接操作文件即可（且有完整历史可回退）。
        """
        p = self.resolve(ident)
        try:
            meta, _ = parse_note(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            meta = {}
        self.trash_dir.mkdir(parents=True, exist_ok=True)
        dest = self.trash_dir / f"{p.stem}--{_stamp()}.md"
        p.rename(dest)
        # 文件离开 notes/ → 结构变了，索引必须重建
        self.invalidate_cache(p)
        return {
            "ok": True,
            "action": "deleted",
            "title": str(meta.get("title", p.stem)),
            "moved_to": str(dest.relative_to(self.root)),
            "note": "已移入 .trash（可恢复）；如需彻底删除请手工处理",
        }

    def trash(self) -> list[dict[str, str]]:
        """列出回收站内容。"""
        if not self.trash_dir.exists():
            return []
        out: list[dict[str, str]] = []
        for p in sorted(self.trash_dir.glob("*.md"), reverse=True):
            out.append({"file": p.name, "path": str(p.relative_to(self.root))})
        return out

    def gc_trash(self, *, max_age_days: int = 30, max_files: int = 0) -> dict[str, Any]:
        """回收站懒清理（方案 5b）—— **物理删除**超期/超量的回收站文件。

        ━━━ 为什么可以物理删（约束 3 的例外）━━━

        回收站文件在 vault 的 git 仓库里（``commit()`` 是 ``add -A``），
        删掉只是从工作树移除，**git 全历史仍可恢复** —— 最终兜底一直都在。
        trash 真正需要覆盖的只是"近期误删"，不是无限期仓库。

        ━━━ 为什么是懒清理而不是后台任务 ━━━

        共享库里"自动删别人的东西"要先有共识；懒清理挂在 stats/trash
        这些**本来就要看回收站**的调用上，不占写路径、不起定时任务，
        且每次清理都可解释（"我在看库况时顺手清的"）。

        Args:
            max_age_days: 超过这个天数（按文件 mtime）的回收站文件清除。0 = 不看年龄。
            max_files: 回收站条数上限，超出按**从旧到新**删到上限内。0 = 不看条数。

        Returns:
            ``{"removed": n, "kept": n, "freed_bytes": b}``
        """
        if not self.trash_dir.exists():
            return {"removed": 0, "kept": 0, "freed_bytes": 0}
        import time

        files = sorted(self.trash_dir.glob("*.md"), key=lambda p: p.stat().st_mtime)
        removed = 0
        freed = 0
        now = time.time()
        cutoff = now - max_age_days * 86400 if max_age_days > 0 else None

        # 先按年龄删（保留较新的）
        for p in files:
            if cutoff is not None and p.stat().st_mtime < cutoff:
                try:
                    freed += p.stat().st_size
                    p.unlink()
                    removed += 1
                except OSError as exc:  # noqa: BLE001
                    logger.warning("回收站清理失败 %s: %s", p.name, exc)

        # 再按条数删（仍超量 → 继续删最旧的）
        if max_files > 0:
            remaining = sorted(self.trash_dir.glob("*.md"), key=lambda p: p.stat().st_mtime)
            for p in remaining[: max(0, len(remaining) - max_files)]:
                try:
                    freed += p.stat().st_size
                    p.unlink()
                    removed += 1
                except OSError as exc:  # noqa: BLE001
                    logger.warning("回收站清理失败 %s: %s", p.name, exc)

        # ⚠️ Python 3.13 的 Path.glob() 返回惰性 map，不能 len() —— 用 sum 数
        kept = sum(1 for _ in self.trash_dir.glob("*.md"))
        if removed:
            # 文件离开 notes/ 之外的目录也会改 git 状态 → 让提交带上这次清理
            self.invalidate_cache()
            logger.info("回收站清理：删除 %d 个（保留 %d，释放 %d 字节）", removed, kept, freed)
        return {"removed": removed, "kept": kept, "freed_bytes": freed}

    def restore(self, filename: str) -> dict[str, Any]:
        """从回收站恢复一条记忆。

        Args:
            filename: 回收站里的文件名（``memory_trash`` 返回的 ``file``）。
        """
        src = self.trash_dir / Path(filename).name
        if not src.exists():
            raise ToolError(
                ErrorCode.NOT_FOUND,
                f"回收站里没有这个文件：{filename}",
                hint="用 memory_trash 列出回收站内容",
            )
        try:
            meta, _ = parse_note(src.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            meta = {}
        title = str(meta.get("title", "")).strip() or src.stem.split("--")[0]
        # force_new=True：恢复的语义是"把这条加回来"。
        # 若已有同名记忆，**绝不能复用它** —— 那等于把别人的内容覆盖掉。
        dest = self._unique_path(title, str(meta.get("id", "restored")), force_new=True)
        src.rename(dest)
        self.invalidate_cache(dest)
        return {
            "ok": True,
            "action": "restored",
            "title": title,
            "path": str(dest.relative_to(self.root)),
        }

    # ══════════════════════ 搜索 ══════════════════════
    # 读路径不进队列：不竞争、要快

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
        status: str = "active",
        scope: str = SCOPE_LONG_TERM,
        viewer: str = "",
        cross_space: bool = False,
    ) -> dict[str, Any]:
        """检索记忆。

        ━━━ 为什么不用 embedding ━━━

        引入向量检索 = 引入 API key、网络依赖、额度限制、模型行为四个
        额外失败点，换来的语义召回在当前量级不划算。子串匹配 + 标签过滤
        已经够用，而且**结果可复现**（同样的库和查询永远同样排序）。

        ━━━ 计分规则 ━━━

        标题 > 标签 > 正文：

          · 标题开头命中   100   （最强信号：标题以关键词起头）
          · 标题包含        60
          · 标签包含        40
          · 正文每处         8，最多计 5 处（防止长文靠堆词霸榜）

        多个关键词是 **AND** —— 全部命中才算（与多数搜索工具一致，
        也更符合"我要找同时提到这两件事的记忆"这个真实意图）。

        Args:
            query: 关键词，空格分隔（多词 AND）。
            tags: 标签过滤，**要求全部命中**（AND）。
            workspace: 工作区过滤。填 ``__none__`` 可查"无工作区"的记忆
                （基础设施类笔记）；留空 = 不过滤（跨项目全搜）。
            limit: 返回条数上限（1..100）。
            offset: 跳过前 N 条（分页用）。
            since: 只要更新时间 >= 该时间（ISO 格式，如 ``2026-10-01``）。
            until: 只要更新时间 <= 该时间。
            sort: ``relevance``（默认，按得分）/ ``updated``（按更新时间倒序）。
            claim: 陈述性质过滤（``fact`` / ``inference`` / ``proposal``），
                空 = 不限。**这是反幻觉的读端抓手** —— 问"现行口径"时
                要 fact，看到 inference 该多留个心眼。
            status: 时效状态过滤（**默认 ``active``**，见下方说明）。
                ``active`` 排除 superseded/expired（review 仍算现行）；
                ``all`` = 不过滤（考古模式）。
            scope: 生效域过滤（v2 S-1，**默认 ``long-term``**——防串台的第一闸：
                任务过程态根本不进默认检索）。``task:<名>`` 只要该任务域；
                ``archived`` 只要归档区；``all`` 全都要（考古）；
                传空串 = 不过滤（兼容旧调用的逃生门）。
            viewer: 当前身份（v2 S-10.2）。**空 = 不做共享档位过滤**
                （内部通道）。给了身份就按 ``share`` 三档筛：
                ``mine`` 只给作者、``space``/``shared`` 给同空间全部读者。
                筛选发生在**打分与 total 之前** —— 否则"共 N 条"会把
                看不见的条目数也报出去，那个数字本身就是泄露。
            cross_space: 经目标空间授权、从**别的空间**捞进来的那次调用。
                True 时只放行 ``shared`` 档位（见 :func:`share_visible`）。

        ━━━ 为什么 status 默认 active（2026-10-13 决策 7）━━━

        agent 问"部署的现行口径"时，superseded 旧条和过期条混在结果里
        是**最典型的误用来源** ——  stale-by-default 是缺陷不是特性。
        默认只返回 active，考古显式 ``status=all``。
        行为变更已在 README 与工具描述里标注。

        Returns:
            ``{"ok", "query", "total", "count", "offset", "results"}``
            —— ``total`` 是过滤后的总数（不只是本页），便于分页。
        """
        q = (query or "").strip().lower()
        words = [w for w in q.split() if w] if q else []
        want = {t.strip().lower() for t in (tags or []) if str(t).strip()}
        want_ws = (workspace or "").strip()
        since_dt = parse_iso(since) if since else None
        until_dt = parse_iso(until) if until else None
        want_claim = (claim or "").strip()
        want_status = (status or "active").strip().lower()
        if want_status not in ("active", "all", "review", "expired",
                               "superseded"):
            raise ToolError(
                ErrorCode.INVALID_ARGUMENT,
                f"status 只能是 active / all / review / expired / "
                f"superseded，收到 {status!r}",
                hint="active=只要现行口径（默认）；all=含被取代/已过期的"
                     "（考古）；review/expired/superseded=只要那一态",
            )
        only_active = want_status == "active"
        # v2（S-1）：scope 生效域。默认 long-term（防串台第一闸）；
        # 空串 = 不过滤（旧调用逃生门）；all = 考古。
        want_scope = (scope or "").strip()
        if want_scope and want_scope != "all" and not valid_scope(want_scope):
            raise ToolError(
                ErrorCode.INVALID_ARGUMENT,
                f"scope 格式不对：{scope!r}",
                hint="long-term / task:<任务名> / archived / all"
                     "（留空=不过滤）",
            )
        # v2（S-10.2）：共享档位。没给身份 = 不过滤（内部通道），
        # 给了才去读空间默认档 —— 不筛选时连配置文件都不碰。
        want_viewer = (viewer or "").strip()
        space_default = Config(self.root).default_share()

        scored: list[tuple[int, Note]] = []
        for note in self.all_notes():
            # ① 工作区过滤。`__none__` 是查"无工作区"的哨兵值 ——
            #    直接用空字符串的话，就无法区分"不过滤"和"要空的"。
            if want_ws:
                if want_ws == "__none__":
                    if note.workspace:
                        continue
                elif note.workspace != want_ws:
                    continue

            # ② 标签过滤（AND）
            note_tags = {t.lower() for t in note.tags}
            if want and not want.issubset(note_tags):
                continue

            # ③ 时间过滤。用 updated 而非 created：找"最近的讨论"时，
            #    一条老记忆被更新过，它才是最新的相关材料。
            if since_dt or until_dt:
                ts = parse_iso(note.updated or note.created)
                if ts is None:
                    continue  # 时间不可解析 → 无法判断，排除（而不是猜）
                if since_dt and ts < since_dt:
                    continue
                if until_dt and ts > until_dt:
                    continue

            # ④ 口径与时效过滤（claim / status）。
            #    放在打分前：不合口径的条目不该为排序贡献任何东西。
            meta = note.meta or {}
            if want_claim and str(meta.get("claim", "")).strip() != want_claim:
                continue
            if only_active:
                st = expiry_state(meta)["state"]
                if st in ("superseded", "expired"):
                    continue
            elif want_status != "all":
                # 单态检索：只要那一态（review=该复查的、expired=已过期的、
                # superseded=已被取代的）。语义与 expiring 视图对齐。
                if expiry_state(meta)["state"] != want_status:
                    continue

            # ④.5 scope 生效域过滤（v2 S-1：检索默认收窄的第一闸）。
            #     任务过程态不进默认范围——b 会话搜天搜地也摸不到 ax。
            if want_scope:
                ns = note.scope
                if want_scope == "all":
                    pass
                elif ns != want_scope:
                    continue

            # ⑥ 共享档位过滤（v2 S-10.2）。同样**在打分与 total 之前**：
            #     看不见的条目不该贡献总数，也不该贡献排序。
            if want_viewer and not share_visible(
                note.share, note.author, want_viewer,
                space_default=space_default, cross_space=cross_space,
            ):
                continue

            # ⑤ 关键词打分
            if not words:
                scored.append((0, note))
                continue
            title_l = note.title.lower()
            body_l = note.body.lower()
            score = 0
            matched_all = True
            for w in words:
                w_score = 0
                if w in title_l:
                    w_score += 100 if title_l.startswith(w) else 60
                if any(w in t for t in note_tags):
                    w_score += 40
                if want_ws == "" and w in note.workspace.lower():
                    # 搜工作区名也算命中（但优先级低于标题、高于正文）——
                    # "vivo 相关的东西"这种查法很常见
                    w_score += 30
                hits = body_l.count(w)
                if hits:
                    w_score += min(hits, 5) * 8
                if w_score == 0:
                    matched_all = False  # 有一个词没命中 → AND 不成立
                    break
                score += w_score
            if matched_all:
                scored.append((score, note))

        # 排序：先相关度，同分时按更新时间倒序（新的在前）
        if sort == "updated":
            scored.sort(key=lambda x: (x[1].updated or x[1].created), reverse=True)
        else:
            scored.sort(
                key=lambda x: (-x[0], x[1].updated or x[1].created), reverse=False
            )
            scored.sort(key=lambda x: -x[0])

        total = len(scored)
        lim = max(1, min(limit, 100))
        off = max(0, offset)
        page = [n for _, n in scored[off : off + lim]]

        return {
            "ok": True,
            "query": query,
            "workspace": want_ws,
            "total": total,
            "count": len(page),
            "offset": off,
            "results": [
                n.to_dict(with_body=False, share_default=space_default)
                for n in page
            ],
        }

    # ══════════════════════ 统计 / 导出 ══════════════════════

    def stats(self) -> dict[str, Any]:
        """库况概览：条数、标签分布、作者分布、工作区分布、时间跨度。

        ``unreadable`` 是**特意暴露**的：有文件读不出来时，
        用户应该能看到，而不是让数据静默消失。

        ``workspaces`` 是工作区分布 —— 多项目共用一库时，
        这是判断"记忆有没有好好分类"的主要依据。
        """
        notes: list[Note] = []
        unreadable = 0
        for p in self._iter_files():
            try:
                notes.append(self._read_note(p))
            except (OSError, UnicodeDecodeError):
                unreadable += 1
        tag_counts: dict[str, int] = {}
        author_counts: dict[str, int] = {}
        ws_counts: dict[str, int] = {}
        for n in notes:
            for t in n.tags:
                tag_counts[t] = tag_counts.get(t, 0) + 1
            a = n.author or "(未署名)"
            author_counts[a] = author_counts.get(a, 0) + 1
            # 空工作区单独归一类 —— 它本身是个有意义的分类
            # （基础设施/跨项目笔记），不该被隐藏或与其它混淆
            ws_counts[n.workspace or "(无工作区)"] = (
                ws_counts.get(n.workspace or "(无工作区)", 0) + 1
            )

        times = [t for t in (parse_iso(n.updated or n.created) for n in notes) if t]
        return {
            "notes": len(notes),
            "unreadable": unreadable,
            "tags": len(tag_counts),
            "top_tags": sorted(tag_counts.items(), key=lambda kv: -kv[1])[:15],
            "authors": sorted(author_counts.items(), key=lambda kv: -kv[1]),
            "workspaces": sorted(ws_counts.items(), key=lambda kv: -kv[1]),
            "root": str(self.root),
            "is_git": self.is_repo(),
            "oldest": min(times).isoformat() if times else "",
            "newest": max(times).isoformat() if times else "",
            "trash": len(self.trash()) if self.trash_dir.exists() else 0,
        }

    def export_markdown(self, *, include_body: bool = True) -> str:
        """把整个库导出成**一个** Markdown 文件。

        用途：喂给没有 MCP 的场合（贴进对话、写进文档、离线备份）。
        纯文本、无依赖 —— 这也是"文件型存储"的一个白拿的好处。

        按标签分组输出，便于阅读；每条带标题、时间、作者。
        """
        notes = self.all_notes()
        by_tag: dict[str, list[Note]] = {}
        untagged: list[Note] = []
        for n in notes:
            if n.tags:
                for t in n.tags:
                    by_tag.setdefault(t, []).append(n)
            else:
                untagged.append(n)

        lines = [
            "# 共享记忆库导出",
            "",
            f"- 导出时间：{now_iso()}",
            f"- 记忆条数：{len(notes)}",
            f"- 标签数：{len(by_tag)}",
            "",
        ]

        def emit(note: Note) -> None:
            lines.append(f"## {note.title}")
            meta_bits = []
            if note.updated or note.created:
                meta_bits.append((note.updated or note.created)[:16].replace("T", " "))
            if note.author:
                meta_bits.append(f"by {note.author}")
            if meta_bits:
                lines.append(f"*{' · '.join(meta_bits)}*")
            lines.append("")
            if include_body:
                lines.append(note.body.strip())
                lines.append("")

        if untagged:
            lines.append("---")
            lines.append("# 未分类")
            lines.append("")
            for n in untagged:
                emit(n)

        for tag in sorted(by_tag):
            lines.append("---")
            lines.append(f"# 标签：{tag}")
            lines.append("")
            for n in by_tag[tag]:
                emit(n)

        return "\n".join(lines)

    # ══════════════════════ 导入 / 导出（方案 14）═════════════════════
    # 以**工作空间（项目）为单位**。导出不重新序列化 —— 直接字节级拷贝
    # 源文件，保证「导出 → 导入 → 再导出」无损往返。

    #: bundle 结构版本。导入方不认识的版本**直接拒绝** ——
    #: 结构变了还硬灌，比不导更糟（静默写错数据）。
    BUNDLE_SCHEMA = "shared-memory-bundle/1"

    #: bundle 内条目文件的合法路径形态：``libs/<库>/notes/<文件>.md``。
    #:
    #: ━━━ 为什么用函数而不是正则（2026-10-13 实测踩坑）━━━
    #: 第一版写成 ``^libs/[\w.-]+/notes/[\w.-]+\.md$``，两个后果：
    #:   ① ``\w`` 不含中文 → ``group-研发`` 整个库导不出去；
    #:   ② 更隐蔽：``\w`` 也不含中文标点 → 标题带 ``·`` ``（）`` 的
    #:      条目被静默跳过（只记 warning，导出结果少一半没人发现）。
    #: _slug 的真实保证是「**没有** ``_UNSAFE`` 里的字符」，白名单
    #: 必须与它同口径（负向校验），而不是另发明一个更窄的正集。
    #:
    #: Windows 保留设备名（CON/PRN/COM1...）也要挡 —— ``CON.md``
    #: 在 Windows 上不是文件而是控制台设备。
    _WIN_RESERVED = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }

    @classmethod
    def _valid_entry_path(cls, f: str) -> bool:
        """bundle 条目路径是否合法（防目录穿越/绝对路径/保留名）。"""
        parts = f.split("/")
        if len(parts) != 4 or parts[0] != "libs" or parts[2] != "notes":
            return False
        lib_id, name = parts[1], parts[3]
        if not lib_id or not name.endswith(".md") or name == ".md":
            return False
        for seg in (lib_id, name):
            if _UNSAFE.search(seg):        # / \ : * ? " < > | 控制字符 → 穿越面
                return False
            if seg.startswith("."):        # 隐藏文件 / ".." 段
                return False
        stem = name[:-3].upper()
        if stem in cls._WIN_RESERVED:
            return False
        return True

    def export_files(
        self,
        *,
        workspace: str = "",
        out_dir: str | Path,
        lib_id: str = "default",
    ) -> dict[str, Any]:
        """把（某工作区的）记忆文件拷贝成 bundle 的一部分。

        **不做权限检查**（那是 hub 的职责），也不写顶层 manifest ——
        本方法只负责"文件 + 条目清单"，多库合并由 hub 做。

        Returns:
            ``{"lib_id", "count", "bytes", "entries": [{lib,file,id,title,workspace,sha256}]}``
        """
        workspace = (workspace or "").strip()
        out = Path(out_dir)
        dest_dir = out / "libs" / lib_id / "notes"
        dest_dir.mkdir(parents=True, exist_ok=True)

        entries: list[dict[str, Any]] = []
        total_bytes = 0
        for note in self.all_notes():
            if workspace and note.workspace != workspace:
                continue
            src = self.root / note.path
            try:
                raw = src.read_bytes()
            except OSError as exc:
                logger.warning("导出跳过读不了的文件 %s: %s", src.name, exc)
                continue
            name = src.name
            # 白名单校验：源文件名理论上是 _slug 产物，但人可能手动放过奇怪名字
            if not self._valid_entry_path(f"libs/{lib_id}/notes/{name}"):
                logger.warning("导出跳过文件名不合法的条目：%s", name)
                continue
            shutil.copy2(src, dest_dir / name)
            total_bytes += len(raw)
            entries.append({
                "lib": lib_id,
                "file": f"libs/{lib_id}/notes/{name}",
                "id": note.id,
                "title": note.title,
                "workspace": note.workspace,
                "sha256": hashlib.sha256(raw).hexdigest(),
            })

        return {
            "lib_id": lib_id,
            "count": len(entries),
            "bytes": total_bytes,
            "entries": entries,
        }

    def plan_import(
        self, bundle: str | Path, *, workspace: str = ""
    ) -> dict[str, Any]:
        """预检一个 bundle：**不写任何东西**，只回答"能不能导、导什么"。

        为什么单独有这一步：导入是唯一"一次写一堆"的入口，
        坏 bundle 必须**在进写队列之前**就失败 —— 否则占着队列、
        写一半才报错，留下半截状态。

        Returns:
            ``{"ok", "bundle", "workspace", "entries": [...], "errors": [...]}``
        """
        b = Path(bundle).expanduser()
        errors: list[str] = []
        if not b.is_dir():
            return {"ok": False, "bundle": str(b), "workspace": workspace,
                    "entries": [], "errors": [f"bundle 目录不存在：{b}"]}

        manifest_path = b / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {"ok": False, "bundle": str(b), "workspace": workspace,
                    "entries": [], "errors": [f"manifest.json 读不了：{exc}"]}

        schema = str(manifest.get("schema", ""))
        if schema != self.BUNDLE_SCHEMA:
            return {"ok": False, "bundle": str(b), "workspace": workspace,
                    "entries": [], "errors": [
                        f"不认识的 bundle 版本：{schema!r}（本服务支持 {self.BUNDLE_SCHEMA}）"]}

        want_ws = (workspace or "").strip() or str(manifest.get("workspace", "") or "").strip()
        entries: list[dict[str, Any]] = []
        for e in manifest.get("entries", []):
            if not isinstance(e, dict):
                errors.append("manifest 里有非对象条目，已跳过")
                continue
            f = str(e.get("file", ""))
            # 安全 1：路径形态白名单（防目录穿越 / 绝对路径 / 保留名）
            if not self._valid_entry_path(f):
                errors.append(f"条目路径不合法，已跳过：{f!r}")
                continue
            # 安全 2：解析后的真实路径必须仍在 bundle 内
            real = (b / f).resolve()
            if not str(real).startswith(str(b.resolve())):
                errors.append(f"条目路径越出 bundle，已跳过：{f!r}")
                continue
            if want_ws and str(e.get("workspace", "")) != want_ws:
                continue  # 工作区过滤：只导目标项目的
            entries.append(e)

        return {
            "ok": not errors or bool(entries),
            "bundle": str(b),
            "workspace": want_ws,
            "entries": entries,
            "errors": errors,
        }

    def import_bundle(
        self,
        bundle: str | Path,
        *,
        on_conflict: str = "skip",
        workspace: str = "",
    ) -> dict[str, Any]:
        """把 bundle 导入**本库**（调用方已通过权限检查）。

        走与普通写入相同的落点逻辑（``_unique_path`` 三态），
        冲突策略按**标题**判定（与写时防重同一把尺子）：

          · ``skip``   （默认）同标题已存在 → 跳过并计数。
                       共享库里"不覆盖"是默认道德；
          · ``update`` 按 upsert 语义整篇覆盖（原文字节级替换）；
          · ``rename`` 加 ``--<id>`` 后缀另存（复用 _unique_path）。

        ⚠️ **刻意不走 apply_rules_on_write / 自动推断** —— 导入的价值
        就是原样还原（含 created/updated 时间戳），再"补全"一轮反而失真。

        Returns:
            ``{"ok", "imported", "skipped", "updated", "renamed", "errors": [...]}``
        """
        plan = self.plan_import(bundle, workspace=workspace)
        if not plan["entries"]:
            return {"ok": False, "imported": 0, "skipped": 0, "updated": 0,
                    "renamed": 0, "errors": plan["errors"] or ["bundle 里没有可导入的条目"]}

        b = Path(plan["bundle"])
        counts = {"imported": 0, "skipped": 0, "updated": 0, "renamed": 0}
        errors: list[str] = list(plan["errors"])

        for e in plan["entries"]:
            f = str(e.get("file", ""))
            src = b / f
            try:
                raw = src.read_bytes()
            except OSError as exc:
                errors.append(f"{f}: 读不了（{exc}）")
                continue
            # 完整性校验：manifest 记了 sha256 就必须对得上
            want_sha = str(e.get("sha256", ""))
            if want_sha and hashlib.sha256(raw).hexdigest() != want_sha:
                errors.append(f"{f}: 校验失败（内容与 manifest 不符），已跳过")
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                errors.append(f"{f}: 不是 UTF-8 文本，已跳过")
                continue

            meta, _body = parse_note(text)
            title = str(meta.get("title", "")).strip() or src.stem.split("--")[0]
            note_id = str(meta.get("id", "")) or _new_id()

            existing = self._find_by_title(title)
            try:
                if existing is None:
                    dest = self._unique_path(title, note_id)
                    # ⚠️ write_bytes 不是 write_text：Windows 上 write_text
                    # 会把 \n 翻译成 \r\n，而原文已是 \r\n → 变成 \r\r\n。
                    # 字节级还原是导入链路的意义，不过任何翻译层。
                    dest.write_bytes(raw)
                    self.invalidate_cache(dest)
                    counts["imported"] += 1
                elif on_conflict == "skip":
                    counts["skipped"] += 1
                elif on_conflict == "update":
                    existing.write_bytes(raw)
                    self.invalidate_cache(existing)
                    counts["updated"] += 1
                elif on_conflict == "rename":
                    dest = self._unique_path(title, note_id, force_new=True)
                    dest.write_bytes(raw)
                    self.invalidate_cache(dest)
                    counts["renamed"] += 1
                else:  # pragma: no cover - 调用方已校验
                    raise ToolError(ErrorCode.INVALID_ARGUMENT,
                                    f"on_conflict 只能是 skip/update/rename，收到 {on_conflict!r}")
            except OSError as exc:
                errors.append(f"{f}: 写入失败（{exc}）")

        return {
            "ok": not errors,
            "imported": counts["imported"],
            "skipped": counts["skipped"],
            "updated": counts["updated"],
            "renamed": counts["renamed"],
            "errors": errors[:20],  # 上限：别把返回值撑爆
        }
