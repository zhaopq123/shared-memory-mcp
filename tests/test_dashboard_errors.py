"""面板接口的错误封装：业务错误不能被当成 500。

━━━ 实测踩的坑 ━━━

`/api/note?ident=<不存在>` 原来回 **500**：`_envelope` 把所有异常
一律当"服务器炸了"。用户点到一个已删除/已归档库里的旧链接，看到的是
"服务器错误"，而不是"这条记忆不在了" —— 排查方向直接跑偏。

`_HTTP_BY_CODE` 这张表本来就在，只是通用封装没用它。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shared_memory.dashboard import _envelope  # noqa: E402
from shared_memory.errors import ErrorCode, ToolError  # noqa: E402


def _raise(exc: Exception):
    def fn():
        raise exc

    return fn


def test_toolerror_not_found_maps_to_404() -> None:
    resp = _envelope(_raise(ToolError(ErrorCode.NOT_FOUND, "找不到这条记忆")))
    assert resp.status_code == 404
    body = json.loads(resp.body)
    assert body["ok"] is False
    assert body["code"] == "NOT_FOUND"
    assert "找不到" in body["message"]


def test_toolerror_forbidden_maps_to_403() -> None:
    resp = _envelope(_raise(ToolError(ErrorCode.FORBIDDEN, "无权访问")))
    assert resp.status_code == 403
    assert json.loads(resp.body)["code"] == "FORBIDDEN"


def test_toolerror_conflict_maps_to_409() -> None:
    resp = _envelope(_raise(ToolError(ErrorCode.CONFLICT, "重名")))
    assert resp.status_code == 409


def test_unknown_exception_still_500() -> None:
    """真 bug 仍然是 500，别把"我们没预料到的错"美化成 4xx。"""
    resp = _envelope(_raise(RuntimeError("boom")))
    assert resp.status_code == 500
    assert "Server error" not in json.dumps(json.loads(resp.body))


def test_success_passthrough() -> None:
    resp = _envelope(lambda: {"ok": True, "n": 1})
    assert resp.status_code == 200
    assert json.loads(resp.body) == {"ok": True, "n": 1}
