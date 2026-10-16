# 客户端自动存取 hook 规范（v2 · S-12.3）

> 面向：**客户端**（agent 宿主）的 hook / 自动化机制。服务端代码在 `src/shared_memory/digest.py`。
> 生成于 2026-10-16。

## 0. 先说诚实边界（决定了这份文档为什么存在）

MCP 是 **client-pull** 协议：服务端**不能主动推送**给客户端。所以：

| | 能不能做到 | 靠什么 |
|---|---|---|
| **自动获取（读）** | ✅ **100% 不依赖 agent 自觉** | 握手时服务端把三段上下文注入 `instructions`（见下） |
| **自动存入（写）** | ⚠️ **无法纯服务端强制** | ① instructions 行为契约（软约束）② 写入类型推断（服务端补元数据）③ **客户端 hook**（本文件） |

**不承诺**：server 主动把记忆推给"不在会话中"的 agent。任何"自动写"都必须由客户端在某个时刻
**发起一次工具调用**。谁发起、什么时候发起，只有客户端知道 —— 这就是 hook 的位置。

## 1. 服务端已经做的（hook 不要重复做这些）

`initialize` 响应的 `instructions` 末尾由 `digest.build_suffix()` 追加三段：

1. **长期摘要** —— 自上次连接以来库里变了什么（含刚进入 review/expired 的）。没变就是空段。
2. **硬约束清单** —— `type=constraint` 的条目，**每次握手必达**（首次连接、零变更也算）。
   硬约束不该靠"想起来去搜"。
3. **当前任务上下文包** —— `type=workflow` 置顶 + 「你正在做 X，相关记忆 N 条」。

`memory_session_start(task=...)` 另返回结构化三段（`constraints` / `task_notes` / `space_brief`）。

所以 hook 的职责**不是**"把记忆塞给 agent"（读侧已经自动了），而是这三件事：
**登记会话**、**在丢上下文之前把结论落盘**、**收工归档**。

## 2. 事件契约（中立命名 → 各家映射）

| 事件 | 何时触发 | 期望动作（工具调用） | 必要性 |
|---|---|---|---|
| `sessionStart` | 会话建立、agent 拿到任务名之后 | `memory_session_start(task, space, project)` | **强烈建议**（不登记=过程态无处可归，且检索范围不收窄→串台） |
| `preCompact` | 客户端要压缩/摘要上下文**之前** | 让 agent 把本会话结论 `memory_remember(scope="task")` 后再压 | **最高价值**：压缩是有损的，这是唯一来得及的时机 |
| `stop` / `sessionEnd` | 一轮回答结束 / 会话关闭 | 自查"有没有该沉淀的结论"→ 有就写；然后 `memory_session_end(task, outcome, summary)` | 建议（`session_end` 不做=任务过程态永久留在默认检索范围里，是库越来越乱的主因） |
| `postToolUse`（写类工具后） | 每次 `memory_remember` 返回后 | 只记录 `ok=false` 的**非重试类**错误到日志 | 可选（观测用） |

**没有 `push` 这种事件**，也不许伪造：任何"服务端定时把记忆发给 agent"的想法在 MCP 上做不到。

## 3. 负载字段约定

客户端 hook 的 payload 形状由**客户端**决定（各家不同），本规范只约定我们读取的字段与缺失时的降级：

```json
{
  "hook_event_name": "sessionStart",     // 或缺省，由脚本自己按挂在哪判断
  "session_id": "abc123",                 // 客户端会话 id（≠ 服务端 session_id）
  "task": "共享记忆v2开发",                 // 缺失 → 用工作区目录名兜底，或跳过登记
  "project": "/path/to/workspace",        // 归属标识，只登记不写盘
  "workspace": "乱七八糟",                 // 与 .mcp.json 的 _belonging.workspace 同值
  "space": "ws-乱七八糟",                   // 库 id；缺失 → 不传 lib（落个人库，最安全）
  "transcript_path": "/…/sess.jsonl",     // 仅兜底型 hook 用
  "last_assistant_text": "…"              // 仅兜底型 hook 用
}
```

