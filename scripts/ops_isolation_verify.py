"""按工作空间隔离的端到端验证脚本（在 vivo 容器内跑）。

要点：streamableHttp 必须先 initialize 取 Mcp-Session-Id，
后续每个请求都带上，否则服务端返回 -32600。
"""
import json
import pathlib
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8200/mcp"
TOKFILE = pathlib.Path("/opt/shared-memory-mcp/.tokens")


def load_tokens() -> dict:
    """按 label 取 token 值。

    ⚠️ 必须和服务端 parse_line 用同一套分段规则（1/2/3/4 段）：
    4 段是 `user:label:scope:token`，token 在**最后一段**。
    早先这里写死 split(":", 2)，4 段行会取到 "read:<token>" 这种废串，
    发出去当然 401 —— 那是脚本 bug，不是服务端鉴权问题。
    """
    out = {}
    for ln in TOKFILE.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        parts = ln.split(":")
        if len(parts) == 1:
            label, tok = "admin", parts[0]
        elif len(parts) == 2:
            label, tok = parts[0], parts[1]
        elif len(parts) == 3:
            label, tok = parts[1], parts[2]
        else:  # 4 段：user:label:scope:token
            label, tok = parts[1], ":".join(parts[3:])
        out.setdefault(label, tok)
    return out


def parse_sse(raw: str):
    for ln in raw.splitlines():
        ln = ln.strip()
        if ln.startswith("data:"):
            raw = ln[5:].strip()
            break
    try:
        return json.loads(raw)
    except Exception:
        return {"_raw": raw[:400]}


def make_client(token: str):
    """建一个带会话的 MCP 客户端。

    ⚠️ 必须复用同一个 client：服务端有失败限速，反复 initialize
    会把计数打满，之后所有请求一律 401 —— 那是限速不是鉴权，
    混在一起看会误判成"只读凭据坏了"。
    """
    sess = {"id": None}

    def rpc(method, params, notify=False):
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notify:
            payload["id"] = 1
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + token,
        }
        if sess["id"]:
            headers["Mcp-Session-Id"] = sess["id"]
        req = urllib.request.Request(BASE, data=json.dumps(payload).encode(), headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=20)
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                sess["id"] = sid
            return parse_sse(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            return {"_http": e.code, "_body": body[:300]}

    rpc("initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "isolation-verify", "version": "1.0"},
    })
    rpc("notifications/initialized", {}, notify=True)
    return rpc


def tool(rpc, name, **args):
    j = rpc("tools/call", {"name": name, "arguments": args})
    if "_http" in j:
        # 401 = 限速打满或凭据无效；403 = scope 不足。二者含义完全不同，
        # 必须分开报，否则会把限速误读成"只读凭据被正确拦住"。
        return {"ok": False, "error": "HTTP%s" % j["_http"], "msg": j.get("_body", "")[:140]}
    if "_raw" in j:
        return {"ok": False, "error": "PARSE", "msg": j["_raw"][:160]}
    res = j.get("result")
    if res is None:
        return {"ok": False, "error": j.get("error", {}).get("code", "?"),
                "msg": str(j.get("error", {}).get("message", ""))[:160]}
    if "content" in res:
        try:
            return json.loads(res["content"][0]["text"])
        except Exception:
            return {"ok": False, "error": "CONTENT", "msg": str(res)[:160]}
    return res


