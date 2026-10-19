"""备份列表摘要（`/api/backups` 的 count / size_kb）。

━━━ 为什么单独立一个文件 ━━━

这两个字段曾是**静默错误**：接口 200、界面正常渲染，只是数字永远是
「0 条 · 4.0 KB」。用户看到的是"备份没成功"，实际包是好的 ——
比报错更糟，因为它会引导人反复重做同一件事。

根因两个：
  · count 读 ``manifest["notes"]``，而导出写的是 ``manifest["entries"]``
  · size 取目录的 ``st_size``（inode 大小，Linux 上恒 4096）

这里钉死正确形态，并保留对早期只写 ``notes`` 的包的兼容。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shared_memory.dashboard import bundle_stats  # noqa: E402


def _bundle(root: Path, entries: list[dict], *, key: str = "entries",
            files: dict[str, bytes] | None = None) -> Path:
    """造一个最小 bundle 目录。"""
    b = root / "all-20261019T120000"
    (b / "libs" / "demo" / "notes").mkdir(parents=True)
    (b / "manifest.json").write_text(
        json.dumps({"schema": "shared-memory-bundle/1", key: entries},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    for name, data in (files or {"libs/demo/notes/a.md": b"# a\n"}).items():
        p = b / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return b


def test_count_reads_entries_key(tmp_path: Path) -> None:
    """导出写的是 entries —— 列表必须按它计数。"""
    b = _bundle(tmp_path, [{"id": f"m{i}"} for i in range(7)])
    count, _ = bundle_stats(b)
    assert count == 7


def test_count_falls_back_to_legacy_notes_key(tmp_path: Path) -> None:
    """早期包只有 notes 键，不能因此显示 0。"""
    b = _bundle(tmp_path, [{"id": "old1"}, {"id": "old2"}], key="notes")
    count, _ = bundle_stats(b)
    assert count == 2


def test_broken_manifest_reports_unknown_not_zero(tmp_path: Path) -> None:
    """manifest 坏掉时返回 None（界面显示 ?），不冒充 0 条。"""
    b = tmp_path / "all-broken"
    b.mkdir()
    (b / "manifest.json").write_text("{not json", encoding="utf-8")
    count, size_kb = bundle_stats(b)
    assert count is None
    assert size_kb >= 0


def test_size_is_file_bytes_not_directory_inode(tmp_path: Path) -> None:
    """大小必须是包内文件字节和；目录 st_size 在 Linux 上恒 4096（=4.0KB）。"""
    payload = b"x" * 50_000
    b = _bundle(
        tmp_path,
        [{"id": "m1"}],
        files={
            "libs/demo/notes/a.md": payload,
            "libs/demo/notes/sub/b.md": b"y" * 30_000,
        },
    )
    _, size_kb = bundle_stats(b)
    assert size_kb >= 78.0, f"只算了目录 inode？得到 {size_kb} KB"
    assert size_kb != 4.0
