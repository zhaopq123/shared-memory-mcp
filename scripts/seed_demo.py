"""给演示库塞几条带层级工作区的记忆，用来验证工作区树。

层级来自工作区名里的 ``-``：
    ``乱七八糟-前端`` 归在 ``乱七八糟`` 之下
    ``个人工作中枢-客户`` 归在 ``个人工作中枢`` 之下
    空工作区 → 归到"未分类"桶

跑法（容器内）：``./venv/bin/python seed_demo.py``
"""

import asyncio
import json

from fastmcp import Client

TOKEN = "demo-token-for-screenshots"
URL = "http://127.0.0.1:8300/mcp"

NOTES = [
    ("乱七八糟-前端", "前端构建产物清理",
     "node_modules 占空间最大，定期清理；lock 文件要提交。"),
    ("乱七八糟-前端", "前端打包体积优化",
     "按需引入 + 分包，首屏从 1.2M 降到 480K。实测有效。"),
    ("乱七八糟-后端", "后端接口约定",
     "统一 POST + JSON，错误码放 code 字段而不是 HTTP 状态。"),
    ("乱七八糟", "乱七八糟工作区总览",
     "这个工作区下分了前端和后端两个子域。"),
    ("个人工作中枢-客户", "客户跟进节奏",
     "首次接触后 48 小时内必须二次触达，否则转化率掉一半。"),
    ("个人工作中枢-客户", "客户档案规范",
     "每条档案必须带：全称、对接人、当前阶段。缺一项视为未完成。"),
    ("个人工作中枢", "个人工作中枢总览",
     "客户、财务、生活三大块；每块下的笔记用 ws/ 标签归拢。"),
    ("个人工作中枢-财务", "报销流程口径",
     "发票需在当月提交，跨月要书面说明；超过 3 个月不再受理。"),
    ("", "环境准备清单",
     "跑这个项目前要装的东西 —— 这条没有工作区，属于跨项目的基础设施笔记。"),
]


async def main() -> None:
    async with Client(URL, auth=TOKEN, timeout=30) as c:
        for ws, title, body in NOTES:
            r = await c.call_tool("memory_write", {
                "title": title, "body": body,
                "workspace": ws, "tags": ["演示"],
            })
            d = json.loads(r.content[0].text)
            mark = "OK  " if d.get("ok") else "FAIL"
            print(f"  {mark} ws={(ws or '(无)'):<16} {title}")

        st = await c.call_tool("memory_stats", {})
        sd = json.loads(st.content[0].text)
        print()
        print("工作区分布（用于验证树形分组）:")
        for name, n in sd.get("workspaces", []):
            print(f"  {name:<26} {n}")


if __name__ == "__main__":
    asyncio.run(main())
