"""InStock 移植因子：new_high_60 / recent_crash / ma30_slope / breakout_recent。"""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from test_factors_alerts_news import _mk_bars  # 复用合成K线


def test_new_high_60():
    from mcp_eastmoney.compute.factors import _new_high
    bars = _mk_bars(80, drift=0.5)
    closes = [float(b["close"]) for b in bars]
    assert _new_high(closes, 60) == 100.0      # 持续上涨末根即新高
    closes[-1] = min(closes[-60:])             # 打断新高
    assert _new_high(closes, 60) == 0.0


def test_recent_crash():
    from mcp_eastmoney.compute.factors import _recent_crash
    bars = _mk_bars(40, drift=0.3)
    assert _recent_crash(bars, 20) == 0.0      # 平稳无灾害
    bars[-1]["change_pct"] = -8.0              # 单日暴跌
    assert _recent_crash(bars, 20) == 100.0
    bars2 = _mk_bars(40, drift=0.3)
    bars2[-1]["change_pct"] = -6.0
    bars2[-2]["change_pct"] = -5.0             # 两日累计 -11%
    assert _recent_crash(bars2, 20) == 100.0
    bars3 = _mk_bars(40, drift=0.3)
    bars3[-1]["change_pct"] = -4.0
    bars3[-2]["change_pct"] = -4.0             # 累计 -8% 未触发
    assert _recent_crash(bars3, 20) == 0.0


def test_ma30_slope():
    from mcp_eastmoney.compute.factors import _ma_slope
    up = [float(b["close"]) for b in _mk_bars(80, drift=0.5)]
    down = list(reversed(up))
    assert _ma_slope(up, 30) is not None and _ma_slope(up, 30) > 0
    assert _ma_slope(down, 30) < 0
    assert _ma_slope(up[:40], 30) is None       # 数据不足


def test_breakout_recent():
    from mcp_eastmoney.compute.factors import _breakout_recent
    bars = _mk_bars(100, drift=0.05, wiggle=0.8)  # 横盘
    assert _breakout_recent(bars, 20) == 0.0
    # 造一个突破日：当日 open < MA60 <= close，且量是 5 日均量 2 倍以上
    closes = [float(b["close"]) for b in bars]
    ma60 = sum(closes[-60:]) / 60
    i = len(bars) - 1
    vols = [float(b["volume"] or 100) for b in bars]
    for b in bars[-6:-1]:
        b["volume"] = 100
    bars[-1]["open"] = ma60 * 0.99
    bars[-1]["close"] = ma60 * 1.01
    bars[-1]["volume"] = 500
    assert _breakout_recent(bars, 20) == 100.0


def test_factors_registered_in_meta():
    from mcp_eastmoney.compute.factors import FACTOR_META, INVERTED_FACTORS
    for k in ("new_high_60", "recent_crash", "ma30_slope", "breakout_recent",
              "parking_apron", "backtrace_ma250", "high_tight_flag", "range_10"):
        assert k in FACTOR_META
    assert "recent_crash" in INVERTED_FACTORS


def test_range_10():
    from mcp_eastmoney.compute.factors import _range_n
    flat = [10.0] * 12
    assert _range_n(flat, 10) == 0.0
    wild = [10.0] * 2 + [10.0] * 8 + [22.0]  # 近10日内 10→22
    assert _range_n(wild, 10) == 120.0
    assert _range_n([1.0, 2.0], 10) is None


def test_high_tight_flag():
    from mcp_eastmoney.compute.factors import _high_tight_flag
    bars = _mk_bars(80, drift=0.3)
    assert _high_tight_flag(bars) in (0.0, 100.0)
    # 构造：第-20根低点 10，随后连续两日涨停，末根 20（≥1.9x）
    bars[-20]["low"] = 10.0
    bars[-20]["close"] = 10.5
    bars[-6]["change_pct"] = 10.0
    bars[-5]["change_pct"] = 10.0
    bars[-1]["close"] = 21.0
    assert _high_tight_flag(bars) == 100.0


def test_parking_apron():
    from mcp_eastmoney.compute.factors import _parking_apron
    bars = _mk_bars(40, drift=0.2)
    for b in bars:
        b["volume"] = 1000.0
    # 涨停日 + 放量
    zt = bars[-4]
    zt.update({"change_pct": 10.0, "volume": 5000.0, "close": 20.0})
    # 随后3日：高开于涨停价上方、小实体、收盘上涨且不破
    d1 = {"open": 20.5, "close": 20.8, "change_pct": 4.0, "volume": 1000}
    d2 = {"open": 20.6, "close": 20.9, "change_pct": 0.5, "volume": 1000}
    d3 = {"open": 20.7, "close": 20.85, "change_pct": -0.2, "volume": 1000}
    bars[-3].update(d1); bars[-2].update(d2); bars[-1].update(d3)
    assert _parking_apron(bars, 15) == 100.0
    bars[-2]["close"] = 19.0   # 破涨停价
    assert _parking_apron(bars, 15) == 0.0


def test_backtrace_ma250_insufficient():
    from mcp_eastmoney.compute.factors import _backtrace_ma250
    bars = _mk_bars(200, drift=0.3)
    assert _backtrace_ma250(bars, 60) == 0.0   # 数据不足恒 0（需310+）


def test_instock_strategy_yamls_loadable():
    import os
    from mcp_eastmoney.recommend.screener import load_strategies
    s = load_strategies()
    expected = {"turtle_new_high", "steady_ma30", "parking_apron", "pullback_ma250",
                "platform_breakout", "no_retrace_uptrend", "climax_limitdown",
                "high_tight_flag", "value_quality", "volume_surge", "low_atr_growth"}
    missing = expected - set(s)
    assert not missing, f"缺失: {missing}"
    for name in expected:
        assert s[name].get("market_regime"), f"{name} 缺 market_regime"
