#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""共享记忆 · 一条命令接入（v2 · S-5.2）。

    # 全局接入（写 ~/.workbuddy/mcp.json）
    python scripts/connect.py "sm://192.168.3.35:8200?k=你的密钥"

    # 只接某个项目（写 <项目>/.mcp.json）
    python scripts/connect.py "sm://192.168.3.35:8200?k=密钥" --project-dir .

    # 先看一眼要写什么（不落盘）
    python scripts/connect.py "sm://192.168.3.35:8200?k=密钥" --dry-run

    # 不用接入码，直接给地址与 token
    python scripts/connect.py --url http://192.168.3.35:8200 --token XXX

━━━ 三条设计约束 ━━━

1. **幂等**：已经配好 `shared-memory` 条目就跳过，不重复写。
   重复跑不会把文件写乱（这是脚本最容易出事的地方）。
2. **不覆盖别人的内容**：只动 `mcpServers["shared-memory"]` 这一个键；
   同文件里其它服务原样保留（缩进、键序都不动结构）。
3. **解析用服务端同一份代码**（``shared_memory.config.parse_connect_uri``）——
   接入码的语法只有一处定义，不存在"脚本认、服务不认"的漂移。

⚠️ token 是凭据：写进文件后请勿把该文件提交进版本库
（``.mcp.json`` 默认加进 .gitignore）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 允许从仓库根直接跑（scripts/ 的上一级有 src/）
_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE / "src"))

# Windows 控制台默认 cp936，吐不出 ✓ 与 ⚠️ —— 直接 UnicodeEncodeError 崩掉，
# 用户看到的是"接入失败"而不是成功。输出编码由脚本定，不由调用方 locale 定。
# 被重定向时 stdout 未必是 TextIOWrapper（pytest 捕获就是），拿不到就跳过。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from shared_memory.config import parse_connect_uri  # noqa: E402
from shared_memory.errors import ToolError  # noqa: E402

SERVER_NAME = "shared-memory"
CLIENT_TYPES = {
    # workbuddy / 大多数支持 streamableHttp 的客户端
    "workbuddy": "streamableHttp",
    "generic": "streamableHttp",
}


def build_entry(mcp_url: str, token: str, client: str) -> dict:
    """生成一条 MCP 服务端条目（与面板「接入」片段语法一致）。"""
    return {
        "type": CLIENT_TYPES.get(client, "streamableHttp"),
        "url": mcp_url,
        "headers": {"Authorization": f"Bearer {token}"},
        "timeout": 240000,
    }


def default_config_path(project_dir: str) -> Path:
    """目标配置文件：给了项目目录 → 项目级；否则 → 全局。"""
    if project_dir:
        return Path(project_dir).expanduser().resolve() / ".mcp.json"
    return Path.home() / ".workbuddy" / "mcp.json"


def merge_config(existing_text: str, entry: dict, *, force: bool = False) -> tuple[str, str]:
    """把条目合并进已有配置文本。返回 ``(新文本, 状态)``。

    状态：``added`` / ``exists``（已配过，跳过）/ ``replaced``（--force）。
    """
    if existing_text.strip():
        try:
            data = json.loads(existing_text)
        except ValueError as exc:
            raise SystemExit(
                f"✗ 目标文件不是合法 JSON（{exc}）。\n"
                "  请先修好它，或换一个路径（--config）。脚本不会擅自覆盖坏文件。"
            )
        if not isinstance(data, dict):
            raise SystemExit("✗ 目标文件的顶层不是对象（应为 {mcpServers: {...}}）。")
    else:
        data = {}
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise SystemExit("✗ 目标文件里的 mcpServers 不是对象。")

    if SERVER_NAME in servers and not force:
        return existing_text, "exists"
    status = "replaced" if SERVER_NAME in servers else "added"
    servers[SERVER_NAME] = entry
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n", status


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="shared-memory-connect",
        description="把本机接上共享记忆服务（解析 sm:// 接入码）",
    )
    ap.add_argument("code", nargs="?", help="接入码：sm://<host>:<port>?k=<密钥>")
    ap.add_argument("--url", default="", help="不用接入码时直接给服务地址")
    ap.add_argument("--token", default="", help="配合 --url 使用的 token")
    ap.add_argument("--project-dir", default="", help="只接这个项目目录（写 .mcp.json）")
    ap.add_argument("--config", default="", help="直接指定要写的配置文件路径")
    ap.add_argument("--client", default="workbuddy", choices=sorted(CLIENT_TYPES),
                    help="客户端类型（决定生成的形态）")
    ap.add_argument("--force", action="store_true", help="已存在也覆盖")
    ap.add_argument("--dry-run", action="store_true", help="只打印将写入的内容")
    args = ap.parse_args(argv)

    # 解析连接参数（接入码优先；否则 url+token）
    if args.code:
        try:
            conn = parse_connect_uri(args.code)
        except ToolError as exc:
            print(f"✗ {exc.message}")
            hint = getattr(exc, "hint", "")
            if hint:
                print(f"  {hint}")
            return 2
        mcp_url, token = conn["mcp_url"], conn["token"]
    elif args.url and args.token:
        mcp_url = args.url.rstrip("/") + "/mcp"
        token = args.token
    else:
        ap.print_help()
        print("\n✗ 需要「接入码」或「--url + --token」二选一。")
        return 2

    target = Path(args.config) if args.config else default_config_path(args.project_dir)
    entry = build_entry(mcp_url, token, args.client)
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    new_text, status = merge_config(existing, entry, force=args.force)

    if args.dry_run:
        print(f"（dry-run）将写入 {target}（状态：{status}）\n")
        print(new_text)
        return 0

    if status == "exists":
        print(f"✓ 已配置过（{SERVER_NAME} 条目已存在），未改动：{target}")
        print("  要覆盖请加 --force。")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(new_text, encoding="utf-8")
    print(f"✓ 已接入（{status}）：{target}")
    print(f"  服务：{mcp_url}")
    print("  ⚠️ 该文件含凭据，别提交进版本库。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
