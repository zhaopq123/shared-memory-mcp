# 贡献指南

> 改代码前先读「几条硬性约定」——那几条都是**测试全绿时测不出、只有真部署才暴露**的坑，
> 违反一条就会在某个深夜以"服务静默死亡"的形式找回来。

---

## 快速开始

```bash
git clone https://github.com/zhaopq123/shared-memory-mcp.git
# 国内镜像：https://gitee.com/zhaopq/shared-memory-mcp
cd shared-memory-mcp

python3 -m venv venv
./venv/bin/pip install -e ".[dev]"     # Windows: venv\Scripts\pip install -e ".[dev]"

./venv/bin/python -m pytest            # 当前 318 项，必须全绿
```

跑起来：

```bash
echo "alice:$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')" > tokens.txt
export SHARED_MEMORY_TOKEN_FILE=$PWD/tokens.txt
export SHARED_MEMORY_HOME=$PWD/home
./venv/bin/python -m shared_memory.http_server
```

---

## 项目结构

```
src/shared_memory/
├── http_server.py    入口：uvicorn + fastmcp
├── service.py        业务编排
├── auth.py           token → user/role/groups，凭据解析
├── tenancy.py        租户：库与用户的归属关系
├── tenant.py         单租户视角
├── workspace.py      工作空间（项目级隔离）
├── request_ctx.py    ★ 写工具清单 WRITE_TOOLS + scope 判定
├── tools_tenant.py   MCP 工具注册（17 个）
├── vault.py          ★ 存储核心：Markdown 读写 + git 版本化
├── queue.py          ★ 单写者 FIFO 写入队列
├── library.py        库元数据
├── knowledge.py      视图聚合（decisions/lessons/todos/expiring/inferences）
├── digest.py         摘要
├── cache.py          读缓存
├── config.py         配置（含库内 settings.json）
├── errors.py
├── dashboard.py      网页面板路由
└── static/           panel.css / panel.js / i18n.js

scripts/
├── ops/              install-runsv.sh（常驻守护生成器）、test-recovery.sh（自愈验证）
├── ops_isolation_verify.py   隔离验证
├── deploy.sh / verify_deploy.py
├── smoke.py / stress.py / e2e.py / seed_demo.py
└── rotate-token.sh / verify-token.sh

tests/                318 项；安全类测试优先于功能测试
docs/                 安装手册、使用教程、设计原理、真实走查
```

---

## 几条硬性约定

### 1. 新增写工具，必须登记进 `WRITE_TOOLS`

MCP 协议里读和写**全都走 POST**，所以**按 HTTP method 判断读写必然误杀**——
只读凭据的 `initialize`、`whoami`、`search` 会被一起拒 403。

判定发生在工具层：

```python
# request_ctx.py
WRITE_TOOLS = {"memory_write", "memory_delete", ...}   # ← 新写工具必须加进来
require_write_scope()                                   # 写工具内部调用
```

**忘了登记 = 这个工具漏过权限检查。** 这是静默的安全漏洞。

### 2. `vault._git()` 必须阻断向上遍历

记忆库有时会被放在代码仓库里（比如生产就是 `/opt/shared-memory-mcp` 内）。
`git -C <lib>` 默认往上找 `.git`，于是记忆的 commit 会落进代码仓库。

必须设 `GIT_CEILING_DIRECTORIES`，并且 `is_repo()` 要用
`rev-parse --show-toplevel` 校验顶层**确实等于**库目录
（只看 `.git` 是否存在会假阳性：空壳目录、上级仓库都能骗过）。

新建库后**必须确认 `is_repo()` 真为 True**，否则记忆没有版本化。

### 3. token 文件：3 段与 4 段是两个语义

```
3 段  user:label:token          → 默认【可读写】
4 段  user:label:scope:token    → scope ∈ {read, readwrite}
```

**"只读"必须显式写 `read`。** 3 段格式缺 scope 会走默认值 `readwrite`，
于是只读凭据在服务重启后变成可写 —— 这是**权限提升**，真实发生过。

取 token 时用 `awk -F: '{print $NF}'`（取最后一段），
**不要盲取第 4 段**——生产文件是两种格式混用的。

### 4. 打包必须带静态资源

```toml
[tool.setuptools.package-data]
shared_memory = ["static/*.css", "static/*.js"]
```

setuptools 默认只收 `.py`。漏了这段，**直接跑源码时面板会 500**（找不到 css/js）。
也正因为如此，安装方式必须是 `pip install -e .`。

### 5. 空列表与 None 要区分

```python
# ❌ 空列表会被静默替换成默认值，语义就丢了
memory_write(tags=[])

# ✅ 想要"没有"就用 None，想要"默认"就别传
```

---

## 测试要求

**安全/越权测试优先于功能测试。** 一个功能没测顶多是 bug，
一个越权没拦就是数据泄露。

必须覆盖的越权场景：

- 只读凭据能否写入（应全部拒绝）
- 能否读写未授权的库（应拒绝）
- 个人库：连管理员都读不到（这是刻意的）
- 端口绑定：`is_loopback_bind` 对 LAN 地址应返回 False

**"声明了但没赋值"也要测**：

```python
# ❌ 这只测字段存在 —— 声明了却没赋值也能过
assert "field" in result

# ✅ 测它真的被填上了
assert result["field"] is not None
```

跑测试：

```bash
./venv/bin/python -m pytest                    # 全量
./venv/bin/python -m pytest tests/test_tenancy.py -v   # 单文件
```

---

## 提交

- 提交信息用中文或英文均可，**说清"为什么"而不是"改了什么"**
- 改 git 历史前**先打备份 tag**
- 重写历史时用显式 `git add <文件>`，**不要用 `git add -A`**
- `git checkout -- .` 会连你刚打的补丁一起还原，小心

PR 前自查：

```bash
./venv/bin/python -m pytest     # 全绿
git diff --stat                 # 确认没有误带凭据/临时文件
```

> ⚠️ **不要把 `.tokens`、`tokens.json`、`tenants.json`、`.env` 提交进来**。
> 这些已在 `.gitignore` 里，但如果你用 `git add -f` 强制加入就会漏出去。
