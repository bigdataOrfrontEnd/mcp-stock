"""P0-② 可评估上下文：recommendations.run_id / risk_flags 落表与回读（v8）。"""
from __future__ import annotations

import json


def _seed_run(db, strategy="shrink_pullback"):
    return db.insert_funnel_run(
        strategy, "2026-11-03", "L2", 5223, 271,
        {"L1_screen_top": 10, "final": 2},
        [{"code": "603009", "name": "北特科技", "trend_score": 81.2}],
        [])


def test_recommendation_with_run_context(tmp_db):
    db = tmp_db
    db.init_db()
    fid = _seed_run(db)
    rid = db.insert_recommendation(
        "603009", "北特科技", "缩量回踩低吸", "汽车零部件", "低吸",
        47.16, 5, 4, "shrink_pullback",
        run_id=fid, risk_flags=json.dumps(["高换手", "量比过热"]))
    # 回读：open 列表带上运行上下文
    rows = db.recommendations_open()
    assert rows[0]["id"] == rid
    assert rows[0]["run_id"] == fid
    assert rows[0]["risk_flags"] == ["高换手", "量比过热"]
    # decision_history 同样带出（证据链闭合的回溯入口）
    h = db.decision_history("603009")
    assert h["decisions"][0]["run_id"] == fid
    assert h["decisions"][0]["risk_flags"] == ["高换手", "量比过热"]
    # run_id 指向的漏斗运行确实存在且是那条策略（join 一致性）
    runs = db.query_funnel_runs(strategy="shrink_pullback")
    assert runs[0]["id"] == fid
    assert runs[0]["candidates"][0]["code"] == "603009"


def test_recommendation_without_run_context_backward_compat(tmp_db):
    db = tmp_db
    db.init_db()
    db.insert_recommendation("600519", "贵州茅台", "旧格式推荐", "白酒", "趋势",
                             100.0, 5, 3, "v1")
    rows = db.recommendations_open()
    assert rows[0]["run_id"] is None
    assert rows[0]["risk_flags"] is None
    h = db.decision_history("600519")
    assert h["decisions"][0]["run_id"] is None
    assert h["decisions"][0]["risk_flags"] is None
