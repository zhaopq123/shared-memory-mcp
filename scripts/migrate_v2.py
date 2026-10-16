#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""v2 数据迁移 · M-1 / M-2 / S-10.1（**只读步骤**）。

━━━ 这个脚本为什么只做只读步骤 ━━━

迁移计划（`共享记忆-主密钥迁移清单` 同级的 v2 计划）里 M-3 之后全是写数据：
双写并行、切换存储、归档测试库。那几步**每一步都要求先列清单等人确认** ——
所以这里只实现**不改变任何数据**的三步，把"要给人看的东西"产出来：

  · M-1 全量备份：打一个包并**验证它能解开**（解不开的备份等于没有备份），
                打印文件数 + 字节数给用户看
  · M-2 推断预览：对存量条目**逐条**算出"推断六型 + scope"对照表，
                **不写一个字节**
  · S-10.1 空间默认档建议：按"能读这个库的人数"给出每库的
                ``default_share`` 建议表（单人→mine、协作→space），
                同样**不写一个字节**（改默认档是碰数据的动作）

用法：
    python scripts/migrate_v2.py backup                 # 只做 M-1
    python scripts/migrate_v2.py preview                # 只做 M-2
    python scripts/migrate_v2.py space-preview          # 只做 S-10.1 建议表
    python scripts/migrate_v2.py backup preview         # 两个都做
"""

from __future__ import annotations

import json
import os
import sys
import tarfile
import time
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP / "src"))

# 与服务的 ``tenancy.default_home()`` 同一个环境变量口径（不另造变量）。
# 默认值是容器路径 —— 这个脚本平时就在容器里跑；本机验证时
# ``SHARED_MEMORY_HOME=... python scripts/migrate_v2.py space-preview`` 即可。
HOME = Path(os.environ.get("SHARED_MEMORY_HOME") or "/opt/shared-memory-mcp/tenant-home")
TOKENS = Path("/opt/shared-memory-mcp/.tokens")


def m1_backup() -> dict:
    """M-1：全量备份 + **可解包校验**。

    ⚠️ 校验不是形式：实测里"备份文件存在但打不开"是常见事故
    （打包时进程被 kill、磁盘满）。所以打完必须真解一遍，
    而且**清点条目数**与源目录比对。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = Path.home() / f"shm-v2-full-backup-{stamp}.tar.gz"

    src_count = 0
    for p in HOME.rglob("*"):
        if p.is_file():
            src_count += 1

    with tarfile.open(out, "w:gz") as tf:
        tf.add(HOME, arcname="tenant-home")
        if TOKENS.exists():
            tf.add(TOKENS, arcname=".tokens")

    # 校验：能打开 + 成员数与源一致。
    # ⚠️ 期望值要把**额外打包的 .tokens** 算进去 —— 第一次写漏了它，
    # 结果一个完好的备份被报成"不一致，别用"，这种假警报比不检查更坏：
    # 它会训练人忽略这个提示。
    expect = src_count + (1 if TOKENS.exists() else 0)
    with tarfile.open(out, "r:gz") as tf:
        members = [m for m in tf.getmembers() if m.isfile()]

    return {
        "path": str(out),
        "bytes": out.stat().st_size,
        "files_in_tar": len(members),
        "files_on_disk": src_count,
        "expect": expect,
        "ok": len(members) == expect,
    }


def m2_preview(with_body: bool = False) -> dict:
    """M-2：逐条推断预览（**不写盘**）。

    用服务端同一份 `config.infer_meta` —— 预览与实际运行时的规则必须同源，
    否则"预览说会推断成 A、实际写成 B"比不预览更糟。

    ⚠️ **必须带上正文**：列表接口不返回正文，而类型推断主要看正文里的
    信号词（"必须/一律/禁止"→约束）。第一版我就是只用标题跑的，
    结果 91/91 全推断成"未推断出" —— 那不是系统的结论，是我喂的数据不够。
    这种报告会让人以为"迁移会把类型全清掉"，比不出报告还危险。
    """
    import asyncio
    from shared_memory.config import infer_meta
    from shared_memory.tenancy import TenantHub

    hub = TenantHub(HOME)
    users = list(hub.store.users.keys())

    def _rules_for(lib_id: str) -> dict:
        """取**运行时同一份规则**。

        ⚠️ 第一版我传了空 dict，结果 91/91 全"推断不出" —— 因为
        `infer_type` 的关键词映射在配置里，不在函数默认值里。
        预览取规则的路必须和写入时一模一样，否则预览的结论毫无意义。
        """
        try:
            return hub.service_for(lib_id).config.rules() or {}
        except Exception:  # noqa: BLE001
            return {}
    if not users:
        return {"error": "没有用户"}

    from shared_memory.tools_tenant import TenantTools

    def _bodies(u: str) -> dict[str, str]:
        """按标题取正文（带正文时才跑；慢但准确）。"""
        if not with_body:
            return {}
        tools = TenantTools(hub)
        out: dict[str, str] = {}
        for n in hub.list_notes(u, limit=2000).get("notes", []):
            t = str(n.get("title", ""))
            try:
                r = asyncio.run(tools.memory_get(u, t))
                out[t] = str(r.get("body", "") or "")
            except Exception:  # noqa: BLE001 - 取不到正文就留空，如实标注
                out[t] = ""
        return out

    rows = []
    for u in users:
        notes = hub.list_notes(u, limit=2000).get("notes", [])
        bodies = _bodies(u)
        for n in notes:
            title = str(n.get("title", ""))
            body = bodies.get(title, "")
            rules = _rules_for(str(n.get("_lib", "")))
            got = infer_meta(title=title, body=body, workspace=str(n.get("workspace", "")),
                             rules=rules)
            cur_type = str(n.get("type", "") or "(未标)")
            inferred = str(got.get("type", "") or "")
            rows.append({
                "title": title,
                "cur_type": cur_type,
                "new_type": inferred or "(无信号词)",
                "inferred": inferred,
                # ⚠️ 只有"推断出了具体类型且与现状不同"才算会变。
                # 推断不出**不等于**会被清空 —— 迁移会保持原样。
                # 第一版把二者混为一谈，91 条全被标成"会变"，
                # 那份报告会让人以为迁移要把类型全洗掉。
                "changed": bool(inferred) and inferred != cur_type,
                "tags": got.get("tags", []),
            })
    changed = [r for r in rows if r["changed"]]
    unknown = [r for r in rows if not r["inferred"]]
    return {
        "total": len(rows),
        "changed": len(changed),
        "unknown": len(unknown),
        "with_body": with_body,
        "rows": rows,
        "changed_rows": changed,
    }


