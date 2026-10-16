"""工具 schema 契约 —— 钉住"一个工具都不许带 outputSchema/anyOf/oneOf"。

━━━ 这个文件存在的理由 ━━━

2026-10-13 产品走查时实锤：fastmcp 4.1.0 会给**每个返回 dict 的工具**
自动生成 ``outputSchema``。而严格模型（GLM-5.3-Flash / Deepseek-V4.1-Flash /
Step-5-Preview）逐字校验每个工具的 schema，见到 ``outputSchema`` 直接拒
**整个请求**（错误码 11129 "The tool definition does not meet the model
requirements"）—— 不是跳过这一个工具，是整个服务对这类模型不可用。

修法：``@mcp.tool(output_schema=None)``。契约按 wire schema 检查，
不绑定 FastMCP 内部实现。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _wire_tools() -> list[dict]:
    """取 MCP 协议层工具定义；这是客户端真正会收到的 tools/list schema。"""
    import asyncio
    import tempfile

    from shared_memory.http_server import build_app
    from shared_memory.tenancy import TenantHub

    async def _list() -> list[dict]:
        with tempfile.TemporaryDirectory() as tmp:
            mcp = build_app(TenantHub(Path(tmp)))
            registered = await mcp.list_tools()
            return [
                tool.to_mcp_tool().model_dump(
                    mode="json", by_alias=True, exclude_none=True
                )
                for tool in registered
            ]

    return asyncio.run(_list())


def test_tool_count_is_nineteen() -> None:
    """工具总数钉在 19 —— 新增/删除需同步 README 与 e2e。

    v2 S-2 新增 ``memory_session_start`` / ``memory_session_end``（会话生命周期）；
    S-4 会把工具面重构到 9 个直觉动词（届时本数字随之更新）。
    """
    tools = _wire_tools()
    assert len(tools) == 19, (
        f"工具数变了：{len(tools)} 个。新增/删除工具都要同步 README 与 e2e.py。"
    )


def test_no_tool_has_output_schema() -> None:
    """严格模型红线：客户端收到的 tools/list 不许带 outputSchema。"""
    tools = _wire_tools()
    offenders = [t["name"] for t in tools if t.get("outputSchema")]
    assert not offenders, (
        f"这些工具带了 outputSchema：{offenders}。"
        "严格模型会因此拒掉整个请求（11129）；装饰器加 output_schema=None。"
    )


def test_no_tool_schema_has_anyof_or_oneof() -> None:
    """严格模型红线：inputSchema 里不许出现 anyOf / oneOf。"""
    def walk(node: object, path: str, out: list[str]) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                here = f"{path}.{key}" if path else key
                if key in ("anyOf", "oneOf"):
                    out.append(here)
                walk(value, here, out)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]", out)

    problems: list[str] = []
    for tool in _wire_tools():
        walk(tool.get("inputSchema") or {}, tool["name"], problems)
    assert not problems, f"inputSchema 含组合关键字：{problems}"


def test_identity_is_never_a_tool_parameter() -> None:
    """身份只能从凭据解析，不能让客户端在参数里自报用户名（越权）。"""
    forbidden = {"user", "username", "user_name", "identity", "actor"}
    problems: list[str] = []

    def walk(node: object, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "properties" and isinstance(value, dict):
                    for prop in forbidden & value.keys():
                        problems.append(f"{path}.{prop}")
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]")

    for tool in _wire_tools():
        walk(tool.get("inputSchema") or {}, tool["name"])
    assert not problems, f"身份参数暴露给客户端了：{problems}"
