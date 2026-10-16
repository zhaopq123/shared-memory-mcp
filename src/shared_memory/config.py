"""配置层 —— 存在 vault 里、面板可改、走同一个写队列。

━━━ 为什么配置不放在环境变量里 ━━━

环境变量的问题：
  · 改一次要重启服务（而在容器里重启意味着 SSH 进去）
  · 不能版本化（谁在什么时候改了什么，查不到）
  · 不能跟着 vault 一起备份/迁移

存进 vault 的 ``_config/`` 目录后，这三件事全都免费获得：
配置跟着 git 走、可回滚、改完立即生效（不用重启）。

━━━ 配置是「文件 + 默认值」两层 ━━━

  · **默认值**写在本模块的 DEFAULTS 里（代码的一部分）
  · **用户覆盖**写在 ``_config/*.md`` 里，读的时候覆盖默认值

这样：配置文件不存在时服务照样正常跑（不需要先初始化），
用户只写想改的那几项，不用抄一份完整配置。

━━━ 并发安全 ━━━

配置的写**必须走同一个 service 写队列**（与记忆写入共用）。
理由：配置也是文件，面板和 agent 同时改会互相覆盖 ——
跟记忆写入是同一类问题，没理由用两套办法。
（所以本模块只提供「读」与「构造内容」，
  真正的写由 service 层排队执行。）
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from .errors import ErrorCode, ToolError

logger = logging.getLogger(__name__)

#: 配置目录名（在 vault 根下）。用下划线开头与 notes/ 区分，
#: 但**不是**隐藏目录 —— 用户要能一眼看到它在哪。
CONFIG_DIR = "_config"

#: v2 行为契约（S-3.1）—— 与 v4 原型 ``INSTRUCTIONS_V2`` 同源。
#:
#: ━━━ 为什么要单独抽成常量 ━━━
#:
#: 这段契约有**两个出口**，必须字字一致：
#:   1. 每个空间的 ``_config/instructions.md``（面板可改、随 vault 走）
#:   2. MCP ``initialize`` 的 instructions（**agent 真正读到的那份**）
#: 抽成常量后一处改、两处生效。
#:
#: ━━━ 与原型原文的两处有意偏差（都是"别下不能执行的指令"）━━━
#:
#: · 原型第 2 条的 ``memory_remember`` 已在 S-4 落地（工具面改名完成），
#:   契约与实现现在**逐字一致**。
#: · 原型第 7 条的 ``versioned`` 开关到 S-8 才提供；这里只保留"按需"的
#:   原则，**不给一个当前点不动的开关名**（写个用不了的参数 = 编造能力）。
INSTRUCTIONS_CONTRACT = """\
## 行为契约（v2：不登记＝没上下文，不归档＝留烂摊子）

1. **开工先调 `memory_session_start(task, space)`** —— 登记会话，拿到该任务的既有记忆与空间的长期记忆（工作流/约束/习惯优先）。
2. **干活中产生结论/踩坑/口径，当场 `memory_remember`**，不要等收工补。
3. **记忆写六型之一**：`workflow` / `constraint` / `habit` / `fact` / `lesson` / `task`。约束类每次会话都会被提醒，写之前想清楚是不是约束。
4. **任务过程态写进任务域**（`memory_remember(scope="task")`）—— 它只对当前会话可见，不会污染别人的检索。
5. **收工必须 `memory_session_end(outcome, summary)`** —— 值得留的转长期，过程态归档。不归档＝你的任务记忆永远占着别人的检索位。
6. **诚实性不变**：`fact` / `inference` / `proposal` 分开写；把推断当事实写是这个库最大的污染源。
7. **版本控制按需**：只有会反复修订的长文档才值得版本化，其余（绝大多数）不需要。
"""

#: 契约锚点行 —— 判断一份指令文件是否已含 v2 契约（S-3.3 迁移判定用）。
CONTRACT_MARKER = "## 行为契约（v2"


#: 默认的 instructions —— 服务端对**所有接入 agent** 说的话。
#:
#: ━━━ 为什么默认就要带「不幻觉」 ━━━
#:
#: 这是共享记忆库，一条编造的内容会被**所有**后续 agent 当成事实引用 ——
#: 污染是复利的。所以「不编造」不是可选的人设，是数据质量的前提条件。
#:
#: 内容分两层：
#:   1. **诚实性**（不编、区分事实与推断）—— 保护数据不被污染
#:   2. **记忆纪律**（先查再写、写就写全、别当垃圾桶）—— 保护库不腐烂
DEFAULT_INSTRUCTIONS = """\
这是一个**跨 agent 共享的长期记忆库**：所有接入的 agent 读写同一份，
没有隔离 —— 你写的就是所有人（以及后续的你）能读到的。

""" + INSTRUCTIONS_CONTRACT + """

## 一、诚实性（核心，不可让步）

- **记忆里没有的东西，一律不许补。** 参数、价格、日期、来源、数字 —— 不知道就说不知道。
- **区分三类陈述**，写记忆时必须能分辨：
  · `fact` —— 有出处、或你**实际验证过**的
  · `inference` —— 你的推测。必须写清"基于什么推的"
  · `proposal` —— 你的建议/取舍，不是既成事实
  **把推断当事实写，是这个库最大的污染源** —— 后续 agent 会当作已知条件继续推理。
