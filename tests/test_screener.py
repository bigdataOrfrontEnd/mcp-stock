"""策略筛选引擎 + 快照/运行存档 单元测试（纯离线，合成截面数据）。"""
from __future__ import annotations

import importlib
import os
import tempfile

import pytest


def _mk_rows() -> list[dict]:
    """合成全市场截面：一只放量突破、一只双低、一只缩量回踩、一只ST、若干背景板。"""
    base = {"volume_ratio": 1.0, "amplitude": 2.0, "total_mv": 100.0, "float_mv": 80.0}
    rows = [
        # 放量突破候选
        {**base, "code": "600001", "name": "突破股", "close": 20.0, "change_pct": 4.5,
         "amount": 5e8, "turnover_rate": 8.0, "volume_ratio": 3.5, "pe": 25, "pb": 2.0},
        # 双低候选
        {**base, "code": "600002", "name": "双低股", "close": 10.0, "change_pct": 0.5,
         "amount": 2e8, "turnover_rate": 1.5, "volume_ratio": 0.9, "pe": 6, "pb": 0.7},
        # 缩量回踩候选
        {**base, "code": "600003", "name": "回踩股", "close": 15.0, "change_pct": -2.5,
         "amount": 1.5e8, "turnover_rate": 2.0, "volume_ratio": 0.5, "pe": 20, "pb": 1.5},
        # ST 应被排除
        {**base, "code": "600004", "name": "ST问题", "close": 5.0, "change_pct": 5.0,
         "amount": 3e8, "turnover_rate": 9.0, "volume_ratio": 4.0, "pe": 10, "pb": 1.0},
        # 涨幅过大（一字板区间外）
        {**base, "code": "600005", "name": "一字板", "close": 30.0, "change_pct": 10.0,
         "amount": 4e8, "turnover_rate": 1.0, "volume_ratio": 2.0, "pe": 40, "pb": 5.0},
        # 次级突破候选（凑横截面对比样本）
        {**base, "code": "600006", "name": "次突破", "close": 18.0, "change_pct": 3.0,
         "amount": 3e8, "turnover_rate": 5.0, "volume_ratio": 2.2, "pe": 28, "pb": 2.2},
        {**base, "code": "600007", "name": "三突破", "close": 12.0, "change_pct": 2.5,
         "amount": 2.5e8, "turnover_rate": 4.0, "volume_ratio": 2.1, "pe": 30, "pb": 2.5},
    ]
    # 背景板：凑横截面样本量
    for i in range(20):
        rows.append({**base, "code": f"0006{i:02d}", "name": f"背景{i}",
                     "close": 8 + i * 0.1,
                     "change_pct": 0.1 * (i % 5 - 2), "amount": 5e7 + i * 1e6,
                     "turnover_rate": 0.5 + i * 0.05, "volume_ratio": 0.6 + i * 0.05,
                     "pe": 10 + i, "pb": 1 + i * 0.1})
    return rows


VB = {
    "name": "volume_breakout", "hard_filters": {
        "exclude_st": True, "amount_min": 2e8, "change_pct_min": 2.0,
        "change_pct_max": 7.0, "price_min": 3, "price_max": 100,
        "volume_ratio_min": 2.0, "turnover_min": 3.0, "total_mv_min": 30,
    },
    "factor_weights": {"volume_ratio": 0.4, "turnover_rate": 0.25,
                       "change_pct": 0.2, "amount": 0.15},
}
DL = {
    "name": "dual_low", "hard_filters": {
        "exclude_st": True, "amount_min": 1e8, "pe_min": 0, "pe_max": 30,
        "pb_min": 0, "pb_max": 3.0, "price_min": 3, "total_mv_min": 50,
    },
    "factor_weights": {"pe": 0.45, "pb": 0.35, "turnover_rate": 0.1, "amount": 0.1},
    "inverted": ["pe", "pb"],
}


def test_volume_breakout_strategy():
    from mcp_eastmoney.compute import screener as S
    r = S.screen(_mk_rows(), VB, top_n=5)
    assert r["filtered"] >= 1
    top = r["ranking"][0]
    assert top["code"] == "600001"          # 突破股第一
    codes = [x["code"] for x in r["ranking"]]
    assert "600004" not in codes            # ST 被排除
    assert "600005" not in codes            # 涨幅10%超上限被排除
    assert top["composite"] > 50


