"""全策略批量扫描（无跨策略聚合）：regime 适配标注 + funnel_runs 存档回溯。"""
from __future__ import annotations


def _cands(codes):
    return [{"code": c, "name": f"股{c}", "close": 10.0,
             "trend_score": 70.0, "trend_grade": "B",
             "snapshot": {"change_pct": 3.0}} for c in codes]


def test_strategy_fits_today_regime_normalization():
    from mcp_eastmoney.recommend.scan_all import strategy_fits_today
    # 归一映射：rotation≈neutral、range_bound≈risk_off
    assert strategy_fits_today(["risk_on"], "risk_on") is True
    assert strategy_fits_today(["risk_on"], "risk_off") is False
    assert strategy_fits_today(["rotation"], "neutral") is True
    assert strategy_fits_today(["range_bound"], "risk_off") is True
    assert strategy_fits_today(["range_bound"], "neutral") is False
    assert strategy_fits_today(["neutral", "risk_on"], "neutral") is True
    # 无情绪数据 → 不做判断
    assert strategy_fits_today(["risk_on"], None) is True
    assert strategy_fits_today([], None) is True


def test_no_aggregate_layer():
    """聚合层已移除：模块与跨策略合并函数不存在。"""
    import importlib.util
    assert importlib.util.find_spec("mcp_eastmoney.recommend.aggregate") is None
    from mcp_eastmoney.recommend import scan_all
    assert not hasattr(scan_all, "merge_by_family")
    assert not hasattr(scan_all, "merge_candidates")


def test_funnel_runs_roundtrip(tmp_db):
    db = tmp_db
    fid = db.insert_funnel_run(
        "volume_breakout", "2026-11-03", "L2", 5223, 156,
        {"L1_screen_top": 10, "L2_trend_out": 3, "final": 6},
        _cands(["600001", "600002"]),
        [{"code": "600009", "stage": "L2", "reason": "趋势分 40 < 65"}])
    runs = db.query_funnel_runs(strategy="volume_breakout")
    assert runs[0]["id"] == fid
    assert runs[0]["stages"]["final"] == 6
    assert runs[0]["candidates"][0]["code"] == "600001"
    assert runs[0]["excluded"][0]["stage"] == "L2"
    # 按日期过滤（run_date 由 insert 时取当天，不接收参数）
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    assert db.query_funnel_runs(run_date=today)
    assert not db.query_funnel_runs(run_date="2020-01-01")
