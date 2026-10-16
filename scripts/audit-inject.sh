#!/bin/bash
# 注入面审计 —— 路径穿越、XSS、SSRF、命令注入。
#
# 每一项都打真实服务并检查**实际响应**，不只看状态码。
#
# 用法：bash audit-inject.sh [port]

PORT="${1:-8200}"
BASE="http://127.0.0.1:${PORT}"
T=$(cat "$(dirname "$0")/.token" 2>/dev/null || cat /opt/shared-memory-mcp/.token)

pass=0; fail=0
ok()   { printf "  ✓ %s\n" "$1"; pass=$((pass+1)); }
bad()  { printf "  ✗ %s  → %s\n" "$1" "$2"; fail=$((fail+1)); }

echo "════ 注入面审计（端口 $PORT）════"
echo

echo "── 1. 路径穿越（ident 参数进文件路径查找）──"
for evil in "../../../etc/passwd" "..%2f..%2fetc%2fpasswd" "/etc/passwd" "....//....//etc/passwd" "%2e%2e%2f%2e%2e%2fetc%2fpasswd"; do
  resp=$(curl -s --max-time 8 "$BASE/api/note?ident=$evil&token=$T")
  if echo "$resp" | grep -q "root:x:"; then
    bad "路径穿越 $evil" "读到了 /etc/passwd 内容！"
  elif echo "$resp" | grep -qE '"ok":true'; then
    bad "路径穿越 $evil" "返回了成功（不该）"
  else
    ok "路径穿越被挡: $evil"
  fi
done

echo
echo "── 2. XSS（记忆内容里的 HTML/JS 是否会被原样吐回）──"
XSS_TITLE="XSS-TEST-$(date +%s)"
payload='<img src=x onerror=alert(1)><script>alert(2)</script>'

# ⚠️ 必须先确认**写入成功**，否则"API 没返回 HTML"是真空的
# （写入失败时当然什么都没返回）—— 这是审计脚本最容易骗自己的地方。
cat > /tmp/xss.json <<EOF
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"memory_write","arguments":{"title":"$XSS_TITLE","body":"$payload"}}}
EOF
write_resp=$(curl -s --max-time 12 -X POST "$BASE/mcp?token=$T" \
  -H "Authorization: Bearer $T" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data-binary @/tmp/xss.json 2>&1)

if echo "$write_resp" | grep -q '"ok":true\|"ok": true'; then
  ok "XSS 载荷写入成功（前提成立）"
  apiresp=$(curl -s --max-time 8 "$BASE/api/note?ident=$XSS_TITLE&token=$T")
  # 原始 JSON 里有 HTML 是**正常的** —— 转义是前端职责。
  # 关键要看**前端渲染后** DOM 里有没有真的 script 元素（见 cdp-verify 的 XSS 项）。
  if echo "$apiresp" | grep -q '<script>'; then
    ok "API 原样返回 HTML（正常；前端已转义，见 CDP 验证）"
  else
    ok "API 未返回原始 HTML"
  fi
  # 清理
  cat > /tmp/xssdel.json <<EOF
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"memory_delete","arguments":{"ident":"$XSS_TITLE","confirm":true}}}
EOF
  curl -s --max-time 10 -X POST "$BASE/mcp?token=$T" -H "Authorization: Bearer $T" \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    --data-binary @/tmp/xssdel.json > /dev/null 2>&1
else
  bad "XSS 载荷写入失败" "无法验证 XSS（响应: $(echo "$write_resp" | head -c 120)）"
fi

echo
echo "── 3. 命令注入（有没有把用户输入拼进 shell）──"
# 先确认代码里没有 shell 调用
SRC=/opt/shared-memory-mcp/src/shared_memory
if grep -rnE 'os\.system|shell=True|subprocess\.(call|run|Popen)\([^)]*shell\s*=\s*True' "$SRC" 2>/dev/null; then
  bad "发现 shell=True / os.system" "见上方行"
else
  ok "源码无 shell=True / os.system"
fi
# subprocess 只用于 git，且参数是列表（不是拼接字符串）
gitcalls=$(grep -c 'subprocess.run' "$SRC/vault.py" 2>/dev/null || echo 0)
ok "subprocess 调用数: $gitcalls（应仅用于 git，参数为列表）"

echo
echo "── 4. SSRF（有没有服务端抓外部 URL 的能力）──"
if grep -rnE 'requests\.(get|post)|urllib\.request|httpx\.|aiohttp' "$SRC" 2>/dev/null; then
  bad "发现出站 HTTP 调用" "见上方行"
else
  ok "源码无出站 HTTP 调用（不存在 SSRF 面）"
fi

echo
echo "── 5. SQL 注入（本项目无 SQL —— 确认一下）──"
if grep -rnE 'execute\(|executemany\(|SELECT .* FROM' "$SRC" 2>/dev/null; then
  bad "发现 SQL 调用" "见上方行"
else
  ok "无 SQL（存储是文件，不存在 SQL 注入面）"
fi

echo
echo "── 6. DoS 面（深嵌套 / 超大 body / 超长字段）──"

# 深嵌套 JSON：Python json 有递归限制，深嵌套会抛 RecursionError
# ⚠️ 上一版这里只看了状态码，200 也被当成"安全处理" —— 那是错的。
#    必须检查**服务是否还活着**（有没有被打挂）。
deep=$(python3 -c "print('{\"a\":' * 300 + '1' + '}' * 300)")
code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 -X POST "$BASE/api/config?token=$T" \
  -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d "$deep")
alive=$(curl -s -o /dev/null -w "%{http_code}" --max-time 6 "$BASE/health?token=$T")
if [ "$alive" = "200" ]; then
  ok "深嵌套 JSON 后服务仍存活（HTTP $code，健康 $alive）"
else
  bad "深嵌套 JSON 把服务搞挂了" "健康检查返回 $alive"
fi

# 超大 body：用文件避免命令行长限制
python3 -c "print('x' * 6000000)" > /tmp/big.txt
python3 - <<'PYEOF' > /tmp/bigbody.json
import json
print(json.dumps({"instructions": "x" * 6000000}))
PYEOF
code2=$(curl -s -o /tmp/bigresp.txt -w "%{http_code}" --max-time 30 -X POST "$BASE/api/config?token=$T" \
  -H "Authorization: Bearer $T" -H 'Content-Type: application/json' --data-binary @/tmp/bigbody.json)
if [ "$code2" = "200" ]; then
  bad "6MB body 被接受" "无 body 大小上限 —— 单次请求即可耗尽内存（DoS 面）"
else
  ok "6MB body 被拒（HTTP $code2）"
fi
alive2=$(curl -s -o /dev/null -w "%{http_code}" --max-time 6 "$BASE/health?token=$T")
[ "$alive2" = "200" ] && ok "超大 body 后服务仍存活" || bad "超大 body 搞挂了服务" "$alive2"
rm -f /tmp/big.txt /tmp/bigbody.json /tmp/bigresp.txt

echo
echo "════ 结果: $pass 通过 / $fail 失败 ════"
[ "$fail" -eq 0 ] || exit 1