def test_dual_low_inverted_pe():
    from mcp_eastmoney.compute import screener as S
    r = S.screen(_mk_rows(), DL, top_n=5)
    codes = [x["code"] for x in r["ranking"]]
    assert codes[0] == "600002"             # 双低股第一（低PE低PB反向加分）
    assert "600004" not in codes


def test_load_strategies_builtin():
    from mcp_eastmoney.compute import screener as S
    strategies = S.load_strategies()
    assert {"volume_breakout", "dual_low", "shrink_pullback"} <= set(strategies)
    vb = strategies["volume_breakout"]
    assert vb["hard_filters"]["volume_ratio_min"] == 1.5  # v2: 软曲线接管上界


def test_missing_field_filtered_out():
    from mcp_eastmoney.compute import screener as S
    rows = [{"code": "688888", "name": "缺字段", "close": 10.0}] + _mk_rows()
    r = S.screen(rows, VB, top_n=5)  # 缺 amount 等 → 硬过滤淘汰
    assert "688888" not in [x["code"] for x in r["ranking"]]


# ---- 计算层因子段（compute_filters / compute_weights） ----------------------------------


def _mk_bars(drift: float, n: int = 80) -> list[dict]:
    price, bars = 20.0, []
    for i in range(n):
        price *= 1 + drift
        bars.append({"date": f"d{i}", "open": price, "high": price * 1.01,
                     "low": price * 0.99, "close": price, "volume": 1e6})
    return bars


SP = {
    "name": "shrink_pullback_test", "hard_filters": {
        "exclude_st": True, "amount_min": 1e8, "change_pct_min": -5.0,
        "change_pct_max": -1.0, "volume_ratio_max": 0.8, "turnover_min": 1.0,
        "price_min": 3, "total_mv_min": 30,
    },
    "factor_weights": {"volume_ratio": 0.4, "turnover_rate": 0.25,
                       "amount": 0.2, "amplitude": 0.15},
    "inverted": ["volume_ratio"],
    "compute_filters": {"mom20": {"min": 0}, "ma20_dev": {"min": -5, "max": 3}},
    "compute_weights": {"mom20": 0.35, "ma20_dev": 0.2,
                        "vol20": 0.15, "price_pos_60": 0.3},
}


def test_compute_stage_blends_and_filters():
    from mcp_eastmoney.compute import screener as S
    rows = _mk_rows()
    bars_map = {"600003": _mk_bars(0.001)}  # 仅回踩股有K线（温和上行，MA20偏离≈2%）
    r = S.screen(rows, SP, top_n=5, bars_get=bars_map.get, pool_size=27)
    assert "compute" in r
    top3 = [x["code"] for x in r["ranking"][:3]]
    assert "600003" in top3                       # 计算层动量分把它顶上去
    hit = [x for x in r["ranking"] if x["code"] == "600003"][0]
    assert hit["compute_score"] is not None
    assert hit["compute_factors"]["mom20"] > 0


def test_compute_stage_degrades_without_bars():
    from mcp_eastmoney.compute import screener as S
    r = S.screen(_mk_rows(), SP, top_n=3, bars_get=lambda c: None, pool_size=27)
    assert r["ranking"], "缺K线时应降级为快照段排序而非清空"
    assert all(x["compute_score"] is None for x in r["ranking"])


def test_compute_stage_rejects_unknown_factor():
    from mcp_eastmoney.compute import screener as S
    bad = {**SP, "compute_weights": {"no_such_factor": 1.0}}
    with pytest.raises(ValueError, match="计算层未注册"):
        S.screen(_mk_rows(), bad, top_n=3, bars_get=lambda c: [])


def test_compute_stage_drops_negative_momentum():
    from mcp_eastmoney.compute import screener as S
    bars_map = {"600003": _mk_bars(-0.004)}       # 下行趋势 → mom20 < 0 被过滤
    r = S.screen(_mk_rows(), SP, top_n=5, bars_get=bars_map.get, pool_size=27)
    assert "600003" not in [x["code"] for x in r["ranking"]]


