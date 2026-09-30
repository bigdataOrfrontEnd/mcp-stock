"""因子库 / 异动监听 / 新闻与财务落库 单元测试（纯离线，合成数据）。"""
from __future__ import annotations

import importlib
import os
import tempfile

import pandas as pd
import pytest


# ---- factors -----------------------------------------------------------------------


def _mk_bars(n: int = 80, base: float = 10.0, drift: float = 0.1,
             wiggle: float = 0.3) -> list[dict]:
    """合成升序日K：趋势 drift% + 正弦扰动 wiggle%（保证波动非零、趋势保留）。"""
    import math
    bars, px = [], base
    for i in range(n):
        px *= 1 + (drift + wiggle * math.sin(i / 3.0)) / 100
        bars.append({
            "date": f"2026-07-{i+1:02d}" if i < 31 else f"2026-08-{i-30:02d}",
            "open": px * 0.99, "high": px * 1.01, "low": px * 0.98, "close": px,
            "volume": 10000.0, "amount": px * 10000, "change_pct": drift,
        })
    return bars


def test_compute_factors_values():
    from mcp_eastmoney.compute import factors as F
    bars = _mk_bars(80)
    fv = F.compute_factors(bars)
    assert fv["mom5"] is not None and fv["mom5"] > 0
    assert fv["vol20"] is not None and fv["vol20"] >= 0
    assert fv["rsi14"] is not None and 0 <= fv["rsi14"] <= 100
    assert fv["price_pos_60"] == 100.0  # 单边上涨 → 顶部
    assert fv["vol60"] is not None
    # 数据不足 → None
    short = F.compute_factors(_mk_bars(5))
    assert short["mom20"] is None and short["vol20"] is None


def test_valuation_and_quality_factors():
    from mcp_eastmoney.compute import factors as F
    bars = _mk_bars(30)
    for b in bars:
        b["pe"], b["pb"], b["turnover_rate"] = 20.0, 3.5, 2.0
        b["amount"] = 1e8
    fv = F.compute_factors(bars, ["pe", "pb", "turnover_20d", "amount_20d"])
    assert fv["pe"] == 20.0 and fv["pb"] == 3.5
    assert fv["turnover_20d"] == 2.0 and fv["amount_20d"] == 1e8
    # 亏损 PE 无意义 → None
    bars2 = [dict(b, pe=-5.0) for b in bars]
    assert F.compute_factors(bars2, ["pe"])["pe"] is None
    # 质量因子来自 fin 输入
    fin = {"roe": 25.0, "gross_margin": 91.0, "net_margin": 50.0}
    fv = F.compute_factors(bars, ["roe", "gross_margin", "net_margin"], fin=fin)
    assert fv["roe"] == 25.0 and fv["gross_margin"] == 91.0
    assert F.compute_factors(bars, ["roe"])["roe"] is None  # 无 fin → None


def test_catalog():
    from mcp_eastmoney.compute import factors as F
    cat = F.catalog()
    assert len(cat) == len(F.FACTOR_META) == 16
    cats = {c["category"] for c in cat}
    assert {"动量", "波动", "流动性", "技术", "估值", "质量"} <= cats
    pe = next(c for c in cat if c["factor"] == "pe")
    assert pe["direction"] == "低值得分高"


def test_rank_stocks_ordering():
    from mcp_eastmoney.compute import factors as F
    up = _mk_bars(80, drift=1.0, wiggle=0.8)     # 强势
    flat = _mk_bars(80, drift=0.0, wiggle=0.05)  # 横盘低波
    down = _mk_bars(80, drift=-0.8, wiggle=0.8)  # 弱势
    ranked = F.rank_stocks([("000001", flat), ("600519", up), ("300750", down)], top_n=3)
    codes = [r["code"] for r in ranked]
    assert codes[0] == "600519" and codes[-1] == "300750"
    assert all(r["composite"] is not None for r in ranked)
    # vol20 反向：波动越低分位越高
    flat_vol = ranked[codes.index("000001")]["scores"]["vol20"]
    assert flat_vol > 50


