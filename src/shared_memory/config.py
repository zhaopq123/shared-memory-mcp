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
from pathlib import Path
from typing import Any

from .errors import ErrorCode, ToolError

logger = logging.getLogger(__name__)

#: 配置目录名（在 vault 根下）。用下划线开头与 notes/ 区分，
#: 但**不是**隐藏目录 —— 用户要能一眼看到它在哪。
CONFIG_DIR = "_config"

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

因此下面两条比"方便"重要得多。

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
    # 自动推断规则：标题/正文命中这些词 → 推断 type
    "infer_type": {
        "lesson": ["教训", "踩坑", "坑点", "别再", "不要再", "事故"],
        "decision": ["决定", "决策", "口径", "约定", "规范", "定案"],
        "todo": ["待办", "TODO", "待确认", "待补充"],
        "ref": ["参考", "资料", "文档索引", "链接汇总"],
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

    # ────────────────────── 读 ──────────────────────

    def instructions(self) -> str:
        """当前生效的 instructions。

        文件存在但**内容为空**时回落到默认值 —— 空内容多半是误操作，
        而"没有指令"会让 agent 失去所有行为约定（比有默认值更糟）。
        要停用请用面板的显式开关（写 ``_disabled``），而不是清空文件。
        """
        raw = _read_text(self.instructions_path)
        if raw is None or not raw.strip():
            return DEFAULT_INSTRUCTIONS
        if raw.strip() == "_disabled":
            return ""
        return raw

    def rules(self) -> dict[str, Any]:
        """当前生效的写入约束规则。"""
        return _read_json(self.rules_path, DEFAULT_RULES)

    def settings(self) -> dict[str, Any]:
        """当前生效的运行参数。"""
        return _read_json(self.settings_path, DEFAULT_SETTINGS)

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
            "defaults": {
                "instructions": DEFAULT_INSTRUCTIONS,
                "rules": DEFAULT_RULES,
                "settings": DEFAULT_SETTINGS,
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


# ══════════════════════ 自动推断 ══════════════════════


def infer_meta(
    *,
    title: str,
    body: str,
    workspace: str,
    rules: dict[str, Any],
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

    Returns:
        只包含**推断出来**的字段（显式传了的不覆盖）。
        可能包含：``type`` / ``claim``。
    """
    if not rules.get("auto_infer", True):
        return {}

    out: dict[str, Any] = {}
    title_low = (title or "").lower()
    # 首段 = 第一个空行之前；没有空行则整段（短文档自然成立）
    first_para = (body or "").split("\n\n", 1)[0]
    head_low = first_para.lower()

    # ── 推断 type：**只认标题** ──
    # 按 infer_type 里配置的顺序找；命中即用（不做多标签）
    infer_type = rules.get("infer_type") or {}
    for kind, words in infer_type.items():
        if not isinstance(words, list):
            continue
        if any(str(w) and str(w).lower() in title_low for w in words):
            out["type"] = kind
            break

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

    # ── 自动推断 type/claim（只在调用方没显式给时）──
    inferred = infer_meta(title=title, body=body, workspace=workspace, rules=rules)
    for k, v in inferred.items():
        if k not in extra:  # 显式传了就不覆盖
            extra[k] = v
    if inferred:
        notes.append("已自动推断：" + "、".join(f"{k}={v}" for k, v in inferred.items()))

    # ── 自动补标签 ──
    # type 也补一个同名标签，这样面板/检索按标签过滤也能命中
    t = extra.get("type")
    if t and t not in tags:
        tags = [*tags, t]

    return tags, extra, notes
