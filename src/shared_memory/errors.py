"""结构化错误信封。

为什么需要它：MCP 工具抛裸异常时，调用方（agent）拿到的是一段堆栈文本，
既不能可靠识别失败原因，也容易把异常当成正常结果继续跑。所以所有失败
都转成 ``{ok: false, code, message, reason}`` —— 与 social-insight-mcp
保持同一形状，两个服务的错误语义一致。
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    """稳定的错误码。用字符串枚举，序列化后人类可读。"""

    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    #: 参数本身合法，但当前身份不允许这个操作（权限问题）。
    #: 与 INVALID_ARGUMENT 分开是刻意的 —— 调用方需要区分
    #: "我传错了"和"我没权限"，前者改参数、后者找管理员。
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    QUEUE_FULL = "QUEUE_FULL"
    STORAGE_ERROR = "STORAGE_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"

    # ── v2（F-2）：把六类反复出现、调用方必须分支处理的失败单独立码 ──
    #
    # 为什么不用 INVALID_ARGUMENT/FORBIDDEN 凑：客户端做自动重试与做
    # "提示用户改参数"是两条完全不同的路径，混在一个码里只能靠匹配
    # 中文文案 —— 文案一改，客户端就瞎了。

    #: 记忆类型不在六型枚举内（S-1.5）。
    INVALID_TYPE = "INVALID_TYPE"
    #: 档位越权：条目的 share / 会话 visibility 不允许这个身份看或改（S-10）。
    #: 与 FORBIDDEN 分开 —— FORBIDDEN 是"你没登录/凭据不对"，
    #: 这个是"你登录了，但这条不在你有权的档位里"。
    SCOPE_DENIED = "SCOPE_DENIED"
    #: 会话未开始就调了需要会话上下文的操作（S-2 生命周期）。
    SESSION_NOT_STARTED = "SESSION_NOT_STARTED"
    #: PIN 连续输错进入锁定期（S-6.1）。带 Retry-After。
    PIN_LOCKED = "PIN_LOCKED"
    #: 主密钥已轮换，手上的旧密钥过了宽限期（S-6.3）。
    KEY_ROTATED = "KEY_ROTATED"
    #: 破坏性/治理动作缺少显式确认（红线：治理只建议、不自动执行）。
    GOV_CONFIRM_REQUIRED = "GOV_CONFIRM_REQUIRED"


class ToolError(Exception):
    """可安全回传给 agent 的工具错误。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        reason: str = "",
        hint: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.reason = reason
        self.hint = hint

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": False,
            "code": self.code.value,
            "message": self.message,
        }
        if self.reason:
            out["reason"] = self.reason
        if self.hint:
            out["hint"] = self.hint
        return out
