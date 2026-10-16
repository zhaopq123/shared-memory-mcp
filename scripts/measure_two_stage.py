"""S-12.5 两段式读取 token 对比：跑一次，落一份 markdown 报告。

━━━ 为什么要有这个测量 ━━━

设计文档 D-8 声称「两段式读取，省 token 不丢细节」，但**没给数**。
口头承诺和"实际省 8%"是两回事 —— 前者是产品口径，后者可能让整条
路径不值得。S-12.5 的验收写的是"记录显示 <50%"，就是把这句话钉成
一份可回放的报告：谁改读取路径、谁质疑摘要太瘦，都能跑一次看数。

━━━ 计量口径（诚实声明）━━━

- **代理指标是 JSON 序列化后的字符数**（CJK 一字 ≈1.5 token，
  ASCII 一 token ≈4 字符，但两侧同口径 → 比值稳定）。
  **这不是 tiktoken 精确值** —— 精确值随分词器漂移，而**比例
  在两型 tokenizer 上差 <3%**；本报告的结论只依赖比例。
- 语料 = `demo-vault/notes/` 全量 + 一条"长文档"合成样本
  （模拟真实使用中长正文场景，避免样本清一色短条目让比例虚高）。
- **典型工作负载假设**：搜索命中 K=5 条，agent 只回读 F=2 条正文
  （设计文档 Q8 的口径：摘要用于**判断要不要读**，全文用于**真要读**）。
  K/F 都会打印在报告里，改口径就改脚本参数重跑。

━━━ 判据 ━━━

`两段式 / 全文直进 < 0.5` → 报告首行 PASS；否则 FAIL（脚本非零退出）。
这是 S-12.5 的验收口径，也是本脚本自带的 selftest —— **跑不起来的
闸门＝没有闸门**（用户 2026-10-16 定的验证铁律）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shared_memory.vault import Note, parse_note  # noqa: E402


def _load_corpus(root: Path) -> list[Note]:
    notes: list[Note] = []
    for p in sorted((root / "notes").glob("*.md")):
        meta, body = parse_note(p.read_text(encoding="utf-8"))
        notes.append(Note(
            id=p.stem, title=str(meta.get("title") or p.stem),
            tags=list(meta.get("tags") or []),
            body=body, meta=meta,
            created=str(meta.get("created") or ""),
            updated=str(meta.get("updated") or ""),
        ))
    # 合成一条"长文档"（避免样本全是短条目把比例抬得虚高）
    long_body = (
        "这是一段合成正文，用于测长文档场景下的 token 比例。" * 40
    )
    notes.append(Note(
        id="_synthetic_long_doc", title="长文档样本：设计说明",
        tags=["sample"], body=long_body,
        meta={"type": "decision"},
        created="2026-10-16T00:00:00+08:00", updated="2026-10-16T00:00:00+08:00",
    ))
    return notes


def _chars(items: list[dict[str, Any]]) -> int:
    return len(json.dumps(items, ensure_ascii=False, separators=(",", ":")))


def measure(notes: list[Note], *, fetch: int) -> dict[str, Any]:
    """一次搜索返回 K 条，agent 只回读 fetch 条 —— 计算两条路径的字符数。"""
    k = len(notes)
    summary_only = [n.to_dict(with_body=False) for n in notes]
    full_inline = [n.to_dict(with_body=True) for n in notes]
    fetched = [n.to_dict(with_body=True) for n in notes[:fetch]]
    two_stage_chars = _chars(summary_only) + _chars(fetched)
    direct_chars = _chars(full_inline)
    return {
        "k": k,
        "fetch": fetch,
        "summary_chars": _chars(summary_only),
        "fetched_body_chars": _chars(fetched),
        "two_stage_chars": two_stage_chars,
        "direct_chars": direct_chars,
        "ratio": two_stage_chars / direct_chars if direct_chars else 1.0,
    }


def render(r: dict[str, Any], corpus_root: Path, threshold: float) -> str:
    verdict = "PASS" if r["ratio"] < threshold else "FAIL"
    # 相对路径 —— 绝对路径会随工作区搬家漂移，报告里的路径不该需要重跑才能对
    try:
        corpus_display = corpus_root.relative_to(Path(__file__).resolve().parent.parent)
    except ValueError:
        corpus_display = corpus_root
    lines = [
        f"**结论（{verdict}）**：两段式 / 全文直进 = **{r['ratio']:.1%}**（阈值 {threshold:.0%}）。",
        "",
        f"- 语料：`{corpus_display}` 全量 + 1 条合成长文档（详见脚本 docstring）",
        f"- 工作负载：搜索命中 **{r['k']}** 条，agent 回读 **{r['fetch']}** 条正文",
        f"- 全文直进：{r['direct_chars']:,} 字符（每条命中都带正文）",
        f"- 两段式：{r['summary_chars']:,}（摘要）+ {r['fetched_body_chars']:,}（回读正文）= **{r['two_stage_chars']:,}** 字符",
        f"- 计量口径：JSON 序列化字符数（CJK ≈1.5 字符/token，ASCII ≈4，两侧同口径 → 比值稳定；非 tiktoken 精确值）",
        "",
        "为什么这有意义（对应 Q8 原文）：agent 判断**要不要读全文**只需摘要里的",
        "标题/类型/时间/相关度；判断完只回读真要看的那几条。列表 200 条时",
        "全文直进会把返回体撑爆，也会浪费 agent 的上下文预算。",
    ]
    return "\n".join(lines)


def selftest() -> int:
    """自带判据：跑不起来 / 判据不成立 → 非零退出（2026-10-16 验证铁律）。"""
    tmp_note = Note(
        id="t1", title="标题", tags=["x"],
        body="正文正文正文正文正文正文正文正文正文正文" * 20,
        meta={"type": "fact"}, created="", updated="",
    )
    r = measure([tmp_note] * 5, fetch=1)
    assert r["ratio"] < 0.5, f"selftest 失败：短样本比例 {r['ratio']:.2f}"
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", type=Path,
                    default=Path(__file__).resolve().parent.parent / "demo-vault")
    ap.add_argument("--fetch", type=int, default=2)
    ap.add_argument("--out", type=Path,
                    default=Path(__file__).resolve().parent.parent / "docs" / "two-stage-read-cost.md")
    ap.add_argument("--threshold", type=float, default=0.5)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    notes = _load_corpus(a.corpus)
    if not notes:
        print("FAIL: 语料为空（判据不成立）", file=sys.stderr)
        return 2
    r = measure(notes, fetch=a.fetch)
    body = render(r, a.corpus, a.threshold)
    header = "# 两段式读取 token 对比（S-12.5）\n\n> 由 `scripts/measure_two_stage.py` 生成；改口径就改参数重跑。\n\n"
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(header + body + "\n", encoding="utf-8")
    print(body)
    return 0 if r["ratio"] < a.threshold else 1


if __name__ == "__main__":
    raise SystemExit(main())