规则：
- **字段拿不到就跳过该动作，不要瞎猜**。猜错库名/任务名 = 把东西写进别人的域。
- `space`/`lib` 缺失时**必须**落到默认（个人库），禁止服务端兜底成"某个共享库"。
- 归属读项目的 `.mcp.json` 的 `_belonging`（`lib` / `workspace` 照抄，**不现编**）。

## 4. 重试与幂等约定（这段必须照做，否则会写出重复垃圾）

| 情况 | 码 | 动作 |
|---|---|---|
| 成功 | — | 不再重试；记录 `mode`（created/updated） |
| **队列满** | `QUEUE_FULL` / HTTP **429** | 按 `Retry-After` 退避，**最多 2 次**（1s → 3s），仍失败就**放弃并记日志**，绝不循环 |
| 网络超时 | — | 重试 **1 次**；两次都失败就放弃 |
| 鉴权失败 | 401 / 403 | **绝不重试**。重试不改变结果，只会刷日志 + 打风控。写日志提示"token 失效，去面板重发" |
| 找不到条目 | 404 | **绝不重试**（hook 场景里通常是标题写错，重试还是错） |
| 参数非法 | `INVALID_ARGUMENT` | **绝不重试**（改参数才算修好，脚本改不了） |

**幂等靠服务端**：`memory_remember` 默认 `mode="upsert"`，**按标题去重** —— 同一个标题重复调
不会产生两条。所以 hook 可以放心重放，但**标题必须由内容稳定派生**
（推荐 `"[hook] <task> - <一句话结论>"`），不要用时间戳/随机数当标题（那等于每次都新建一条）。

其它硬约定：
1. **hook 永不阻塞会话**：整体超时预算 ≤3s，超时即放弃（记忆写失败可以下次补，会话卡住不可接受）。
2. **不写临时中间态**：只写"结论/踩坑/口径"。把"我打算接下来做 X"写进库里 = 污染。
   判据：这条东西三个月后对别人还有用吗？没有就别写。
3. **写入必带 `scope="task"`**（会话内的过程态），升长期交给 `memory_session_end` 归类，
   hook 自己不要判断。
4. **禁止在 hook 里做删除**（`memory_forget`）。删除类动作一律人工确认，这是本项目的铁律。

## 5. 样例 A：提醒型 hook（推荐 · 不写数据，只把话送回会话）

最稳的形态：**不代替 agent 决定写什么**，只在收工/压缩前提示它自己写。
Claude Code 风格 `hooks`（WorkBuddy 类同构客户端按各自格式对齐，见 §7 的未验证声明）：

```jsonc
// settings.json（片段）
{
  "hooks": {
    "PreCompact": [
      {
        "matcher": "auto",
        "hooks": [
          { "type": "command",
            "command": "shared-memory-remind.sh preCompact" }
        ]
      }
    ],
    "Stop": [
      { "hooks": [ { "type": "command", "command": "shared-memory-remind.sh stop" } ] }
    ]
  }
}
```

```bash
#!/usr/bin/env bash
# shared-memory-remind.sh —— 只输出提醒，不写库、不阻塞。
# 入参：$1 = 事件名（preCompact | stop）
set -uo pipefail
EV="${1:-stop}"
MSG=""
case "$EV" in
  preCompact) MSG="上下文即将压缩：先 memory_recall(scope=\"task\") 看已落盘的过程态，把本轮新结论 memory_remember(scope=\"task\")，再让它压缩。" ;;
  stop)       MSG="本轮结束前自检：有结论/踩坑/口径没写进共享记忆吗？有就 memory_remember(scope=\"task\")；收工调 memory_session_end。" ;;
esac
# 只走 stdout 的 JSON，退出码恒 0（绝不因为提示失败而打断会话）
if [ -n "$MSG" ]; then
  printf '{"systemMessage":"%s"}\n' "$MSG"
fi
exit 0
```

## 6. 样例 B：兜底直写型（脚本调 MCP，带退避与幂等）

客户端支持"会话结束时跑一段脚本"但不支持往会话里注话时用它。**只在检测到确实有结论性文本时写**，
标题稳定派生（幂等）。需要 `pip install fastmcp`（服务端同库，版本已对齐）。

