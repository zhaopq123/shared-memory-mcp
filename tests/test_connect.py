"""接入零门槛（v2 · S-5）—— 接入码解析 + connect 脚本。

━━━ 为什么脚本也要测 ━━━

`connect.py` 会**改写用户的 MCP 配置文件**。这类脚本出错的代价很高
（把别人配好的 `mcpServers` 写没、把好文件写成坏 JSON），
而它平时没人盯着看。所以用子进程真跑一遍：干净目录 → 写入 → 重复跑 → 报错路径。

接入码的语法归 `shared_memory.config.parse_connect_uri`（单一真相源），
脚本只是调用它 —— 所以语法用例在 test_memory.py，这里测**文件行为**。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "connect.py"


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )


def test_connect_writes_entry_into_clean_dir(tmp_path: Path) -> None:
    """干净目录跑一次就接上了（写 .mcp.json）。"""
    r = run("sm://10.1.2.3:8200?k=SECRET", "--project-dir", str(tmp_path))
    assert r.returncode == 0, r.stderr + r.stdout

    data = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    entry = data["mcpServers"]["shared-memory"]
    assert entry["url"] == "http://10.1.2.3:8200/mcp"
    assert entry["headers"]["Authorization"] == "Bearer SECRET"
    assert entry["type"] == "streamableHttp"


def test_connect_is_idempotent_and_keeps_other_servers(tmp_path: Path) -> None:
    """重复跑不重写；同文件里**别人的服务**一个都不许动。"""
    cfg = tmp_path / ".mcp.json"
    cfg.write_text(json.dumps({
        "mcpServers": {"别的服务": {"url": "http://x/mcp", "note": "keep me"}}
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    r1 = run("sm://10.1.2.3:8200?k=A", "--project-dir", str(tmp_path))
    assert r1.returncode == 0
    text1 = cfg.read_text(encoding="utf-8")

    r2 = run("sm://10.1.2.3:8200?k=B", "--project-dir", str(tmp_path))
    assert r2.returncode == 0
    assert "已配置过" in r2.stdout
    assert cfg.read_text(encoding="utf-8") == text1, "重复跑不该改动文件"

    data = json.loads(text1)
    assert data["mcpServers"]["别的服务"]["note"] == "keep me"
    assert data["mcpServers"]["shared-memory"]["headers"]["Authorization"] == "Bearer A"


def test_connect_dry_run_does_not_write(tmp_path: Path) -> None:
    """--dry-run 只打印，不落盘。"""
    r = run("sm://10.1.2.3:8200?k=A", "--project-dir", str(tmp_path), "--dry-run")
    assert r.returncode == 0
    assert "dry-run" in r.stdout
    assert not (tmp_path / ".mcp.json").exists()


def test_connect_refuses_to_clobber_broken_json(tmp_path: Path) -> None:
    """目标文件是坏 JSON 时**停下报错**，不擅自覆盖（可能有人手写过）。"""
    cfg = tmp_path / ".mcp.json"
    cfg.write_text("{ 这不是 JSON", encoding="utf-8")
    r = run("sm://10.1.2.3:8200?k=A", "--project-dir", str(tmp_path))
    assert r.returncode != 0
    assert "不是合法 JSON" in r.stdout + r.stderr
    assert cfg.read_text(encoding="utf-8") == "{ 这不是 JSON"


def test_connect_reports_bad_code_readably(tmp_path: Path) -> None:
    """非法接入码要**指明哪一段不对**（不是笼统的"参数错误"）。"""
    r = run("http://10.1.2.3:8200?k=A", "--project-dir", str(tmp_path))
    assert r.returncode == 2
    assert "协议头不对" in r.stdout
    assert not (tmp_path / ".mcp.json").exists()


def test_connect_force_overwrites(tmp_path: Path) -> None:
    """显式 --force 才覆盖（把"无意覆盖"和"有意重配"分开）。"""
    run("sm://10.1.2.3:8200?k=OLD", "--project-dir", str(tmp_path))
    r = run("sm://10.1.2.3:8200?k=NEW", "--project-dir", str(tmp_path), "--force")
    assert r.returncode == 0 and "已接入" in r.stdout
    data = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["shared-memory"]["headers"]["Authorization"] == "Bearer NEW"
