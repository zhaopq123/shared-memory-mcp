"""生产环境端到端验证 —— 用**真 token** 打**真服务**。

    cd /opt/shared-memory-mcp
    PYTHONPATH=src ./venv/bin/python scripts/e2e.py

━━━ 与 smoke.py 的分工 ━━━

  · smoke.py —— 本地起一个临时服务，验证"能不能起来"
  · **e2e.py** —— 打已经跑着的生产服务（8200），验证"真的能用"

后者不可替代：它验的是真实部署状态（守护循环、token 文件、
真实数据目录），而这些在临时环境里是模拟的。

━━━ v2 改版（S-4）━━━

工具面收敛到 9 个直觉动词（+7 个旧名别名，过渡期）。本脚本按**新名**
走全链路：空间 → 会话 → 记住 → 召回 → 取全文 → 提交 → 备份 → 软删。
工作空间**创建/共享**已从工具面移到面板（agent 不该自己改权限），
所以这里不再有那条旅程 —— 那是刻意的收窄，不是遗漏。

样例条目统一用 ``E2E自检·`` 前缀，跑完自动软删（重复跑不增长）。
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# 可移植：优先用仓库自带的 src，其次退回生产部署路径。
_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE / "src") if (_HERE / "src").is_dir() else "/opt/shared-memory-mcp/src")

URL = os.environ.get("SHARED_MEMORY_E2E_URL", "http://127.0.0.1:8200/mcp")
PREFIX = "E2E自检·"
#: 专用自检沙箱库（admin 自有可写）—— 不往个人库里灌测试数据。
SANDBOX = "ws-e2e自检"

#: 期望的工具面：9 个新名 + 7 个旧名别名（过渡期）。
CANON = {
    "memory_session_start", "memory_session_end",
    "memory_recall", "memory_remember", "memory_get", "memory_forget",
    "memory_spaces", "memory_history", "memory_backup",
}
ALIASES = {
    "memory_search", "memory_write", "memory_delete",
    "memory_libs", "memory_workspaces", "memory_export", "memory_import",
}


def token() -> str:
    """从 token 文件读第一把（格式：user:label:token）。"""
    path = os.environ.get(
        "SHARED_MEMORY_TOKEN_FILE", "/opt/shared-memory-mcp/.tokens"
    )
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        return parts[-1]          # 最后一段是 token
    raise SystemExit(f"token 文件里没有凭据：{path}")


async def main() -> int:
    from fastmcp import Client

    tok = token()
    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        print(f"  {'✓' if cond else '✗'} {name}" + (f"  {detail}" if detail else ""))
        ok = ok and cond

    async def call(c, name: str, args: dict | None = None) -> dict:
        r = await c.call_tool(name, args or {})
        if getattr(r, "data", None) is not None:
            return r.data
        return json.loads(r.content[0].text)

    print(f"\n{'═' * 62}\n  生产环境端到端验证（127.0.0.1:8200）· v2 工具面\n{'═' * 62}\n")

    async with Client(URL, auth=tok, timeout=60) as c:
        # ── 1. 工具面 ──
        print("1. 工具面（9 新 + 7 别名）")
        tools = await c.list_tools()
        names = {t.name for t in tools}
        check("新名齐全（9 个）", CANON <= names,
              f"缺 {sorted(CANON - names)}" if CANON - names else "")
        check("旧名别名仍在（过渡期）", ALIASES <= names,
              f"缺 {sorted(ALIASES - names)}" if ALIASES - names else "")
        check("工具面收窄（<= 16）", len(names) <= 16, f"实际 {len(names)}")

        # ── 2. 空间与身份（旧 whoami/libs/workspaces 三合一）──
        print("2. 空间与身份")
        sp = await call(c, "memory_spaces")
        check("memory_spaces 返回身份", bool(sp.get("user", {}).get("name")))
        check("含库清单", bool(sp.get("libraries")))
        check("含工作空间共享范围", "spaces" in sp)

        # ── 3. 会话生命周期（防串台三闸）──
        print("3. 会话生命周期")
        st = await call(c, "memory_session_start",
                        {"task": "e2e自检", "space": SANDBOX})
        check("开工返回三段上下文包",
              all(k in st for k in ("constraints", "task_notes", "space_brief")))

        await call(c, "memory_remember", {
            "title": PREFIX + "过程态", "body": "关键词 自检词A",
            "lib": SANDBOX, "scope": "task",
        })
        got = await call(c, "memory_recall", {"query": "自检词A"})
        check("本会话任务域默认可召回",
              any(i["title"] == PREFIX + "过程态" for i in got.get("results", [])))

        en = await call(c, "memory_session_end", {"task": "e2e自检", "outcome": "done"})
        check("收工归类成功", en.get("ok") is True)
        check("过程态被归档",
              PREFIX + "过程态" in (en.get("archived", []) + en.get("promoted", [])))
        after = await call(c, "memory_recall", {"query": "自检词A", "scope": "long-term"})
        check("归档后退出默认检索",
              not any(i["title"] == PREFIX + "过程态" for i in after.get("results", [])))

        # ── 4. 长期记忆：写 → 召回 → 取全文 ──
        print("4. 长期记忆链路")
        await call(c, "memory_remember", {
            "title": PREFIX + "长期条", "body": "关键词 自检词B。依据：e2e。",
            "lib": SANDBOX, "claim": "fact",
        })
        hit = await call(c, "memory_recall", {"query": "自检词B"})
        check("长期条可召回",
              any(i["title"] == PREFIX + "长期条" for i in hit.get("results", [])))
        full = await call(c, "memory_get",
                          {"ident": PREFIX + "长期条", "lib": SANDBOX})
        check("memory_get 取到全文", "自检词B" in (full.get("note", {}).get("body", "")))

        # ── 5. 旧名别名行为等价 ──
        print("5. 旧名别名等价")
        a = await call(c, "memory_recall", {"query": "自检词B"})
        b = await call(c, "memory_search", {"query": "自检词B"})
        check("memory_search ≡ memory_recall",
              [i["title"] for i in a.get("results", [])]
              == [i["title"] for i in b.get("results", [])])

        # ── 6. 提交（history 合一）──
        print("6. 提交")
        cm = await call(c, "memory_history", {"lib": SANDBOX, "commit": True})
        check("memory_history(commit=True) 成功", cm.get("ok") is True,
              str(cm.get("changed", ""))[:60])

        # ── 7. 越权 ──
        print("7. 越权防护")
        bad = await call(c, "memory_recall", {"query": "x", "lib": "user-nobody"})
        check("无权库被拒（403 而非空结果）",
              bad.get("code") == "FORBIDDEN" or bad.get("ok") is False,
              str(bad.get("code", "")))

        # ── 8. 写保护（关键：**不能静默成功**）──
        print("8. 写保护")
        ro = await call(c, "memory_remember", {
            "title": PREFIX + "越权写", "body": "x", "lib": "lib-不存在",
        })
        check("写不存在的库被拒（不是静默落别处）",
              ro.get("ok") is False, str(ro.get("code", "")))

        # ── 9. 备份：导出 → 导入（幂等）──
        print("9. 备份（导出 / 导入合一）")
        ex = await call(c, "memory_backup", {"action": "export", "lib": SANDBOX})
        check("导出成功", ex.get("ok") is True)
        if ex.get("ok"):
            # 返回里 bundle 是**导出目录**（键名就是 bundle，不是 out_dir）
            im = await call(c, "memory_backup", {
                "action": "import", "bundle": ex.get("bundle", ""),
                "target_lib": SANDBOX, "on_conflict": "skip",
            })
            check("导入成功（skip 幂等）", im.get("ok") is True,
                  f"skipped={im.get('skipped')}")

        # ── 10. 软删除（预览 → 确认）──
        print("10. 软删除")
        prev = await call(c, "memory_forget",
                          {"ident": PREFIX + "长期条", "lib": SANDBOX})
        check("不传 confirm 只预览（不删）", "confirm=True" in str(prev.get("message", "")),
              str(prev.get("code", "")))
        de = await call(c, "memory_forget",
                        {"ident": PREFIX + "长期条", "confirm": True, "lib": SANDBOX})
        check("confirm 后软删", de.get("ok") is True)
        gone = await call(c, "memory_recall", {"query": "自检词B"})
        check("删后不再召回",
              not any(i["title"] == PREFIX + "长期条" for i in gone.get("results", [])))

        # ── 11. 接入零门槛（connect 脚本，S-5.5）──
        print("11. 接入零门槛（connect）")
        connect_py = Path(__file__).resolve().parent / "connect.py"
        with tempfile.TemporaryDirectory() as td:
            def _run_connect() -> subprocess.CompletedProcess:
                return subprocess.run(
                    [sys.executable, str(connect_py),
                     "sm://127.0.0.1:8200?k=E2E自检", "--project-dir", td],
                    capture_output=True, text=True, encoding="utf-8", timeout=60,
                )

            r1 = _run_connect()
            cfg_path = Path(td) / ".mcp.json"
            check("干净目录跑一次即接入", r1.returncode == 0 and cfg_path.exists(),
                  r1.stdout.strip().splitlines()[0] if r1.stdout else "")
            if cfg_path.exists():
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                entry = cfg["mcpServers"]["shared-memory"]
                check("条目指向本服务且带凭据",
                      entry["url"].endswith("/mcp") and "Bearer" in str(entry["headers"]))
            r2 = _run_connect()
            check("重复跑幂等（不重写）", "已配置过" in r2.stdout)

    print("\n" + "═" * 62)
    print("  结果: " + ("全部通过 ✓" if ok else "有失败 ✗"))
    print(f"  说明: 样例条目统一 {PREFIX} 前缀并在末尾软删（重复跑不增长）")
    print("═" * 62)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
