"""真机冒烟测试 —— 在**真实进程**里验证服务能起来。

━━━ 为什么必须单独测（而不是只跑 pytest）━━━

单测测的是逻辑；**冒烟测的是"能不能起来"**。这两件事失败的模式不同：

  · 逻辑错 → 测试红
  · **起不来** → 测试全绿，但一启动就崩

而后者最常见的三个原因，全是单测覆盖不到的：
  1. import 循环 / 依赖缺失
  2. ``main()`` 里的启动顺序问题（鉴权自检在监听前）
  3. uvicorn + 自定义 ASGI 的 lifespan 不兼容

所以这个脚本**真的启动服务**，然后打真请求。
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def banner(t: str) -> None:
    print(f"\n{'═' * 64}\n  {t}\n{'═' * 64}")


async def main() -> int:
    ok = True

    # ══════════════ 1. 危险默认：无凭据 + 对外绑定 → 拒绝启动 ══════════════
    banner("1. 危险默认（绑 0.0.0.0 且无凭据，必须拒绝启动）")
    with tempfile.TemporaryDirectory() as td:
        os.environ["SHARED_MEMORY_HOME"] = str(Path(td) / "home")
        os.environ["SHARED_MEMORY_TOKEN"] = ""
        os.environ.pop("SHARED_MEMORY_TOKEN_FILE", None)
        os.environ.pop("SHARED_MEMORY_ALLOW_INSECURE", None)
        os.environ["HOST"] = "0.0.0.0"
        # 新开一个租户中枢（隔离上面单测的状态）
        from shared_memory.auth import Auth
        from shared_memory.tenancy import TenantHub

        hub = TenantHub(Path(td) / "home2")
        auth = Auth(bind_host="0.0.0.0", tokens=[], store=hub.store)
        allowed, reason = auth.preflight()
        if allowed:
            print("  ✗ 竟然允许启动！—— 危险默认没生效")
            ok = False
        else:
            print(f"  ✓ 已拒绝：{reason.strip()[:80]}…")
        await hub.close()

    # ══════════════ 2. 有凭据 → 允许启动 ══════════════
    banner("2. 有 token → 允许启动")
    with tempfile.TemporaryDirectory() as td:
        from shared_memory.auth import Auth, Token
        from shared_memory.tenancy import TenantHub

        hub = TenantHub(Path(td) / "home")
        auth = Auth(bind_host="0.0.0.0", tokens=[Token(token="t", user="admin")],
                    store=hub.store)
        allowed, _ = auth.preflight()
        print("  ✓ 允许启动" if allowed else "  ✗ 被拒绝（不该）")
        ok = ok and allowed
        await hub.close()

    # ══════════════ 3. 首次启动 → 建管理员 + 返回一次明文密码 ══════════════
    banner("3. 首次启动：管理员 + 初始密码")
    with tempfile.TemporaryDirectory() as td:
        from shared_memory.tenancy import TenantHub

        hub = TenantHub(Path(td) / "home")
        boot = hub.bootstrap()
        if not boot:
            print("  ✗ 没有返回初始化信息")
            ok = False
        else:
            user, password = boot["user"], boot["password"]
            print(f"  ✓ 管理员: {user}，密码长度 {len(password)}")
            # 密码不能明文落盘
            on_disk = (Path(td) / "home" / "tenants.json").read_text(encoding="utf-8")
            if password in on_disk:
                print("  ✗ 密码明文写进了配置文件！")
                ok = False
            else:
                print("  ✓ 密码只以哈希存盘")
            # 幂等
            if hub.bootstrap() is not None:
                print("  ✗ 第二次 bootstrap 又建了一次（不幂等）")
                ok = False
            else:
                print("  ✓ 幂等（重复启动不会重建）")
        await hub.close()

    # ══════════════ 4. 真实 HTTP：起服务 + 打请求 ══════════════
    banner("4. 真实 HTTP 冒烟（真起一个服务）")
    with tempfile.TemporaryDirectory() as td:
        os.environ["SHARED_MEMORY_HOME"] = str(Path(td) / "home")
        os.environ["SHARED_MEMORY_TOKEN"] = ""
        os.environ.pop("SHARED_MEMORY_TOKEN_FILE", None)
        os.environ["HOST"] = "127.0.0.1"      # loopback → 免凭据可起
        os.environ["PORT"] = "18299"
        os.environ["SHARED_MEMORY_ALLOW_INSECURE"] = "1"

        import uvicorn

        from shared_memory.auth import Auth
        from shared_memory.dashboard import make_routes
        from shared_memory.http_server import build_app, _build_auth
        from shared_memory.tenancy import TenantHub
        from starlette.applications import Starlette

        hub = TenantHub(Path(td) / "home")
        hub.bootstrap()
        auth = _build_auth("127.0.0.1", hub)
        # 给 admin 发一把 token 供测试
        tok = auth.add_token(user="admin", label="smoke")

        mcp_app = build_app(hub).http_app(path="/mcp")
        panel = Starlette(routes=make_routes(hub, auth=auth))

        async def root(scope, receive, send):
            if scope.get("type") == "lifespan":
                await mcp_app(scope, receive, send)
                return
            if scope.get("type") == "http" and scope.get("path") in ("/mcp", "/mcp/"):
                await mcp_app(scope, receive, send)
                return
            await panel(scope, receive, send)

        from shared_memory.http_server import _wrap_auth
        app = _wrap_auth(root, auth)

        config = uvicorn.Config(app, host="127.0.0.1", port=18299,
                                log_level="warning", lifespan="on")
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve())

        # 等服务起来
        import urllib.request
        for _ in range(60):
            await asyncio.sleep(0.25)
            try:
                urllib.request.urlopen("http://127.0.0.1:18299/favicon.ico",
                                       timeout=1).read()
                break
            except Exception:
                continue

        # 等服务的判定不能只看 started —— lifespan 完成才算真的能响应。
        # 而请求必须用**异步**客户端：在同一个事件循环里用阻塞的
        # urllib 会把循环占住，服务反而没人处理 → 自己锁死自己
        # （冒烟测试第一版就是这么错的，表现为"服务起了但全超时"）。
        ready = False
        # 用 urllib + 线程池而不是 httpx：**不引入新依赖**。
        # httpx 虽然是 fastmcp 的传递依赖，但目标机上没装 ——
        # 冒烟测试的价值就在于"在真实环境里跑"，多一个依赖就多一个失败点。
        import urllib.error
        from concurrent.futures import ThreadPoolExecutor

        pool = ThreadPoolExecutor(max_workers=4)

        def sync_get(path: str, headers: dict | None = None) -> tuple[int, str]:
            """在**线程里**做阻塞请求 —— 不占事件循环。

            关键：在 async 里直接调 urlopen 会把循环占住，
            服务反而没人处理请求 → 自己锁死自己（第一版就这么错的，
            表现为"服务起来了但所有请求超时"）。
            """
            req = urllib.request.Request("http://127.0.0.1:18299" + path,
                                         headers=headers or {})
            try:
                with urllib.request.urlopen(req, timeout=8) as r:
                    return r.status, r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode("utf-8", "replace")
            except Exception as exc:  # noqa: BLE001
                return 0, str(exc)

        async def get(path: str, headers: dict | None = None) -> tuple[int, str]:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(pool, sync_get, path, headers)

        # 探测就绪要带凭据：无凭据探测会记失败，累计多了触发限速（实测踩过）。
        # ⚠️ 判定条件要包含 **204** —— favicon 已豁免鉴权，返回 204。
        # 第一版只判 (200,401,403)，于是服务明明是好的却报"没就绪"。
        for _ in range(60):
            await asyncio.sleep(0.25)
            code, _ = await get("/favicon.ico",
                                {"Authorization": f"Bearer {tok.token}"})
            if code in (200, 204, 401, 403):   # 能响应就算就绪
                ready = True
                break
        if not ready:
            print("  ✗ 服务 15 秒内没就绪")
            ok = False
        else:
            print("  ✓ 服务已就绪")

        # 4a. 无凭据 → 401
        code, _ = await get("/api/whoami")
        print(f"  {'✓' if code == 401 else '✗'} 无凭据 /api/whoami → {code}")
        ok = ok and code == 401

        # 4b. 带 Bearer → 200，且能看到自己是 admin
        code, body = await get("/api/whoami",
                               {"Authorization": f"Bearer {tok.token}"})
        is_admin = '"is_admin": true' in body or '"is_admin":true' in body
        print(f"  {'✓' if code == 200 and is_admin else '✗'} "
              f"Bearer /api/whoami → {code}，is_admin={is_admin}")
        ok = ok and code == 200 and is_admin

        # 4c. MCP 端点无凭据 → 401（这是最初的核心诉求）
        code, _ = await get("/mcp")
        print(f"  {'✓' if code == 401 else '✗'} 无凭据 /mcp → {code}")
        ok = ok and code == 401

        # 4d. MCP 工具列表（真调 MCP）
        try:
            from fastmcp import Client
            async with Client("http://127.0.0.1:18299/mcp", auth=tok.token,
                              timeout=25) as c:
                tools = await c.list_tools()
                names = sorted(t.name for t in tools)
                print(f"  ✓ MCP 工具 {len(tools)} 个")
                print(f"      {', '.join(names)}")
                import json
                r = await c.call_tool("memory_whoami")
                d = json.loads(r.content[0].text)
                print(f"  ✓ memory_whoami → user={d['user']['name']}, "
                      f"role={d['user']['role']}, libs={len(d['libraries'])}")
                r2 = await c.call_tool("memory_write", {
                    "title": "冒烟测试条目", "body": "这是一次冒烟测试。",
                })
                d2 = json.loads(r2.content[0].text)
                print(f"  ✓ memory_write → ok={d2.get('ok')}, "
                      f"lib={d2.get('_lib')}, committed={d2.get('committed')}, "
                      f"pending={d2.get('commit_pending')}")
                r3 = await c.call_tool("memory_commit")
                d3 = json.loads(r3.content[0].text)
                print(f"  ✓ memory_commit → committed_count={d3.get('committed_count')}")
                r4 = await c.call_tool("memory_search", {"query": "冒烟测试"})
                d4 = json.loads(r4.content[0].text)
                print(f"  ✓ memory_search → total={d4.get('total')}")
                ok = ok and bool(d2.get("ok")) and d4.get("total", 0) >= 1
        except Exception as exc:
            print(f"  ✗ MCP 客户端调用失败: {type(exc).__name__}: {exc}")
            ok = False

        # 4e. 静态资源
        code, body = await get("/panel.css",
                               {"Authorization": f"Bearer {tok.token}"})
        has_css = ".wshead" in body
        print(f"  {'✓' if code == 200 and has_css else '✗'} "
              f"/panel.css → {code}，含工作区树样式={has_css}")
        ok = ok and code == 200 and has_css

        code, body = await get("/", {"Authorization": f"Bearer {tok.token}"})
        print(f"  {'✓' if code == 200 and 'panel.css' in body else '✗'} "
              f"面板 HTML → {code}")
        ok = ok and code == 200

        pool.shutdown(wait=True)

    server.should_exit = True
    await task
    await hub.close()

    # ══════════════ 5. git 历史里的作者 ══════════════
    banner("5. git 提交带作者")
    with tempfile.TemporaryDirectory() as td:
        from shared_memory.tenancy import TenantHub

        h = TenantHub(Path(td) / "home")
        h.store.users["bob"] = __import__("shared_memory.tenant", fromlist=["User"]).User(
            name="bob", role="member", groups=["研发"])
        h.store.libs["group-研发"] = __import__("shared_memory.tenant", fromlist=["Library"]).Library(
            id="group-研发", owner="group:研发")
        h.store.save()
        h.fast_commit = True
        await h.write("bob", title="作者测试", body="x", lib_id="group-研发")
        log = h.service_for("group-研发").git_log(5)["commits"]
        authors = {c.get("author") for c in log}
        print(f"  {'✓' if 'bob' in authors else '✗'} git author = {authors}")
        ok = ok and "bob" in authors
        await h.close()

    print(f"\n{'═' * 64}")
    print("  冒烟结果: " + ("全部通过 ✓" if ok else "有失败 ✗"))
    print("═" * 64)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
