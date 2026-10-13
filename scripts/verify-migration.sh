#!/bin/bash
# 迁移后验证 + git 提交
cd /opt/shared-memory-mcp

echo "════ 1. 各库现状 ════"
for d in tenant-home/libs/*/; do
    [ -d "$d" ] || continue
    name=$(basename "$d")
    n=$(ls "$d/notes" 2>/dev/null | wc -l)
    t=$(ls "$d/.trash" 2>/dev/null | wc -l)
    echo "  $name: $n 条（回收站 $t）"
done

echo
echo "════ 2. user-admin 库的内容（应只有真实记忆）════"
ls tenant-home/libs/user-admin/notes/ 2>/dev/null | head -12

echo
echo "════ 3. 测试库登记是否已移除 ════"
python3 -c "
import json
d = json.load(open('tenant-home/tenants.json'))
print('  已登记的库:', list(d.get('libs', {}).keys()))
print('  用户:', list(d.get('users', {}).keys()))
"

echo
echo "════ 4. git 提交各库改动 ════"
for d in tenant-home/libs/*/; do
    [ -d "$d/.git" ] || continue
    name=$(basename "$d")
    cd "$d"
    git add -A 2>/dev/null
    if git diff --cached --quiet 2>/dev/null; then
        echo "  $name: 无改动"
    else
        git -c user.name=admin -c user.email=admin@shared-memory.local \
            commit -q -m "chore: 迁移真实记忆 + 清理压测数据" 2>&1 | head -2
        echo "  $name: 已提交 $(git rev-parse --short HEAD)"
    fi
    cd /opt/shared-memory-mcp
done

echo
echo "════ 5. 通过 MCP 验证（服务能读到新数据吗）════"
PYTHONPATH=/opt/shared-memory-mcp/src ./venv/bin/python - <<'PYEOF'
import asyncio, json
from fastmcp import Client

TOK = open("/opt/shared-memory-mcp/.tokens").read().splitlines()
TOK = [l for l in TOK if l.strip() and not l.startswith("#")][0].split(":")[-1]

async def main():
    async with Client("http://127.0.0.1:8200/mcp", auth=TOK, timeout=25) as c:
        r = await c.call_tool("memory_libs")
        d = json.loads(r.content[0].text)
        print("  库清单:")
        for lib in d.get("libraries", []):
            print(f"    {lib['lib']:<20} {lib['notes']} 条  ({lib['label']})")
        r2 = await c.call_tool("memory_search", {"query": ""})
        d2 = json.loads(r2.content[0].text)
        print(f"\n  跨库搜索（空查询）→ {d2.get('total')} 条")
        for n in d2.get("results", [])[:6]:
            print(f"    [{n.get('_lib_label')}] {n['title'][:44]}")

asyncio.run(main())
PYEOF
