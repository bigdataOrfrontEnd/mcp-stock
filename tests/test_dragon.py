"""龙头五维评分器 v2 测试（纯离线合成涨停池）。"""
from __future__ import annotations


def _zt(code, name, lian, seal, blast, turn, industry, pct=10.0):
    return {"code": code, "name": name, "lian_ban": lian,
            "first_seal_time": seal, "blast_count": blast,
            "turnover_rate": turn, "industry": industry, "change_pct": pct,
            "amount": 5e8, "close": 10.0}


SECTOR = {
    "汽车零部件": {"zt_count": 4, "sector_pct": 3.2, "cum10": 8.5},
    "出版": {"zt_count": 1, "sector_pct": 0.5, "cum10": 2.0},
    "电池": {"zt_count": 3, "sector_pct": 2.0, "cum10": -1.0},
}
# 快照：龙头秒板低振幅，跟风大振幅（回落后回封）
SNAP = {
    "600001": {"open": 10.5, "high": 11.0, "low": 10.4, "amplitude": 5.5},
    "600002": {"open": 10.0, "high": 11.0, "low": 9.8, "amplitude": 11.0},
    "600003": {"open": 10.2, "high": 11.0, "low": 10.1, "amplitude": 8.0},
}


def _rows():
    # 汽车零部件3只：龙头09:25先封，小弟10:00/10:30跟随 → 龙头应有领先度加分
    return [
        _zt("600001", "带动龙头", 3, "092500", 0, 15.0, "汽车零部件"),
        _zt("600011", "板块小弟", 2, "100000", 1, 10.0, "汽车零部件"),
        _zt("600012", "板块小弟2", 1, "103000", 0, 8.0, "汽车零部件"),
        _zt("600002", "烂板跟风", 1, "142500", 3, 3.0, "出版"),
        _zt("600003", "普通涨停", 2, "103000", 1, 8.0, "电池"),
    ]


def test_lead_margin_drives_leader():
    from mcp_eastmoney.compute import dragon as D
    rows = _rows()
    margin = D._lead_margin(rows)
    # 600001 比同板块平均(10:15=615分)早约50分钟 → 正领先度
    m0 = margin[0]
    assert m0 is not None and m0 > 30
    assert margin[3] is None            # 独苗板块中性
    r = D.score_dragons(rows, {"600001": 25.0, "600011": 15.0, "600012": 8.0,
                               "600002": 2.0, "600003": 10.0},
                        SECTOR, {"broad": 0.5}, snap_map=SNAP,
                        sector_abs={"汽车零部件": 3.0, "电池": 2.0})
    assert r[0]["code"] == "600001"
    d = r[0]["dims"]
    assert d["drive"] > 65              # 早封+带动+强板块共鸣


def test_anti_drop_prefers_low_amplitude():
    """振幅低（秒板不回头）→ 抗跌分高。"""
    from mcp_eastmoney.compute import dragon as D
    r = D.score_dragons(_rows(), {}, SECTOR, {"broad": 0.5}, snap_map=SNAP)
    dims = {x["code"]: x["dims"]["anti_drop"] for x in r}
    assert dims["600001"] > dims["600002"]   # 振幅5.5 < 11.0


def test_board_decay_not_linear():
    """衰减式连板分：最高板100，低4板=60（线性式只有20）。"""
    from mcp_eastmoney.compute import dragon as D
    rows = _rows()  # max_lian=3
    r = D.score_dragons(rows, {}, SECTOR, {"broad": 0.5})
    by = {x["code"]: x for x in r}
    # board=80; mom空→中性50 → leadership=65
    assert abs(by["600012"]["dims"]["leadership"] - 65.0) < 3


def test_yizi_not_penalized_on_turnover():
    """一字板（snapshot high==low）不罚换手：封板质量满分时流动性较高。"""
    from mcp_eastmoney.compute import dragon as D
    rows = [_zt("600100", "一字龙头", 5, "092500", 0, 0.5, "汽车零部件")]
    snap = {"600100": {"open": 11.0, "high": 11.0, "low": 11.0, "amplitude": 0.0}}
    r = D.score_dragons(rows, {}, SECTOR, {"broad": 0.5}, snap_map=snap)
    liq = r[0]["dims"]["liquidity"]
    assert liq >= 80    # 只看封板质量(100) → SEAL_W/(TURN_W+SEAL_W)*100 ≈ 81.8


def test_veto_and_weights():
    from mcp_eastmoney.compute import dragon as D
    assert abs(sum(D.DIM_WEIGHTS.values()) - 1.0) < 1e-9
    rows = _rows()
    r = D.score_dragons(rows, {}, SECTOR, {"broad": 0.5}, snap_map=SNAP)
    vetoed = [x for x in r if not x["is_true_dragon"]]
    assert all(x["vetoes"] for x in vetoed)


def test_early_impulse_and_v21_weights():
    """有15min数据时：早盘冲击力参与带动性；高冲击者得分提升。"""
    from mcp_eastmoney.compute import dragon as D
    rows = _rows()
    # 龙头早盘45分钟冲5%，小弟们只有1~2%
    imp = {"600001": 5.0, "600011": 4.5, "600012": 1.0, "600002": 0.5, "600003": 2.0}
    r3 = D.score_dragons(rows, {}, SECTOR, {"broad": 0.5}, snap_map=SNAP,
                         impulse_map=imp)
    r2 = D.score_dragons(rows, {}, SECTOR, {"broad": 0.5}, snap_map=SNAP)
    d21 = {x["code"]: x["dims"]["drive"] for x in r3}
    d20 = {x["code"]: x["dims"]["drive"] for x in r2}
    assert d21["600001"] >= d20["600001"]   # 已饱和保持满分
    assert d21["600011"] > d20["600011"]     # 高冲击 → 带动性提升
    assert d21["600002"] < d20["600002"]    # 低冲击 → 带动性下降


def test_early_impulse_helper():
    from mcp_eastmoney.recommend.dragon import _early_impulse
    bars = [{"open": 10.0, "close": 10.5}, {"open": 10.5, "close": 10.6},
            {"open": 10.6, "close": 11.0}, {"open": 11.0, "close": 11.0}]
    assert abs(_early_impulse(bars) - 10.0) < 0.01
    assert _early_impulse(bars[:2]) is None  # 不足4根 → None
