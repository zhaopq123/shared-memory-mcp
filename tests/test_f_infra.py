"""F 组 · Phase 2 基础设施补齐（F-1~F-6）。

━━━ 这一组在防什么 ━━━

前面 46 项做的是"产品有没有"，这一组做的是"**运维会不会被骗**"：

  · F-1 配错的变量不能静默用默认值跑起来（`SHARED_MEMORY_TOKEN_FILE` 指了个
    不存在的文件，旧行为是"当没 token"—— 鉴权判定整个跑偏）
  · F-2 客户端要能分支处理失败，不能靠匹配中文文案
  · F-3 一次请求的多行日志必须串得起来，且**绝不带凭据**
  · F-4 探针只看一个数就能判积压
  · F-5 SIGTERM 后排空写队列，别留下半截文件
  · F-6 配置示例与代码逐条对齐（写了而代码不读 = 编造能力）
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shared_memory.auth import Auth, Token  # noqa: E402
from shared_memory.config import KNOWN_ENV_VARS, validate_startup_env  # noqa: E402
from shared_memory.errors import ErrorCode, ToolError  # noqa: E402
from shared_memory.http_server import (  # noqa: E402
    LOG_FORMAT,
    RequestIdFilter,
    _wrap_auth,
    drain_hub_on_shutdown,
    install_request_id_logging,
    shutdown_drain_timeout,
)
from shared_memory.tenancy import TenantHub  # noqa: E402
from shared_memory.tenant import Library, User  # noqa: E402
from shared_memory.tools_tenant import TenantTools  # noqa: E402
from shared_memory.vault import validate_v2_fields  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def hub(tmp_path: Path) -> TenantHub:
    h = TenantHub(tmp_path / "home")
    h.store.users["admin"] = User(name="admin", role="admin", display="管理员")
    h.store.libs["user-admin"] = Library(id="user-admin", owner="user:admin",
                                         created_by="admin")
    h.store.save()
    return h


@pytest.fixture()
def tools(hub: TenantHub) -> TenantTools:
    return TenantTools(hub)


# ══════════════════════════ F-1 启动校验 ══════════════════════════


def test_f1_empty_env_passes() -> None:
    """所有变量都有默认值 —— 空环境必须**能启动**（产品承诺：不用先初始化）。"""
    errors, warnings = validate_startup_env({})
    assert errors == []
    assert warnings == []


def test_f1_flags_every_malformed_value_by_name() -> None:
    """畸形值 → 每条错误里必须**点名是哪个变量**（否则运维只能挨个猜）。"""
    errors, _ = validate_startup_env({
        "PORT": "82o0",
        "LOG_LEVEL": "vorbse",
        "SHARED_MEMORY_COMMIT_DEBOUNCE": "-3",
        "SHARED_MEMORY_AUTOCOMMIT": "maybe",
        "HOST": "",
    })
    joined = "\n".join(errors)
    assert len(errors) == 5, joined
    for name in ("PORT", "LOG_LEVEL", "SHARED_MEMORY_COMMIT_DEBOUNCE",
                 "SHARED_MEMORY_AUTOCOMMIT", "HOST"):
        assert name in joined, f"没点名 {name}"


def test_f1_missing_token_file_is_fatal(tmp_path: Path) -> None:
    """⚠️ 配了 token 文件却读不到 → 拒绝启动。

    静默当成"一把 token 都没有"是最坏的一种降级：服务起来了，
    但鉴权语义已经不是操作者以为的那个。
    """
    errors, _ = validate_startup_env(
        {"SHARED_MEMORY_TOKEN_FILE": str(tmp_path / "nope")}
    )
    assert any("SHARED_MEMORY_TOKEN_FILE" in e for e in errors), errors
    assert validate_startup_env({})[0] == [], "没配这个变量时不该拦"


def test_f1_token_file_that_exists_is_accepted(tmp_path: Path) -> None:
    p = tmp_path / "tokens"
    p.write_text("admin:abc\n", encoding="utf-8")
    assert validate_startup_env({"SHARED_MEMORY_TOKEN_FILE": str(p)})[0] == []


def test_f1_relative_home_warns_not_fatal(tmp_path: Path) -> None:
    """相对路径只提醒（能跑，但会随启动目录漂移）—— 不该阻止启动。"""
    errors, warnings = validate_startup_env({"SHARED_MEMORY_HOME": "home"})
    assert errors == []
    assert any("SHARED_MEMORY_HOME" in w and "相对路径" in w for w in warnings)


def test_f1_home_pointing_at_a_file_is_fatal(tmp_path: Path) -> None:
    p = tmp_path / "plain-file"
    p.write_text("x", encoding="utf-8")
    errors, _ = validate_startup_env({"SHARED_MEMORY_HOME": str(p)})
    assert any("不是目录" in e for e in errors), errors


def test_f1_warns_on_typoed_variable() -> None:
    """拼错的变量名比没配更危险 —— 它看起来像生效了。"""
    _, warnings = validate_startup_env({"SHARED_MEMORY_TOKN": "abc"})
    assert any("SHARED_MEMORY_TOKN" in w for w in warnings), warnings


def test_f1_known_vars_are_not_flagged_as_typos() -> None:
    env = {k: "x" for k in KNOWN_ENV_VARS if k.startswith("SHARED_MEMORY_")}
    _, warnings = validate_startup_env(env)
    assert not [w for w in warnings if "未识别" in w], warnings


def test_f1_main_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """验收口径：缺/错变量时**进程退出码非 0**，且日志说明是哪个。"""
    pytest.importorskip("uvicorn")
    from shared_memory import http_server

    monkeypatch.setenv("PORT", "not-a-port")
    with pytest.raises(SystemExit) as ei:
        http_server.main()
    assert "启动配置校验未通过" in str(ei.value)


# ══════════════════════════ F-2 错误码 ══════════════════════════

NEW_CODES = (
    "INVALID_TYPE", "SCOPE_DENIED", "SESSION_NOT_STARTED",
    "PIN_LOCKED", "KEY_ROTATED", "GOV_CONFIRM_REQUIRED",
)


@pytest.mark.parametrize("name", NEW_CODES)
def test_f2_new_code_defined_and_stable(name: str) -> None:
    """码名与值必须逐字一致 —— 值是要写进客户端 if 里的字符串。"""
    code = ErrorCode[name]
    assert code.value == name
    assert ToolError(code, "x").to_dict()["code"] == name


def test_f2_bad_type_raises_invalid_type() -> None:
    with pytest.raises(ToolError) as ei:
        validate_v2_fields({"type": "decision"})
    assert ei.value.code is ErrorCode.INVALID_TYPE


async def test_f2_task_scope_without_session_raises_session_not_started(
    tools: TenantTools
) -> None:
    r = await tools.memory_write(
        "admin", title="无会话过程态", body="x", lib="user-admin", scope="task",
    )
    assert r["ok"] is False and r["code"] == "SESSION_NOT_STARTED", r
    assert "memory_session_start" in r["hint"]


async def test_f2_session_end_of_unknown_session_raises_session_not_started(
    tools: TenantTools
) -> None:
    r = await tools.memory_session_end("admin", task="从没开过的会话")
    assert r["ok"] is False and r["code"] == "SESSION_NOT_STARTED", r


async def test_f2_delete_without_confirm_raises_gov_confirm_required(
    tools: TenantTools
) -> None:
    """破坏性动作未确认 → 独立码（客户端据此弹确认框，而不是当参数错误）。"""
    r = await tools.memory_delete("admin", "任意一条")
    assert r["ok"] is False and r["code"] == "GOV_CONFIRM_REQUIRED", r


def test_f2_key_rotated_is_told_apart_from_bad_key(tmp_path: Path) -> None:
    """⚠️ 轮换掉的旧密钥要能**单独认出来**，不能只说"未授权"。

    不然主人刚点完轮换、agent 全掉线，日志写着"未授权"，
    排查方向直接跑偏成"我 token 抄错了"。
    """
    import time

    a = Auth(bind_host="127.0.0.1")
    path = tmp_path / "state" / "master-key.json"
    assert a.attach_master_key(path) is False, "新路径上还没有主密钥"
    old = a.set_master_key()
    assert a.master_key_enabled(), "set 之后应识别为已启用（attach 的返回语义是「是否已存在」）"
    new = a.rotate_master_key()

    assert a.verify_master(old), "宽限期内旧密钥仍应可用"
    assert a.verify_master(new)
    assert not a.is_rotated_out(old), "还在宽限期内，不该算轮换掉"

    # 把宽限期改成"已经过去"（等价于过了 60 秒，不用真等）
    data = a._load_master()
    data["prev"]["until"] = time.time() - 1
    a._write_master(data)

    assert not a.verify_master(old), "过宽限期后旧密钥必须失效"
    assert a.is_rotated_out(old), "必须认出这是被轮换掉的旧密钥"
    assert not a.is_rotated_out(new), "当前密钥不是轮换掉的"
    assert not a.is_rotated_out("瞎写的密钥")


def test_f2_pin_locked_reported_with_code(tmp_path: Path) -> None:
    """PIN 锁定的响应带 code=PIN_LOCKED + Retry-After（前端才知道是"等"不是"错"）。"""
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from shared_memory.dashboard import make_routes

    h = TenantHub(tmp_path / "home")
    h.bootstrap()
    a = Auth(bind_host="127.0.0.1", store=h.store)
    a.attach_pin(tmp_path / "state" / "pin.json")
    a.set_pin("246810")
    client = TestClient(Starlette(routes=make_routes(h, auth=a)))

    for _ in range(5):
        client.post("/api/auth/login", json={"pin": "000000"})
    r = client.post("/api/auth/login", json={"pin": "246810"})   # 锁定期连对的也拒
    assert r.status_code == 429, r.text
    assert r.json()["code"] == "PIN_LOCKED", r.json()
    assert r.headers.get("Retry-After"), "没给 Retry-After"


# ══════════════════════════ F-3 请求 ID 日志 ══════════════════════════


class _Recorder:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.called = False

    async def __call__(self, scope, receive, send) -> None:
        self.called = True
        await send({"type": "http.response.start",
                    "status": self.status, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})


async def _asgi(app: Any, path: str, *, headers: list[tuple[bytes, bytes]] | None = None,
                method: str = "GET") -> None:
    scope = {"type": "http", "method": method, "path": path, "query_string": b"",
             "headers": headers or [], "client": ("1.2.3.4", 1234),
             "scheme": "http", "server": ("127.0.0.1", 8200), "root_path": ""}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        return None

    await app(scope, receive, send)


def _formatted(caplog: pytest.LogCaptureFixture) -> list[str]:
    """按 LOG_FORMAT 渲染，验证的是**运维真正看到的字符串**。"""
    out = []
    for record in caplog.records:
        record.request_id = getattr(record, "request_id", "-")
        out.append(logging.Formatter(LOG_FORMAT).format(record))
    return out


def test_f3_filter_injects_request_id_from_context() -> None:
    from shared_memory.request_ctx import new_request_id

    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello", (), None)
    assert RequestIdFilter().filter(record) is True
    assert record.request_id == "-", "不在请求上下文里必须给占位符而不是崩"
    rid = new_request_id()
    RequestIdFilter().filter(record)
    assert record.request_id == rid


@pytest.mark.asyncio
async def test_f3_one_request_one_request_id(caplog: pytest.LogCaptureFixture) -> None:
    """⚠️ 一次请求打出的所有日志行必须共用同一个 rid（否则串不起来）。"""
    auth = Auth(bind_host="127.0.0.1")            # loopback → 免鉴权
    app = _wrap_auth(_Recorder(), auth)
    with caplog.at_level(logging.INFO, logger="shared_memory.http"):
        await _asgi(app, "/health")
    lines = _formatted(caplog)
    ids = set(re.findall(r"rid=([0-9a-f]{8})", "\n".join(lines)))
    assert len(ids) == 1, f"同一请求出现多个 rid：{ids} / {lines}"
    assert "请求完成" in "\n".join(lines)


@pytest.mark.asyncio
async def test_f3_two_requests_get_different_ids(caplog: pytest.LogCaptureFixture) -> None:
    auth = Auth(bind_host="127.0.0.1")
    app = _wrap_auth(_Recorder(), auth)
    seen = []
    for _ in range(2):
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="shared_memory.http"):
            await _asgi(app, "/health")
        seen.append(set(re.findall(r"rid=([0-9a-f]{8})", "\n".join(_formatted(caplog)))))
    assert seen[0] and seen[1] and not (seen[0] & seen[1]), "两次请求共用了 rid"


@pytest.mark.asyncio
async def test_f3_denied_requests_still_log_with_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """401 这类被中间件挡掉的请求同样要留痕 —— 安全事件没日志等于没发生。"""
    auth = Auth(bind_host="0.0.0.0", tokens=[Token(token="real-key", user="admin")])
    app = _wrap_auth(_Recorder(), auth)
    with caplog.at_level(logging.INFO, logger="shared_memory.http"):
        await _asgi(app, "/health")
    lines = "\n".join(_formatted(caplog))
    assert "rid=" in lines and "401" in lines, lines


@pytest.mark.asyncio
async def test_f3_logs_never_contain_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """⚠️ 日志里绝不能出现 token/PIN 明文。"""
    auth = Auth(bind_host="0.0.0.0", tokens=[Token(token="real-key", user="admin")])
    app = _wrap_auth(_Recorder(), auth)
    with caplog.at_level(logging.INFO, logger="shared_memory.http"):
        await _asgi(app, "/health", headers=[(b"authorization", b"Bearer super-secret")])
    lines = "\n".join(_formatted(caplog))
    assert "super-secret" not in lines, "凭据被写进日志了"
    assert "real-key" not in lines


def test_f3_install_is_idempotent_and_picks_up_existing_handlers() -> None:
    """重复安装不叠加过滤器（每条日志过两遍同一个过滤器 = 白干活）；
    而且装的是 **handler** 上的过滤器 —— 子 logger propagate 上来的记录
    只有挂在 handler 上才拦得到。"""
    handler = logging.NullHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        for _ in range(2):
            install_request_id_logging()
        n = sum(1 for f in handler.filters if isinstance(f, RequestIdFilter))
        assert n == 1, f"过滤器被装了 {n} 次"
    finally:
        root.removeHandler(handler)


def test_f3_main_wires_the_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() 必须真的装上过滤器 —— 否则格式里的 %(request_id)s 会炸日志。"""
    pytest.importorskip("uvicorn")
    from shared_memory import http_server

    monkeypatch.setenv("PORT", "abc")      # 校验阶段就退出，不真起服务
    with pytest.raises(SystemExit):
        http_server.main()
    assert any(
        isinstance(f, RequestIdFilter)
        for h in logging.getLogger().handlers for f in h.filters
    ), "main() 没装 RequestIdFilter"