- **无法确认时明确标注**"未核实"，而不是含糊带过。
- 发现记忆里有错误时，**指出来并写回修正**（用 superseded-by 标记旧条），不要沉默地绕过。

## 二、记忆纪律

1. **先查再写**：动手前先 `memory_search`，命中就采信并引用；确认没有才新建。
2. **写就写全**：一条记忆至少要有 —— 背景（为什么）、结论（是什么）、依据（凭什么）。
   只写结论的记忆别人无法判断该不该用。
3. **标时效**：会过期的内容写 `valid-until`（硬过期）或 `review-after`（该复查）。
4. **取代而非删除**：旧记忆失效时用 `superseded-by` 指向新条，**不要删** ——
   删了就没人知道"这条路走过、不通"。
5. **别当垃圾桶**：能从代码/文件直接读到的、临时中间状态、只对当前这句话
   有意义的上下文，不要写进来。

## 三、写之前问自己

- 这条对**别人**有用吗，还是只对我当前这一步有用？
- 三个月后有人读到它，会不会被误导？
- 结论背后的"为什么"写清楚了吗？
"""

#: 默认约束规则（JSON 结构，面板可改）。
#:
#: 强度设计："自动补全 + 提醒"，**不硬拒**。
#: 理由见 autoInferMeta / service.write 的实现注释。
DEFAULT_RULES: dict[str, Any] = {
    "version": 1,
    # 写入时缺失字段怎么办：
    #   "fill"   —— 自动补默认值（推荐）
    #   "warn"   —— 只提醒不补
    #   "reject" —— 拒绝写入（不推荐：agent 会反复重试换写法）
    "missing_field_policy": "fill",
    # 是否启用自动推断 type/claim
    "auto_infer": True,
    # 要求每个工作区名不能为空？（空 = 允许跨项目记忆）
    "require_workspace": False,
    # 标题长度上限（防止 agent 写一整段当标题）
    "max_title_chars": 120,
    # 正文长度上限（0 = 不限）。超长正文通常是 agent 把整段对话贴进来了
    "max_body_chars": 0,
    # 自动推断规则：标题命中这些词 → 推断 type（v2 六型，S-1）。
    # 顺序即优先级：具体信号（约束/流程）优先于通用词（结论）——
    # "部署流程（必须走队列）" 该判 workflow 而不是 fact。
    # v1→v2 映射：decision→fact（明显约束被 constraint 优先接走）、
    # lesson→lesson、todo→task、ref→fact。
    "infer_type": {
        "lesson": ["教训", "踩坑", "坑点", "别再", "不要再", "事故"],
        "constraint": ["必须", "一律", "禁止", "不得", "铁律", "红线", "硬约束"],
        "workflow": ["流程", "步骤", "操作手册", "怎么做", "SOP"],
        "habit": ["习惯", "偏好", "风格"],
        "task": ["待办", "TODO", "待确认", "待补充", "进行中"],
        "fact": ["决定", "决策", "口径", "约定", "规范", "定案", "参考", "资料"],
    },
    # 自动推断 claim：这些词出现 → 标为 inference（而不是默认的 fact）
    "infer_claim_inference": [
        "可能", "大概", "推测", "猜测", "应该是", "看起来像", "也许是", "估计",
        "不确定", "未确认", "未核实",
    ],
    # 这些词出现 → 标为 fact（有实测依据）
    "infer_claim_fact": [
        "实测", "已验证", "测试通过", "复现", "亲眼", "日志显示", "报错如下",
    ],
}

#: 运行参数默认值（面板可改，部分改完立即生效）
DEFAULT_SETTINGS: dict[str, Any] = {
    "version": 1,
    # 写入静默多少秒后自动 git 提交
    "commit_debounce_seconds": 8,
    # 检索默认返回条数
    "search_default_limit": 20,
    # 面板列表默认展示条数
    "panel_page_size": 200,
    # 面板自动刷新间隔（秒，0 = 不自动刷新）
    "panel_refresh_seconds": 15,
    # 检索的默认时效口径（决策 7，2026-10-13）：
    #   "active" —— 默认只返回现行口径（排除 superseded/expired）
    #   "all"    —— 含被取代/已过期的（考古模式）
    # 调用方显式传 status 时以调用方为准；老部署想恢复旧行为改成 "all"。
    # （原 expired_sort 设置被本项取代，已移除。）
    "search_default_status": "active",
    # 回收站懒清理（方案 5b）：超过天数或超过条数的 trash 文件物理清除
    # （git 全历史是兜底）。0 = 关闭对应维度。
    "trash_ttl_days": 30,
    "trash_max_files": 200,
    # 单库条数配额告警阈值（方案 5c）：到量只在 stats 里提醒，不硬拒。0 = 关闭
    "quota_warn_notes": 500,
    # 上一条正文的本地快照保留几版（v2 · S-8.1）。0 = 关闭。
    # 这是"防手滑"层，**与 versioned 无关**：默认档下 git 不再每次写都提交，
    # 所以改坏一条记忆能不能就地找回，全靠这个目录。
    "history_keep": 3,
}


# ══════════════════ 空间共享属性（v2 · S-10.1，决策 D-2/D-3）══════════════════

#: ``_config/space.json`` 的默认值。
#:
#: ━━━ 两个键分别管什么 ━━━
#:
#: · ``default_share`` —— 条目**没显式标 share** 时继承的档位（D-3）。
#:   默认 ``space``：自己的空间本来就只有自己，标不标都只有同空间可见；
#:   要"仅自己"的私密条目由写入方显式传 ``mine``。
#: · ``accept_shared_from`` —— **目标空间授权**（D-2 的原话）。
#:   列在这里的空间 id，其 ``shared`` 条目才会进入本空间的跨空间检索面。
#:   方向是**接收方主动拉**，不是发送方硬塞 —— 否则别人改一下档位
#:   就能把内容推到我的检索结果里，那不算授权。
DEFAULT_SPACE: dict[str, Any] = {
    "version": 1,
    "default_share": "space",
    "accept_shared_from": [],
}

#: 条目共享档位（与 :mod:`shared_memory.vault` 的 ``SHARE_LEVELS`` 同源）。
SHARE_LEVELS: tuple[str, ...] = ("mine", "space", "shared")


#: 接入码默认端口（与 start-mcp.sh 的 PORT 默认值一致）。
DEFAULT_PORT = 8200


# ══════════════════ 启动期配置校验（F-1）══════════════════
#
# ━━━ 为什么"必填"清单是空的 ━━━
#
# 本产品每个变量都有可用默认值（见模块 docstring：服务不需要先初始化就能跑）。
# 所以 F-1 拦的不是"你没配"，而是**三类真会咬人的情况**：
#   ① 畸形值 —— PORT=abc、LOG_LEVEL=vorbse、DEBOUNCE=-1，
#      现在的下场是运行到一半抛 traceback，或**静默用默认值跑起来**（更坏）。
#   ② 配了路径但文件不在 —— SHARED_MEMORY_TOKEN_FILE 指了个不存在的文件，
#      旧行为只 logger.error 然后当"一把 token 都没有"，
#      于是鉴权判定整个跑偏（实测最坑的静默降级）。
#   ③ 拼错变量名 —— SHARED_MEMORY_TOKN=... 看起来配了，实际没生效。
#
# 错误（errors）→ 拒绝启动；提醒（warnings）→ 照常启动但说清楚。

#: 已知的环境变量白名单（拼写探测器用；值含义见 ``.env.example``）。
#: 最后两个是**客户端脚本**（e2e / 走查）读的，服务本身不读 ——
#: 列进来是为了同机跑脚本时不误报"未识别的变量"。
KNOWN_ENV_VARS: frozenset[str] = frozenset({
    "HOST", "PORT", "LOG_LEVEL",
    "SHARED_MEMORY_HOME", "SHARED_MEMORY_ROOT",
    "SHARED_MEMORY_TOKEN", "SHARED_MEMORY_TOKEN_FILE",
    "SHARED_MEMORY_AUTOCOMMIT", "SHARED_MEMORY_COMMIT_DEBOUNCE",
    "SHARED_MEMORY_ALLOW_INSECURE", "SHARED_MEMORY_SHUTDOWN_DRAIN_TIMEOUT",
    "SHARED_MEMORY_E2E_URL", "SHARED_MEMORY_E2E_TOKEN",
})

_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
#: 会被本服务读取的变量前缀（用于识别拼错的变量名）。
_OWN_PREFIXES = ("SHARED_MEMORY_",)


def _int_env(env: dict[str, str], name: str, errors: list[str],
             *, minimum: int = 0) -> None:
    raw = (env.get(name) or "").strip()
    if not raw:
        return
    try:
        if int(raw) < minimum:
            raise ValueError(raw)
    except ValueError:
        errors.append(f"{name}={raw!r} 不是 ≥{minimum} 的整数")


def validate_startup_env(
    env: dict[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    """校验启动配置，返回 ``(errors, warnings)``。

    ``errors`` 非空时调用方**必须拒绝启动**（快速失败，退出码非 0），
    并把每一条缺/错的变量名打进日志 —— 运维看日志第一行就知道改哪个。
    """
    e = dict(os.environ if env is None else env)
    errors: list[str] = []
    warnings: list[str] = []

    host = (e.get("HOST") or "").strip()
    if "HOST" in e and not host:
        errors.append("HOST 被显式设为空 —— 绑所有地址请写 0.0.0.0，"
                      "只绑本机写 127.0.0.1")

    raw_port = (e.get("PORT") or "").strip()
    if raw_port:
        try:
            port = int(raw_port)
            if not 1 <= port <= 65535:
                raise ValueError(raw_port)
        except ValueError:
            errors.append(f"PORT={raw_port!r} 不是 1~65535 的整数")

    raw_level = (e.get("LOG_LEVEL") or "").strip()
    if raw_level and raw_level.upper() not in _LOG_LEVELS:
        errors.append(f"LOG_LEVEL={raw_level!r} 非法，"
                      f"只认 {'、'.join(sorted(_LOG_LEVELS))}")

    _int_env(e, "SHARED_MEMORY_COMMIT_DEBOUNCE", errors)
    auto = (e.get("SHARED_MEMORY_AUTOCOMMIT") or "").strip()
    if auto and auto.lower() not in ("0", "1", "true", "false"):
        errors.append(f"SHARED_MEMORY_AUTOCOMMIT={auto!r} 只认 0/1")

    # ② 配了 token 文件却读不到 = 静默降级成无凭据启动，必须硬拦。
    token_file = (e.get("SHARED_MEMORY_TOKEN_FILE") or "").strip()
    if token_file and not Path(token_file).is_file():
        errors.append(f"SHARED_MEMORY_TOKEN_FILE={token_file!r} 不存在或不是文件"
                      " —— 服务不会带着「没有凭据」的状态启动，"
                      "要么把文件放好，要么删掉这一行")

    for name in ("SHARED_MEMORY_HOME", "SHARED_MEMORY_ROOT"):
        raw = (e.get(name) or "").strip()
        if not raw:
            continue
        p = Path(raw)
        if not p.is_absolute():
            warnings.append(f"{name}={raw!r} 是相对路径，会随启动目录漂移"
                            "（ systemd/runsv 的工作目录未必是你想的那个）")
        if p.exists() and not p.is_dir():
            errors.append(f"{name}={raw!r} 已存在但不是目录，记忆库无处可建")

    # ③ 拼写探测器：前缀是我们的、名字不在白名单 → 大概率打错字了。
    for key in e:
        if key.startswith(_OWN_PREFIXES) and key not in KNOWN_ENV_VARS:
            warnings.append(f"未识别的变量 {key}（本服务不读取，"
                            "拼错的配置变量比没配更危险 —— 它看起来像生效了）")

    if (e.get("SHARED_MEMORY_TOKEN") or "").strip():
        warnings.append("SHARED_MEMORY_TOKEN 写在环境变量里会出现在 "
                        "`ps e`、容器 inspect 与崩溃转储中，建议改用 "
                        "SHARED_MEMORY_TOKEN_FILE")

    return errors, warnings



def parse_connect_uri(uri: str) -> dict[str, Any]:
    """解析**接入码** → 连接参数（v2 · S-5.1）。

    格式：``sm://<host>:<port>?k=<密钥>``

      · ``sm://192.168.x.x:8200?k=abc123``  标准
      · 端口可省（默认 8200）：``sm://192.168.x.x?k=abc123``
      · 密钥键名兼容 ``k`` / ``key`` / ``token``

    ━━━ 为什么是自定义 scheme 而不是一条 JSON ━━━

    **人要能口述、能手抄**。JSON 里嵌套引号与花括号，抄错一个字符就是
    "连不上但不知道为什么"。接入码短、可肉眼校验、可以贴微信发给自己。

    Returns:
        ``{"host", "port", "token", "base_url", "mcp_url"}``

    Raises:
        ToolError: 格式不合法 —— 消息必须指明**是哪一段**不对，
            不能只丢一句"非法输入"（那等于让用户猜）。
    """
    from urllib.parse import parse_qs, unquote, urlparse

    raw = (uri or "").strip()
    if not raw:
        raise ToolError(
            ErrorCode.INVALID_ARGUMENT,
            "接入码为空",
            hint="形如 sm://192.168.x.x:8200?k=你的密钥（端口可省）",
        )
    if "://" not in raw:
        raise ToolError(
            ErrorCode.INVALID_ARGUMENT,
            f"接入码缺少协议头：{raw[:40]}",
            hint="应以 sm:// 开头，例如 sm://192.168.x.x:8200?k=密钥",
        )
    scheme = raw.split("://", 1)[0].strip().lower()
    if scheme != "sm":
        raise ToolError(
            ErrorCode.INVALID_ARGUMENT,
            f"协议头不对：{scheme}://",
            hint="本服务的接入码一律用 sm:// 开头",
        )

    u = urlparse(raw)
    if not u.hostname:
        raise ToolError(
            ErrorCode.INVALID_ARGUMENT,
            "接入码里没有主机地址",
            hint="sm:// 后面要跟 IP 或域名，例如 sm://192.168.x.x:8200?k=密钥",
        )
    try:
        port = u.port or DEFAULT_PORT
    except ValueError as exc:
        raise ToolError(
            ErrorCode.INVALID_ARGUMENT,
            "端口不是合法数字",
            hint=f"接在主机后面写端口，例如 sm://{u.hostname}:8200?k=密钥（{exc}）",
        ) from exc
    if not (0 < port < 65536):
        raise ToolError(
            ErrorCode.INVALID_ARGUMENT,
            f"端口超出范围：{port}",
            hint="端口应在 1~65535 之间",
        )

    q = parse_qs(u.query or "")
    token = ""
    for key_name in ("k", "key", "token"):
        vals = q.get(key_name) or []
        if vals and str(vals[0]).strip():
            token = unquote(str(vals[0])).strip()
            break
    if not token:
        raise ToolError(
            ErrorCode.INVALID_ARGUMENT,
            "接入码里没有密钥",
            hint="在末尾加 ?k=你的密钥（?k= 后面的部分就是凭据）",
        )

    base = f"http://{u.hostname}:{port}"
    return {
        "host": u.hostname,
        "port": port,
        "token": token,
        "base_url": base,
        "mcp_url": base + "/mcp",
    }


def config_dir(root: Path) -> Path:
    """vault 里的配置目录。"""
    return root / CONFIG_DIR


def _read_text(path: Path) -> str | None:
    """读文本文件；不存在或读不了返回 None（不抛）。"""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    """读 JSON 配置；缺失/损坏时回落默认值。

    ⚠️ 损坏时**回落而不是爆** —— 一个手滑写坏的配置文件不该让整个
    记忆服务起不来（那会连带所有 agent 都用不了）。同时记 warning，
    让用户在日志里看到。
    """
    raw = _read_text(path)
    if raw is None:
        return dict(default)
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("配置 %s 解析失败（将使用默认值）: %s", path.name, exc)
        return dict(default)
    if not isinstance(loaded, dict):
        logger.warning("配置 %s 不是对象（将使用默认值）", path.name)
        return dict(default)
    # 浅合并：用户只写想改的键，其余用默认值补上
    merged = dict(default)
    merged.update(loaded)
    return merged


class Config:
    """一份 vault 的配置视图。

    读是**每次访问都重新读文件**（不缓存）—— 这样面板改完立即生效，
    不需要重启，也不需要处理缓存失效。配置很小，读开销可忽略。
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.dir = config_dir(self.root)

    # ────────────────────── 路径 ──────────────────────

    @property
    def instructions_path(self) -> Path:
        """给 agent 的指令（纯文本 Markdown）。"""
        return self.dir / "instructions.md"

    @property
    def rules_path(self) -> Path:
        """写入约束规则（JSON）。"""
        return self.dir / "rules.json"

    @property
    def settings_path(self) -> Path:
        """运行参数（JSON）。"""
        return self.dir / "settings.json"

    @property
    def space_path(self) -> Path:
        """空间共享属性（JSON，v2 S-10.1）。"""
        return self.dir / "space.json"

    # ────────────────────── 读 ──────────────────────

    def instructions(self) -> str:
        """当前生效的 instructions。

        文件存在但**内容为空**时回落到默认值 —— 空内容多半是误操作，
        而"没有指令"会让 agent 失去所有行为约定（比有默认值更糟）。
        要停用请用面板的显式开关（写 ``_disabled``），而不是清空文件。

        ━━━ v2 行为契约在**哪一层**（S-3）━━━

        七条契约由 **MCP `initialize` 的 instructions 保证**（见 http_server），
        每个 agent 一握手必拿到 —— 与这里有没有自定义文件无关。

        这份 ``_config/instructions.md`` 是**空间级补充**：可以加"我们组的规矩"，
        也可以整份改成别的（用户说了算，服务端**不偷偷注入** —— 否则用户在
        面板里删掉契约、保存后又冒出来，是 bug 级的困惑）。

        缺契约的**存量**文件用 ``migrate_instructions_contract()``
        一次性头部追加（保留原文）。
        """
        raw = _read_text(self.instructions_path)
        if raw is None or not raw.strip():
            return DEFAULT_INSTRUCTIONS
        if raw.strip() == "_disabled":
            return ""
        return raw

    def migrate_instructions_contract(self) -> dict[str, Any]:
        """把 v2 行为契约**物理**并进存量 instructions.md（头部追加，不覆盖）。

        只处理"文件已存在且缺契约"这一种情形：

        · 文件不存在 → 不动（没指令的空间本来就走默认值，已含契约）；
          **不主动建文件** —— 建了会让 ``instructions_is_default`` 误报成
          "用户改过"，那是假信号。
        · 已含契约 → 不动。
        · 缺契约 → 写 ``契约 + 分隔线 + 原文``，**原文逐字保留**。

        Returns:
            ``{"ok", "changed", "reason", "path"}``。
        """
        p = self.instructions_path
        raw = _read_text(p)
        if raw is None:
            return {"ok": True, "changed": False, "reason": "missing", "path": str(p)}
        if raw.strip() == "_disabled" or CONTRACT_MARKER in raw:
            return {"ok": True, "changed": False, "reason": "up-to-date", "path": str(p)}
        p.write_text(INSTRUCTIONS_CONTRACT + "\n\n---\n\n" + raw, encoding="utf-8")
        return {"ok": True, "changed": True, "reason": "prepended", "path": str(p)}

    def rules(self) -> dict[str, Any]:
        """当前生效的写入约束规则。"""
        return _read_json(self.rules_path, DEFAULT_RULES)

    def settings(self) -> dict[str, Any]:
        """当前生效的运行参数。"""
        return _read_json(self.settings_path, DEFAULT_SETTINGS)

    def space(self) -> dict[str, Any]:
        """当前生效的空间共享属性（v2 S-10.1）。"""
        return _read_json(self.space_path, DEFAULT_SPACE)

    def default_share(self) -> str:
        """条目未标 ``share`` 时继承的档位；非法值退回 ``space``。

        这里**不抛**：读侧每次检索都会走，一条写坏的空间配置不该让
        整个检索失败（与 ``_read_json`` 损坏回落同一套取舍）。
        写侧另有 :meth:`validate_space` 拦在落盘之前。
        """
        v = str(self.space().get("default_share") or "").strip()
        return v if v in SHARE_LEVELS else "space"

    def accept_shared_from(self) -> list[str]:
        """本空间授权接收哪些其它空间的 ``shared`` 条目（目标空间授权）。"""
        raw = self.space().get("accept_shared_from")
        if not isinstance(raw, list):
            return []
        return [str(x).strip() for x in raw if str(x).strip()]

    def snapshot(self) -> dict[str, Any]:
        """给面板看的一份完整配置快照。

        额外带上 ``*_is_default`` 标志 —— 面板要能告诉用户
        "你现在看到的是默认值，还是你自己改过的"。
        """
        return {
            "ok": True,
            "instructions": self.instructions(),
            "instructions_is_default": (
                _read_text(self.instructions_path) is None
                or not (_read_text(self.instructions_path) or "").strip()
            ),
            "instructions_disabled": (
                (_read_text(self.instructions_path) or "").strip() == "_disabled"
            ),
            "rules": self.rules(),
            "rules_is_default": self.rules_path.exists() is False,
            "settings": self.settings(),
            "settings_is_default": self.settings_path.exists() is False,
            "space": self.space(),
            "space_is_default": self.space_path.exists() is False,
            "defaults": {
                "instructions": DEFAULT_INSTRUCTIONS,
                "rules": DEFAULT_RULES,
                "settings": DEFAULT_SETTINGS,
                "space": DEFAULT_SPACE,
            },
            "dir": str(self.dir),
        }

    # ────────────────────── 校验 ──────────────────────

    def validate_rules(self, rules: dict[str, Any]) -> list[str]:
        """校验规则，返回问题列表（空 = 合法）。

        ━━━ 为什么要有这一步（漏洞 9）━━━
        配置改错了会让**服务行为异常**，而且症状往往离配置很远
        （"为什么视图是空的" ← 其实是 infer_type 被写成了字符串）。
        所以保存前校验，把问题挡在写盘之前，并明确告诉是哪一项。

        这里**宽松**：只检查会导致错误行为的类型问题，
        不检查"值合不合理"（那属于口味，不该由校验器决定）。
        """
        problems: list[str] = []

        policy = rules.get("missing_field_policy")
        if policy not in ("fill", "warn", "reject"):
            problems.append(
                f"missing_field_policy 只能是 fill / warn / reject，收到 {policy!r}"
            )

        for key in ("auto_infer", "require_workspace"):
            if key in rules and not isinstance(rules[key], bool):
                problems.append(f"{key} 必须是布尔值（true/false），收到 {rules[key]!r}")

        for key in ("max_title_chars", "max_body_chars"):
            v = rules.get(key)
            if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 0):
                problems.append(f"{key} 必须是非负整数，收到 {v!r}")

        # infer_type / infer_claim_* 必须是"字符串 → 字符串列表"的映射
        for key in ("infer_type", "infer_claim_inference", "infer_claim_fact"):
            v = rules.get(key)
            if v is None:
                continue
            if key == "infer_type":
                if not isinstance(v, dict):
                    problems.append(f"{key} 必须是对象：{{类型: [关键词…]}}")
                    continue
                for k, words in v.items():
                    if not isinstance(k, str) or not isinstance(words, list) or \
                       not all(isinstance(w, str) for w in words):
                        problems.append(f"{key}.{k} 必须是字符串数组")
            else:
                if not isinstance(v, list) or not all(isinstance(w, str) for w in v):
                    problems.append(f"{key} 必须是字符串数组")

        return problems

    def validate_settings(self, settings: dict[str, Any]) -> list[str]:
        """校验运行参数，返回问题列表。"""
        problems: list[str] = []

        bounds: dict[str, tuple[int, int]] = {
            "commit_debounce_seconds": (0, 3600),
            "search_default_limit": (1, 200),
            "panel_page_size": (1, 2000),
            "panel_refresh_seconds": (0, 3600),
            "trash_ttl_days": (0, 3650),
            "trash_max_files": (0, 100000),
            "quota_warn_notes": (0, 1000000),
            "history_keep": (0, 50),
        }
        for key, (lo, hi) in bounds.items():
            v = settings.get(key)
            if v is None:
                continue
            if not isinstance(v, int) or isinstance(v, bool):
                problems.append(f"{key} 必须是整数，收到 {v!r}")
            elif not (lo <= v <= hi):
                problems.append(f"{key} 必须在 {lo}~{hi} 之间，收到 {v}")

        if settings.get("search_default_status") not in (None, "active", "all"):
            problems.append(
                "search_default_status 只能是 active / all，"
                f"收到 {settings.get('search_default_status')!r}"
            )

        return problems

    def validate_space(self, space: dict[str, Any]) -> list[str]:
        """校验空间共享属性，返回问题列表（空 = 合法）。

        ⚠️ ``default_share`` 必须逐值校验，不接受"猜"：档位写错
        （``"mine "` 多个空格、``"Space"`` 大写、``"all"``）会让
        **本该私密的东西变公开**或反过来 —— 后者只是看不见，前者是事故。
        """
        problems: list[str] = []

        ds = space.get("default_share")
        if ds not in SHARE_LEVELS:
            problems.append(
                f"default_share 只能是 {' / '.join(SHARE_LEVELS)}，收到 {ds!r}"
            )

        aw = space.get("accept_shared_from")
        if aw is not None:
            if not isinstance(aw, list):
                problems.append(
                    f"accept_shared_from 必须是空间 id 数组，收到 {aw!r}"
                )
            elif not all(isinstance(x, str) and x.strip() for x in aw):
                problems.append("accept_shared_from 的每一项都要是非空字符串")

        return problems