def test_rank_with_inverted_pe():
    from mcp_eastmoney.compute import factors as F
    def bars_with(pe):
        b = _mk_bars(30)
        for x in b:
            x["pe"] = pe
        return b
    ranked = F.rank_stocks(
        [("cheap", bars_with(8.0)), ("expensive", bars_with(80.0))],
        factors=["pe"], top_n=2)
    assert ranked[0]["code"] == "cheap"          # 低 PE 得分高
    assert ranked[0]["scores"]["pe"] == 100.0


# ---- alerts ------------------------------------------------------------------------


def test_price_change_alert():
    from mcp_eastmoney.compute import alerts as A
    a = A.check_stock("600519", {"change_pct": 6.5, "price": 100, "open": 95,
                                 "prev_close": 94}, None)
    types = [x["type"] for x in a]
    assert "price_change" in types


def test_limit_open_alert():
    from mcp_eastmoney.compute import alerts as A
    bars = _mk_bars(30)
    bars[-1]["change_pct"] = 10.0  # 昨日涨停
    a = A.check_stock("000001", {"change_pct": 3.0, "price": 10, "open": 10,
                                 "prev_close": 10}, bars)
    assert any(x["type"] == "limit_open" for x in a)


def test_ma5_break_and_gap():
    from mcp_eastmoney.compute import alerts as A
    bars = _mk_bars(30, drift=-0.5)  # 下跌 → 价在 MA5 下
    bars[-1]["close"] = bars[-1]["close"]  # 保持
    a = A.check_stock("000002", {
        "change_pct": 1.0,
        "price": bars[-1]["close"] * 1.06,  # 现价大幅高于 MA5
        "open": bars[-2]["close"] * 1.03,   # 跳空 +3%
        "prev_close": bars[-2]["close"],
    }, bars)
    types = [x["type"] for x in a]
    assert "ma5_break" in types and "gap" in types


def test_no_false_alert():
    from mcp_eastmoney.compute import alerts as A
    bars = _mk_bars(30, drift=0.05)
    a = A.check_stock("600000", {
        "change_pct": 0.5, "price": bars[-1]["close"],
        "open": bars[-2]["close"] * 1.001, "prev_close": bars[-2]["close"],
    }, bars)
    assert a == []


# ---- db: news / financial（临时库隔离） -----------------------------------------------


@pytest.fixture()
def tmp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("STOCK_DB_PATH", os.path.join(d, "test.db"))
        from mcp_eastmoney.data import db
        importlib.reload(db)
        db.init_db()
        yield db


def test_news_roundtrip(tmp_db):
    db = tmp_db
    rows = [
        {"url": "https://x/1", "scope": "market", "title": "央行降准",
         "content": "内容A", "source": "财联社", "pub_time": "2026-09-29 09:00"},
        {"url": "https://x/2", "scope": "stock:600519", "code": "600519",
         "title": "贵州茅台新品发布", "content": "内容B", "source": "东财",
         "pub_time": "2026-09-29 10:00"},
    ]
    assert db.upsert_news(rows) == 2
    assert db.upsert_news(rows) == 2  # 幂等
    got = db.query_news(code="600519")
    assert len(got) == 1 and "茅台" in got[0]["title"]
    got_kw = db.query_news(keyword="降准")
    assert len(got_kw) == 1
    assert db.news_last_fetch("market") is not None


def test_financial_roundtrip(tmp_db):
    db = tmp_db
    df = pd.DataFrame([
        {"日期": "2026-06-30", "摊薄每股收益(元)": "23.5", "净资产收益率(%)": "30.1",
         "销售毛利率(%)": "91.5", "每股净资产_调整后(元)": "180", "每股经营性现金流(元)": "20",
         "总资产利润率(%)": "20", "销售净利率(%)": "50", "主营业务收入(元)": "1e10",
         "净利润(元)": "5e9"},
        {"日期": "2026-03-31", "摊薄每股收益(元)": "11", "净资产收益率(%)": "14",
         "销售毛利率(%)": "91", "每股净资产_调整后(元)": "170", "每股经营性现金流(元)": "10",
         "总资产利润率(%)": "10", "销售净利率(%)": "49", "主营业务收入(元)": "5e9",
         "净利润(元)": "2.4e9"},
    ])
    assert db.upsert_financial_indicators("600519", df) == 2
    got = db.query_financial_indicators("600519")
    assert len(got) == 2
    assert got[0]["report_date"] == "2026-06-30"  # 倒序
    assert got[0]["roe"] == pytest.approx(30.1)


