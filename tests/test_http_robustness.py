"""HTTP 健壮性与凭据持久化回归（2026-10-13 产品走查产出）。

钉住三件走查实锤的事：
  1. 未启用鉴权（loopback）时 /health 必须能被监控探针免凭据访问
  2. 绝对形式请求目标（RFC 7230 §5.3.2）必须被接受，不能 404
  3. 面板发的 token 必须**重启后仍有效**（原来只存内存，重启全失效）

这三条的共同点：**单测/curl 探活看不出来**，只有真部署 + 真重启才暴露。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shared_memory.auth import Auth, Token  # noqa: E402
from shared_memory.http_server import _normalize_absolute_form  # noqa: E402


class _Recorder:
    """下游 app 的替身：记录是否被调用。"""

    def __init__(self) -> None:
        self.called = False

    async def __call__(self, scope, receive, send) -> None:
        self.called = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})


async def _call(mw, path: str, *, method: str = "GET",
                headers: list[tuple[bytes, bytes]] | None = None):
    from shared_memory.http_server import _wrap_auth as wrap

    rec = _Recorder()
    captured: dict = {}

    async def send(msg):
        if msg["type"] == "http.response.start":
            captured["status"] = msg["status"]

    scope = {
        "type": "http", "method": method, "path": path,
        "query_string": b"", "headers": headers or [], "client": ("1.2.3.4", 1234),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await wrap(rec, mw)(scope, receive, send)
    return captured.get("status"), rec.called


# ══════════════════════ 1. /health 探活 ══════════════════════


@pytest.mark.asyncio
async def test_health_open_when_auth_not_required() -> None:
    """⚠️ 绑 loopback（未启用鉴权）时 /health 必须免凭据放行。

    否则监控探针（systemd / 容器 healthcheck / 守护脚本）拿不到 token，
    探活直接不可用 —— 而日志明明说"未强制鉴权"，自相矛盾。
    """
    auth = Auth(bind_host="127.0.0.1")  # loopback → required=False
    status, called = await _call(auth, "/health")
    assert called, "loopback 下 /health 被拦了 —— 监控探针没法用"


@pytest.mark.asyncio
async def test_health_requires_auth_when_publicly_bound() -> None:
    """⚠️ 绑公网（auth.required）时 /health 仍必须要求凭据。

    与上一条配对：不能为了探活把公网部署的 health 也放开。
    """
    auth = Auth(bind_host="0.0.0.0", tokens=[Token(token="real", user="admin")])
    status, called = await _call(auth, "/health")
    assert status == 401, f"公网绑定下 /health 无凭据返回了 {status}"
    assert not called


@pytest.mark.asyncio
async def test_health_with_bearer_works_when_public() -> None:
    """公网绑定下带正确凭据 → /health 正常放行（对照组）。"""
    auth = Auth(bind_host="0.0.0.0", tokens=[Token(token="real", user="admin")])
    status, called = await _call(
        auth, "/health", headers=[(b"authorization", b"Bearer real")]
    )
    assert called, "带正确凭据的 /health 被拦了"


# ══════════════════════ 2. 绝对形式请求目标 ══════════════════════


def test_normalize_absolute_form_rewrites_path() -> None:
    """绝对形式 target 必须被归一化成路径。"""
    scope = {"type": "http", "path": "http://127.0.0.1:8200/mcp",
             "query_string": b""}
    _normalize_absolute_form(scope)
    assert scope["path"] == "/mcp"
    assert scope["raw_path"] == b"/mcp"


def test_normalize_absolute_form_keeps_query() -> None:
    """绝对形式里的 query 不能丢（/health?verbose=1 之类）。"""
    scope = {"type": "http", "path": "https://host/health?verbose=1",
             "query_string": b""}
    _normalize_absolute_form(scope)
    assert scope["path"] == "/health"
    assert scope["query_string"] == b"verbose=1"


def test_normalize_leaves_normal_path_untouched() -> None:
    """普通路径不能被改动（幂等 + 不误伤）。"""
    scope = {"type": "http", "path": "/mcp", "query_string": b""}
    _normalize_absolute_form(scope)
    assert scope["path"] == "/mcp"


@pytest.mark.asyncio
async def test_absolute_form_target_reaches_mcp() -> None:
    """⚠️ 绝对形式请求打到 /mcp 必须能走到下游（原来 404）。

    症状形态：面板正常、MCP 端点 404 —— 因为路由把整个 URL 当路径匹配。
    用带凭据的请求验证：归一化之后 /mcp 才能命中鉴权后的放行路径。
    """
    auth = Auth(bind_host="0.0.0.0", tokens=[Token(token="real", user="admin")])
    status, called = await _call(
        auth, "http://127.0.0.1:8200/mcp", method="POST",
        headers=[(b"authorization", b"Bearer real")],
    )
    assert called, "绝对形式 target 没被归一化 —— MCP 会 404"
    assert status != 404, f"绝对形式 target 被当成未知路径（{status}）"


@pytest.mark.asyncio
async def test_absolute_form_target_normalized_before_auth() -> None:
    """归一化必须发生在鉴权判断之前 —— 否则鉴权会把 /mcp 当成未知路径。

    反例形态：scope["path"] 还是完整 URL → 鉴权/路由都匹配不上。
    """
    auth = Auth(bind_host="0.0.0.0", tokens=[Token(token="real", user="admin")])
    # 无凭据 → 401（证明走到了鉴权，而不是 404"路径不存在"）
    status, _ = await _call(auth, "http://127.0.0.1:8200/api/state")
    assert status == 401, (
        f"绝对形式 target 下数据接口返回 {status}（应为 401）—— "
        "归一化没发生在鉴权之前"
    )


# ══════════════════════ 4. health 真正接入业务判定 ══════════════════════


def test_tenant_health_aggregates_real_service_checks(tmp_path: Path) -> None:
    """health 不是永远 ok 的 stub；必须聚合真实库的 Service.health()。"""
    from shared_memory.tenancy import TenantHub

    hub = TenantHub(tmp_path / "home")
    hub.bootstrap()
    result = hub.health("admin")
    assert result["ok"] is True
    assert result["libs"] >= 1
    assert result["notes"] == 0
    assert result["problems"] == []
    assert result["queue"]["max_depth"] > 0
    assert "home" not in result, "健康端点不应泄露服务端绝对路径"


def test_loopback_health_aggregates_all_libraries(tmp_path: Path) -> None:
    """loopback 探针没有用户身份时，汇总范围是本机全部库。"""
    from shared_memory.tenancy import TenantHub

    hub = TenantHub(tmp_path / "home")
    hub.bootstrap()
    result = hub.health("")
    assert result["ok"] is True
    assert result["user"] == ""
    assert result["libs"] == len(hub.store.all_libs())


# ══════════════════════ 5. token 持久化 ══════════════════════


def test_tokens_persist_across_restart(tmp_path: Path) -> None:
    """⚠️ 面板发的 token 必须**重启后仍有效**。

    原来 token 只存内存 —— 服务一重启全部失效，症状是
    "昨天还好好的，今天所有 agent 都 401"。
    """
    path = tmp_path / "state" / "tokens.json"

    # 第一次启动：发一把 token，落盘
    a1 = Auth(bind_host="0.0.0.0", store=None)
    a1.attach_persistence(path)
    tok = a1.add_token(user="admin", label="笔记本", scope="readwrite")
    assert path.exists(), "token 没落盘"
    assert a1.resolve(tok.token)[0] == "admin"

    # 第二次启动（新进程）：从文件加载，旧 token 仍可用
    a2 = Auth(bind_host="0.0.0.0", store=None)
    loaded = a2.attach_persistence(path)
    assert loaded == 1, f"重启后加载了 {loaded} 把 token（应为 1）"
    assert a2.resolve(tok.token)[0] == "admin", "重启后旧 token 失效了"


def test_revoked_token_stays_revoked_after_restart(tmp_path: Path) -> None:
    """吊销必须持久 —— 否则"离职吊销"重启一次就复活，是安全洞。"""
    path = tmp_path / "state" / "tokens.json"

    a1 = Auth(bind_host="0.0.0.0")
    a1.attach_persistence(path)
    tok = a1.add_token(user="bob", label="离职", scope="readwrite")
    assert a1.revoke_token("离职") == 1

    a2 = Auth(bind_host="0.0.0.0")
    a2.attach_persistence(path)
    assert a2.resolve(tok.token)[0] == "", "已吊销的 token 重启后复活了！"


def test_readonly_scope_survives_restart(tmp_path: Path) -> None:
    """⚠️ 只读凭据重启后必须**仍是只读**。

    这是权限提升漏洞的回归：如果持久化丢了 scope，只读 token
    重启后变成可写 —— 实测踩过的同类问题（token 文件缺 scope 段）。
    """
    path = tmp_path / "state" / "tokens.json"

    a1 = Auth(bind_host="0.0.0.0")
    a1.attach_persistence(path)
    tok = a1.add_token(user="intern", label="实习生", scope="read")

    a2 = Auth(bind_host="0.0.0.0")
    a2.attach_persistence(path)
    user, scope = a2.resolve(tok.token)
    assert user == "intern"
    assert scope == "read", f"只读凭据重启后变成了 {scope!r}（权限提升！）"


def test_no_persistence_path_means_memory_only(tmp_path: Path) -> None:
    """没挂持久化路径时不能崩（纯内存模式仍要能用）。"""
    a = Auth(bind_host="0.0.0.0")
    tok = a.add_token(user="x", label="t")
    assert a.resolve(tok.token)[0] == "x"
    assert a.persist_path is None


def test_corrupt_token_file_fails_closed(tmp_path: Path) -> None:
    """坏 token 文件必须阻止启动，不能按空状态继续并覆盖原凭据。

    把损坏文件当空继续运行，随后自动发 token 会覆盖旧文件：既丢失旧凭据，
    又让用户误以为原来的 token 仍有效。安全做法是失败关闭并给修复指引。
    """
    path = tmp_path / "state" / "tokens.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    original = "{ 这不是合法 json"
    path.write_text(original, encoding="utf-8")

    a = Auth(bind_host="0.0.0.0")
    with pytest.raises(RuntimeError, match="拒绝启动"):
        a.attach_persistence(path)
    assert path.read_text(encoding="utf-8") == original, "坏文件被覆盖了！"


def _write_token_state(path: Path, *, tokens: list | None = None,
                      revoked_external: list | None = None) -> bytes:
    payload = {
        "schema": "shared-memory-tokens/1",
        "tokens": tokens or [],
        "revoked_external": revoked_external or [],
    }
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw


def test_malformed_external_tombstone_fails_closed_without_partial_load(
    tmp_path: Path,
) -> None:
    """坏吊销墓碑不能被跳过后启动；也不能部分加载前面的有效 token。"""
    path = tmp_path / "state" / "tokens.json"
    good_managed = {
        "token": "persisted-good", "user": "admin", "label": "managed",
        "scope": "readwrite", "created": 1, "last_used": 0,
        "revoked_at": None, "managed": True,
    }
    # 没 revoked_at 的墓碑不能被静默丢弃；那可能让已吊销的环境 token 复活。
    broken_tombstone = {
        "token": "env-revoked", "user": "admin", "label": "external",
        "scope": "readwrite",
    }
    original = _write_token_state(
        path, tokens=[good_managed], revoked_external=[broken_tombstone]
    )
    auth = Auth(
        bind_host="0.0.0.0",
        tokens=[Token(token="env-revoked", user="admin", label="external", managed=False)],
    )

    with pytest.raises(RuntimeError, match="未加载任何 token"):
        auth.attach_persistence(path)

    assert path.read_bytes() == original, "拒绝启动时原凭据文件被改写了"
    assert auth.resolve("persisted-good")[0] == "", "解析失败前部分加载了 managed token"
    # 进程正常流程必须终止；原本已注入的凭据对象未被 attach 部分改动。
    assert auth.resolve("env-revoked")[0] == "admin"


def test_managed_must_be_boolean_and_cannot_coerce_false_string(tmp_path: Path) -> None:
    """managed='false' 不能被 bool() 强转成 True 并加载为长期 token。"""
    path = tmp_path / "state" / "tokens.json"
    forged = {
        "token": "not-a-managed-token", "user": "admin", "label": "external",
        "scope": "readwrite", "created": 1, "last_used": 0,
        "revoked_at": None, "managed": "false",
    }
    original = _write_token_state(path, tokens=[forged])
    auth = Auth(bind_host="0.0.0.0")

    with pytest.raises(RuntimeError, match="managed 必须是布尔值"):
        auth.attach_persistence(path)

    assert path.read_bytes() == original, "拒绝启动时原凭据文件被改写了"
    assert auth.resolve("not-a-managed-token")[0] == "", "错误类型的 managed token 被加载"


def test_malformed_token_record_is_atomic(tmp_path: Path) -> None:
    """任何坏 token 记录都应整文件失败关闭，而不是跳过后部分启动。"""
    path = tmp_path / "state" / "tokens.json"
    good = {
        "token": "good", "user": "admin", "label": "good", "scope": "read",
        "created": 1, "last_used": 0, "revoked_at": None, "managed": True,
    }
    malformed = {"token": "bad", "user": "admin", "scope": "all"}
    original = _write_token_state(path, tokens=[good, malformed])
    auth = Auth(bind_host="0.0.0.0")

    with pytest.raises(RuntimeError, match="tokens\\[1\\].scope 非法"):
        auth.attach_persistence(path)

    assert path.read_bytes() == original
    assert auth.resolve("good")[0] == "", "坏文件前面的记录被部分加载"


def test_env_token_revocation_survives_restart(tmp_path: Path) -> None:
    """来自环境文件的 token 被吊销后，重启不能因环境重新注入而复活。"""
    path = tmp_path / "state" / "tokens.json"
    env_token = Token(token="env-secret", user="admin", label="env-admin", managed=False)

    first = Auth(bind_host="0.0.0.0", tokens=[env_token])
    first.attach_persistence(path)
    assert first.revoke_token("env-admin") == 1
    assert first.resolve("env-secret")[0] == ""
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["tokens"] == [], "环境 token 不应作为 active 凭据持久化"
    assert saved["revoked_external"][0]["token"] == "env-secret"

    # 模拟重启：环境文件再次注入同一条 active token。
    restarted = Auth(
        bind_host="0.0.0.0",
        tokens=[Token(token="env-secret", user="admin", label="env-admin", managed=False)],
    )
    restarted.attach_persistence(path)
    assert restarted.resolve("env-secret")[0] == "", (
        "已吊销的环境 token 因重启重新注入而复活"
    )


def test_removed_env_token_is_not_pinned_by_persistence(tmp_path: Path) -> None:
    """删掉环境配置后，active env token 不应因 state 文件里副本而继续可用。"""
    path = tmp_path / "state" / "tokens.json"
    first = Auth(
        bind_host="0.0.0.0",
        tokens=[Token(token="temporary-env-secret", user="admin", label="env", managed=False)],
    )
    first.attach_persistence(path)
    first.add_token(user="admin", label="panel-created")  # 写快照
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert all(t["token"] != "temporary-env-secret" for t in saved["tokens"])

    restarted = Auth(bind_host="0.0.0.0")  # 环境配置已移除
    restarted.attach_persistence(path)
    assert restarted.resolve("temporary-env-secret")[0] == ""
    assert restarted.resolve(saved["tokens"][0]["token"])[0] == "admin"


def test_persist_failure_rolls_back_new_token(tmp_path: Path) -> None:
    """写入失败时不能发出未持久化 token —— 否则重启后静默失效。"""
    path = tmp_path / "state" / "tokens.json"
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    path = blocker / "nested" / "tokens.json"
    a = Auth(bind_host="0.0.0.0")
    a.attach_persistence(path)
    with pytest.raises(OSError, match="无法持久化"):
        a.add_token(user="admin", label="not-issued")
    assert not any(x["label"] == "not-issued" for x in a.list_tokens())


def test_revoke_failure_rolls_back_active_token(tmp_path: Path) -> None:
    """持久化吊销失败时必须回滚，不能只在内存吊销却重启复活。"""
    path = tmp_path / "state" / "tokens.json"
    a = Auth(bind_host="0.0.0.0")
    a.attach_persistence(path)
    tok = a.add_token(user="admin", label="cannot-revoke")
    # 持久化路径临时不可写：替换成指向不存在父目录下的目标
    a.persist_path = tmp_path / "missing" / "nested" / "tokens.json"
    # 持久化仍可能创建目录成功，所以令 parent 路径是文件
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    a.persist_path = blocker / "tokens.json"
    with pytest.raises(OSError, match="吊销状态"):
        a.revoke_token("cannot-revoke")
    assert a.resolve(tok.token)[0] == "admin", "吊销失败但 token 仍被内存吊销"


def test_external_token_cannot_be_rotated_to_ephemeral_credential(tmp_path: Path) -> None:
    """环境 token 不能被轮换成未写回环境配置、重启就失效的新 token。"""
    path = tmp_path / "state" / "tokens.json"
    external = Token(
        token="env-source-token", user="admin", label="deployed", managed=False
    )
    auth = Auth(bind_host="0.0.0.0", tokens=[external])
    auth.attach_persistence(path)

    with pytest.raises(ValueError, match="来自环境配置或临时会话"):
        auth.rotate_token("deployed")

    assert auth.resolve("env-source-token")[0] == "admin"
    assert not path.exists(), "拒绝轮换外部凭据不应落盘任何 token"


def test_browser_sessions_are_not_persisted_as_long_lived_tokens(tmp_path: Path) -> None:
    """临时浏览器会话不能写进长期 token 文件，也不应允许轮换。"""
    path = tmp_path / "state" / "tokens.json"
    auth = Auth(bind_host="0.0.0.0")
    auth.attach_persistence(path)
    session = auth.make_session("admin")
    assert auth.resolve(session)[0] == "admin"

    # 下一次有持久化动作时只能保存 managed 长期凭据，不能把 session pin 住。
    managed = auth.add_token(user="admin", label="long-lived")
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved_values = {t["token"] for t in saved["tokens"]}
    assert managed.token in saved_values
    assert session not in saved_values

    with pytest.raises(ValueError, match="来自环境配置或临时会话"):
        auth.rotate_token(f"session:{'admin'[:8]}")

    restarted = Auth(bind_host="0.0.0.0")
    restarted.attach_persistence(path)
    assert restarted.resolve(session)[0] == ""
    assert restarted.resolve(managed.token)[0] == "admin"


def test_rotate_is_atomic_and_persistent(tmp_path: Path) -> None:
    """轮换要原子持久：重启后旧的吊销、新的有效。"""
    path = tmp_path / "state" / "tokens.json"
    a1 = Auth(bind_host="0.0.0.0")
    a1.attach_persistence(path)
    old = a1.add_token(user="admin", label="rotate", scope="read")
    rotated = a1.rotate_token("rotate")
    assert rotated is not None
    _, new_value = rotated
    assert a1.resolve(old.token)[0] == ""
    assert a1.resolve(new_value) == ("admin", "read")

    a2 = Auth(bind_host="0.0.0.0")
    a2.attach_persistence(path)
    assert a2.resolve(old.token)[0] == ""
    assert a2.resolve(new_value) == ("admin", "read")


def test_token_file_has_no_duplicate_after_reload(tmp_path: Path) -> None:
    """重复加载不能把同一条 token 灌两遍（否则 list 里出现幽灵条目）。"""
    path = tmp_path / "state" / "tokens.json"
    a1 = Auth(bind_host="0.0.0.0")
    a1.attach_persistence(path)
    a1.add_token(user="admin", label="t")

    a2 = Auth(bind_host="0.0.0.0")
    a2.attach_persistence(path)
    a2.attach_persistence(path)  # 再挂一次
    assert len(a2.list_tokens()) == 1, f"重复加载：{len(a2.list_tokens())} 条"


def test_token_file_is_valid_json_with_schema(tmp_path: Path) -> None:
    """落盘文件带 schema 版本号 —— 将来格式演进能识别。"""
    path = tmp_path / "state" / "tokens.json"
    a = Auth(bind_host="0.0.0.0")
    a.attach_persistence(path)
    a.add_token(user="admin", label="t")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema"] == "shared-memory-tokens/1"
    assert isinstance(data["tokens"], list)
