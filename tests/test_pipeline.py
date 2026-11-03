"""推荐漏斗编排器测试（依赖注入，纯离线合成数据）。"""
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
    r = asyncio.run(P.run_pipeline(
        "volume_breakout", top_n=3, rows=rows, bars_map=bars_map,
        forecasts={"600007": {"forecast_type": "预减", "change_pct": -60}}))
    assert "error" not in r
    codes = [c["code"] for c in r["candidates"]]
    assert "600001" in codes and "600006" not in codes
    exc = {e["code"]: e for e in r["excluded"]}
    # 600006 下跌股：被 L1.5(price_pos_60≥75) 或 L2(趋势分) 淘汰，均正确
    assert "600006" not in codes
    assert "600007" in exc and exc["600007"]["stage"] == "L3"
    cand = r["candidates"][0]
    assert cand["trend_score"] >= 65
    assert "shares" in cand["position"] and cand["position"]["shares"] > 0
    assert cand["position"]["stop_loss"] < cand["close"]


def test_pipeline_yizi_excluded(tmp_db):
    """末尾一字板 → L3 淘汰。"""
    from mcp_eastmoney.recommend import pipeline as P
    bars_map = {"600001": _bars_yizi()}
    r = asyncio.run(P.run_pipeline("volume_breakout", top_n=3, rows=_mk_rows(), bars_map=bars_map, fetch_history=False))
    exc = {e["code"]: e for e in r["excluded"]}
    assert exc.get("600001", {}).get("stage") == "L3"
    assert "一字板" in exc["600001"]["reason"]


def test_pipeline_insufficient_bars(tmp_db):
    from mcp_eastmoney.recommend import pipeline as P
    bars_map = {"600001": _bars_up(20)}  # 不足30根
    r = asyncio.run(P.run_pipeline("volume_breakout", top_n=3, rows=_mk_rows(), bars_map=bars_map, fetch_history=False))
    exc = {e["code"]: e for e in r["excluded"]}
    assert exc.get("600001", {}).get("stage") == "L2"


def test_pipeline_unknown_strategy():
    from mcp_eastmoney.recommend import pipeline as P
    r = asyncio.run(P.run_pipeline("nope"))
    assert "error" in r and "available" in r


def test_pipeline_stale_bars_excluded(tmp_db):
    """K线末根落后于快照日期且不补拉 → L2 淘汰（新鲜度服务端强制）。"""
    from mcp_eastmoney.data import db
    db.upsert_snapshot_as_daily(
        [{"code": "600001", "name": "A", "price": 10.0, "change_pct": 3.0,
          "open": 9.8, "high": 10.2, "low": 9.8,
          "volume": 1000, "amount": 2e8, "turnover_rate": 3.0,
          "pe": 20.0, "pb": 2.0, "total_mv": 80.0}],
        "2026-12-31")
    bars = _bars_up()  # 末根 2026-08-29 < 2026-12-31
    from mcp_eastmoney.recommend import pipeline as P
    r = asyncio.run(P.run_pipeline("volume_breakout", top_n=3, rows=_mk_rows(),
                                   bars_map={"600001": bars}, fetch_history=False))
    exc = {e["code"]: e for e in r["excluded"]}
    assert exc["600001"]["stage"] == "L2" and "过期" in exc["600001"]["reason"]


def test_pipeline_l25_strategy_recheck(tmp_db, monkeypatch):
    """L2.5 策略条件复核：L1.5 缺K线降级放行的票，L2 补数据后必须补考 compute_filters。

    时序模拟：第一次读库（L1.5）返回空 → 降级放行；之后（L2，模拟补拉完成）
    返回强趋势但非 60 日新高的 K 线 → 趋势分过、策略条件 new_high_60=100 不过，
    只有 L2.5 能拦住它。
    """
    from mcp_eastmoney.recommend import pipeline as P
    from mcp_eastmoney.recommend import screener as SC

    custom = {"name": "test_l25", "display_name": "L25测试", "style": "趋势",
              "hard_filters": {"exclude_st": True, "amount_min": 100000000},
              "factor_weights": {"volume_ratio": 1.0},
              "compute_filters": {"new_high_60": {"min": 100}}}
    monkeypatch.setattr(SC, "load_strategies", lambda: {"test_l25": custom})

    bars = _bars_up(70, drift=0.8)      # 强趋势，趋势分应达标
    bars[-1]["close"] = bars[-1]["close"] * 0.9   # 打断新高，趋势仍多头

    state = {"seen": set()}

    def fake_query(code, limit=60, **kw):
        if code != "600001":
            return []
        if code in state["seen"]:       # L2 阶段（补拉后）：返回 K 线
            return list(reversed(bars))
        state["seen"].add(code)         # L1.5 阶段：首次读库为空 → 降级放行
        return []

    tmp_db.query_klines_daily = fake_query  # pipeline 经模块属性调用，stub 生效

    r = asyncio.run(P.run_pipeline(
        "test_l25", top_n=3, rows=_mk_rows(), min_trend_score=40,
        bars_map=None, forecasts={}, fetch_history=False))
    exc = {e["code"]: e for e in r["excluded"]}
    assert "600001" not in [c["code"] for c in r["candidates"]]
    hit = exc.get("600001")
    assert hit is not None and "复核" in hit["reason"] and "new_high_60" in hit["reason"]


def test_bundled_strategies_loadable_from_recommend():
    from mcp_eastmoney.recommend import screener as S
    assert {"volume_breakout", "dual_low", "shrink_pullback"} <= set(S.load_strategies())



def test_pipeline_stop_after_l2(tmp_db):
    """stop_after='L2'：策略边界模式，L2 出口即标的池，跳过 L2.5/L3/L4。"""
    from mcp_eastmoney.recommend import pipeline as P
    bars_map = {"600001": _bars_up()}
    r = asyncio.run(P.run_pipeline(
        "volume_breakout", top_n=3, rows=_mk_rows(), bars_map=bars_map,
        forecasts={}, fetch_history=False, stop_after="L2"))
    assert r["mode"] == "L2"
    assert "标的池" in r["next_step"]
    for c in r["candidates"]:
        assert "trend_score" in c and "position" not in c   # 无定仓（L4 未跑）
        assert c["trend_score"] >= 65
    # 一字板票在 L2 模式不被排雷（L3 未跑）
    r2 = asyncio.run(P.run_pipeline(
        "volume_breakout", top_n=3, rows=_mk_rows(), bars_map={"600001": _bars_yizi()},
        forecasts={}, fetch_history=False, stop_after="L2"))
    assert any(c["code"] == "600001" for c in r2["candidates"])