# ══════════════════════ 自动推断 ══════════════════════


#: 类型推断的优先序（v2 S-12.2 / 设计文档 D-9-3）。
#: 这两型必须**先判**，理由不是"它们更重要"，是**错的代价不对称**：
#: 一条 ``教训：必须先跑 lint`` 判成 lesson，视图里少一条硬约束、
#: digest 的常驻清单**永远不会带上它** —— 整个"约束每次必达"机制
#: 对这条失效，而且没人会察觉。反过来把 lesson 判成 constraint，
#: 只是常驻清单里多一行（顶多吵）。
_INFER_PRIORITY: tuple[str, ...] = ("constraint", "workflow")


def infer_meta(
    *,
    title: str,
    body: str,
    workspace: str,
    rules: dict[str, Any],
    active_task_scope: str = "",
) -> dict[str, Any]:
    """根据标题/正文自动推断元数据。

    ━━━ 为什么要有自动推断（漏洞 1）━━━

    视图（决策记录/踩坑清单/待办）依赖 ``type`` 这类字段。
    但 **agent 写记忆时不会记得填** —— 它赶着写正文。

    结果：视图全是空的，变成摆设，而且你还多了一堆要维护的字段。
    这是这类系统最常见的死法。

    所以：**不填也有值**。推断错了也无所谓 —— 视图只是入口，
    点进去能看到原文；标错比不标强（这点与"保守只认显式字段"是
    明确的取舍，选了激进）。

    ━━━ ⚠️ 但「全正文扫描」是错的（实测踩过）━━━

    最初实现把 keywords 在整个正文里找。结果一条**部署文档**因为正文里
    有一句"视图包括决策/踩坑/待办"被推断成 ``type=lesson``；
    另一条因为提到"未核实的推断"被标成 ``claim=inference``。

    原因很本质：**正文里出现一个词，通常是在"讨论这个概念"，
    而不是"这条笔记本身就是那个东西"**。
    列举关键词的地方必然误命中。

    修法（按证据强度分层）：
      · ``type``   —— **只看标题**。标题是作者对这条内容的一句话概括，
                      "教训：xxx" 就是教训。这是最强信号，且不被正文噪音污染。
      · ``claim``  —— **标题 + 正文首段**（第一个空行之前）。
                      可信度判断看的是"作者怎么起头陈述的"。
                      只认首段而不是"前 N 字"，是因为**长度阈值挡不住列举**：
                      一条短文档的整段都在 300 字内，列举照样命中（实测踩过）。

    宁可漏判（类型留空，视图少收录一条），不可误判
    （错标成 lesson 会污染踩坑清单，错标 claim=fact 会让推断被当真理引用）。

    ━━━ 想标得准怎么办 ━━━
    显式传 ``extra={"type": "lesson", "claim": "fact"}`` ——
    显式值永远优先于推断。推断只是"没填时的兜底"，不是最终裁决。
    （D-9-a：误判 workflow/constraint 的兜底同一条 —— 建议值，可覆盖。）

    ━━━ v2 S-12.2 的两处扩展 ━━━
    1. **优先序**：constraint / workflow 先判（见 :data:`_INFER_PRIORITY`）。
    2. **scope 推断**：判成 ``task``（待办/进行中型）且调用方给了
       ``active_task_scope``（当前会话绑定的任务域）时，建议把它写进
       任务域 —— 过程态**本来就该随会话归档**，留在长期记忆里就是
       "记忆越来越大越来越乱"的主因。
       没开会话（``active_task_scope`` 为空）时不猜，按缺省长期。

    Args:
        active_task_scope: 当前会话的任务域（``task:<名>``），由调用方
            从会话登记里取。**服务端不猜会话** —— 猜错会把条目写进
            别人的域。

    Returns:
        只包含**推断出来**的字段（显式传了的不覆盖）。
        可能包含：``type`` / ``claim`` / ``scope``。
    """
    if not rules.get("auto_infer", True):
        return {}

    out: dict[str, Any] = {}
    title_low = (title or "").lower()
    # 首段 = 第一个空行之前；没有空行则整段（短文档自然成立）
    first_para = (body or "").split("\n\n", 1)[0]
    head_low = first_para.lower()

    # ── 推断 type：**只认标题** ──
    # 命中即用（不做多标签）。先扫优先型（constraint / workflow），
    # 再按配置的顺序扫其余 —— 顺序错了会让"教训：必须 X"这类标题
    # 被 lesson 抢走（代价不对称，见 _INFER_PRIORITY 的说明）。
    infer_type = rules.get("infer_type") or {}
    order = [k for k in _INFER_PRIORITY if k in infer_type] + [
        k for k in infer_type if k not in _INFER_PRIORITY
    ]
    for kind in order:
        words = infer_type.get(kind)
        if not isinstance(words, list):
            continue
        if any(str(w) and str(w).lower() in title_low for w in words):
            out["type"] = kind
            break

    # ── 推断 scope：过程态落当前会话绑定的任务域（S-12.2）──
    if out.get("type") == "task" and active_task_scope:
        out["scope"] = active_task_scope

    # ── 推断 claim：标题 + 正文开头 ──
    # 优先级：明确"未核实/推测" > 明确"实测" > 不标（默认由读取端处理）
    #
    # 为什么推测优先：把一条"可能是这样"的内容标成 fact 是**有害**的，
    # 反过来把一条实测标成 inference 只是保守。错误的代价不对称。
    inf_words = rules.get("infer_claim_inference") or []
    fact_words = rules.get("infer_claim_fact") or []
    probe = f"{title_low}\n{head_low}"
    if any(str(w) and str(w).lower() in probe for w in inf_words):
        out["claim"] = "inference"
    elif any(str(w) and str(w).lower() in probe for w in fact_words):
        out["claim"] = "fact"

    return out


