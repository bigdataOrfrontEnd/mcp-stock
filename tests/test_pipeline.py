"""推荐漏斗编排器测试（依赖注入，纯离线合成数据）。"""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from test_screener import _mk_rows, VB  # 复用合成截面

VB = {**VB, "name": "volume_breakout"}


def _bars_up(n=60, drift=0.8, wiggle=0.3):
    """强势上涨bars（trend_score 应得高分）。"""
    from test_factors_alerts_news import _mk_bars
    bars = _mk_bars(n, drift=drift, wiggle=wiggle)
    for b in bars:
        b["turnover_rate"] = 5.0
        b["volume"] = 20000.0
    return bars


def _bars_down(n=60):
    from test_factors_alerts_news import _mk_bars
    bars = _mk_bars(n, drift=-1.2, wiggle=0.5)
    for b in bars:
        b["turnover_rate"] = 2.0
    return bars


def _bars_yizi(n=60):
    """末尾一根一字板。"""
    bars = _bars_up(n)
    last = bars[-1]
    last.update({"open": last["close"], "high": last["close"], "low": last["close"],
                 "change_pct": 10.0})
    return bars


def test_pipeline_happy_path(tmp_db, monkeypatch):
    """强势候选通过全链，弱勢单被 L2 淘汰。"""
    from mcp_eastmoney.recommend import pipeline as P
    rows = _mk_rows()
    bars_map = {"600001": _bars_up(), "600006": _bars_down(), "600007": _bars_up()}
    # 600001 查财务预告 → db 无（不排雷）；600007 注入预减 → L3 淘汰
    r = P.run_pipeline("volume_breakout", top_n=3, rows=rows,
                       bars_map=bars_map,
                       forecasts={"600007": {"forecast_type": "预减", "change_pct": -60}})
    assert "error" not in r
    codes = [c["code"] for c in r["candidates"]]
    assert "600001" in codes and "600006" not in codes
    exc = {e["code"]: e for e in r["excluded"]}
    assert "600006" in exc and exc["600006"]["stage"] == "L2"
    assert "600007" in exc and exc["600007"]["stage"] == "L3"
    cand = r["candidates"][0]
    assert cand["trend_score"] >= 65
    assert "shares" in cand["position"] and cand["position"]["shares"] > 0
    assert cand["position"]["stop_loss"] < cand["close"]


def test_pipeline_yizi_excluded(tmp_db):
    """末尾一字板 → L3 淘汰。"""
    from mcp_eastmoney.recommend import pipeline as P
    bars_map = {"600001": _bars_yizi()}
    r = P.run_pipeline("volume_breakout", top_n=3, rows=_mk_rows(), bars_map=bars_map)
    exc = {e["code"]: e for e in r["excluded"]}
    assert exc.get("600001", {}).get("stage") == "L3"
    assert "一字板" in exc["600001"]["reason"]


def test_pipeline_insufficient_bars(tmp_db):
    from mcp_eastmoney.recommend import pipeline as P
    bars_map = {"600001": _bars_up(20)}  # 不足30根
    r = P.run_pipeline("volume_breakout", top_n=3, rows=_mk_rows(), bars_map=bars_map)
    exc = {e["code"]: e for e in r["excluded"]}
    assert exc.get("600001", {}).get("stage") == "L2"


def test_pipeline_unknown_strategy():
    from mcp_eastmoney.recommend import pipeline as P
    r = P.run_pipeline("nope")
    assert "error" in r and "available" in r


def test_bundled_strategies_loadable_from_recommend():
    from mcp_eastmoney.recommend import screener as S
    assert {"volume_breakout", "dual_low", "shrink_pullback"} <= set(S.load_strategies())
