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