# ---- db: 派生截面（quotes_daily）/ screen_runs ------------------------------------------


@pytest.fixture()
def tmp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("STOCK_DB_PATH", os.path.join(d, "t.db"))
        from mcp_eastmoney.data import db
        importlib.reload(db)
        db.init_db()
        yield db


def test_derived_cross_section(tmp_db):
    """quotes_daily 派生截面：量比/振幅由历史行现算，市值来自摄入列。"""
    db = tmp_db
    # 前5个交易日：每日成交量 100（股），收盘 10 元
    from mcp_eastmoney.data.db import upsert_klines_daily
    import pandas as pd
    hist = pd.DataFrame([
        {"code": "600519", "date": f"2026-09-2{i}", "open": 10, "high": 10.1,
         "low": 9.9, "close": 10, "volume": 100, "amount": 1000,
         "pctChg": 0, "turn": 1} for i in range(1, 6)])
    upsert_klines_daily(hist)
    rows = [{"code": "600519", "name": "贵州茅台", "price": 11.0, "change_pct": 10.0,
             "open": 10.5, "high": 11.0, "low": 10.2,
             "volume": 2, "amount": 3.26e9, "turnover_rate": 0.21,  # 2手 = 200股
             "pe": 18.97, "pb": 6.15, "total_mv": 15445.76, "float_mv": 15445.76}]
    assert db.upsert_snapshot_as_daily(rows, "2026-09-26") == 1
    got = db.query_market_snapshot("2026-09-26")
    assert got[0]["code"] == "600519"
    assert got[0]["pe"] == 18.97 and got[0]["total_mv"] == 15445.76
    assert got[0]["volume_ratio"] == 2.0           # 200 / 5日均量100
    assert got[0]["amplitude"] == 8.0              # (11.0-10.2)/前收10*100
    assert db.snapshot_dates()[0] == "2026-09-26"   # 截面日期 = 最新交易日


def test_screen_runs_roundtrip(tmp_db):
    db = tmp_db
    ranking = [{"code": "600001", "name": "突破股", "composite": 88.5},
               {"code": "600002", "name": "双低股", "composite": 72.1}]
    rid = db.insert_screen_run("volume_breakout", 5190, 33, ranking)
    runs = db.query_screen_runs(5)
    assert runs[0]["id"] == rid
    assert runs[0]["strategy"] == "volume_breakout"
    assert runs[0]["top_codes"][:2] == ["600001", "600002"]


# ---- index_daily / lhb / forecast 三表读写 + 轮动 ---------------------------------------


def test_index_daily_and_rotation(tmp_db):
    db = tmp_db
    # 两个行业指数，各3天：半导体重3日连涨，白酒3日连跌
    up = [{"date": f"2026-09-2{i}", "open": 100, "high": 105, "low": 99,
           "close": 100 + i * 5, "volume": 1e6, "amount": 1e8, "change_pct": 2.0}
          for i in range(3)]
    down = [{"date": f"2026-09-2{i}", "open": 100, "high": 101, "low": 90,
             "close": 100 - i * 5, "volume": 1e6, "amount": 1e8, "change_pct": -2.0}
            for i in range(3)]
    assert db.upsert_index_daily("881121", "半导体", "industry", up) == 3
    assert db.upsert_index_daily("881273", "白酒", "industry", down) == 3
    assert db.upsert_index_daily("sh.000001", "上证指数", "broad", up) == 3
    assert db.get_last_index_date("881121") == "2026-09-22"
    assert len(db.query_index_daily("881121")) == 3
    uni = db.index_universe("industry")
    assert {u["name"] for u in uni} == {"半导体", "白酒"}
    r = db.index_rotation("industry", days=3, top_n=2)
    assert r["top"][0]["name"] == "半导体"
    assert r["bottom"][0]["name"] == "白酒"
    assert r["top"][0]["chg_pct"] >= 10


