"""策略漏斗（只做 L1→L2）编排测试：依赖注入，纯离线合成数据。"""
from __future__ import annotations

import asyncio
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


def test_pipeline_l1_l2_contract(tmp_db, monkeypatch):
    """L1→L2：强势候选进池，弱势单被 L2 淘汰；出口结构符合契约。"""
    from mcp_eastmoney.recommend import pipeline as P
    from mcp_eastmoney.recommend import screener as SC
    monkeypatch.setattr(SC, "load_strategies", lambda: {"volume_breakout": VB})
    bars_map = {"600001": _bars_up(), "600006": _bars_down()}
    r = asyncio.run(P.run_pipeline(
        "volume_breakout", top_n=10, rows=_mk_rows(), bars_map=bars_map,
        fetch_history=False))
    assert "error" not in r
    codes = [c["code"] for c in r["candidates"]]
    assert "600001" in codes and "600006" not in codes
    exc = {e["code"]: e for e in r["excluded"]}
    assert exc["600006"]["stage"] == "L2"
    cand = r["candidates"][0]
    # L2 出口字段：只有趋势证据，不含 L3/L4 产物
    assert cand["trend_score"] >= 65 and "trend_items" in cand
    assert "position" not in cand and "flags" not in cand
    assert r["mode"] == "L2"
    assert set(r["stages"]) == {"L1_screen_top", "L2_trend_in",
                                "L2_trend_out", "final"}
    # 各自落表：本次运行已写入 funnel_runs（唯一存档点）
    from mcp_eastmoney.data import db
    runs = db.query_funnel_runs(strategy="volume_breakout", limit=1)
    assert runs and runs[0]["id"] == r["run_id"]
    assert runs[0]["mode"] == "L2"
    assert runs[0]["candidates"][0]["code"] in codes


def test_pipeline_insufficient_bars(tmp_db, monkeypatch):
    """K线不足 30 根 → L2 淘汰（次新股/无历史）。"""
    from mcp_eastmoney.recommend import pipeline as P
    from mcp_eastmoney.recommend import screener as SC
    monkeypatch.setattr(SC, "load_strategies", lambda: {"volume_breakout": VB})
    r = asyncio.run(P.run_pipeline("volume_breakout", top_n=3,
                                   rows=_mk_rows(),
                                   bars_map={"600001": _bars_up(20)},
                                   fetch_history=False))
    exc = {e["code"]: e for e in r["excluded"]}
    assert exc["600001"]["stage"] == "L2"


def test_pipeline_stale_bars_excluded(tmp_db, monkeypatch):
    """K线末根落后于截面日期且不补拉 → L2 淘汰（新鲜度服务端强制）。"""
    from mcp_eastmoney.recommend import screener as SC
    monkeypatch.setattr(SC, "load_strategies", lambda: {"volume_breakout": VB})
    from mcp_eastmoney.data import db
    db.upsert_snapshot_as_daily(
        [{"code": "600001", "name": "A", "price": 10.0, "change_pct": 3.0,
          "open": 9.8, "high": 10.2, "low": 9.8,
          "volume": 1000, "amount": 2e8, "turnover_rate": 3.0,
          "pe": 20.0, "pb": 2.0, "total_mv": 80.0}],
        "2026-12-31")
    from mcp_eastmoney.recommend import pipeline as P
    r = asyncio.run(P.run_pipeline("volume_breakout", top_n=3, rows=_mk_rows(),
                                   bars_map={"600001": _bars_up()},
                                   fetch_history=False))
    exc = {e["code"]: e for e in r["excluded"]}
    assert exc["600001"]["stage"] == "L2" and "过期" in exc["600001"]["reason"]


def test_pipeline_unknown_strategy():
    from mcp_eastmoney.recommend import pipeline as P
    r = asyncio.run(P.run_pipeline("nope"))
    assert "error" in r and "available" in r


def test_bundled_strategies_loadable_from_recommend():
    from mcp_eastmoney.recommend import screener as S
    assert {"volume_breakout", "dual_low", "shrink_pullback"} <= set(S.load_strategies())