def apply_rules_on_write(
    *,
    title: str,
    body: str,
    tags: list[str],
    workspace: str,
    extra: dict[str, Any],
    rules: dict[str, Any],
    active_task_scope: str = "",
) -> tuple[list[str], dict[str, Any], list[str]]:
    """写入前按规则处理，返回 ``(tags, extra, notes)``。

    ``notes`` 是给调用方的提醒列表 —— 会随写入结果一起返回给 agent，
    让它在**不被打断**的前提下知道"你少填了什么，我替你补了什么"。

    ━━━ 为什么不硬拒（漏洞 1 的对策核心）━━━

    硬拦的风险：agent 被拒后会**反复换写法重试** —— 浪费轮次、烧额度，
    而它只是想记一条笔记。约束的目的是**保证数据合格**，
    不是"教育 agent"。

    所以：能补的补上，补不了的提醒，**一律放行**。
    只有 ``missing_field_policy == "reject"`` 时（用户显式选择）才拒绝。
    """
    notes: list[str] = []
    policy = rules.get("missing_field_policy", "fill")

    # ── 标题长度 ──
    max_title = rules.get("max_title_chars") or 0
    if max_title and len(title) > max_title:
        if policy == "reject":
            raise ToolError(
                ErrorCode.INVALID_ARGUMENT,
                f"标题过长（{len(title)} > {max_title} 字符）",
                hint="标题应是简短摘要，正文请放 body",
            )
        notes.append(f"标题超长（{len(title)} > {max_title}），已按规则截断")
        title = title[:max_title].rstrip()

    # ── 正文长度 ──
    max_body = rules.get("max_body_chars") or 0
    if max_body and len(body) > max_body:
        if policy == "reject":
            raise ToolError(
                ErrorCode.INVALID_ARGUMENT,
                f"正文过长（{len(body)} > {max_body} 字符）",
            )
        notes.append(f"正文超长（{len(body)} > {max_body}），已截断")
        body = body[:max_body]

    # ── 工作区 ──
    if rules.get("require_workspace") and not workspace:
        if policy == "reject":
            raise ToolError(
                ErrorCode.INVALID_ARGUMENT,
                "规则要求每条记忆都必须带 workspace",
                hint="传 workspace 参数，或把 require_workspace 改为 false",
            )
        notes.append("规则建议带 workspace，本条未填（当成跨项目记忆处理）")

    # ── 自动推断 type/claim/scope（只在调用方没显式给时）──
    # ⚠️ notes 只报**真正落进 extra 的那几项**：显式值覆盖掉的建议不能
    # 再写成"已自动推断"，否则 agent 收到的是"我改了你的 scope"这种谎话，
    # 而它明明传了 long-term（D-9-a 的另一半：诚实报告建议被采纳与否）。
    inferred = infer_meta(
        title=title, body=body, workspace=workspace, rules=rules,
        active_task_scope=active_task_scope,
    )
    applied: dict[str, Any] = {}
    skipped: list[str] = []
    for k, v in inferred.items():
        if k not in extra:
            extra[k] = v
            applied[k] = v
        else:
            skipped.append(k)
    if applied:
        notes.append("已自动推断：" + "、".join(f"{k}={v}" for k, v in applied.items()))
    if skipped:
        notes.append(
            "推断建议已让位给你的显式值：" + "、".join(skipped)
        )

    # ── 自动补标签 ──
    # type 也补一个同名标签，这样面板/检索按标签过滤也能命中
    t = extra.get("type")
    if t and t not in tags:
        tags = [*tags, t]

    return tags, extra, notes