# ══════════════════════════ F-4 健康检查队列深度 ══════════════════════════


async def test_f4_health_exposes_flat_queue_depth(hub: TenantHub) -> None:
    """探针只要一个数就能判积压，不必钻进嵌套对象（F-4）。"""
    svc = hub.service_for("user-admin")
    await svc.queue.submit(lambda: _sleep_done())
    result = hub.health("admin")
    assert "queue_depth" in result, "缺 queue_depth 字段"
    assert result["queue_depth"] == result["queue"]["pending"], "两个来源漂移了"
    assert isinstance(result["queue_depth"], int)


async def _sleep_done() -> bool:
    return True


def test_f4_depth_reads_from_real_queue_stats(hub: TenantHub) -> None:
    """字段必须来自 queue.stats()，不是重新算一遍（两处真相必然漂移）。"""
    src = (Path(__file__).resolve().parent.parent / "src" / "shared_memory"
           / "tenancy.py").read_text(encoding="utf-8")
    block = src.split("def health", 1)[1]
    assert '"queue_depth": pending' in block


# ══════════════════════════ F-5 优雅停机 ══════════════════════════


def test_f5_drain_budget_parsing() -> None:
    assert shutdown_drain_timeout({}) == 10.0
    assert shutdown_drain_timeout({"SHARED_MEMORY_SHUTDOWN_DRAIN_TIMEOUT": "3"}) == 3.0
    assert shutdown_drain_timeout({"SHARED_MEMORY_SHUTDOWN_DRAIN_TIMEOUT": "0"}) == 0.0
    assert shutdown_drain_timeout(
        {"SHARED_MEMORY_SHUTDOWN_DRAIN_TIMEOUT": "soon"}
    ) == 10.0, "非法值必须退回默认，而不是抛错卡住停机"


