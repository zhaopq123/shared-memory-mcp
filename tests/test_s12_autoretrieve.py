"""v2 · S-12 自动存取（读全自动 + 写入推断 + 诚实边界）。

━━━ 这一片要钉住的五件事 ━━━

1. **三段注入**（S-12.1）：握手时 instructions 末尾带上
   长期摘要 / 硬约束清单 / 当前任务上下文包。
2. **constraint 每次必达**：这一段与游标无关 —— 首连、零变更都要在。
   硬约束靠"agent 想起来搜"就等于没有（D-9-1）。
3. **注入层必须过共享档位闸**：digest 是服务端**主动**塞进提示词的，
   调用方没索取就能看到。漏过滤 = 主动泄露别人的 ``mine`` 条目 ——
   比工具面泄露更严重，因为连"查询"这个动作都没有。
4. **写入推断**（S-12.2）：constraint/workflow 优先判型；判成 task 的
   过程态自动落当前会话任务域。推断只是**建议值**，显式传必覆盖（D-9-a）。
5. **诚实边界入文档与设置页**（S-12.3/S-12.4）：client-pull 写不了强制，
   不承诺推送。

口径来源：``02_交付件/共享记忆/shared-memory-mcp-v2.1-增量设计-20261016.md``
的 D-7 / D-9 与「S-12」切片行。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shared_memory import digest as D  # noqa: E402
from shared_memory.config import DEFAULT_RULES, apply_rules_on_write, infer_meta  # noqa: E402
from shared_memory.tenancy import TenantHub  # noqa: E402
from shared_memory.tenant import Library, User  # noqa: E402
from shared_memory.tools_tenant import TenantTools  # noqa: E402

SRC = Path(__file__).resolve().parent.parent / "src" / "shared_memory"
ROOT = SRC.parent.parent


@pytest.fixture()
def hub(tmp_path: Path) -> TenantHub:
    h = TenantHub(tmp_path / "home")
    h.store.users["alice"] = User(name="alice", role="admin", groups=["研发"])
    h.store.users["bob"] = User(name="bob", role="member", groups=["研发"])
    h.store.users["carol"] = User(name="carol", role="member", groups=["销售"])
    h.store.libs["company"] = Library(id="company", owner="company")
    h.store.libs["group-研发"] = Library(id="group-研发", owner="group:研发")
    h.store.libs["group-销售"] = Library(id="group-销售", owner="group:销售")
    h.store.save()
    return h


async def _note(hub: TenantHub, user: str, title: str, **kw) -> None:
    await hub.write(user, title=title, body="内容", lib_id="group-研发",
                    author=user, **kw)


# ══════════════════════ S-12.1 三段注入 ══════════════════════


@pytest.mark.asyncio
async def test_three_sections_present(hub: TenantHub) -> None:
    """三段都在，且顺序按设计文档：变更摘要 → 硬约束 → 任务上下文。"""
    await _note(hub, "alice", "部署流程 SOP", extra={"type": "workflow"})
    await _note(hub, "alice", "禁止手改 tenants.json", extra={"type": "constraint"})
    await _note(hub, "bob", "别人写的一条变更", extra={"type": "fact"})

    await _suffix(hub, "alice")                     # 建基线
    await _note(hub, "bob", "新的现行口径", extra={"type": "fact"})
    suffix, _ = await _suffix(hub, "alice")

    for head in ("记忆库变更", "硬约束", "当前任务上下文"):
        assert head in suffix, f"缺一段（{head}）：{suffix!r}"
    assert (suffix.index("记忆库变更") < suffix.index("硬约束")
            < suffix.index("当前任务上下文")), f"段序错：{suffix!r}"
    assert "禁止手改 tenants.json" in suffix
    assert "新的现行口径" in suffix
    assert "部署流程 SOP" in suffix, "工作流没进第三段"


@pytest.mark.asyncio
async def test_constraint_delivered_on_first_connect(hub: TenantHub) -> None:
    """⚠️ 首次连接（还没游标）也必须带约束清单。

    那恰恰是最需要约束的一刻 —— 旧实现在这里只返回任务上下文，
    约束被"零噪音"原则一起挡掉了，属于把两件事混为一谈。
    """
    await _note(hub, "alice", "一律先备份再改数据", extra={"type": "constraint"})
    suffix, cursors = await _suffix(hub, "alice")
    assert "一律先备份再改数据" in suffix, f"首连没带约束：{suffix!r}"
    assert "硬约束" in suffix
    assert "alice" in cursors, "首连没建基线"


@pytest.mark.asyncio
async def test_constraint_delivered_when_nothing_changed(hub: TenantHub) -> None:
    """零变更：第 1 段为空（不刷屏），第 2 段照给（必达）。"""
    await _note(hub, "alice", "禁止跑整篇重生成脚本", extra={"type": "constraint"})
    await _suffix(hub, "alice")                     # 基线
    suffix, _ = await _suffix(hub, "alice")         # 什么都没变
    assert "记忆库变更" not in suffix, "无变更却播报了变更"
    assert "禁止跑整篇重生成脚本" in suffix, "零变更时约束消失了"


@pytest.mark.asyncio
async def test_manifest_empty_when_no_constraints(hub: TenantHub) -> None:
    """一条约束都没有 → 不铺空段（零噪音仍然成立）。"""
    await _note(hub, "alice", "普通事实条目", extra={"type": "fact"})
    suffix, _ = await _suffix(hub, "alice")
    assert suffix == ""


# ══════════════════════ 注入层的共享档位闸 ══════════════════════


@pytest.mark.asyncio
async def test_digest_does_not_leak_others_private_notes(hub: TenantHub) -> None:
    """⚠️ 别人的 ``mine`` 条目不得出现在你的摘要里（主动注入 = 更严重的泄露）。

    这是 S-12.1 顺手补上的洞：compute() 原来只过生效域，没过档位闸，
    而 digest 是**服务端主动**塞进 instructions 的 —— 调用方没查询
    就看到标题，连"存在性探针"都省了。
    """
    await _suffix(hub, "alice")                     # 先给 alice 建基线
    await _note(hub, "bob", "bob 的私密结论",
                extra={"type": "fact", "share": "mine"})
    suffix, _ = await _suffix(hub, "alice")
    assert "bob 的私密结论" not in suffix, f"泄露了别人的 mine 条目：{suffix!r}"

    # 同一条如果是 space 档位，就该正常播报（闸没误伤）
    await _note(hub, "bob", "bob 的公开结论",
                extra={"type": "fact", "share": "space"})
    suffix2, _ = await _suffix(hub, "alice")
    assert "bob 的公开结论" in suffix2, "档位闸误伤了可共享条目"


@pytest.mark.asyncio
async def test_constraint_manifest_does_not_leak_others_private(hub: TenantHub) -> None:
    """约束清单同样过闸：别人的私密约束不能常驻进你的提示词。"""
    await _note(hub, "alice", "alice 的公开约束",
                extra={"type": "constraint", "share": "space"})
    await _note(hub, "bob", "bob 的私密约束",
                extra={"type": "constraint", "share": "mine"})
    m = D.constraint_manifest(hub, "alice")
    assert "alice 的公开约束" in m
    assert "bob 的私密约束" not in m, f"约束清单泄露：{m!r}"
    # bob 自己看得见自己的
    assert "bob 的私密约束" in D.constraint_manifest(hub, "bob")


# ══════════════════════ S-12.1 第三段：工作流置顶 ══════════════════════


@pytest.mark.asyncio
async def test_workflow_pinned_in_context_package(hub: TenantHub) -> None:
    """任务上下文包里 workflow 在前（D-9-2），且无活跃会话时也给出。"""
    await _note(hub, "alice", "发布流程：先测后推", extra={"type": "workflow"})
    line = D.session_context(hub, "alice", hub.home)
    assert "工作流（按它走）" in line, f"工作流没进上下文包：{line!r}"
    assert "发布流程：先测后推" in line
    assert line.index("工作流") < (line.index("你正在做") if "你正在做" in line
                                else len(line)), "工作流没置顶"
    # 换组的人读不到那个工作流 → 没有任何资产时保持空串（零噪音）
    assert D.session_context(hub, "carol", hub.home) == ""


# ══════════════════════ S-12.2 类型推断优先序 ══════════════════════


def test_constraint_wins_over_lesson_in_inference() -> None:
    """「教训：必须 X」判成 constraint，不是 lesson。

    代价不对称：判成 lesson → 这条硬约束**永远不进常驻清单**，
    整个机制对它失效且无人察觉；判成 constraint → 只是清单多一行。
    """
    out = infer_meta(title="教训：必须先跑 lint 再提交", body="正文",
                     workspace="w", rules=DEFAULT_RULES)
    assert out.get("type") == "constraint", out


def test_workflow_still_inferred_from_its_own_words() -> None:
    out = infer_meta(title="部署流程：五步操作手册", body="正文",
                     workspace="w", rules=DEFAULT_RULES)
    assert out.get("type") == "workflow", out


def test_untouched_titles_still_lesson() -> None:
    """不含 constraint/workflow 信号词时，其它型照旧（没有把好判型判坏）。"""
    out = infer_meta(title="踩坑记录：Windows 只读对象", body="正文",
                     workspace="w", rules=DEFAULT_RULES)
    assert out.get("type") == "lesson", out


# ══════════════════════ S-12.2 scope 推断 ══════════════════════


def test_task_type_infers_task_scope() -> None:
    """判成 task + 有当前会话 → 建议落任务域。"""
    out = infer_meta(title="待确认：是否清 .trash", body="正文",
                     workspace="w", rules=DEFAULT_RULES,
                     active_task_scope="task:共享记忆v2")
    assert out.get("type") == "task", out
    assert out.get("scope") == "task:共享记忆v2", out


def test_no_scope_guess_without_session() -> None:
    """没开会话时**不猜**（猜错会把条目写进别人的域）。"""
    out = infer_meta(title="待确认：是否清 .trash", body="正文",
                     workspace="w", rules=DEFAULT_RULES)
    assert "scope" not in out, out


def test_explicit_scope_beats_inference() -> None:
    """⚠️ D-9-a：推断只是建议值，显式传的一定向后覆盖。"""
    tags, extra, notes = apply_rules_on_write(
        title="待确认：是否清 .trash", body="正文", tags=[], workspace="w",
        extra={"scope": "long-term"}, rules=DEFAULT_RULES,
        active_task_scope="task:共享记忆v2",
    )
    assert extra["scope"] == "long-term", extra
    assert not any("task:共享记忆v2" in n for n in notes), (
        f"返回里把被覆盖的建议当成了推断结果：{notes}"
    )


@pytest.mark.asyncio
async def test_write_binds_inferred_task_note_to_session(
    hub: TenantHub, tmp_path: Path
) -> None:
    """端到端：登记会话后写「待办：…」，条目自动落在该会话任务域。"""
    tools = TenantTools(hub)
    r = await tools.memory_session_start("alice", "共享记忆v2", space="group-研发")
    assert r["ok"] is True, r

    w = await tools.memory_write(
        "alice", title="待办：补上注入层文档", body="正文",
        lib="group-研发", author="alice",
    )
    assert w["ok"] is True, w
    got = hub.get("alice", "待办：补上注入层文档", lib_id="group-研发")
    assert got["note"].get("scope") == "task:共享记忆v2", got["note"]


@pytest.mark.asyncio
async def test_explicit_long_term_scope_not_overridden_by_inference(
    hub: TenantHub, tmp_path: Path
) -> None:
    """显式 scope="long-term" 时推断必须让路（哪怕标题判成 task）。"""
    tools = TenantTools(hub)
    await tools.memory_session_start("alice", "共享记忆v2", space="group-研发")
    w = await tools.memory_write(
        "alice", title="待办：长期有效的口径", body="正文",
        lib="group-研发", author="alice", scope="long-term",
    )
    assert w["ok"] is True, w
    got = hub.get("alice", "待办：长期有效的口径", lib_id="group-研发")
    assert "scope" not in got["note"], got["note"]   # long-term 是缺省，不输出


# ══════════════════════ S-12.3 / S-12.4 文档与设置页 ══════════════════════


def test_client_hooks_doc_is_actionable() -> None:
    """文档不是说明文：必须有事件表、重试约定、可复制样例、未验证声明。"""
    doc = (ROOT / "docs" / "client-hooks.md").read_text(encoding="utf-8")
    for probe in ("sessionStart", "preCompact", "Retry-After", "QUEUE_FULL",
                  "fastmcp", "memory_remember", "systemMessage"):
        assert probe in doc, f"hook 规范缺 {probe}"
    # 诚实边界两面都要写：client-pull + 不承诺推送
    assert "client-pull" in doc and "不承诺" in doc
    # 未验证的东西必须标出来，不能冒充实测
    assert "未验证声明" in doc, "文档把没实测的 hook 格式写成事实了"


def test_settings_page_states_honest_boundary() -> None:
    """设置页有「自动存取（诚实边界）」一节（S-12.4 的另一半）。"""
    js = (SRC / "static" / "panel.js").read_text(encoding="utf-8")
    for key in ("cfg.autoTitle", "cfg.autoRead", "cfg.autoWrite", "cfg.autoNoPush"):
        assert f"t('{key}')" in js, f"设置页没引用 {key}"
    i18n = (SRC / "static" / "i18n.js").read_text(encoding="utf-8")
    assert "docs/client-hooks.md" in i18n, "设置页没指向 hook 规范"


# ══════════════════════ 小工具 ══════════════════════


async def _suffix(hub: TenantHub, user: str) -> tuple[str, dict]:
    """build_suffix 是同步重活（扫库 + 文件 IO），卸到线程池里跑。"""
    import asyncio

    return await asyncio.to_thread(
        lambda: D.build_suffix(hub, user, hub.home)
    )
