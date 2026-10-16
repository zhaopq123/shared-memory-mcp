"""请求身份 —— 把「这个请求代表谁」从中间件传到工具层。

━━━ 为什么需要它（而不是让工具收 username 参数）━━━

MCP 工具的参数 schema 是**暴露给客户端的**。如果把 ``user_name`` 做成
工具参数，客户端就能自己填 —— 那等于没有权限系统。

而 FastMCP 的工具签名由 schema 决定，中间件没法直接往调用里塞参数。
所以用 **contextvar** 传递：

    中间件（已鉴权）→ set_current_user("张三")
        工具层     → get_current_user() → "张三"

contextvar 是协程安全的（每个请求一个上下文），
不需要 threading.local 那套，也不会在并发请求间串号。

━━━ 默认值 ━━━

未设置时返回 ``""``，工具层会明确拒绝（而不是回落到某个默认用户）——
"忘了设身份就用管理员跑" 是最危险的那种默认值。
"""

from __future__ import annotations

from contextvars import ContextVar

#: 当前请求的身份。空串 = 未鉴权（工具层必须拒绝）。
_current_user: ContextVar[str] = ContextVar("shm_current_user", default="")

#: 当前请求的凭据 scope（read / readwrite）。用于"只读 token 不能写"。
_current_scope: ContextVar[str] = ContextVar("shm_current_scope", default="")

#: 请求来源（IP），用于日志与限速
_current_source: ContextVar[str] = ContextVar("shm_current_source", default="")


def set_identity(user: str, scope: str = "", source: str = "") -> None:
    """由鉴权中间件调用（在放行之前）。"""
    _current_user.set(user or "")
    if scope:
        _current_scope.set(scope)
    if source:
        _current_source.set(source)


def current_user() -> str:
    """取当前请求的身份。空串表示未鉴权。"""
    return _current_user.get()


def current_scope() -> str:
    return _current_scope.get()


def current_source() -> str:
    return _current_source.get()


def require_user() -> str:
    """取身份，未鉴权则抛错。

    工具层统一用它开头 —— 这样"忘了检查身份"在结构上不可能发生。
    """
    from .errors import ErrorCode, ToolError

    u = current_user()
    if not u:
        raise ToolError(
            ErrorCode.FORBIDDEN,
            "这个请求没有身份（未鉴权）",
            hint="用 Authorization: Bearer <token> 访问；token 见管理员分配",
        )
    return u


# ══════════════════════ scope 守卫（只读凭据不能写）══════════════════════
#
# ⚠️ 为什么守卫必须在**工具层**而不是 HTTP method 层：
#
# MCP 协议里读和写**全都是 POST**。按 method 判读写（POST=写）会把
# 只读凭据连 initialize 和 memory_search 一起拦掉 —— 只读凭据直接不可用。
# 这个坑踩过一次：只读 token 一律返回 403，连 whoami 都调不通。
#
# 所以改成按**工具语义**判定：显式登记哪些工具会改数据，
# 命中名单才检查 scope。新增写工具时忘了登记 → 漏拦（见下方"漏了怎么办"）。

#: 会改变数据的工具。**新增写工具必须登记到这里**，否则只读凭据能写它。
#:
#: v2（S-4）三条注意：
#:   ① 只登记**真正会调 ``require_write_scope`` 的那个名字**。
#:   ② 旧名别名（memory_write/memory_delete/memory_import）**不用登记** ——
#:      它们直接调新函数，检查在那边做，登记了是重复。
#:   ③ ``memory_history`` / ``memory_backup`` 是**混合工具**：只有
#:      ``commit=True`` / ``action=import`` 分支才校验 —— 只读凭据
#:      照样能看历史、能导出（那是纯读）。
WRITE_TOOLS = frozenset({
    "memory_session_end",
    "memory_remember",
    "memory_forget",
    "memory_history",     # 仅 commit=True 分支会查
    "memory_backup",      # 仅 action=import 分支会查
})


def require_write_scope(tool_name: str) -> None:
    """只读凭据调用写工具时抛错。

    读工具不进这里 —— 读对只读凭据是完全放开的。

    Args:
        tool_name: 当前工具名，用于判断是否需要写权限。
    """
    if tool_name not in WRITE_TOOLS:
        return

    from .errors import ErrorCode, ToolError

    scope = current_scope()
    if scope == "readwrite":
        return

    raise ToolError(
        ErrorCode.FORBIDDEN,
        f"此凭据是只读的，不能调用 {tool_name}",
        hint="需要写权限请让管理员发一把 readwrite 凭据",
    )
