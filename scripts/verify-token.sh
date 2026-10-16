#!/bin/bash
# 用新 token 验证 MCP 真能连通（DSH 客户端有缓存，这里直接打）
TOK=$(grep -v '^#' /opt/shared-memory-mcp/.tokens | grep -v '^$' | head -1 | awk -F: '{print $NF}')
echo "token 指纹: ${TOK:0:4}…${TOK: -4}"

cd /opt/shared-memory-mcp
PYTHONPATH=/opt/shared-memory-mcp/src timeout 60 ./venv/bin/python - "$TOK" <<'PYEOF'
import asyncio, json, sys
from fastmcp import Client

TOK = sys.argv[1]

async def main():
    async with Client("http://127.0.0.1:8200/mcp", auth=TOK, timeout=25) as c:
        tools = await c.list_tools()
        print(f"✓ 连通，{len(tools)} 个工具")
        r = await c.call_tool("memory_whoami")
        d = json.loads(r.content[0].text)
        print(f"✓ 身份: {d['user']['name']} / {d['user']['role']}")
        print(f"✓ 库: {[l['id'] for l in d['libraries']]}")

        # 真写一条，确认 DSH 那边重连后能用
        r2 = await c.call_tool("memory_write", {
            "title": "多租户改造上线（2026-10-12）",
            "body": "shared-memory-mcp 完成多租户改造并上线。\n\n"
                    "## 做了什么\n"
                    "1. **强制鉴权**：绑非 loopback 且无凭据 → 拒绝启动（不再是无鉴权裸跑）\n"
                    "2. **多库**：company（全员只读）/ group-xxx（组内读写）/ user-xxx（个人私有）\n"
                    "3. **身份**：token 绑定用户；git 提交带作者（不可抵赖）\n"
                    "4. **跨库搜索**：权限集由服务端算，调用方无法指定 → 结构上不可能越权\n"
                    "5. **提交语义**：committed=false + commit_pending=true，"
                    "避免 agent 误判失败而重试（重试会写出重复内容）\n\n"
                    "## 验证\n"
                    "· 161 个单测全过\n"
                    "· 生产 e2e 全过（写→提交→搜→视图→git 历史）\n"
                    "· 局域网 IP 零凭据审计：23 项 401，零泄漏",
            "tags": ["共享记忆", "部署", "安全"],
        })
        d2 = json.loads(r2.content[0].text)
        print(f"✓ 写入: ok={d2.get('ok')} lib={d2.get('_lib')}")

        r3 = await c.call_tool("memory_commit")
        d3 = json.loads(r3.content[0].text)
        print(f"✓ 提交: {d3.get('committed_count')}/{d3.get('lib_count')} 个库")

asyncio.run(main())
PYEOF
