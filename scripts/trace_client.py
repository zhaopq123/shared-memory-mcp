# -*- coding: utf-8 -*-
"""复现 fastmcp Client(URL) 404 问题，并打印每个请求的 URL 形态。"""
import asyncio
import os
import httpx2

_orig_build = httpx2.AsyncClient.build_request
_orig_send = httpx2.AsyncClient.send


def build_request(self, method, url, **kw):
    req = _orig_build(self, method, url, **kw)
    print(f"[build] {method} url={url!r} -> req.url={req.url!r} base={self.base_url!r}")
    return req


async def send(self, request, **kw):
    r = await _orig_send(self, request, **kw)
    print(f"[send ] {request.method} {str(request.url)!r} -> {r.status_code}")
    return r


httpx2.AsyncClient.build_request = build_request
httpx2.AsyncClient.send = send

from fastmcp import Client

URL = os.environ.get("SHARED_MEMORY_E2E_URL", "http://127.0.0.1:8201/mcp")
TOKEN = os.environ.get("SHARED_MEMORY_E2E_TOKEN", "").strip()


async def main():
    if not TOKEN:
        raise SystemExit("请通过 SHARED_MEMORY_E2E_TOKEN 环境变量提供测试 token")
    try:
        async with Client(URL, auth=TOKEN) as c:
            tools = await c.list_tools()
            print(f"OK {len(tools)} tools")
    except Exception as e:
        print(f"FAIL {type(e).__name__}: {str(e)[:120]}")


asyncio.run(main())