def test_lhb_roundtrip(tmp_db):
    import pandas as pd
    db = tmp_db
    df = pd.DataFrame([{
        "代码": "002545", "名称": "东方铁塔", "收盘价": "13.82", "涨跌幅": "9.98",
        "龙虎榜净买额": "50000000", "龙虎榜买入额": "80000000", "龙虎榜卖出额": "30000000",
        "龙虎榜成交额": "110000000", "净买额占总成交比": "5.5", "换手率": "12.3",
        "流通市值": "155.85", "上榜原因": "日涨幅偏离值达7%", "解读": "机构净买入",
    }])
    assert db.upsert_lhb(df, "2026-09-29") == 1
    rows = db.query_lhb("2026-09-29")
    assert rows[0]["code"] == "002545"
    assert rows[0]["net_buy"] == 5e7
    assert db.lhb_dates() == ["2026-09-29"]


def test_forecast_roundtrip(tmp_db):
    import pandas as pd
    db = tmp_db
    df = pd.DataFrame([{
        "股票代码": "002058", "股票简称": "紫竹高科", "预测指标": "净利润",
        "业绩变动": "下降", "预测数值": "3000万", "业绩变动幅度": "-65.5%",
        "业绩变动原因": "主营产品价格下滑", "预告类型": "预减",
        "上年同期值": "8700万", "公告日期": "2026-08-30",
    }])
    assert db.upsert_earnings_forecast(df) == 1
    rows = db.query_earnings_forecast(code="002058")
    assert rows[0]["forecast_type"] == "预减"
    assert rows[0]["change_pct"] == -65.5
    rows2 = db.query_earnings_forecast(ftype="预增")
    assert rows2 == []


# ---- AlphaSift 式锚点软打分 ----


def test_anchor_score_curve():
    from mcp_eastmoney.compute import crosssec
    s = crosssec.anchor_score([2.5, 8.0, 0.5, 5.0, None, 6.5],
                              full=(2.0, 5.0), hard=(0.0, 8.0))
    assert s[0] == 100.0        # 满分区
    assert s[1] == 0.0          # 上硬界
    assert s[2] == 25.0        # 下衰减区中点(0.5在0~2之间)
    assert s[3] == 100.0
    assert s[4] is None         # 缺数据保持
    assert 0 < s[5] < 100       # 衰减区（6.5 → (8-6.5)/3 ≈ 50）
    assert abs(s[5] - 50.0) < 1


def test_anchor_invalid_params():
    from mcp_eastmoney.compute import crosssec
    import pytest
    with pytest.raises(ValueError):
        crosssec.anchor_score([1.0], full=(5.0, 2.0), hard=(0.0, 8.0))


def test_screener_soft_scores(tmp_db):
    from mcp_eastmoney.recommend import screener as S
    strat = {
        "name": "t_soft", "hard_filters": {"exclude_st": True, "amount_min": 1e8},
        "factor_weights": {"turnover_rate": 0.5},
        "soft_scores": {"volume_ratio": {"full": [2.0, 3.0],
                                         "hard": [1.0, 6.0], "weight": 0.5}},
    }
    rows = [
        {"code": "600001", "name": "温和", "close": 10, "amount": 2e8,
         "turnover_rate": 5.0, "volume_ratio": 2.5},      # 锚点满分
        {"code": "600002", "name": "过热", "close": 10, "amount": 2e8,
         "turnover_rate": 5.0, "volume_ratio": 6.0},      # 衰减到0
        {"code": "600003", "name": "不足", "close": 10, "amount": 2e8,
         "turnover_rate": 5.0, "volume_ratio": 1.0},      # 下界
    ]
    r = S.screen(rows, strat, top_n=3)
    by = {x["code"]: x for x in r["ranking"]}
    # 温和满分 > 过热(量比软分0) > 不足(下界0但换手同分) — 过热和不足均被压制
    assert by["600001"]["composite"] > by["600002"]["composite"]
    assert by["600001"]["composite"] > by["600003"]["composite"]


def test_load_strategies_with_soft():
    from mcp_eastmoney.recommend import screener as S
    s = S.load_strategies()
    vb = s["volume_breakout"]
    assert "soft_scores" in vb and "change_pct" in vb["soft_scores"]
    assert "oversold_reversal" in s              # 新策略已内置
    assert s["oversold_reversal"]["soft_scores"]["change_pct"]["full"] == [-4.5, -2.5]
