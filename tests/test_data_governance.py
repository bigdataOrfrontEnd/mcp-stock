"""数据治理：schema 版本化 / 策略注册表 / 参数快照 / α 落库 / 单写入口。"""
from __future__ import annotations

import json
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "mcp_eastmoney"


# ---- 1. 单写入口：INSERT/UPDATE/DELETE 只允许出现在 data/db.py -----------------------
WRITE_SQL = re.compile(
    r"\.\s*(?:execute|executemany|executescript)\s*\(\s*[f]?[\"'](?:[^\"']|\n)*?"
    r"\b(?:INSERT|UPDATE|DELETE|REPLACE)\b", re.IGNORECASE)


def test_single_write_entry_db_only():
    offenders = []
    for p in SRC.rglob("*.py"):
        if str(p).endswith("data/db.py"):
            continue
        code = p.read_text(encoding="utf-8")
        for m in WRITE_SQL.finditer(code):
            offenders.append(f"{p.name}: {m.group(0)[:60]!r}")
    assert not offenders, f"发现绕过 db.py 的直接写 SQL：{offenders}"


# ---- 2. schema 版本化迁移 ------------------------------------------------------------
def test_schema_version_migration(tmp_db):
    db = tmp_db
    db.init_db()
    v = db._conn().execute("PRAGMA user_version").fetchone()[0]
    assert v == db.SCHEMA_VERSION >= 2
    cols = [r[1] for r in db._conn().execute("PRAGMA table_info(recommendations)")]
    assert "strategy_def" in cols
    cols_r = [r[1] for r in db._conn().execute("PRAGMA table_info(recommendation_reviews)")]
    assert "alpha_pct" in cols_r
    tables = {r[0] for r in db._conn().execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "strategies" in tables
    # 幂等：重复 init 不炸、版本不变
    db.init_db()
    assert db._conn().execute("PRAGMA user_version").fetchone()[0] == v


# ---- 3. 策略注册表同步 ---------------------------------------------------------------
def test_strategy_registry_sync(tmp_db, monkeypatch):
    db = tmp_db
    db.init_db()
    import tempfile, os
    from mcp_eastmoney.recommend import screener
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "test_strat.yaml").write_text(
            "name: test_strat\nversion: 2.1\ndisplay_name: 测试\n"
            "hard_filters: {amount_min: 500000000}\n", encoding="utf-8")
        monkeypatch.setenv("STOCK_STRATEGY_DIR", d)
        import importlib
        importlib.reload(screener)  # _USER_DIR 在模块导入时读取，需重载
        try:
            loaded = screener.load_strategies()
            assert "test_strat" in loaded
            reg = {s["name"]: s for s in db.strategy_registry()}
            assert reg["test_strat"]["version"] == "2.1"
            assert reg["test_strat"]["source"] == "user"
            assert reg["test_strat"]["definition"]["hard_filters"]["amount_min"] == 5e8
            # 内置策略也注册了
            assert any(s["source"] == "packaged" for s in db.strategy_registry())
        finally:
            monkeypatch.delenv("STOCK_STRATEGY_DIR", raising=False)
            importlib.reload(screener)  # 恢复默认用户策略目录，避免污染后续测试


# ---- 4. 推荐参数快照 + α 落库 + stats 输出 --------------------------------------------
def test_recommendation_snapshot_and_alpha(tmp_db):
    db = tmp_db
    db.init_db()
    db.upsert_index_daily("sh.000001", "上证指数", "broad", [
        {"date": "2026-02-02", "open": 1, "high": 1, "low": 1, "close": 3000,
         "volume": 1, "amount": 1, "change_pct": 0.0},
        {"date": "2026-02-09", "open": 1, "high": 1, "low": 1, "close": 3060,
         "volume": 1, "amount": 1, "change_pct": 0.5}])
    snap = json.dumps({"name": "t", "hard_filters": {"amount_min": 1}}, ensure_ascii=False)
    rid = db.insert_recommendation("000002", "万科A", "测试", "地产", "趋势",
                                   10.0, 5, 3, "t", rec_date="2026-02-02",
                                   strategy_def=snap)
    row = db._conn().execute(
        "SELECT strategy_def FROM recommendations WHERE id=?", (rid,)).fetchone()
    assert json.loads(row[0])["hard_filters"]["amount_min"] == 1
    # review 带 α：个股 +5% vs 基准 +2% → α=3
    db.upsert_review(rid, "2026-02-09", 5, 10.5, 5.0, 6.0, -1.0, 1, "终评: 赢",
                     alpha_pct=3.0)
    stats = db.recommendation_stats("skill_version", days=365)
    assert stats[0]["avg_alpha_pct"] == 3.0
