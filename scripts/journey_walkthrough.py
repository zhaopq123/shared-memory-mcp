# -*- coding: utf-8 -*-
"""新用户全旅程走查：像真实 agent 那样用一遍 shared-memory-mcp。

⚠️ 只对 disposable 测试服务运行。脚本会创建测试记忆和一个工作空间库；
它不会清理（保留输出供走查），因此不要指向生产环境。

用法：
  SHARED_MEMORY_E2E_URL=http://127.0.0.1:8201/mcp \
  SHARED_MEMORY_E2E_TOKEN=<测试 token> python scripts/journey_walkthrough.py
"""
import asyncio
import json
import os
from datetime import datetime
from urllib.request import ProxyHandler, Request, build_opener

from fastmcp import Client

URL = os.environ.get("SHARED_MEMORY_E2E_URL", "http://127.0.0.1:8201/mcp")
TOKEN = os.environ.get("SHARED_MEMORY_E2E_TOKEN", "").strip()


def show(label: str, data) -> None:
    text = json.dumps(data, ensure_ascii=False, default=str)
    if len(text) > 400:
        text = text[:400] + " …"
    print(f"  {label}: {text}")


def assert_ok(label: str, data) -> None:
    if not isinstance(data, dict) or not data.get("ok"):
        raise AssertionError(f"{label} 失败：{data!r}")


async def main() -> None:
    if not TOKEN:
        raise SystemExit(
            "缺少 SHARED_MEMORY_E2E_TOKEN。不要把 token 写进脚本；"
            "请从 disposable 测试服务的启动输出/测试凭据文件中读取后通过环境变量传入。"
        )
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    workspace_tag = f"walkthrough-{run_id}"
    title_fact = f"走查-{run_id}-部署必须先装包"
    title_inference = f"走查-{run_id}-测试时间观察"

    async with Client(URL, auth=TOKEN, timeout=30) as c:
        print("\n── 1. 开工先调 whoami ──")
        r = await c.call_tool("memory_whoami")
        assert_ok("whoami", r.data)
        show("whoami", r.data)
        own_lib = next(
            (lib["id"] for lib in r.data.get("libraries", [])
             if lib.get("kind") == "user" and lib.get("writable")),
            "",
        )

        print("\n── 2. 写事实（测试专用工作空间标签） ──")
        r = await c.call_tool("memory_write", {
            "title": title_fact,
            "body": "README 快速开始要求 pip install -e .；没安装直接 -m 启动会 ModuleNotFoundError。",
            "tags": ["走查", "部署"],
            "workspace": workspace_tag,
            "claim": "fact",
        })
        assert_ok("write fact", r.data)
        show("write", r.data)

        print("\n── 3. 写推断（服务端应保留 claim 区分） ──")
        r = await c.call_tool("memory_write", {
            "title": title_inference,
            "body": "观察到测试执行耗时较长；具体瓶颈仍需 profile 验证。",
            "tags": ["走查", "性能"],
            "workspace": workspace_tag,
            "claim": "inference",
        })
        assert_ok("write inference", r.data)
        show("write2", r.data)

        print("\n── 4. 按关键词检索 ──")
        r = await c.call_tool("memory_search", {"query": run_id})
        assert_ok("search", r.data)
        assert r.data.get("total", 0) >= 2, "检索没找到刚写的两条记忆"
        show("search", r.data)

        print("\n── 5. 读全文（ident 可传标题或 id） ──")
        r = await c.call_tool("memory_get", {"ident": title_fact})
        assert_ok("get", r.data)
        show("get", r.data)

        print("\n── 6. 看结构视图 ──")
        for kind in ("overview", "inferences", "expiring"):
            r = await c.call_tool("memory_view", {"kind": kind})
            assert_ok(f"view({kind})", r.data)
            show(f"view({kind})", r.data)

        print("\n── 7. 越权/不存在库：返回可操作错误而不是泄露数据 ──")
        r = await c.call_tool("memory_search", {"query": "x", "lib": "user-nobody"})
        assert r.data.get("ok") is False, "不存在的库应拒绝"
        show("unknown-lib", r.data)

        print("\n── 8. create 撞名：给出冲突提示，不覆盖原文 ──")
        r = await c.call_tool("memory_write", {
            "title": title_fact, "body": "重复创建试试", "mode": "create",
        })
        assert r.data.get("code") == "CONFLICT", f"撞名应返回 CONFLICT：{r.data}"
        show("create-conflict", r.data)

        print("\n── 9. 建独立工作空间库（请只在 disposable 服务跑） ──")
        r = await c.call_tool("memory_workspace_create", {
            "workspace": f"走查-{run_id}", "label": f"走查 {run_id}",
        })
        assert_ok("workspace_create", r.data)
        workspace_lib = r.data["lib"]
        show("ws-create", r.data)
        r = await c.call_tool("memory_workspaces")
        assert_ok("workspaces", r.data)
        show("ws-list", r.data)

        print("\n── 10. 汇总健康状况 ──")
        r = await c.call_tool("memory_stats")
        assert_ok("stats", r.data)
        show("stats", r.data)

        print("\n── 11. 导出/导入：workspace 是条目项目标签，lib 才是库 id ──")
        r = await c.call_tool("memory_export", {"workspace": workspace_tag})
        assert_ok("export", r.data)
        assert r.data.get("count", 0) >= 2, f"bundle 应含两条，实际：{r.data}"
        show("export", r.data)
        r = await c.call_tool("memory_import", {
            "bundle": r.data["bundle"],
            "target_lib": workspace_lib,
            "workspace": workspace_tag,
            "on_conflict": "skip",
        })
        assert_ok("import", r.data)
        assert r.data.get("imported") == 2, f"空目标库应导入两条记忆：{r.data}"
        show("import", r.data)
        r = await c.call_tool("memory_stats", {"lib": workspace_lib})
        assert_ok("workspace stats", r.data)
        assert r.data.get("notes") == 2, f"导入后目标库应有两条记忆：{r.data}"
        show("workspace-stats", r.data)

        print("\n── 12. HTTP 健康探针（loopback 无凭据应放行） ──")
        health_url = URL.rsplit("/mcp", 1)[0] + "/health"
        request = Request(health_url, headers={"Authorization": f"Bearer {TOKEN}"})

        def fetch_health() -> dict:
            # 明确禁用系统代理：本机验证不能让 HTTP_PROXY 把请求转到外部代理。
            opener = build_opener(ProxyHandler({}))
            with opener.open(request, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))

        health = await asyncio.to_thread(fetch_health)
        assert_ok("health", health)
        assert "home" not in health, "health 不应泄露服务端绝对路径"
        show("health", health)

    print(f"\n全旅程通过。测试标签={workspace_tag}；测试库={workspace_lib}。")
    print("本脚本不会清理测试记忆/库；请只在 disposable 测试服务运行。")


if __name__ == "__main__":
    asyncio.run(main())
