# -*- coding: utf-8 -*-
"""探测 shared-memory-mcp 的 tools/list 是否含 outputSchema / anyOf。

严格模型（GLM-5.3-Flash、Deepseek-V4.1-Flash、Step-5-Preview）会逐字校验
工具 schema，见到 outputSchema 或 anyOf 直接拒整个请求（错误码 11129 /
"The tool definition does not meet the model requirements"）。

用法：python scripts/probe_tools_list.py
退出码 0 = 干净；1 = 有问题（并打印明细）。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def find_issues(node: object, path: str, out: list) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else key
            if key == "anyOf":
                out.append((here, json.dumps(value, ensure_ascii=False)[:200]))
            if key == "oneOf":
                out.append((here, json.dumps(value, ensure_ascii=False)[:200]))
            find_issues(value, here, out)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            find_issues(item, f"{path}[{i}]", out)


async def main() -> int:
    from shared_memory.http_server import build_app
    from shared_memory.tenancy import TenantHub

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        hub = TenantHub(Path(tmp))
        mcp = build_app(hub)

        from fastmcp import Client

        async with Client(mcp) as client:
            tools = await client.list_tools()

    print(f"工具数：{len(tools)}")
    problems: list = []
    for tool in tools:
        # 用 model_dump 拿协议层真实的 schema（Tool 对象属性名随版本变，
        # 直接摸 .output_schema/.parameters 在客户端侧会 AttributeError）
        d = tool.model_dump(mode="json", exclude_none=True)
        if d.get("outputSchema"):
            problems.append((f"{tool.name}.outputSchema", "存在 outputSchema"))
        found: list = []
        find_issues(d.get("inputSchema") or {}, tool.name, found)
        problems.extend(found)

    if problems:
        print(f"\n发现 {len(problems)} 个问题：")
        for path, detail in problems:
            print(f"  ✗ {path}: {detail}")
        return 1
    print("\n干净：无 outputSchema / anyOf / oneOf / 可空联合类型")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