def main():
    toks = load_tokens()
    print("== 读到 %d 把 token: %s" % (len(toks), ", ".join(toks)))
    fails = []

    # 每把 token 只建一次连接，全脚本复用（限速敏感）
    rpcs = {label: make_client(tok) for label, tok in toks.items()}

    # 1. 每把 token 写一条，确认落在自己的库
    print("\n== 写入（不指定 lib，走 default_write_lib）==")
    for label, note in [("ws-乱七八糟", "隔离验证-A库"),
                        ("ws-个人工作中枢", "隔离验证-B库"),
                        ("ws-私人", "隔离验证-C库")]:
        r = tool(rpcs[label], "memory_write", title=note, body="来自 " + label + " 的记忆")
        lib = (r.get("note") or {}).get("lib") or r.get("lib") or r.get("_lib_label")
        print("  %-14s ok=%-5s 落库=%s" % (label, r.get("ok"), lib))
        if not r.get("ok"):
            fails.append("write %s: %s" % (label, r))

    # 2. 越权：用 A 的 token 显式指定 B 的库
    print("\n== 越权写（拿 A 的 token 写 B 的库，应 FORBIDDEN）==")
    pairs = [("ws-乱七八糟", "user-ws-zhongshu", "B库"),
             ("ws-个人工作中枢", "user-ws-lanzaluoduo", "A库"),
             ("ws-私人", "user-ws-lanzaluoduo", "A库")]
    for label, target, desc in pairs:
        r = tool(rpcs[label], "memory_write", title="越权尝试-" + desc,
                 body="不该写成功", lib=target)
        status = "BLOCKED" if not r.get("ok") else "!!! LEAKED !!!"
        print("  %-14s → %-20s %-8s %-14s %s" % (label, target, desc, status, r.get("error")))
        if r.get("ok"):
            fails.append("越权写泄漏 %s -> %s" % (label, target))

    # 3. 越权读
    print("\n== 越权读（应 NOT_FOUND 或 FORBIDDEN）==")
    for label, target, ident in [("ws-乱七八糟", "user-ws-zhongshu", "隔离验证-B库"),
                                 ("ws-私人", "user-ws-lanzaluoduo", "隔离验证-A库")]:
        r = tool(rpcs[label], "memory_get", ident=ident, lib=target)
        status = "BLOCKED" if not r.get("ok") else "!!! LEAKED !!!"
        print("  %-14s → %-20s %-14s err=%s" % (label, target, status, r.get("error")))
        if r.get("ok"):
            fails.append("越权读泄漏 %s -> %s" % (label, target))

    # 4. 只读凭据：读必须通（这是修好 MCP 全 POST 那个坑的关键验证）
    print("\n== 只读凭据：读操作必须放行 ==")
    for tool_name, args in [("memory_whoami", {}),
                            ("memory_search", {"query": "隔离验证"}),
                            ("memory_list", {})]:
        r = tool(rpcs["collab-协作方只读"], tool_name, **args)
        status = "OK" if r.get("ok") else "!!! 应放行却失败 !!!"
        print("  collab-只读  %-16s %-16s %s" % (tool_name, status, r.get("error") or ""))
        if not r.get("ok"):
            fails.append("只读凭据读操作被误拦: %s (%s)" % (tool_name, r.get("msg", "")[:80]))

    # 5. 只读凭据：写必须被拦
    # 注意拦截发生在**工具层**（返回 "此凭据是只读的"），不是 HTTP 403 ——
    # MCP 全是 POST，按 method 判读写会误伤读操作，所以守卫放在工具层。
    print("\n== 只读凭据：写操作必须被拦 ==")
    write_cases = [
        ("memory_write", {"title": "只读越权-write", "body": "x"}),
        ("memory_delete", {"ident": "隔离验证-A库"}),
        ("memory_restore", {"filename": "nonexistent.md", "lib": "company"}),
        ("memory_commit", {}),
        ("memory_import", {"bundle": "data:text/plain;base64,e30="}),
    ]
    for tool_name, args in write_cases:
        r = tool(rpcs["collab-协作方只读"], tool_name, **args)
        msg = (r.get("msg") or "")
        blocked = not r.get("ok")
        if not blocked:
            status = "!!! LEAKED !!!"
        elif "只读" in msg:
            status = "BLOCKED(工具层守卫)"
        else:
            status = "BLOCKED(但原因=%s: %s)" % (r.get("error"), msg[:60])
        print("  collab-只读  %-16s %s" % (tool_name, status))
        if not blocked:
            fails.append("只读凭据写成功: %s" % tool_name)
        elif "只读" not in msg:
            # 参数校验等其它错误不算 scope 拦截通过，但也不算泄漏
            print("        （注：非 scope 拦截，而是 %s）" % (r.get("error") or "其它错误"))

    # 6. 库面确认
    print("\n== 可见库面（whoami）==")
    for label in ["ws-乱七八糟", "ws-个人工作中枢", "ws-私人", "collab-协作方只读"]:
        r = tool(rpcs[label], "memory_whoami")
        libs = [l.get("id") for l in (r.get("libraries") or [])]
        print("  %-18s %s" % (label, libs))

    print("\n==== %s ====" % ("全部通过，无串台" if not fails else "有 %d 项失败" % len(fails)))
    for f in fails:
        print("  FAIL:", f)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
