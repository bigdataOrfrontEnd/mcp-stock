"""情绪周期定性（v2）— 从 server 抽取，供 MCP 工具与聚合层共用。

规则（今日 vs 上一快照日）：
  基础：高度断层≥4板→分歧；总数↑且二板↑→高潮；总数↑→发酵；二板↑→分歧；双降→退潮
  增强（开盘啦口径）：赚钱效应≤-2%→压制退潮；≥+2%且晋级率≥25%→发酵升格高潮；
                      封板率<60%且高潮→降为分歧（覆盖不足 1/3 时赚钱效应不采信）
读本地库（zt_pool/quotes_daily），零网络。
"""
from __future__ import annotations

from typing import Any

# 情绪标签 → 市场状态（与策略 market_regime 匹配用）
LABEL_TO_REGIME = {
    "发酵期": "risk_on",
    "高潮期": "risk_on",
    "分歧期": "neutral",
    "退潮期": "risk_off",
}


def classify(cur: dict, prev: dict, cur_codes: set[str], prev_codes: set[str],
             prev_today_chgs: list[float]) -> dict[str, Any]:
    """纯计算：输入双日统计与晋级/赚钱效应数据，输出完整定性结果。"""
    total_up = cur["total"] >= prev["total"]
    ladder_up = cur["lianban_2plus"] >= prev["lianban_2plus"]
    height_drop = prev["max_lian_ban"] - cur["max_lian_ban"]
    if height_drop >= 4:
        label, why = "分歧期", f"最高连板 {prev['max_lian_ban']}→{cur['max_lian_ban']} 断层(回落{height_drop}板)"
    elif total_up and ladder_up:
        label, why = "高潮期", f"涨停 {prev['total']}→{cur['total']} 且二板以上 {prev['lianban_2plus']}→{cur['lianban_2plus']} 双升"
    elif total_up:
        label, why = "发酵期", f"涨停 {prev['total']}→{cur['total']} 升但二板以上 {prev['lianban_2plus']}→{cur['lianban_2plus']} 降(广度先行)"
    elif ladder_up:
        label, why = "分歧期", f"涨停 {prev['total']}→{cur['total']} 降但二板以上 {prev['lianban_2plus']}→{cur['lianban_2plus']} 升(高度与广度背离)"
    else:
        label, why = "退潮期", f"涨停 {prev['total']}→{cur['total']} 且二板以上 {prev['lianban_2plus']}→{cur['lianban_2plus']} 双降"

    v2: dict = {}
    adjustments: list[str] = []
    base_label = label
    if cur["total"]:
        v2["seal_rate_pct"] = round((cur["total"] - cur["blasted"]) / cur["total"] * 100, 1)
    if prev_codes:
        v2["promote_rate_pct"] = round(
            len(prev_codes & cur_codes) / len(prev_codes) * 100, 1)
    if prev_today_chgs and len(prev_today_chgs) >= max(3, len(prev_codes) // 3):
        me = round(sum(prev_today_chgs) / len(prev_today_chgs), 2)
        v2["money_effect_pct"] = me
        v2["money_effect_coverage"] = f"{len(prev_today_chgs)}/{len(prev_codes)}"
        if me <= -2 and label != "退潮期":
            label = "退潮期"
            adjustments.append(f"赚钱效应 {me:+.1f}%（昨日涨停今日平均）≤-2% → 压制为退潮期")
        elif me >= 2 and label == "发酵期" and v2.get("promote_rate_pct", 0) >= 25:
            label = "高潮期"
            adjustments.append(f"赚钱效应 {me:+.1f}% + 晋级率 {v2['promote_rate_pct']}% → 发酵升格高潮")
    if label == "高潮期" and v2.get("seal_rate_pct", 100) < 60:
        label = "分歧期"
        adjustments.append(f"封板率 {v2['seal_rate_pct']}% <60%（炸板过半）→ 高潮降为分歧")

    return {"date": cur["date"], "prev_date": prev["date"], "label": label,
            "regime": LABEL_TO_REGIME.get(label), "base_label": base_label,
            "rule_version": 2, "metrics": {"current": cur, "prev": prev},
            "v2": v2, "rationale": why, "adjustments": adjustments}


def sentiment_from_db(dbi, date_iso: str | None = None) -> dict[str, Any]:
    """读库版：dbi = mcp_eastmoney.data.db。date_iso 截止日（含），默认最新。"""
    stats = dbi.zt_pool_daily_stats(limit=10)
    if not stats:
        return {"error": "本地无涨停池快照，先 get_zt_pool(date=...) 落库"}
    if date_iso:
        stats = [s for s in stats if s["date"] <= date_iso]
        if not stats:
            return {"error": f"{date_iso} 及之前无本地快照"}
    cur = stats[0]
    if len(stats) < 2:
        return {"date": cur["date"], "error": "仅有一日快照，无法对比定性"}
    prev = stats[1]
    cur_codes = {r["code"] for r in dbi.query_zt_pool(cur["date"])}
    prev_codes = {r["code"] for r in dbi.query_zt_pool(prev["date"])}
    chgs: list[float] = []
    for code in prev_codes:
        bars = dbi.query_klines_daily(code, limit=1)
        if bars and bars[0].get("date") == cur["date"] and bars[0].get("change_pct") is not None:
            chgs.append(float(bars[0]["change_pct"]))
    out = classify(cur, prev, cur_codes, prev_codes, chgs)
    out["history"] = stats[:5]
    return out
