"""生产环境端到端验证 —— 用**真 token** 打**真服务**。

    cd /opt/shared-memory-mcp
    PYTHONPATH=src ./venv/bin/python scripts/e2e.py

━━━ 与 smoke.py 的分工 ━━━

  · smoke.py —— 本地起一个临时服务，验证"能不能起来"
  · **e2e.py** —— 打已经跑着的生产服务（8200），验证"真的能用"

后者不可替代：它验的是真实部署状态（守护循环、token 文件、
真实数据目录），而这些在临时环境里都是模拟的。
"""

import asyncio
import json
import os
import sys
from pathlib import Path

# 可移植：优先用仓库自带的 src，其次退回生产部署路径。
_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE / "src") if (_HERE / "src").is_dir() else "/opt/shared-memory-mcp/src")

URL = os.environ.get("SHARED_MEMORY_E2E_URL", "http://127.0.0.1:8200/mcp")


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

    print(f"\n{'═' * 62}\n  生产环境端到端验证（127.0.0.1:8200）\n{'═' * 62}\n")

    async with Client(URL, auth=tok, timeout=30) as c:
        # ── 1. 身份 ──
        print("1. 身份")
        tools = await c.list_tools()
        names = sorted(t.name for t in tools)
        print(f"   工具 {len(tools)} 个: {', '.join(names)}")
        check("工具齐全（>= 17 个）", len(tools) >= 17,
              f"实际 {len(tools)} 个 —— 少了说明注册漏了，多了记得同步这里与 README")

        r = await c.call_tool("memory_whoami")
        d = json.loads(r.content[0].text)
        me = d.get("user", {})
        print(f"   身份: {me.get('name')} / {me.get('role')}")
        for lib in d.get("libraries", []):
            print(f"   库: {lib['id']:<16} owner={lib['owner']:<14} "
                  f"可写={'是' if lib['writable'] else '否'}")
        check("是 admin", me.get("is_admin") is True)
        check("有公司库", any(l["id"] == "company" for l in d["libraries"]))

        # ── 2. 写 ──
        print("\n2. 写入（默认落个人库）")
        r = await c.call_tool("memory_write", {
            "title": "E2E 验证条目",
            "body": "这是一次端到端验证写入。用来确认 写→提交→搜 闭环。",
            "tags": ["e2e"],
        })
        d = json.loads(r.content[0].text)
        print(f"   ok={d.get('ok')} lib={d.get('_lib')} "
              f"committed={d.get('committed')} pending={d.get('commit_pending')}")
        check("写入成功", d.get("ok") is True)
        check("落进个人库", d.get("_lib", "").startswith("user-"))
        check("明确告知 pending（不是失败）", d.get("commit_pending") is True)

        # ── 3. 提交 ──
        print("\n3. 提交")
        r = await c.call_tool("memory_commit")
        d = json.loads(r.content[0].text)
        print(f"   committed_count={d.get('committed_count')}/{d.get('lib_count')}")
        check("提交成功", d.get("committed_count", 0) >= 1)

        # ── 4. 搜索 ──
        print("\n4. 搜索（跨可读库）")
        r = await c.call_tool("memory_search", {"query": "端到端验证"})
        d = json.loads(r.content[0].text)
        print(f"   total={d.get('total')}，命中库: "
              f"{ {x.get('_lib_label') for x in d.get('results', [])} }")
        check("搜到了", d.get("total", 0) >= 1)
        check("结果标了来源库", all("_lib_label" in x for x in d["results"]))

        # ── 5. 公司库只读保护 ──
        # ⚠️ 注意：admin **能写**公司库是**设计如此**（管理场景需要）。
        # 只读保护针对的是 member。这里用 admin 身份测，所以预期"允许"。
        # 要验证只读保护，得用 member 身份 —— 见下方第 6 步的越权测试。
        print("\n5. 公司库写入（admin 应允许 —— 设计如此）")
        r = await c.call_tool("memory_write", {
            "title": "E2E 公司库测试", "body": "admin 写的，应该成功。",
            "lib": "company", "tags": ["e2e"],
        })
        d = json.loads(r.content[0].text)
        print(f"   ok={d.get('ok')} lib={d.get('_lib')}")
        check("admin 可写公司库（设计如此）", d.get("ok") is True)

        # 真正验证只读保护：member 身份
        print("\n5b. 只读保护（member 不能写公司库）")
        # 没有 member token 时跳过（不能为测试去建用户 —— 那会改生产数据）
        member_tok = os.environ.get("SHARED_MEMORY_MEMBER_TOKEN", "")
        if member_tok:
            async with Client(URL, auth=member_tok, timeout=30) as c2:
                r = await c2.call_tool("memory_write", {
                    "title": "member 试图写公司库", "body": "应被拒", "lib": "company",
                })
                d2 = json.loads(r.content[0].text)
                print(f"   ok={d2.get('ok')} code={d2.get('code')}")
                check("member 被拒（FORBIDDEN）",
                      d2.get("ok") is False and d2.get("code") == "FORBIDDEN")
                check("错误说明是只读", "只读" in str(d2.get("message", "")))
        else:
            print("   （未提供 member token，跳过 —— 单测里已覆盖这条）")

        # ── 6. 越权 ──
        print("\n6. 越权防护")
        r = await c.call_tool("memory_search", {"query": "x", "lib": "user-nobody"})
        d = json.loads(r.content[0].text)
        print(f"   访问他人个人库: ok={d.get('ok')} code={d.get('code')}")
        check("被拒绝", d.get("ok") is False)

        # ── 7. 视图 ──
        print("\n7. 体系视图")
        for kind in ("overview", "lessons", "decisions", "expiring"):
            r = await c.call_tool("memory_view", {"kind": kind})
            d = json.loads(r.content[0].text)
            print(f"   {kind:<12} total={d.get('total')}")
            check(f"{kind} 视图可用", d.get("ok") is True)

        # ── 8. git 历史 ──
        print("\n8. git 历史（作者）")
        r = await c.call_tool("memory_stats", {"lib": d.get("_lib", "user-admin")})
        d = json.loads(r.content[0].text)
        commits = d.get("commits", [])[:3]
        for cm in commits:
            print(f"   {cm.get('commit')} {cm.get('author'):<12} {cm.get('subject', '')[:40]}")
        check("有提交记录", len(commits) > 0)
        check("提交带作者字段", all("author" in c2 for c2 in commits))

    print(f"\n{'═' * 62}")
    print("  结果: " + ("全部通过 ✓" if ok else "有失败 ✗"))
    print("═" * 62)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
