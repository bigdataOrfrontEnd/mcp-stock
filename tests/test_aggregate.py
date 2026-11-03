"""聚合层：策略池合并（共振/行业/资金流/情绪适配）+ funnel/aggregate 存档回溯。"""
from __future__ import annotations


def _cands(codes):
    return [{"code": c, "name": f"股{c}", "close": 10.0,
             "trend_score": 70.0, "trend_grade": "B",
             "snapshot": {"change_pct": 3.0}} for c in codes]


def test_merge_resonance_and_sorting():
    from mcp_eastmoney.recommend.aggregate import merge_candidates
    pools = {
        "strategy_a": _cands(["600001", "600002", "600003"]),
        "strategy_b": _cands(["600001", "600002"]),
        "strategy_c": _cands(["600001"]),
    }
    ind = {"600001": "银行", "600002": "白酒", "600003": "软件"}
    flow = {"银行": 2, "白酒": 1, "软件": 50}
    out = merge_candidates(pools, ind, flow, "risk_on",
                           {"strategy_a": ["risk_on"], "strategy_b": ["risk_off"]})
    assert len(out) == 3
    # 600001 共振 3 排第一；600002 共振 2（资金流排名 1）次之
    assert out[0]["code"] == "600001" and out[0]["resonance"] == 3
    assert out[1]["code"] == "600002" and out[1]["resonance"] == 2
    assert out[2]["code"] == "600003" and out[2]["resonance"] == 1
    assert out[0]["hit_strategies"] == ["strategy_a", "strategy_b", "strategy_c"]
    assert out[0]["flow_rank"] == 2 and out[0]["industry"] == "银行"
    # 情绪适配：600001 命中 3 策略中 1 个适配 risk_on
    assert out[0]["sentiment_fit"] == "1/3"


def test_merge_missing_industry_ranked_last():
    from mcp_eastmoney.recommend.aggregate import merge_candidates
    pools = {"a": _cands(["600001"]), "b": _cands(["600002"])}
    out = merge_candidates(pools, {"600001": "银行"}, {"银行": 30})
    # 同共振：有资金流排名(600001) 排在无行业(600002) 前
    assert out[0]["code"] == "600001"
    assert out[1]["flow_rank"] is None and out[1]["industry"] is None


def test_funnel_and_aggregate_roundtrip(tmp_db):
    db = tmp_db
    fid = db.insert_funnel_run(
        "volume_breakout", "2026-11-02", "L2", 5223, 156,
        {"L1_screen_top": 10, "L2_trend_out": 3, "final": 6},
        _cands(["600001", "600002"]),
        [{"code": "600009", "stage": "L2", "reason": "趋势分 40 < 65"}])
    runs = db.query_funnel_runs(strategy="volume_breakout")
    assert runs[0]["id"] == fid
    assert runs[0]["stages"]["final"] == 6
    assert runs[0]["candidates"][0]["code"] == "600001"
    assert runs[0]["excluded"][0]["stage"] == "L2"

    aid = db.insert_aggregate_run("高潮期", "risk_on", [fid],
                                  [{"code": "600001", "resonance": 2}])
    aggs = db.query_aggregate_runs()
    assert aggs[0]["id"] == aid
    assert aggs[0]["funnel_run_ids"] == [fid]
    assert aggs[0]["result"][0]["resonance"] == 2
    assert aggs[0]["sentiment_label"] == "高潮期"
