"""新增指标（BIAS/CCI/WR）+ 量比告警 + trend_score 卖点对称 测试。"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from test_factors_alerts_news import _mk_bars


def test_bias():
    from mcp_eastmoney.compute import indicators as I
    bars = _mk_bars(40, drift=2.0)   # 单边上涨 → 正乖离
    vals = I.bias([float(b["close"]) for b in bars], 6)
    assert vals[-1] is not None and vals[-1] > 0
    r = I.analyze(bars)
    assert r["bias6"] > 0


def test_cci_range():
    from mcp_eastmoney.compute import indicators as I
    bars = _mk_bars(40, drift=2.0)
    vals = I.cci(bars)
    assert vals[-1] is not None and vals[-1] > 0    # 上涨趋势 CCI 为正
    r = I.analyze(bars)
    assert isinstance(r["cci14"], float)
    assert vals[10] is None                          # 前置期 None


def test_wr_direction():
    from mcp_eastmoney.compute import indicators as I
    up = _mk_bars(40, drift=1.5)                     # 收在高位 → WR 低（超买侧）
    vals = I.wr(up)
    assert vals[-1] is not None and vals[-1] < 50
    r = I.analyze(up)
    assert r["wr14"] < 50


def test_volume_spike_alert():
    from mcp_eastmoney.compute import alerts as A
    a = A.check_stock("600000", {"change_pct": 1.0, "volume_ratio": 3.5,
                                 "price": 10, "open": 10, "prev_close": 10}, None)
    types = [x["type"] for x in a]
    assert "volume_spike" in types
    lv = [x["level"] for x in a if x["type"] == "volume_spike"][0]
    assert lv == "warn"
    # 量比 6 → critical
    a2 = A.check_stock("600000", {"change_pct": -1.0, "volume_ratio": 6.0,
                                  "price": 10, "open": 10, "prev_close": 10}, None)
    lv2 = [x["level"] for x in a2 if x["type"] == "volume_spike"][0]
    assert lv2 == "critical"
    # 量比正常 → 不触发
    a3 = A.check_stock("600000", {"change_pct": 0.5, "volume_ratio": 1.1,
                                  "price": 10, "open": 10, "prev_close": 10}, None)
    assert "volume_spike" not in [x["type"] for x in a3]


def test_trend_score_sell_symmetry():
    from mcp_eastmoney.compute import trend_score as T
    bars = _mk_bars(40)
    # 无缠论输入 → 可评分且归一化到100内
    r = T.score_trend(bars)
    assert "error" not in r and 0 <= r["score"] <= 100 and not r["chan_used"]
    # 近期卖点 → 缠论分=0（对称）
    chan = {"signals": [{"type": "三卖", "confidence": 0.8, "stop_loss": 9.0}],
            "position": "中枢下方弱势区"}
    r2 = T.score_trend(bars, chan=chan)
    item = [x for x in r2["items"] if x["item"] == "缠论结构"][0]
    assert item["score"] == 0 and "卖点" in item["reason"]
    # 近期买点 → 高分
    chan2 = {"signals": [{"type": "二买", "confidence": 0.9, "stop_loss": 9.0}],
             "position": "中枢上方强势区"}
    r3 = T.score_trend(bars, chan=chan2)
    item3 = [x for x in r3["items"] if x["item"] == "缠论结构"][0]
    assert item3["score"] >= 12