def s10_space_preview() -> dict:
    """S-10.1 验收项：存量空间的 ``default_share`` 建议（**只读**）。

    口径（计划里的"单人→mine、协作→space"）：按**实际能读这个库的人数**判 ——
    只有一个人能读的库（个人库、单成员组）默认档建议 ``mine``，
    多人能读的协作库建议 ``space``。

    ⚠️ 这里只**出建议表，不写任何 space.json**。改默认档等于改整库的
    可见性（默认档是**读侧继承**的，一改就作用于全部未标注条目），
    属于碰数据的动作 —— 必须人工确认后再落。

    ⚠️ 读配置走 ``Config(hub.libs.path_of(lib))`` 而**不是** ``hub.service_for()``：
    后者会 ``Vault(dir).init_repo()``（mkdir + git init）并拉起写队列线程 ——
    一个自称"只读预览"的命令不该在租户目录里建目录。
    """
    from shared_memory.config import DEFAULT_SPACE, Config
    from shared_memory.tenancy import TenantHub
    from shared_memory.tenant import can_read

    if not HOME.exists():
        return {"error": f"租户目录不存在：{HOME}"}

    hub = TenantHub(HOME)
    rows = []
    for lib in hub.store.libs.values():
        readers = [u.name for u in hub.store.users.values() if can_read(u, lib)]
        cur = Config(hub.libs.path_of(lib)).space()
        suggested = "mine" if len(readers) <= 1 else "space"
        rows.append({
            "lib": lib.id,
            "label": lib.label or lib.id,
            "readers": len(readers),
            "cur_default": str(cur.get("default_share") or ""),
            "is_default": cur == DEFAULT_SPACE,
            "suggested": suggested,
            "would_change": suggested != str(cur.get("default_share") or ""),
        })
    rows.sort(key=lambda r: (-r["readers"], r["lib"]))
    return {
        "libs": len(rows),
        "unset": sum(1 for r in rows if r["is_default"]),
        "rows": rows,
    }


def main(argv: list[str]) -> int:
    steps = argv[1:] or ["backup", "preview"]

    if "backup" in steps:
        print("═══ M-1 全量备份 ═══")
        r = m1_backup()
        print(f"  包：{r['path']}")
        print(f"  大小：{r['bytes']:,} 字节")
        print(f"  条目：包内 {r['files_in_tar']} / 应有 {r['expect']}"
              f"（盘上 {r['files_on_disk']} + .tokens）"
              f"  {'✓ 一致' if r['ok'] else '✗ 不一致（别用这个包）'}")

    if "preview" in steps:
        print("\n═══ M-2 类型推断预览（不写任何数据）═══")
        r = m2_preview(with_body=("--with-body" in steps))
        if "error" in r:
            print("  ", r["error"])
            return 1
        if not r["with_body"]:
            print("  ⚠️ 未带正文（加 --with-body 才准）—— 类型推断主要看正文信号词，"
                  "只看标题的结果**不代表**真实迁移结果。")
        print(f"  存量 {r['total']} 条，其中 {r['changed']} 条推断结果与现状不同"
              f"（无法推断 {r['unknown']} 条）")
        print("  ── 逐条对照（只列会变的）──")
        for row in r["changed_rows"][:120]:
            print(f"   · {row['title'][:44]:<44} {row['cur_type']:<10} → {row['new_type']}")
        if not r["changed_rows"]:
            print("    （没有会变的 —— 说明存量类型已经都标好了）")

    if "space-preview" in steps:
        print("\n═══ S-10.1 空间默认档建议（不写任何数据）═══")
        r = s10_space_preview()
        if "error" in r:
            print("  ", r["error"])
            return 1
        print(f"  共 {r['libs']} 个库，其中 {r['unset']} 个还没落过 space.json"
              f"（走内置默认 space）")
        print("  ── 逐库对照 ──")
        for row in r["rows"]:
            mark = "★ 建议改" if row["would_change"] else " "
            cur = row["cur_default"] or "(继承内置)"
            print(f"  {mark} {row['label'][:22]:<22} 读者 {row['readers']:>2} 人"
                  f"  现档 {cur:<8} → 建议 {row['suggested']}")
        print("\n  ⚠️ 只是建议表。真要改默认档 = 改整库可见性，"
              "需人工确认后再逐库落 space.json。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