```python
#!/usr/bin/env python3
"""收工兜底：把会话最后一段结论写进共享记忆（幂等 + 有界重试）。
用法：shm-sessionend.py <task> <token-env> [--text-file PATH]
环境变量：SHM_URL（默认 http://127.0.0.1:18299/mcp）、SHM_TOKEN
"""
import asyncio, json, os, sys
from pathlib import Path

MAX_TRIES = 3          # 1 次正常 + 2 次退避（429/超时），超过就放弃
BACKOFF = (1, 3)       # 秒


def _should_skip(text: str) -> bool:
    """太短 / 纯过程性的不写 —— 宁可少存，不要污染。"""
    t = (text or "").strip()
    return len(t) < 40 or t.startswith(("好的", "收到", "我先", "接下来我"))


async def main() -> int:
    task = sys.argv[1] if len(sys.argv) > 1 else ""
    if not task:
        return 0
    text = ""
    if "--text-file" in sys.argv:
        p = Path(sys.argv[sys.argv.index("--text-file") + 1])
        if p.is_file():
            text = p.read_text(encoding="utf-8")[-1200:]
    if _should_skip(text):
        return 0

    url = os.environ.get("SHM_URL", "http://127.0.0.1:18299/mcp")
    tok = os.environ.get("SHM_TOKEN", "")
    if not tok:
        print("SHM_TOKEN 未设置：跳过（不重试、不报错）", file=sys.stderr)
        return 0

    # 归属照抄项目 .mcp.json 的 _belonging —— 不现编库名
    lib = ""
    for cand in (Path.cwd() / ".mcp.json",):
        if cand.is_file():
            try:
                lib = str(json.loads(cand.read_text(encoding="utf-8"))
                          .get("_belonging", {}).get("lib", ""))
            except Exception:
                pass

    from fastmcp import Client
    args = {
        "title": f"[hook] {task} - 收尾结论",   # 稳定标题 = 幂等（upsert 按标题去重）
        "body": text,
        "scope": "task",                       # 过程态；升长期交给 session_end
        "mode": "upsert",
    }
    if lib:
        args["lib"] = lib

    for i in range(MAX_TRIES):
        try:
            async with Client(url, auth=tok, timeout=6) as c:
                r = await c.call_tool("memory_remember", args)
            print("已写入：", getattr(r, "content", r))
            return 0
        except Exception as exc:               # 429/超时/网络 —— 有界退避
            if i + 1 >= MAX_TRIES:
                print(f"写入失败，放弃（{exc}）", file=sys.stderr)
                return 1
            await asyncio.sleep(BACKOFF[min(i, len(BACKOFF) - 1)])
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

## 7. 非 hook 客户端（其他品牌 agent）

拿不到 hook 事件的客户端只能靠**行为契约**。把下面这段贴进该客户端的自定义指令/系统提示
（服务端 `instructions` 已含同样口径，贴这段是为了覆盖"客户端截断 instructions"的情况）：

```text
共享记忆（shared_memory MCP）使用纪律：
1. 开工：先 memory_session_start(task=<本任务名>, space=<库>)。
2. 动手前：memory_recall 检索既有口径；握手时 instructions 里已带来的
   「硬约束」必须遵守，不需要再搜。
3. 有结论/踩坑/定口径：当场 memory_remember(scope="task")，
   别等会话结束（会话可能随时被压缩或中断）。
4. 收工：memory_session_end(task, outcome, summary) 归类，否则过程态永久污染默认检索。
5. 只存结论，不存中间态；不做删除（memory_forget 需人工确认）。
```

⚠️ **未验证声明**（写清楚，别当成已实测）：
- 样例 A 的 `hooks` 配置形状按 Claude Code 风格写；**本机 WorkBuddy 的
  `~/.workbuddy/settings.json` 目前没有 `hooks` 段（2026-10-16 实查）**，
  落地前须确认你所用客户端是否支持该事件与 `systemMessage` 输出协议。
- 样例 B 的 `fastmcp.Client(url, auth=token)` 用法与服务端 `scripts/e2e.py`、
  `scripts/smoke.py` 一致（那两处已实测跑通 HTTP 通道）。
