"""get_decision_history：同票历史决策档案（盈亏 + 基准 alpha + 教训）。"""
from __future__ import annotations


def _seed(db):
    db.init_db()
    # 上证指数：rec 日收 3000，eval 日收 3090（+3%）
    db.upsert_index_daily("sh.000001", "上证指数", "broad", [
        {"date": "2026-01-05", "open": 3000, "high": 3010, "low": 2990,
         "close": 3000, "volume": 1, "amount": 1, "change_pct": 0.0},
        {"date": "2026-01-12", "open": 3080, "high": 3095, "low": 3070,
         "close": 3090, "volume": 1, "amount": 1, "change_pct": 1.0}])
    rid = db.insert_recommendation("600519", "贵州茅台", "放量突破", "白酒", "趋势",
                                   100.0, 5, 3, "volume_breakout")
    db._conn().execute(
        "INSERT INTO recommendation_reviews (rec_id, eval_date, days_held, close, "
        "pnl_pct, max_gain_pct, max_drawdown_pct, hit_target, final_note) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (rid, "2026-01-12", 5, 110.0, 10.0, 12.0, -2.0, 1, "顺势持有，断板未触发"),
    ).connection.commit()
    # 手动置为 final 并回写历史推荐日（alpha 计算需要 rec 日与 eval 日都有指数收盘）
    c = db._conn()
    c.execute("UPDATE recommendations SET status='final', rec_date='2026-01-05' WHERE id=?", (rid,))
    c.commit()
    c.close()
    return rid


def test_decision_history_pnl_alpha_lessons(tmp_db):
    _seed(tmp_db)
    h = tmp_db.decision_history("600519")
    assert h["code"] == "600519"
    assert h["count"] == 1
    s = h["summary"]
    assert s["evaluated"] == 1 and s["wins"] == 1 and s["win_rate_pct"] == 100.0
    # 个股 +10% vs 基准 +3% → alpha = +7
    assert h["decisions"][0]["alpha_pct"] == 7.0
    assert s["avg_alpha_pct"] == 7.0
    assert len(h["lessons"]) == 1 and "顺势持有" in h["lessons"][0]


def test_decision_history_empty(tmp_db):
    db = tmp_db
    db.init_db()
    h = db.decision_history("000001")
    assert h["count"] == 0
    assert h["summary"]["evaluated"] == 0
    assert h["summary"]["win_rate_pct"] is None
    assert h["decisions"] == []
