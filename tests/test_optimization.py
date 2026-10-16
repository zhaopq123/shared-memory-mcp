"""优化方案测试（2026-10-13）—— 对应 OPTIMIZATION-DESIGN 的方案 2-14。

━━━ 覆盖清单 ━━━

  方案 2   stats git 历史按需（with_commits）
  方案 3   upsert action 标志
  方案 4   write 时效参数 + 坏日期拒绝
  方案 5a  视图折叠被取代条目
  方案 5b  回收站懒清理（gc_trash）
  方案 5c  配额告警
  方案 6   视图目录不依赖任何库
  方案 9   search claim / status 口径过滤
  方案 10  写时关联提示（related）
  方案 11  启动变更摘要（digest）
  方案 14  导入导出往返 / 冲突三策略 / 防穿越 / 版本校验
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shared_memory import digest  # noqa: E402
from shared_memory.errors import ErrorCode  # noqa: E402
from shared_memory.knowledge import expiry_state  # noqa: E402
from shared_memory.tenant import Library, TenantStore, User  # noqa: E402
from shared_memory.tenancy import TenantHub  # noqa: E402
from shared_memory.tools_tenant import TenantTools  # noqa: E402


# ══════════════════════ 夹具 ══════════════════════


@pytest.fixture()
def hub(tmp_path: Path) -> TenantHub:
    """三用户 / 四库环境（与 test_tenancy 同布局，减少认知负担）。"""
    h = TenantHub(tmp_path / "home")
    store = h.store
    store.users["alice"] = User(
        name="alice", password=User.hash_password("alice-pw"),
        role="admin", groups=["研发"], display="Alice",
    )
    store.users["bob"] = User(
        name="bob", password=User.hash_password("bob-pw"),
        role="member", groups=["研发"], display="Bob",
    )
    store.libs["company"] = Library(
        id="company", label="公司", owner="company", writable_by_members=False,
    )
    store.libs["group-研发"] = Library(
        id="group-研发", label="研发组", owner="group:研发",
    )
    store.libs["user-bob"] = Library(
        id="user-bob", label="Bob 的个人库", owner="user:bob",
    )
    store.save()
    return h


async def _write(hub: TenantHub, title: str, *, lib: str = "group-研发",
                 body: str = "正文", **kw) -> dict:
    """往研发组库写一条（bob 有写权限）。"""
    return await hub.write("bob", title=title, body=body, lib_id=lib, **kw)


def _titles(result: dict) -> list[str]:
    return [r["title"] for r in result.get("results", [])]


# ══════════════════════ 方案 9：search 口径过滤 ══════════════════════


async def test_search_claim_filter(hub: TenantHub) -> None:
    """claim=fact 只回 fact；inference 单独可查。"""
    await _write(hub, "部署端口是 8200", extra={"claim": "fact"})
    await _write(hub, "端口可能是 9000", extra={"claim": "inference"})

    facts = hub.search("bob", "端口", lib_id="group-研发", claim="fact")
    assert "部署端口是 8200" in _titles(facts)
    assert "端口可能是 9000" not in _titles(facts)

    infs = hub.search("bob", "端口", lib_id="group-研发", claim="inference")
    assert "端口可能是 9000" in _titles(infs)


async def test_search_status_filters_superseded(hub: TenantHub) -> None:
    """被取代的条目默认（active）搜不到；status=all 考古模式能搜到。"""
    await _write(hub, "旧口径：用 http")
    await _write(hub, "新口径：用 https")
    await _write(hub, "旧口径：用 http", extra={"superseded-by": "新口径：用 https"})

    active = hub.search("bob", "口径", lib_id="group-研发", status="active")
    assert "旧口径：用 http" not in _titles(active)
    assert "新口径：用 https" in _titles(active)

    archaeology = hub.search("bob", "口径", lib_id="group-研发", status="all")
    assert "旧口径：用 http" in _titles(archaeology)


async def test_search_status_filters_expired(hub: TenantHub) -> None:
    """valid-until 已过的条目：active 排除、all 含。"""
    await _write(hub, "过期的临时口径", extra={"valid-until": "2020-01-01"})

    active = hub.search("bob", "过期", lib_id="group-研发", status="active")
    assert "过期的临时口径" not in _titles(active)
    allr = hub.search("bob", "过期", lib_id="group-研发", status="all")
    assert "过期的临时口径" in _titles(allr)


# ══════════════════════ 方案 4：时效参数 ══════════════════════


async def test_write_rejects_invalid_valid_until(hub: TenantHub) -> None:
    """坏日期在进队列前就被拒（INVALID_ARGUMENT）。"""
    with pytest.raises(Exception) as ei:
        await _write(hub, "坏日期", extra={"valid-until": "下周五"})
    assert ei.value.code == ErrorCode.INVALID_ARGUMENT


async def test_write_accepts_valid_until_and_marks_state(hub: TenantHub) -> None:
    """合法日期落 frontmatter，expiry_state 认它。"""
    await _write(hub, "有到期日的口径", extra={"valid-until": "2030-01-01"})
    got = hub.get("bob", "有到期日的口径", lib_id="group-研发")
    meta = got["note"]
    assert meta.get("valid-until") == "2030-01-01"
    assert expiry_state(meta)["state"] == "active"


async def test_write_rejects_blank_superseded_by(hub: TenantHub) -> None:
    """superseded-by 传纯空白 → 拒（手误保护）；空串 = 清除标记（合法）。"""
    with pytest.raises(Exception) as ei:
        await _write(hub, "空白取代", extra={"superseded-by": "   "})
    assert ei.value.code == ErrorCode.INVALID_ARGUMENT

    # 空串的合法用途：upsert 时清除已被设上的取代标记
    await _write(hub, "先标记后清除", extra={"superseded-by": "某新条"})
    meta1 = hub.get("bob", "先标记后清除", lib_id="group-研发")["note"]
    assert meta1.get("superseded-by") == "某新条"
    await _write(hub, "先标记后清除", extra={"superseded-by": ""})
    meta2 = hub.get("bob", "先标记后清除", lib_id="group-研发")["note"]
    assert meta2.get("superseded-by", "") == ""


async def test_upsert_keeps_expiry_fields_when_not_given(hub: TenantHub) -> None:
    """更新不重传时效字段 → 保留原值（防"只想改正文却清掉有效期"）。"""
    await _write(hub, "带有效期的条目", extra={"valid-until": "2030-01-01"})
    await _write(hub, "带有效期的条目", body="改了正文")
    meta = hub.get("bob", "带有效期的条目", lib_id="group-研发")["note"]
    assert meta.get("valid-until") == "2030-01-01"
    assert "改了正文" in meta.get("body", "")


# ══════════════════════ 方案 3：action 标志 ══════════════════════


async def test_upsert_action_created_then_updated(hub: TenantHub) -> None:
    """首建 action=created，再写 action=updated —— agent 靠它判断
    自己是不是在重复写同一条。"""
    r1 = await _write(hub, "action 测试")
    assert r1["action"] == "created"
    r2 = await _write(hub, "action 测试", body="第二版")
    assert r2["action"] == "updated"


async def test_create_mode_conflicts(hub: TenantHub) -> None:
    """mode=create 遇已存在 → CONFLICT。"""
    await _write(hub, "只此一条")
    with pytest.raises(Exception) as ei:
        await _write(hub, "只此一条", mode="create")
    assert ei.value.code == ErrorCode.CONFLICT


# ══════════════════════ 方案 10：写时关联提示 ══════════════════════


async def test_related_notes_hint_on_write(hub: TenantHub) -> None:
    """写一条与既有记忆相似的 → 响应带 related top3（防重复建设的抓手）。"""
    await _write(hub, "vivo 手机服务器 IP 会漂移")
    r = await _write(hub, "vivo 手机服务器 IP 漂移怎么办")
    related = r.get("related") or []
    assert any("IP" in x["title"] for x in related), f"应有 related，实得 {related}"


async def test_no_related_when_unrelated(hub: TenantHub) -> None:
    """完全无关的标题 → 不带 related（不制造噪音）。"""
    await _write(hub, "咖啡机销售话术")
    r = await _write(hub, "量子计算入门")
    assert not r.get("related")


# ══════════════════════ 方案 5a：视图折叠 ══════════════════════


async def test_view_folds_superseded_by_default(hub: TenantHub) -> None:
    """decisions 视图默认不含被取代条目；expiring 恒含。"""
    await _write(hub, "决策甲：选 A 方案")
    await _write(hub, "决策甲：选 A 方案", extra={"superseded-by": "决策乙：选 B 方案"})

    dec = hub.view("bob", "decisions", lib_id="group-研发")
    assert "决策甲：选 A 方案" not in [i["title"] for i in dec["items"]]

    dec_all = hub.view("bob", "decisions", lib_id="group-研发",
                       include_superseded=True)
    assert "决策甲：选 A 方案" in [i["title"] for i in dec_all["items"]]

    exp = hub.view("bob", "expiring", lib_id="group-研发")
    assert "决策甲：选 A 方案" in [i["title"] for i in exp["items"]]


# ══════════════════════ 方案 6：视图目录 ══════════════════════


async def test_view_catalog_needs_no_lib(hub: TenantHub) -> None:
    """不填 kind → 静态目录，任何身份都能拿（无需可读库）。"""
    tools = TenantTools(hub)
    cat = await tools.memory_view("bob", "")
    assert cat["ok"] and any(v["kind"] == "decisions" for v in cat["views"])


# ══════════════════════ 方案 5b：回收站 gc ══════════════════════


async def test_trash_gc_removes_old(hub: TenantHub) -> None:
    """回收站超龄文件被 gc 清掉；新文件保留。"""
    import os
    import time

    await _write(hub, "待删条目")
    await hub.delete("bob", "待删条目", lib_id="group-研发")
    svc = hub.service_for("group-研发")
    trash = svc.vault.trash_dir
    assert len(list(trash.glob("*.md"))) == 1

    # 把回收站文件改成 10 天前 → max_age_days=1 清掉它
    old = time.time() - 10 * 86400
    for p in trash.glob("*.md"):
        os.utime(p, (old, old))
    r = svc.vault.gc_trash(max_age_days=1, max_files=0)
    assert r["removed"] == 1 and r["kept"] == 0

    # 新删的（mtime 是现在）→ 保留
    await _write(hub, "再删一条")
    await hub.delete("bob", "再删一条", lib_id="group-研发")
    r2 = svc.vault.gc_trash(max_age_days=3650, max_files=0)
    assert r2["removed"] == 0 and r2["kept"] == 1


# ══════════════════════ 方案 5c：配额告警 ══════════════════════


async def test_quota_warning_in_stats(hub: TenantHub) -> None:
    """超过 quota_warn_notes → stats 带 quota_warning。"""
    import json as _json

    svc = hub.service_for("group-研发")
    # 配置是每次访问重读文件的 —— 直接写 settings.json（部分键即可，
    # 浅合并补默认值），这也顺带测了"改配置不用重启"。
    svc.config.settings_path.parent.mkdir(parents=True, exist_ok=True)
    svc.config.settings_path.write_text(
        _json.dumps({"quota_warn_notes": 2}), encoding="utf-8"
    )
    await _write(hub, "配额一")
    await _write(hub, "配额二")
    s = hub.stats("bob", lib_id="group-研发")
    assert "quota_warning" in s
    await _write(hub, "配额三")
    s2 = hub.stats("bob", lib_id="group-研发")
    assert "quota_warning" in s2


# ══════════════════════ 方案 2：stats with_commits ══════════════════════


async def test_stats_commits_on_demand(hub: TenantHub) -> None:
    """默认 stats 不带 git 历史（稳态零 fork）；显式要才带。"""
    await _write(hub, "提交测试")
    lean = hub.stats("bob", lib_id="group-研发")
    assert "commits" not in lean
    rich = hub.stats("bob", lib_id="group-研发", with_commits=True)
    assert "commits" in rich


# ══════════════════════ 方案 14：导入导出 ══════════════════════


async def test_export_import_roundtrip_byte_identical(hub: TenantHub, tmp_path: Path) -> None:
    """导出 → 导入新库 → 文件字节级一致（这条链路的意义就是不重写）。"""
    await _write(hub, "项目甲·口径一", body="甲项目正文一", workspace="项目甲")
    await _write(hub, "项目甲·口径二", body="甲项目正文二", workspace="项目甲")
    await _write(hub, "项目乙·别的", body="乙项目正文", workspace="项目乙")

    out = tmp_path / "out"
    r = hub.export("bob", workspace="项目甲", lib_id="group-研发", out_dir=str(out))
    assert r["ok"] and r["count"] == 2
    bundle = Path(r["bundle"])
    manifest = (bundle / "manifest.json")
    assert manifest.exists()
    # 工作区过滤：乙项目的不在 bundle 里
    entries = __import__("json").loads(manifest.read_text(encoding="utf-8"))["entries"]
    assert all(e["workspace"] == "项目甲" for e in entries)

    # 导入 bob 的个人库（原本空的）
    r2 = await hub.import_bundle("bob", str(bundle), target_lib="user-bob")
    assert r2["imported"] == 2 and r2["_lib"] == "user-bob"

    # 字节级一致：对比导出文件与导入落盘文件
    src_files = sorted((bundle / "libs" / "group-研发" / "notes").glob("*.md"))
    dst_files = sorted((hub.service_for("user-bob").vault.root / "notes").glob("*.md"))
    assert len(dst_files) == 2
    for s in src_files:
        assert any(s.read_bytes() == d.read_bytes() for d in dst_files), \
            f"{s.name} 导入后字节不一致"


async def test_import_conflict_skip_update_rename(hub: TenantHub, tmp_path: Path) -> None:
    """冲突三策略：skip 不覆盖 / update 覆盖 / rename 都留。"""
    await _write(hub, "冲突条目", body="原版")
    out = tmp_path / "out2"
    r = hub.export("bob", lib_id="group-研发", out_dir=str(out))
    bundle = Path(r["bundle"])

    # 改 bundle 里的内容，使"导入版"与目标库现状不同
    note_file = next((bundle / "libs" / "group-研发" / "notes").glob("*.md"))
    note_file.write_text(note_file.read_text(encoding="utf-8") + "\n导入版追加\n",
                         encoding="utf-8")
    # manifest 的 sha256 会对不上 → 先重算（模拟"合法但内容不同"的 bundle）
    import hashlib, json
    man = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    for e in man["entries"]:
        p = bundle / e["file"]
        e["sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
    (bundle / "manifest.json").write_text(json.dumps(man, ensure_ascii=False), encoding="utf-8")

    # 导回同一个库 → 与现有条目同标题冲突
    r_skip = await hub.import_bundle("bob", str(bundle), target_lib="group-研发",
                                     on_conflict="skip")
    assert r_skip["skipped"] == 1 and r_skip["updated"] == 0
    body_after_skip = hub.get("bob", "冲突条目", lib_id="group-研发")["note"]["body"]
    assert "导入版追加" not in body_after_skip

    r_upd = await hub.import_bundle("bob", str(bundle), target_lib="group-研发",
                                    on_conflict="update")
    assert r_upd["updated"] == 1
    body_after_upd = hub.get("bob", "冲突条目", lib_id="group-研发")["note"]["body"]
    assert "导入版追加" in body_after_upd

    r_ren = await hub.import_bundle("bob", str(bundle), target_lib="group-研发",
                                    on_conflict="rename")
    assert r_ren["renamed"] == 1
    files = list((hub.service_for("group-研发").vault.root / "notes").glob("*.md"))
    assert len(files) == 2  # 原名 + 改名的副本


async def test_import_rejects_path_traversal(hub: TenantHub, tmp_path: Path) -> None:
    """manifest 里藏 ../ 穿越路径 → 预检拦下，不落盘。"""
    await _write(hub, "正常条目", body="x")
    out = tmp_path / "out3"
    hub.export("bob", lib_id="group-研发", out_dir=str(out))
    bundle = next(out.iterdir())

    import json
    man = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    man["entries"].append({
        "file": "libs/group-研发/notes/../../../../etc/evil.md",
        "id": "evil", "title": "evil", "workspace": "", "sha256": "",
    })
    (bundle / "manifest.json").write_text(json.dumps(man, ensure_ascii=False), encoding="utf-8")
    # 造一个真实文件让"路径存在"（但白名单必须先在形态层拦下）
    evil = bundle / "libs" / "group-研发" / "notes" / ".." / ".." / ".." / ".." / "etc"
    evil.mkdir(parents=True, exist_ok=True)
    (evil / "evil.md").write_text("pwned", encoding="utf-8")

    plan = hub.service_for("group-研发").vault.plan_import(bundle)
    assert all("../" not in e["file"] for e in plan["entries"])
    assert plan["errors"], "穿越条目必须报错"

    r = await hub.import_bundle("bob", str(bundle), target_lib="group-研发")
    # 穿越条目没进来； vault 外的文件也没被写成记忆
    assert not any("evil" in f.name for f in
                   (hub.service_for("group-研发").vault.root / "notes").glob("*.md"))


async def test_import_rejects_unknown_schema(hub: TenantHub, tmp_path: Path) -> None:
    """schema 版本不匹配 → 明确拒绝（返回 ok=False，不许静默乱导）。"""
    await _write(hub, "版本条目")
    out = tmp_path / "out4"
    hub.export("bob", lib_id="group-研发", out_dir=str(out))
    bundle = next(out.iterdir())
    import json
    man = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    man["schema"] = "shared-memory-bundle/999"
    (bundle / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
    r = await hub.import_bundle("bob", str(bundle), target_lib="user-bob")
    assert r["ok"] is False
    assert any("版本" in e or "schema" in e.lower() for e in r["errors"])
    # 什么都没导进去
    assert r["imported"] == 0
    assert not list((hub.service_for("user-bob").vault.root / "notes").glob("*.md"))


async def test_export_permission_required(hub: TenantHub, tmp_path: Path) -> None:
    """bob 可导公司库（导出是读操作）；但导入公司库（member 不可写）→ 403。"""
    r = hub.export("bob", lib_id="company", out_dir=str(hub.home / "exports-test"))
    assert r["ok"]
    await _write(hub, "给导入用的条目")
    out = hub.home / "exports-test2"
    exp = hub.export("bob", lib_id="group-研发", out_dir=str(out))
    with pytest.raises(Exception) as ei:
        await hub.import_bundle("bob", exp["bundle"], target_lib="company")
    assert ei.value.code == ErrorCode.FORBIDDEN


# ══════════════════════ 方案 11：digest ══════════════════════


async def test_digest_first_connect_silent(hub: TenantHub, tmp_path: Path) -> None:
    """首连只建基线，不播报（零噪音）。"""
    await _write(hub, "已有记忆")
    suffix, cursors = digest.build_suffix(hub, "bob", tmp_path)
    assert suffix == ""
    assert "bob" in cursors


async def test_digest_reports_changes(hub: TenantHub, tmp_path: Path) -> None:
    """基线后有新写入 → 下次连接播报，然后推进游标，再下次静默。"""
    await _write(hub, "基线记忆")
    digest.build_suffix(hub, "bob", tmp_path)          # 建基线
    await _write(hub, "新写入口径")
    suffix, _ = digest.build_suffix(hub, "bob", tmp_path)
    assert "新写入口径" in suffix and "研发组" in suffix
    # 游标已推进 → 无新变化时静默
    suffix2, _ = digest.build_suffix(hub, "bob", tmp_path)
    assert suffix2 == ""


async def test_digest_steady_state_alert_not_repeated(hub: TenantHub, tmp_path: Path) -> None:
    """基线前就处于告警态的条目：摘要**不反复播报**（那是 expiring 视图
    的职责）。变更带来的新告警才播，且只播一次。"""
    await _write(hub, "老告警条目", extra={"review-after": "2020-01-01"})
    digest.build_suffix(hub, "bob", tmp_path)          # 基线（该条已在告警态）
    suffix, _ = digest.build_suffix(hub, "bob", tmp_path)
    assert suffix == ""  # 无新变更 → 静默，不刷屏

    # 新写一条到期的 → 这次播
    await _write(hub, "新告警条目", extra={"review-after": "2020-01-01"})
    s1, _ = digest.build_suffix(hub, "bob", tmp_path)
    assert "新告警条目" in s1 and "该复查" in s1
    assert "老告警条目" not in s1  # 老告警不重复播

    s2, _ = digest.build_suffix(hub, "bob", tmp_path)
    assert s2 == ""  # 播过一次就翻篇


async def test_digest_concurrent_connects_no_race(hub: TenantHub, tmp_path: Path) -> None:
    """10 用户同时连：游标写不能互踩（tmp 同名 WinError 32 实测坑，
    以及跨用户整表保存互抹的深层竞态），且精确一次（第三连全静默）。

    ━━━ 断言口径说明 ━━━
    digest 的语义是「**你可读的库**变了」，不是「你写的东西」——
    共享组库里别人的写入当然该出现（那是共享记忆的意义）。
    所以断言"每人看到 10 条变更"，不是"只看到自己的"。
    """
    import asyncio

    users = [f"u{i}" for i in range(10)]
    from shared_memory.tenant import User as _U
    for u in users:
        hub.store.users[u] = _U(name=u, password=_U.hash_password("x"),
                                role="member", groups=["研发"])
    hub.store.save()

    for u in users:
        await _write(hub, f"{u}-基线", lib="group-研发")

    async def connect(u: str) -> str:
        return await asyncio.to_thread(
            lambda: digest.build_suffix(hub, u, tmp_path)[0])

    s0 = await asyncio.gather(*[connect(u) for u in users])
    assert all(s == "" for s in s0), "首连应全部静默（建基线）"

    for u in users:
        await _write(hub, f"{u}-新条", lib="group-研发")

    s1 = await asyncio.gather(*[connect(u) for u in users])
    for u, s in zip(users, s1):
        assert "研发组" in s and "10 条更新" in s, \
            f"{u} 的摘要没反映组库变更：{s!r}"

    s2 = await asyncio.gather(*[connect(u) for u in users])
    assert all(s == "" for s in s2), "第三连应全部静默（seen 记账 + 无竞态丢更新）"


# ══════════════════════ 部署实测回归（2026-10-13 vivo 生产抓到的两个 bug）══════════════════


async def test_search_status_single_state(hub: TenantHub) -> None:
    """status 单态检索（review/expired/superseded）。

    生产实测 bug：vault 白名单只有 active/all，工具描述却承诺支持
    单态 —— 传 expired 直接 INVALID_ARGUMENT，claim 过滤形同虚设
    （因为按态检索根本到不了过滤那一步）。
    """
    await _write(hub, "现行条目", body="x")
    await _write(hub, "过期条目", body="x", extra={"valid-until": "2020-01-01"})
    await _write(hub, "该复查条目", body="x", extra={"review-after": "2020-01-01"})

    svc = hub.service_for("group-研发")
    r_exp = svc.search("条目", status="expired")
    t_exp = _titles(r_exp)
    assert "过期条目" in t_exp, f"expired 单态应命中：{t_exp}"
    assert "现行条目" not in t_exp and "该复查条目" not in t_exp

    r_rev = svc.search("条目", status="review")
    assert "该复查条目" in _titles(r_rev)

    r_all = svc.search("条目", status="all")
    assert len(_titles(r_all)) >= 3, "all = 不按态过滤"

    # 非法值仍然要拒（白名单不是放开不管）
    with pytest.raises(Exception) as ei:
        svc.search("条目", status="bogus")
    assert ei.value.code == ErrorCode.INVALID_ARGUMENT


async def test_backlinks_finds_refs_beyond_first_lib(hub: TenantHub) -> None:
    """不传 lib 时跨库合并，不能停在第一个可读库。

    生产实测 bug：hub.backlinks 的循环在第一个库（company）就
    return，而 service.backlinks 对查不到的标题返回空信封不抛错 ——
    条目在个人库时 backlinks 恒为空。乙引用甲，甲在 user-bob，
    bob 的可读顺序里 company 在前，原实现必然漏掉。
    """
    await _write(hub, "甲·被引用", body="甲正文。", lib="user-bob")
    await _write(hub, "乙·引用方", body="参见 [[甲·被引用]]。", lib="user-bob")

    r = hub.backlinks("bob", "甲·被引用")  # 不传 lib_id → 跨库
    assert "乙·引用方" in r["backlinks"], f"跨库合并应找到引用：{r}"
    assert r.get("lib") == "user-bob", "应标出标题实际所在库"
    # 显式传 lib 的路径不受影响
    r2 = hub.backlinks("bob", "甲·被引用", lib_id="user-bob")
    assert "乙·引用方" in r2["backlinks"]
    # 标题不存在时给空但不炸
    r3 = hub.backlinks("bob", "不存在的标题")
    assert r3["ok"] and r3["backlinks"] == []


# ══════════════════════ 工具层透传 ══════════════════════


async def test_tools_layer_passthrough(hub: TenantHub) -> None:
    """TenantTools 的新参数真的透传到 hub/service/vault。"""
    tools = TenantTools(hub)
    w = await tools.memory_write(
        "bob", title="工具层条目", body="正文",
        lib="group-研发", claim="fact",
        valid_until="2030-06-30", review_after="2030-01-01",
    )
    assert w["ok"]
    meta = hub.get("bob", "工具层条目", lib_id="group-研发")["note"]
    assert meta["valid-until"] == "2030-06-30"
    assert meta["claim"] == "fact"

    s = await tools.memory_search("bob", "工具层", lib="group-研发", claim="fact")
    assert "工具层条目" in _titles(s)
    s_no = await tools.memory_search("bob", "工具层", lib="group-研发", claim="inference")
    assert "工具层条目" not in _titles(s_no)

    # 坏 claim 在工具层就被拒（返回错误字典，不抛）
    bad_claim = await tools.memory_write(
        "bob", title="坏 claim", body="x", lib="group-研发", claim="fakt")
    assert bad_claim["ok"] is False

    # 坏日期在工具层就被拒（返回错误字典，不抛）
    bad = await tools.memory_write("bob", title="坏日期二", body="x", lib="group-研发",
                                   valid_until="not-a-date")
    assert bad["ok"] is False

    st = await tools.memory_stats("bob", lib="group-研发", with_commits=True)
    assert "commits" in st

    v = await tools.memory_view("bob", kind="decisions", lib="group-研发",
                                include_superseded=True)
    assert v["ok"]

    cat = await tools.memory_view("bob", "")
    assert cat["ok"]


async def test_tools_export_import(hub: TenantHub, tmp_path: Path) -> None:
    """工具层的 memory_export / memory_import 全链路。"""
    tools = TenantTools(hub)
    await tools.memory_write("bob", title="导出条目", body="正文", lib="group-研发",
                             workspace="项目X")
    e = await tools.memory_export("bob", workspace="项目X", lib="group-研发",
                                  out_dir=str(tmp_path / "exp"))
    assert e["ok"] and e["count"] == 1
    i = await tools.memory_import("bob", e["bundle"], target_lib="user-bob")
    assert i["imported"] == 1
    got = hub.get("bob", "导出条目", lib_id="user-bob")
    assert got["ok"]

    # 非法冲突策略在工具层被拒
    bad = await tools.memory_import("bob", e["bundle"], target_lib="user-bob",
                                    on_conflict="overwrite")
    assert bad["ok"] is False


async def test_tools_return_envelope_not_exception(hub: TenantHub) -> None:
    """工具层必须**返回**错误信封而不是抛异常（v1 契约，v2 保持）。

    抛异常的话，不同 MCP 客户端行为不一致（有的当协议错误、有的吞掉），
    调用方拿不到可判断的失败原因。本测试由 test_memory.py 的 v1 版本
    迁移而来（方案 7）。
    """
    tools = TenantTools(hub)
    r = await tools.memory_get("bob", "不存在的条目")
    assert r["ok"] is False and r["code"] == "NOT_FOUND"

    r2 = await tools.memory_delete("bob", "whatever")  # 未确认
    assert r2["ok"] is False and r2["code"] == "GOV_CONFIRM_REQUIRED"


async def test_tools_write_search_export_roundtrip(hub: TenantHub, tmp_path: Path) -> None:
    """写 → 搜 → 按工作空间导出 → 导入新库（v1 旧测试的 v2 等价物）。

    v1 版测的是 memory_export 出整库 markdown；v2 的导出是
    **工作空间粒度 + 字节级 bundle**，语义不同了，测法也不同。
    """
    tools = TenantTools(hub)
    w = await tools.memory_write(
        "bob", title="工具写入", body="正文", lib="group-研发", workspace="项目Y")
    assert w["ok"]

    s = await tools.memory_search("bob", "工具写入", lib="group-研发")
    assert s["ok"] and s["total"] == 1

    st = await tools.memory_stats("bob", lib="group-研发")
    assert st["ok"] and st["notes"] == 1

    e = await tools.memory_export("bob", workspace="项目Y", lib="group-研发",
                                  out_dir=str(tmp_path / "rt"))
    assert e["ok"] and e["count"] == 1
    i = await tools.memory_import("bob", e["bundle"], target_lib="user-bob")
    assert i["imported"] == 1
    # 字节级还原：导入后的文件与原文件一致
    src = next((Path(e["bundle"]) / "libs" / "group-研发" / "notes").glob("*.md"))
    dst_files = list(
        (hub.service_for("user-bob").vault.root / "notes").glob("*.md"))
    assert any(src.read_bytes() == d.read_bytes() for d in dst_files)