def test_latest_financials(tmp_db):
    db = tmp_db
    df = pd.DataFrame([
        {"日期": "2026-06-30", "摊薄每股收益(元)": "1", "净资产收益率(%)": "10",
         "销售毛利率(%)": "30", "每股净资产_调整后(元)": "5", "每股经营性现金流(元)": "1",
         "总资产利润率(%)": "5", "销售净利率(%)": "8", "主营业务收入(元)": "1e9",
         "净利润(元)": "8e7"},
        {"日期": "2026-03-31", "摊薄每股收益(元)": "0.5", "净资产收益率(%)": "5",
         "销售毛利率(%)": "28", "每股净资产_调整后(元)": "4.8", "每股经营性现金流(元)": "0.5",
         "总资产利润率(%)": "2.5", "销售净利率(%)": "7", "主营业务收入(元)": "5e8",
         "净利润(元)": "3.5e7"},
    ])
    db.upsert_financial_indicators("000001", df)
    fin = db.latest_financials(["000001", "999999"])
    assert fin["000001"]["report_date"] == "2026-06-30"  # 取最新报告期
    assert fin["000001"]["roe"] == pytest.approx(10)
    assert "999999" not in fin


def test_valuation_columns_migration(tmp_db):
    db = tmp_db
    df = pd.DataFrame([{
        "date": "2026-09-29", "code": "600519", "open": 1500, "high": 1520,
        "low": 1490, "close": 1510, "volume": 1e6, "amount": 1.5e9,
        "pctChg": "0.5", "turn": "0.3", "peTTM": "25.5", "pbMRQ": "8.2", "psTTM": "12.1",
    }])
    db.upsert_klines_daily(df)
    row = db.query_klines_daily("600519")[0]
    assert row["pe"] == pytest.approx(25.5)
    assert row["pb"] == pytest.approx(8.2)
    assert row["ps"] == pytest.approx(12.1)


# ---- chips（筹码分布） ----------------------------------------------------------------


def test_chip_rising_high_profit():
    """单边上涨 → 获利盘接近100%，均成本低于现价。"""
    from mcp_eastmoney.compute import chips as C
    bars = [dict(b, turnover_rate=3.0) for b in _mk_bars(60, drift=1.0, wiggle=0.2)]
    r = C.analyze(bars)
    assert r["profit_ratio_pct"] > 90
    assert r["avg_cost"] < float(bars[-1]["close"])


def test_chip_falling_low_profit():
    """单边下跌 → 获利盘接近0，均成本高于现价，上方存在套牢压力。"""
    from mcp_eastmoney.compute import chips as C
    bars = [dict(b, turnover_rate=3.0) for b in _mk_bars(60, drift=-1.0, wiggle=0.2)]
    r = C.analyze(bars)
    assert r["profit_ratio_pct"] < 10
    assert r["avg_cost"] > float(bars[-1]["close"])
    assert r["pressure_above"] is not None


def test_chip_roundtrip_db(tmp_db):
    db = tmp_db
    rows = [{"code": "002545", "trade_date": "2026-09-28",
             "profit_ratio_pct": 62.5, "avg_cost": 13.1,
             "cost_90_low": 12.0, "cost_90_high": 14.8,
             "concentration_pct": 10.3}]
    assert db.upsert_chip_daily(rows) == 1
    got = db.query_chip_daily("002545")
    assert got[0]["profit_ratio_pct"] == 62.5
    assert got[0]["concentration_pct"] == 10.3


def test_chip_insufficient_bars():
    from mcp_eastmoney.compute import chips as C
    assert "error" in C.analyze(_mk_bars(5))
