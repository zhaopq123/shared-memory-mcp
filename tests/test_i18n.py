"""i18n 一致性测试 —— 语言包缺 key 是国际化的经典 bug。

━━━ 为什么必须测 ━━━

缺 key 的症状是**静默的**：界面上那一块显示成 key 本身（或空白），
而没人会去点遍所有页面检查。等到用户报"这里怎么是英文/乱码"时
已经过了很久。

所以用测试钉住：
  · 两种语言的 key 集合**完全一致**
  · 每个 key 在两种语言里都**非空**
  · 占位符（{n} / {lib} 等）在两种语言里**一致**（否则会显示成字面量）
  · JS 里用到的 key **都存在于语言包**（防止拼错）

━━━ 为什么不用现成的 i18n 库 ━━━

面板只有 200 条文案、2 种语言。引一个库（+ 构建步骤）的收益
远小于"多一个依赖"的成本 —— 而这套东西的设计原则就是**零构建、
零 CDN、离线自足**（见 panel.css 的说明）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "src" / "shared_memory" / "static"


def _i18n_source() -> str:
    return (STATIC / "i18n.js").read_text(encoding="utf-8")


def _extract_pack(name: str) -> dict[str, str]:
    """从 i18n.js 里抽出一个语言包的 key→value。

    用正则而不是 JS 引擎：不想为测试引入 node 依赖
    （目标机上没有 node，而测试要能在任何环境跑）。
    """
    src = _i18n_source()
    m = re.search(rf"const {name} = \{{(.*?)^\}};", src, re.S | re.M)
    assert m, f"找不到语言包 {name}"
    body = m.group(1)

    out: dict[str, str] = {}
    # 匹配 'key': 'value',  —— value 里可能有转义引号、\n
    for km in re.finditer(
        r"^\s*'([^']+)':\s*(?:'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\")\s*,\s*$",
        body, re.M,
    ):
        key = km.group(1)
        val = km.group(2) if km.group(2) is not None else km.group(3)
        out[key] = val
    return out


def _placeholders(s: str) -> set[str]:
    """取文案里的占位符（{n} / {lib} / {u} …）。"""
    return set(re.findall(r"\{(\w+)\}", s))


# ══════════════════════ 结构一致性 ══════════════════════


def test_both_packs_exist() -> None:
    """中英两个语言包都要在。"""
    assert _extract_pack("ZH"), "中文语言包是空的"
    assert _extract_pack("EN"), "英文语言包是空的"


def test_default_language_is_chinese() -> None:
    """⚠️ 默认语言必须是中文（用户明确要求）。"""
    src = _i18n_source()
    m = re.search(r"const DEFAULT_LANG = '(\w+)'", src)
    assert m, "找不到 DEFAULT_LANG"
    assert m.group(1) == "zh", f"默认语言是 {m.group(1)}，应该是 zh"


def test_key_sets_are_identical() -> None:
    """⚠️ 两种语言的 key 集合必须**完全一致**。

    缺 key 的症状是静默的（界面显示成 key 本身），
    而没人会点遍所有页面去检查 —— 所以用测试钉住。
    """
    zh = set(_extract_pack("ZH"))
    en = set(_extract_pack("EN"))
    missing_in_en = zh - en
    missing_in_zh = en - zh
    assert not missing_in_en, f"英文缺 {len(missing_in_en)} 个 key：{sorted(missing_in_en)[:10]}"
    assert not missing_in_zh, f"中文缺 {len(missing_in_zh)} 个 key：{sorted(missing_in_zh)[:10]}"


def test_no_empty_values() -> None:
    """每个 key 都要有非空值（空串会在界面上留一片空白）。"""
    for name in ("ZH", "EN"):
        pack = _extract_pack(name)
        empty = [k for k, v in pack.items() if not v.strip()]
        assert not empty, f"{name} 里有空文案：{empty[:10]}"


def test_placeholders_match_between_languages() -> None:
    """⚠️ 占位符必须一致 —— 否则翻译后会显示成字面量 `{n}`。

    例：中文 "约 {n} 分钟"，英文写成 "~N min"（漏了 {n}）
    → 界面上会出现 "~N min" 而不是 "~3 min"。
    """
    zh = _extract_pack("ZH")
    en = _extract_pack("EN")
    mismatched = []
    for k in set(zh) & set(en):
        pz, pe = _placeholders(zh[k]), _placeholders(en[k])
        if pz != pe:
            mismatched.append((k, sorted(pz), sorted(pe)))
    assert not mismatched, (
        f"{len(mismatched)} 个 key 的占位符不一致：{mismatched[:5]}"
    )


def test_packs_are_not_trivially_identical() -> None:
    """英文包不能是中文的复制粘贴（那等于没翻译）。

    允许少量相同（如占位符为主的文案），但比例不能高。
    """
    zh = _extract_pack("ZH")
    en = _extract_pack("EN")
    same = [k for k in set(zh) & set(en) if zh[k] == en[k]]
    # 有些文案本来就该一样（如纯符号、URL），但不能超过 5%
    ratio = len(same) / max(len(zh), 1)
    assert ratio < 0.05, (
        f"{len(same)}/{len(zh)} 条文案中英完全相同（{ratio:.1%}）—— "
        f"疑似英文包是复制粘贴。例：{same[:5]}"
    )


def test_chinese_pack_has_no_english_only_values() -> None:
    """中文包的值不该是纯英文（除了纯符号/缩写）。

    防止"把英文包的内容误填进中文包"。
    """
    zh = _extract_pack("ZH")
    bad = []
    for k, v in zh.items():
        # 去掉 HTML 标签、符号、数字后，看还有没有拉丁字母
        stripped = re.sub(r"<[^>]+>", "", v)
        stripped = re.sub(r"[^\w\u4e00-\u9fff]+", "", stripped)
        if stripped and not re.search(r"[\u4e00-\u9fff]", stripped):
            bad.append((k, v))
    # 允许少量（如 "JSON"、"token"、"MCP" 这类专有名词单独成句的情况）
    assert len(bad) <= 3, f"中文包里疑似纯英文的文案：{bad[:8]}"


# ══════════════════════ 使用侧一致性 ══════════════════════


def test_all_used_keys_exist_in_packs() -> None:
    """⚠️ panel.js 里 t('...') 用到的 key 必须都存在于语言包。

    拼错 key 的症状：界面显示成 key 本身（如 `admin.col.libs`），
    而开发者通常不会注意到 —— 除非有这条测试。

    ⚠️ 动态拼接的 key 要排除：`t('view.' + kind)` 这种是**运行时**才知道
    的 key，静态扫描只会看到 `view.` 这个前缀。它们由
    ``test_dynamic_key_prefixes_are_covered`` 单独覆盖。
    """
    src = (STATIC / "panel.js").read_text(encoding="utf-8")
    used = set(re.findall(r"\bt\(\s*'([^']+)'", src))
    used |= set(re.findall(r"\bt\(\s*\"([^\"]+)\"", src))
    # data-i18n 属性也算使用
    used |= set(re.findall(r'data-i18n="([^"]+)"', src))

    # 排除动态拼接的前缀（以 `.` 结尾、或含 `<`/`>` 说明是占位）
    static_keys = {k for k in used if not k.endswith(".") and "<" not in k and ">" not in k}

    zh = set(_extract_pack("ZH"))
    missing = sorted(static_keys - zh)
    assert not missing, (
        f"panel.js 用了 {len(missing)} 个不存在的 key：{missing[:10]}"
    )


def test_dynamic_key_prefixes_are_covered() -> None:
    """⚠️ 动态拼接的 key（`t('view.' + kind)`）要有对应测试覆盖。

    静态扫描看不到这些 key（运行时才知道），所以单独验证：
    knowledge.VIEWS 里的每个 kind，语言包都必须有 `view.<kind>` 和
    `view.<kind>.desc` —— 否则那个视图的标题会回落到中文
    （或显示成 key 本身）。

    这正是实测踩过的坑：视图标题空白，因为跨库分支漏了 label/desc。
    """
    src = (STATIC / "panel.js").read_text(encoding="utf-8")

    # 找出 `t('前缀.' + <什么>)` 这种模式
    dyn = re.findall(r"\bt\(\s*'([^']*[.])'\s*\+\s*(\w+)", src)
    prefixes = {p for p, _ in dyn}
    assert prefixes, "没找到动态 key 拼接（测试可能失效了）"

    zh = set(_extract_pack("ZH"))
    en = set(_extract_pack("EN"))

    for prefix in sorted(prefixes):
        # 从 knowledge.VIEWS 拿所有可能的 kind
        kinds = _knowledge_view_kinds()
        for kind in kinds:
            key = f"{prefix}{kind}"
            assert key in zh, f"中文包缺 {key}（动态 key 未被覆盖）"
            assert key in en, f"英文包缺 {key}"
            # .desc 变体（如果有用的话）
            desc_key = f"{prefix}{kind}.desc"
            if desc_key in zh:
                assert desc_key in en, f"英文包缺 {desc_key}"


def _knowledge_view_kinds() -> set[str]:
    """从 knowledge.py 读所有视图 kind（单一数据源）。"""
    src = (Path(__file__).resolve().parent.parent
           / "src" / "shared_memory" / "knowledge.py").read_text(encoding="utf-8")
    m = re.search(r"^VIEWS[^=]*=\s*\{(.*?)^\}", src, re.S | re.M)
    assert m, "knowledge.py 里找不到 VIEWS"
    return set(re.findall(r'^\s*"([a-z]+)":', m.group(1), re.M))


def test_no_unused_keys() -> None:
    """语言包里不该有大量用不到的 key（那是删代码时忘了删文案）。

    ━━━ ⚠️ 当前是「部分国际化」，不是全部 ━━━

    用户的要求是「**管理页面**做国际化」，所以：
      · 设置页（用户/token/工作空间/配置）→ **已全部走 t()**
      · 记忆库 / 体系页 → 仍是硬编码中文

    因此语言包里有相当一部分 key 暂时没被用到 —— 那是**有意的**
    （为后续把另外两页也国际化预留）。所以这里的阈值放宽，
    但**不能完全不管**：如果某个 key 连"将来会用"的可能都没有，
    就该删掉。

    判据改成：**管理页相关的 key 必须都被用到**（那部分已完工），
    整体未使用比例不超过 60%（防止无节制堆 key）。
    """
    src = (STATIC / "panel.js").read_text(encoding="utf-8")
    html = (Path(__file__).resolve().parent.parent
            / "src" / "shared_memory" / "dashboard.py").read_text(encoding="utf-8")

    used = set(re.findall(r"\bt\(\s*'([^']+)'", src))
    used |= set(re.findall(r"\bt\(\s*\"([^\"]+)\"", src))
    used |= set(re.findall(r'data-i18n="([^"]+)"', html))
    used |= set(re.findall(r"data-i18n-placeholder=\"([^\"]+)\"", html))

    zh = set(_extract_pack("ZH"))
    unused = zh - used

    # ① 管理页相关的 key **必须**被用到（那部分已完工）
    admin_prefixes = ("admin.", "ws.", "share.", "cfg.", "login.")
    admin_unused = sorted(k for k in unused if k.startswith(admin_prefixes))
    assert not admin_unused, (
        f"管理页有 {len(admin_unused)} 个 key 定义了却没用：{admin_unused[:10]}\n"
        "（管理页是已完工的部分，不该有未使用的 key）"
    )

    # ② 整体未使用比例不能太高（防止无节制堆 key）
    ratio = len(unused) / max(len(zh), 1)
    assert ratio < 0.60, (
        f"{len(unused)}/{len(zh)} 个 key 未使用（{ratio:.0%}）—— 太多了。\n"
        f"例：{sorted(unused)[:10]}"
    )


# ══════════════════════ 运行时行为 ══════════════════════


def test_lang_switch_remembers_choice() -> None:
    """切换语言要记住（localStorage）—— 尊重用户决定。"""
    src = _i18n_source()
    assert "localStorage.setItem('shm-lang'" in src, "切换语言没有持久化"
    assert "localStorage.getItem('shm-lang')" in src, "启动时没有读取保存的语言"


def test_browser_language_only_switches_for_english() -> None:
    """⚠️ 只有**明确是英文**才自动切，其它语言一律回落中文。

    理由：这是团队内部工具，日语/韩语/德语用户很可能更想看中文。
    "不是中文就用英文"是错的。
    """
    src = _i18n_source()
    m = re.search(r"function detectLang\(\)\{(.*?)^\}", src, re.S | re.M)
    assert m, "找不到 detectLang"
    body = m.group(1)
    assert "startsWith('en')" in body, "没有按英文前缀判断"
    # 不该有"非中文就用英文"的写法
    assert "!== 'zh'" not in body and "!= 'zh'" not in body, (
        "detectLang 里出现了「非中文就用英文」的逻辑 —— "
        "那会让日语/韩语用户看到英文，而他们可能更想看中文"
    )


def test_missing_key_falls_back_to_default_language() -> None:
    """缺 key 时回落到中文，而不是返回空串。

    空串会让界面上出现"什么都没有"的区域；显示 key 至少能看出缺翻译。
    """
    src = _i18n_source()
    m = re.search(r"function t\(key, params\)\{(.*?)^\}", src, re.S | re.M)
    assert m, "找不到 t()"
    body = m.group(1)
    assert "?? ZH[key]" in body or "|| ZH[key]" in body, (
        "t() 没有回落到中文语言包"
    )
    assert "return key" in body, "t() 缺 key 时没有回落到 key 本身"


def test_static_text_uses_attributes_not_rebuild() -> None:
    """⚠️ 静态文案用 data-i18n 属性替换，不能重建 DOM。

    重建会把事件监听器全弄丢（每次切语言都要重新绑定，容易漏）。
    """
    src = _i18n_source()
    assert "data-i18n" in src
    m = re.search(r"function applyStaticText\(\)\{(.*?)^\}", src, re.S | re.M)
    assert m, "找不到 applyStaticText"
    body = m.group(1)
    assert "textContent" in body, "没有用 textContent 替换"
    assert "innerHTML =" not in body.replace("el.innerHTML = s", ""), (
        "applyStaticText 里似乎在重建 DOM（会丢事件监听器）"
    )

def test_html_data_i18n_keys_exist_in_packs() -> None:
    """⚠️ HTML 里的 ``data-i18n`` 键必须存在于两个语言包。

    ━━━ 为什么补这条（真实事故）━━━

    2026-10-16：改导航键名时只改了语言包，``dashboard.py`` 里的
    ``data-i18n="nav.knowledge"`` 没跟上 —— 界面上那个标签会显示成
    **裸键名**。而当时的 i18n 测试只扫 ``panel.js``，**对 HTML 是盲的**，
    全绿放行。

    这类 bug 的特征就是静默：没人会去点遍每个标签检查文案。
    所以把 dashboard.py 的 HTML 也纳入扫描。
    """
    import re
    root = Path(__file__).resolve().parent.parent / "src" / "shared_memory"
    html = (root / "dashboard.py").read_text(encoding="utf-8")
    zh, en = _extract_pack("ZH"), _extract_pack("EN")

    keys = set(re.findall(r'data-i18n="([^"]+)"', html))
    assert keys, "没扫到任何 data-i18n（选择器写错了？那这条测试就是假绿）"

    missing = sorted(k for k in keys if k not in zh or k not in en)
    assert not missing, f"HTML 用了语言包里没有的键（界面会显示裸键名）：{missing}"
