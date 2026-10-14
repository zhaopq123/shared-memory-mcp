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
        home_lib = d.get("_lib", "")           # 默认落库，后面 stats 用它
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

        # 真正验证只读保护：只读凭据（member 或 scope=read）
        print("\n5b. 只读凭据：能读、不能写")
        # 没有只读 token 时跳过（不能为测试去建用户 —— 那会改生产数据）
        member_tok = os.environ.get("SHARED_MEMORY_MEMBER_TOKEN", "")
        if member_tok:
            async with Client(URL, auth=member_tok, timeout=30) as c2:
                r = await c2.call_tool("memory_search", {"query": "E2E", "limit": 3})
                d2r = json.loads(r.content[0].text)
                check("只读凭据：读放行", d2r.get("ok") is True)
                # 写被拒：fastmcp 客户端可能抛 ToolError，也可能返回 ok=false —— 两种都算"被拒"
                blocked, msg = False, ""
                try:
                    r = await c2.call_tool("memory_write", {
                        "title": "只读凭据试图写公司库", "body": "应被拒", "lib": "company",
                    })
                    d2 = json.loads(r.content[0].text)
                    blocked, msg = (d2.get("ok") is False), str(d2.get("message", ""))
                    print(f"   write: ok={d2.get('ok')} code={d2.get('code')}")
                except Exception as exc:  # ToolError
                    blocked, msg = True, str(exc)
                    print(f"   write: 被拒（{type(exc).__name__}）")
                check("只读凭据：写被拒", blocked)
                check("错误说明是只读", "只读" in msg)
        else:
            print("   （未提供只读 token，跳过 —— 单测里已覆盖这条）")

        # ── 6. 越权 ──
        print("\n6. 越权防护")
        denied, det = False, ""
        try:
            r = await c.call_tool("memory_search", {"query": "x", "lib": "user-nobody"})
            d = json.loads(r.content[0].text)
            denied, det = (d.get("ok") is False), f"ok={d.get('ok')} code={d.get('code')}"
        except Exception as exc:  # 客户端抛错也是"被拒绝"的一种表现
            denied, det = True, f"{type(exc).__name__}"
        print(f"   访问他人个人库: {det}")
        check("被拒绝", denied)

        # ── 7. 视图 ──
        print("\n7. 体系视图")
        for kind in ("overview", "lessons", "decisions", "expiring"):
            r = await c.call_tool("memory_view", {"kind": kind})
            d = json.loads(r.content[0].text)
            print(f"   {kind:<12} total={d.get('total')}")
            check(f"{kind} 视图可用", d.get("ok") is True)

        # ── 8. git 历史 ──
        print("\n8. git 历史（作者）")
        r = await c.call_tool("memory_stats", {
            "lib": home_lib or "user-admin", "with_commits": True,
        })
        d = json.loads(r.content[0].text)
        commits = d.get("commits", [])[:3]
        for cm in commits:
            print(f"   {cm.get('commit')} {str(cm.get('author') or '-'):<12} {cm.get('subject', '')[:40]}")
        check("有提交记录", len(commits) > 0)
        check("提交带作者字段", all("author" in c2 for c2 in commits))

        def _rows(d: dict, *keys: str) -> list:
            """从响应里取列表字段（不同工具的键名不完全一致）。"""
            for k in keys:
                v = d.get(k)
                if isinstance(v, list):
                    return v
            return []

        me_name = me.get("name", "admin")

        # ── 9. 工作空间全生命周期（自检沙盘，可重复跑）──
        print("\n9. 工作空间 = 库：建库 → 写 → 读 → 引用 → 共享往返")
        ws_name = "e2e自检"
        ws_lib = "ws-" + ws_name
        try:
            r = await c.call_tool("memory_workspace_create", {"workspace": ws_name})
            d = json.loads(r.content[0].text)
            print(f"   create: ok={d.get('ok')} lib={d.get('lib')}")
        except Exception as exc:  # 已存在（重复跑）属正常
            print(f"   create: 已存在，复用沙盘库（{str(exc)[:60]}）")
        # 以"库在册"为准
        r = await c.call_tool("memory_libs")
        d = json.loads(r.content[0].text)
        lib_ids = [l.get("lib") or l.get("id") for l in _rows(d, "libraries", "libs", "results")]
        check("沙盘库在册（ws-e2e自检）", ws_lib in lib_ids,
              f"共 {len(lib_ids)} 库: {', '.join(x for x in lib_ids if x)}")

        r = await c.call_tool("memory_write", {
            "title": "E2E 沙盘·被引用条目",
            "body": "本条被 [[E2E 沙盘·引用者]] 引用，用于验证引用关系。",
            "lib": ws_lib, "workspace": ws_name, "tags": ["e2e"],
        })
        d = json.loads(r.content[0].text)
        check("沙盘写入（被引用条目）", d.get("ok") is True, f"lib={d.get('_lib')}")
        r = await c.call_tool("memory_write", {
            "title": "E2E 沙盘·引用者",
            "body": "本条引用 [[E2E 沙盘·被引用条目]]，用于验证 backlinks。",
            "lib": ws_lib, "workspace": ws_name, "tags": ["e2e"],
        })
        d = json.loads(r.content[0].text)
        check("沙盘写入（引用者）", d.get("ok") is True)

        r = await c.call_tool("memory_get", {"ident": "E2E 沙盘·被引用条目", "lib": ws_lib})
        d = json.loads(r.content[0].text)
        check("memory_get 按标题读全文",
              d.get("ok") is True and "被引用" in json.dumps(d, ensure_ascii=False))

        r = await c.call_tool("memory_list", {"lib": ws_lib, "tag": "e2e", "limit": 20})
        d = json.loads(r.content[0].text)
        titles = [x.get("title") for x in _rows(d, "results", "notes", "items")]
        check("memory_list 按 tag 过滤", "E2E 沙盘·引用者" in titles)

        r = await c.call_tool("memory_backlinks", {"title": "E2E 沙盘·被引用条目", "lib": ws_lib})
        d = json.loads(r.content[0].text)
        check("backlinks 找到引用者",
              d.get("ok") is True and "引用者" in json.dumps(d, ensure_ascii=False))

        r = await c.call_tool("memory_search", {"query": "沙盘", "lib": ws_lib, "limit": 5})
        d = json.loads(r.content[0].text)
        check("沙盘内可搜到", d.get("total", 0) >= 2)
        r = await c.call_tool("memory_search", {"query": "沙盘", "workspace": ws_name, "limit": 5})
        d = json.loads(r.content[0].text)
        check("按 workspace 过滤可搜到", d.get("total", 0) >= 2)

        r = await c.call_tool("memory_workspace_share", {"lib": ws_lib, "share": "company"})
        d = json.loads(r.content[0].text)
        check("share：仅自己 → 公司", d.get("ok") is True)
        r = await c.call_tool("memory_workspace_share", {"lib": ws_lib, "share": f"user:{me_name}"})
        d = json.loads(r.content[0].text)
        check("share：公司 → 收回（仅自己）", d.get("ok") is True)

        r = await c.call_tool("memory_workspaces")
        d = json.loads(r.content[0].text)
        ws_rows = _rows(d, "libraries", "workspaces", "results", "items")
        row = [w for w in ws_rows if (w.get("lib") or w.get("id")) == ws_lib]
        check("memory_workspaces 列出沙盘", len(row) >= 1,
              (row[0].get("share_label", "") if row else ""))

        # ── 10. 回收站：删除 → 恢复 ──
        print("\n10. 回收站往返")
        r = await c.call_tool("memory_delete", {"ident": "E2E 沙盘·引用者", "confirm": True, "lib": ws_lib})
        d = json.loads(r.content[0].text)
        moved = d.get("moved_to", "")
        print(f"   moved_to={moved}")
        check("删除进回收站", d.get("action") == "deleted")
        fname = moved.split("/")[-1] if moved else ""
        restored = False
        for cand in (fname, moved):
            if not cand:
                continue
            try:
                r = await c.call_tool("memory_restore", {"filename": cand, "lib": ws_lib})
                d = json.loads(r.content[0].text)
                restored = d.get("ok") is True
            except Exception:
                restored = False
            if restored:
                break
        check("从回收站恢复", restored)

        # ── 11. 导出 / 导入（幂等）──
        print("\n11. 导出 / 导入")
        r = await c.call_tool("memory_export", {"lib": ws_lib})
        d = json.loads(r.content[0].text)
        bundle = d.get("bundle") or d.get("out_dir") or d.get("path") or ""
        print(f"   bundle={bundle}  raw={json.dumps(d, ensure_ascii=False)[:160]}")
        check("导出成功", d.get("ok") is True and bool(bundle))
        if bundle:
            r = await c.call_tool("memory_import", {
                "bundle": bundle, "target_lib": ws_lib, "on_conflict": "skip",
            })
            d = json.loads(r.content[0].text)
            print(f"   import raw={json.dumps(d, ensure_ascii=False)[:200]}")
            check("重复导入不崩（skip 幂等）", d.get("ok") is True)

    print(f"\n{'═' * 62}")
    print("  结果: " + ("全部通过 ✓" if ok else "有失败 ✗"))
    print("  说明: 自检沙盘库 ws-e2e自检 及其中 2 条固定样例保留（重复跑不增长）")
    print("═" * 62)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
