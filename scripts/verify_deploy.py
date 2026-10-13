"""自动推断 + 视图的端到端验证脚本（在 vivo 容器内跑）。

为什么单独写成文件而不是内联：内联 heredoc 经过
PowerShell → ssh → bash 三层转义后，中文和引号必然被破坏。
写成文件再传，内容原样到达。
"""

import asyncio
import json
import sys

sys.path.insert(0, "/opt/shared-memory-mcp/src")

from fastmcp import Client  # noqa: E402

TOKEN = [l.strip().split(":")[-1] for l in open("/opt/shared-memory-mcp/.tokens") if l.strip() and not l.startswith("#")][0]
URL = "http://127.0.0.1:8200/mcp"


async def main() -> int:
    ok = True
    async with Client(URL, auth=TOKEN, timeout=30) as c:

        # ── 1. 自动推断：标题含「教训」应推断出 type=lesson ──
        r = await c.call_tool("memory_write", {
            "title": "教训：配置改错要能查出来",
            "body": "实测：配置写错时症状离配置很远。所以保存前必须校验，"
                    "并明确告诉用户是哪一项不合法。",
            "workspace": "mcp-dev",
            "tags": ["测试", "配置"],
        })
        d = json.loads(r.content[0].text)
        print("1. 写入:", d.get("ok"), "| action:", d.get("action"))
        print("   notes:", d.get("notes"))
        if not d.get("ok"):
            ok = False

        # ── 2. 读回看推断结果落盘了没 ──
        g = await c.call_tool("memory_get", {"ident": "教训：配置改错要能查出来"})
        n = json.loads(g.content[0].text)["note"]
        print("2. type:", n.get("type"), "| claim:", n.get("claim"))
        print("   workspace:", n.get("workspace"))
        print("   tags:", n.get("tags"))
        if n.get("type") != "lesson":
            print("   ✗ 自动推断未生效（期望 lesson）")
            ok = False

        # ── 3. 视图：应该能在踩坑清单里看到它 ──
        v = await c.call_tool("memory_view", {"kind": "lessons"})
        vd = json.loads(v.content[0].text)
        titles = [i["title"] for i in vd.get("items", [])]
        print("3. 踩坑视图 total:", vd.get("total"), "->", titles)
        if "教训：配置改错要能查出来" not in titles:
            print("   ✗ 视图没收录新条目")
            ok = False

        # ── 4. 工作区过滤 ──
        s = await c.call_tool("memory_search", {"query": "", "workspace": "mcp-dev"})
        sd = json.loads(s.content[0].text)
        print("4. 按工作区 mcp-dev 搜:", sd.get("total"), "条")
        if sd.get("total", 0) < 1:
            print("   ✗ 工作区过滤没生效")
            ok = False

        # ── 5. 引用：写一条引用它的记忆，验证反向链接 ──
        await c.call_tool("memory_write", {
            "title": "引用测试条目",
            "body": "参见 [[教训：配置改错要能查出来]] 了解更多。",
        })
        b = await c.call_tool("memory_backlinks", {"title": "教训：配置改错要能查出来"})
        bd = json.loads(b.content[0].text)
        print("5. 反向引用:", bd.get("backlinks"))
        if "引用测试条目" not in bd.get("backlinks", []):
            print("   ✗ 反向引用没识别")
            ok = False

        # ── 6. 时效：写一条已过期的，验证时效告警视图 ──
        await c.call_tool("memory_write", {
            "title": "过期测试条目",
            "body": "这条已经过期了。",
            "mode": "upsert",
        })
        # 用 vault 直接补时效字段（MCP 的 write 还没暴露 valid-until 参数，
        # 这里验证的是**系统能否识别**已过期的条目）
        import pathlib
        import re
        vault_notes = pathlib.Path("/opt/shared-memory-mcp/vault/notes")
        for p in vault_notes.glob("*.md"):
            if p.name.startswith("过期测试条目"):
                txt = p.read_text(encoding="utf-8")
                txt = txt.replace("updated:", "valid-until: 2020-01-01\nupdated:", 1)
                p.write_text(txt, encoding="utf-8")
        e = await c.call_tool("memory_view", {"kind": "expiring"})
        ed = json.loads(e.content[0].text)
        names = [i["title"] for i in ed.get("items", [])]
        print("6. 时效告警:", ed.get("total"), "->", names)
        if "过期测试条目" not in names:
            print("   ✗ 过期条目没被识别")
            ok = False

        # ── 7. 清理测试数据 ──
        for t in ("教训：配置改错要能查出来", "引用测试条目", "过期测试条目"):
            await c.call_tool("memory_delete", {"ident": t, "confirm": True})
        st = await c.call_tool("memory_stats", {})
        sd2 = json.loads(st.content[0].text)
        print("7. 清理后条数:", sd2.get("notes"))

    print()
    print("结果:", "全部通过 ✓" if ok else "有失败项 ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