async def test_f5_drain_stops_queues_and_leaves_no_half_written_file(
    hub: TenantHub, tools: TenantTools,
) -> None:
    """⚠️ 排空后：服务已停、文件是完整的（验收：kill -TERM 后无半截文件）。"""
    r = await tools.memory_write(
        "admin", title="停机不丢写", body="完整正文，一个字都不能少", lib="user-admin",
    )
    assert r["ok"] is True, r
    assert hub._services, "写入没创建出库服务，测不到排空"

    assert await drain_hub_on_shutdown(hub) is True
    assert hub._services == {}, "排空后服务应被清空"

    files = list((hub.home / "user-admin" / "notes").glob("*.md"))
    if not files:      # 目录布局以真实 vault 为准，退化到全目录搜索
        files = list(hub.home.rglob("停机不丢写.md"))
    assert files, "记忆文件没落盘"
    body = files[0].read_text(encoding="utf-8")
    assert "完整正文，一个字都不能少" in body, "落盘内容是半截的"


async def test_f5_drain_failure_does_not_block_shutdown(
    hub: TenantHub, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """排空失败只告警 —— 卡住不退比少一条 git 记录严重得多。"""
    async def boom() -> None:
        raise RuntimeError("写队列炸了")

    monkeypatch.setattr(hub, "close", boom)
    assert await drain_hub_on_shutdown(hub) is False


def test_f5_shutdown_hook_is_on_the_lifespan_path() -> None:
    """钩子必须挂在 lifespan 的 shutdown.complete 之后（自己装信号处理器会
    把 uvicorn 排空在途请求的逻辑顶掉）。"""
    src = (REPO_ROOT / "src" / "shared_memory" / "http_server.py").read_text(
        encoding="utf-8"
    )
    assert "lifespan.shutdown.complete" in src
    assert "signal.signal(" not in src, "不该自己装 SIGTERM 处理器"


# ══════════════════════════ F-6 配置示例 ══════════════════════════

#: 服务本体真正读取的变量（脚本专用的 E2E_* 不在这里）。
SERVICE_VARS = (
    "HOST", "PORT", "LOG_LEVEL",
    "SHARED_MEMORY_HOME", "SHARED_MEMORY_ROOT",
    "SHARED_MEMORY_TOKEN", "SHARED_MEMORY_TOKEN_FILE",
    "SHARED_MEMORY_AUTOCOMMIT", "SHARED_MEMORY_COMMIT_DEBOUNCE",
    "SHARED_MEMORY_ALLOW_INSECURE", "SHARED_MEMORY_SHUTDOWN_DRAIN_TIMEOUT",
)


def _env_example_lines() -> list[str]:
    return (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize("name", SERVICE_VARS)
def test_f6_env_example_covers_each_service_var(name: str) -> None:
    """配置示例与代码逐条对齐（F-1 的白名单就是那条对齐线的两端）。"""
    text = "\n".join(_env_example_lines())
    assert re.search(rf"^\s*#?\s*{name}=", text, re.M), f".env.example 里没提 {name}"


def test_f6_env_example_has_no_real_secrets() -> None:
    """验收：grep 确认无真实值。

    ⚠️ 只查**凭据本体**（``SHARED_MEMORY_TOKEN``）—— ``_TOKEN_FILE`` 的值是
    路径、不是密钥，把它也算成"真实值"是误报（第一版就误报过）。
    """
    for line in _env_example_lines():
        if line.lstrip().startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() in ("SHARED_MEMORY_TOKEN", "SHARED_MEMORY_MASTER_KEY",
                           "SHARED_MEMORY_PIN"):
            assert not value.strip(), f"{key} 带了真实凭据：{value!r}"


def test_f6_dotenv_is_not_committable() -> None:
    """验收：.gitignore 含 .env（但 .env.example 仍可入库）。"""
    ignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignore
    assert "!.env.example" in ignore


def test_f6_no_fabricated_master_key_env_var() -> None:
    """⚠️ 文档不许承诺代码里没有的开关。

    主密钥与 PIN 只能在面板设置（盘上只存哈希），所以 .env.example 里
    必须**明写这一点**，而不是顺手编两个 SHARED_MEMORY_MASTER_KEY / _PIN。
    """
    text = "\n".join(_env_example_lines())
    assert "SHARED_MEMORY_MASTER_KEY=" not in text
    assert "不走环境变量" in text
