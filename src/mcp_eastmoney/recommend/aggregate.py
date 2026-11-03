"""聚合层 — 15 策略 L2 标的池 → 统一候选榜（漏斗外，不选股不筛股）。

职责（对应 AlphaSift 的组合/横向层，规则制、不拍权重）：
  ① 并行跑全部策略 stop_after="L2"，每次运行存档 funnel_runs（可回溯）
  ② 合并去重 → 标注共振数（hit_strategies / resonance）
  ③ 外部证据标注：所属行业、板块资金流排名（sector_flow，体系不一致时留 None）、
     情绪适配（策略 market_regime × 当日情绪 regime）
  ④ 透明排序：共振 ↓ → 板块资金流排名 ↑（缺数据排最后）→ trend_score ↓
  ⑤ 聚合榜存档 aggregate_runs（关联 funnel_run_ids，证据链完整）

不做：加权综合分（无标签前禁止）、二次筛选、替代 AI 终审。
"""
from __future__ import annotations

import asyncio
from typing import Any

from . import screener
from .pipeline import run_pipeline


def merge_candidates(pools: dict[str, list[dict]],
                     industry_map: dict[str, str] | None = None,
                     flow_rank: dict[str, int] | None = None,
                     sentiment_regime: str | None = None,
                     strategy_regimes: dict[str, list[str]] | None = None) -> list[dict]:
    """纯函数：合并各策略候选池 → 标注排序后的榜单（可独立测试）。

    pools: {strategy: candidates(L2 出口)}
    industry_map: {code: 行业名}；flow_rank: {行业名: 资金流排名}
    sentiment_regime: 当日市况；strategy_regimes: {策略: [适用 regime]}
    """
    industry_map = industry_map or {}
    flow_rank = flow_rank or {}
    strategy_regimes = strategy_regimes or {}
    merged: dict[str, dict] = {}
    for strat, cands in pools.items():
        for c in cands:
            e = merged.setdefault(c["code"], {
                "code": c["code"], "name": c.get("name"),
                "close": c.get("close"),
                "trend_score": c.get("trend_score"),
                "trend_grade": c.get("trend_grade"),
                "hit_strategies": [], "snapshots": []})
            e["hit_strategies"].append(strat)
            if c.get("trend_score") is not None:
                e["trend_score"] = max(e["trend_score"] or 0, c["trend_score"])
            if c.get("snapshot") and len(e["snapshots"]) < 1:
                e["snapshot"] = c["snapshot"]

    out = []
    for code, e in merged.items():
        ind = industry_map.get(code)
        rank = flow_rank.get(ind) if ind else None
        fit = None
        if sentiment_regime:
            fits = [s for s in e["hit_strategies"]
                    if sentiment_regime in (strategy_regimes.get(s) or [])]
            fit = f"{len(fits)}/{len(e['hit_strategies'])}"
        out.append({**e, "industry": ind, "flow_rank": rank,
                    "resonance": len(e["hit_strategies"]),
                    "sentiment_fit": fit})
    # 排序：共振 ↓ → 资金流排名 ↑（None 最后）→ trend ↓
    out.sort(key=lambda x: (-x["resonance"],
                            x["flow_rank"] if x["flow_rank"] is not None else 9999,
                            -(x["trend_score"] or 0)))
    for e in out:
        e.pop("snapshots", None)
    return out


async def aggregate_scan(top_n: int = 10, min_trend_score: float = 65.0,
                         strategies: list[str] | None = None,
                         fetch_history: bool = False) -> dict[str, Any]:
    """全链：15 策略 L2（存档）→ 聚合（存档）→ 返回榜单。

    返回 {sentiment, stats, aggregated[], funnel_run_ids, aggregate_run_id}。
    """
    from ..data import db

    # ① 情绪定性（供适配标注；失败不阻断）
    from ..compute.sentiment import sentiment_from_db
    senti = sentiment_from_db(db)
    label = senti.get("label")
    regime = senti.get("regime")

    # ② 各策略 L2（每跑一次存档 funnel_runs）
    all_strats = screener.load_strategies()
    names = strategies or sorted(all_strats)
    pools: dict[str, list[dict]] = {}
    funnel_ids: list[int] = []
    errors: dict[str, str] = {}
    for name in names:
        try:
            r = await run_pipeline(name, top_n=top_n,
                                   min_trend_score=min_trend_score,
                                   stop_after="L2", fetch_history=fetch_history)
        except Exception as e:
            errors[name] = f"{type(e).__name__}: {e}"
            continue
        if "error" in r:
            errors[name] = r["error"]
            continue
        pools[name] = r["candidates"]
        try:
            fid = db.insert_funnel_run(
                name, r.get("snapshot_date"), "L2",
                r.get("universe", 0), r.get("filtered", 0),
                r.get("stages", {}), r["candidates"], r.get("excluded", []))
            funnel_ids.append(fid)
        except Exception:
            pass  # 存档失败不阻断聚合

    # ③ 外部证据：行业映射 + 资金流排名（证监会行业名 vs 东财板块名未对齐时 rank=None）
    industry_map: dict[str, str] = {}
    codes = {c["code"] for p in pools.values() for c in p}
    c_ = db._conn()
    try:
        for code, ind in c_.execute(
                "SELECT code, industry FROM stock_industry WHERE code IN "
                f"({','.join('?' * len(codes))})", list(codes)).fetchall():
            if ind:
                industry_map[code] = ind
    finally:
        c_.close()
    flow_rank: dict[str, int] = {}
    try:
        flows = db.query_sector_flow("industry", limit=100)
        for i, r in enumerate(sorted(flows, key=lambda x: -(x["main_net"] or 0)), 1):
            flow_rank[r["name"]] = i
    except Exception:
        pass
    strategy_regimes = {n: (s.get("market_regime") or [])
                        for n, s in all_strats.items()}

    # ④ 合并排序
    aggregated = merge_candidates(pools, industry_map, flow_rank,
                                  regime, strategy_regimes)
    # ⑤ 存档
    agg_id = None
    try:
        agg_id = db.insert_aggregate_run(label, regime, funnel_ids, aggregated)
    except Exception:
        pass

    n_res = sum(1 for a in aggregated if a["resonance"] >= 2)
    return {
        "sentiment": {"label": label, "regime": regime,
                      "rationale": senti.get("rationale")} if label else None,
        "stats": {"strategies_run": len(names), "with_candidates": len(pools),
                  "errors": errors or None,
                  "total_candidates": sum(len(v) for v in pools.values()),
                  "unique": len(aggregated), "resonant": n_res,
                  "flow_rank_covered": sum(1 for a in aggregated
                                           if a["flow_rank"] is not None)},
        "funnel_run_ids": funnel_ids,
        "aggregate_run_id": agg_id,
        "aggregated": aggregated,
        "next_step": "AI 终审榜单（共振优先）→ 层3/4 深评 → 精选 ≤3 → save_recommendation",
    }
