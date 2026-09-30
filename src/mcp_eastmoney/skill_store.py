"""报告归档 — 生成的报告存到 data/reports/（文件存储，非数据库）。

模板本体已迁移至 DSH skill 层（deepseek-harness/scratch-plugin/skills/），
本模块只负责报告归档与列举。
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_REPORT_DIR = Path(os.environ.get(
    "STOCK_REPORT_DIR", str(_PROJECT_ROOT / "data" / "reports")))


def save_report(rtype: str, content: str) -> dict:
    """归档生成的报告到 data/reports/YYYY-MM-DD-<type>.md。"""
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{datetime.now().strftime('%Y-%m-%d')}-{rtype}.md"
    p = _REPORT_DIR / fname
    p.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(p), "type": rtype}


def list_reports(limit: int = 10) -> list[dict]:
    """列出已归档的报告（按文件名倒序 = 最新在前）。"""
    if not _REPORT_DIR.exists():
        return []
    out = []
    for p in sorted(_REPORT_DIR.glob("*.md"), reverse=True)[:limit]:
        out.append({"file": p.name, "size": p.stat().st_size})
    return out
