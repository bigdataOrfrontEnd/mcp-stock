"""龙头五维评分器测试（纯离线合成涨停池）。"""
from __future__ import annotations


def _zt(code, name, lian, seal, blast, turn, industry, pct=10.0):
    return {"code": code, "name": name, "lian_ban": lian,
            "first_seal_time": seal, "blast_count": blast,
            "turnover_rate": turn, "industry": industry, "change_pct": pct,
            "amount": 5e8, "close": 10.0, "open": 9.5}


SECTOR = {
    "汽车零部件": {"zt_count": 4, "sector_pct": 3.2, "cum10": 8.5},
    "出版": {"zt_count": 1, "sector_pct": 0.5, "cum10": 2.0},
    "电池": {"zt_count": 3, "sector_pct": 2.0, "cum10": -1.0},
}


def test_true_dragon_ranked_first():
    from mcp_eastmoney.compute import dragon as D
    rows = [
        _zt("600001", "早封龙头", 3, "092500", 0, 15.0, "汽车零部件"),
        _zt("600002", "烂板跟风", 1, "142500", 3, 3.0, "出版"),
        _zt("600003", "普通涨停", 2, "103000", 1, 8.0, "电池"),
    ]
    mom5 = {"600001": 25.0, "600002": 2.0, "600003": 10.0}
    r = D.score_dragons(rows, mom5, SECTOR, {"broad": 0.5})
    assert r[0]["code"] == "600001"
    assert r[0]["is_true_dragon"]
    d = r[0]["dims"]
    assert d["drive"] > 60            # 早封+强板块
    # 烂板（3次炸板+晚封+弱板块+低换手）应被否决或垫底
    last = r[-1]
    assert last["code"] == "600002"
    assert (not last["is_true_dragon"]) or last["composite"] < r[0]["composite"]


def test_veto_fires_on_floor():
    from mcp_eastmoney.compute import dragon as D
    rows = [
        _zt("600001", "龙头", 4, "092500", 0, 15.0, "汽车零部件"),
        _zt("600002", "纯跟风", 1, "145500", 5, 1.0, "出版", pct=9.9),
    ]
    mom5 = {"600001": 30.0, "600002": 1.0}
    r = D.score_dragons(rows, mom5, SECTOR, {"broad": 1.0})
    vetoed = [x for x in r if not x["is_true_dragon"]]
    assert vetoed and vetoed[0]["code"] == "600002"
    assert vetoed[0]["vetoes"]            # 有明确否决原因


def test_missing_data_neutral():
    """mom5/行业缺失 → 中性50分，不崩不偏向。"""
    from mcp_eastmoney.compute import dragon as D
    rows = [_zt("600001", "无历史", 2, "100000", 0, 8.0, "未知行业")]
    r = D.score_dragons(rows, {}, {}, {"broad": None})
    assert r[0]["dims"]["absorption"] == 50.0


def test_weights_sum():
    from mcp_eastmoney.compute import dragon as D
    assert abs(sum(D.DIM_WEIGHTS.values()) - 1.0) < 1e-9
    assert set(D.DIM_FLOORS) == {"drive", "leadership", "anti_drop", "liquidity"}
