"""全策略批量扫描 — 15 策略各自跑 L1→L2、各自排序、各自落表（funnel_runs）。

设计决定（2026-11-04）：**不做任何跨策略处理**。曾有的聚合层（v1 全局共振
总榜 → v2 分族聚合）已移除——策略间互不相关（regime 互斥），跨策略合并
缺乏依据；对齐 AlphaSift 的"每策略一条道"模型：排序只在策略内部发生，
落表也只落各策略自己的 funnel_runs。

AI 终审的入口即本工具的返回：各策略榜单 + 当日情绪 + 每策略 market_regime
的适配标注（fits_today），由 AI 自行决定今天看哪些道、每道看几只。

不做：合并/去重/共振/跨策略排序/加权/二次筛选。
"""
from __future__ import annotations

from typing import Any

from . import screener
from .pipeline import run_pipeline

# 策略 regime 值 → 情绪三值的归一映射（rotation≈震荡、range_bound≈防守）
_REGIME_GROUPS: dict[str, set[str]] = {
    "risk_on": {"risk_on"},
    "neutral": {"neutral", "rotation"},
    "risk_off": {"risk_off", "range_bound"},
}


def strategy_fits_today(strategy_regimes: list[str],
                        sentiment_regime: str | None) -> bool:
    """纯函数：策略 market_regime 是否适配当日市况（归一后取交集）。"""
    if not sentiment_regime:
        return True  # 无情绪数据时不做判断，全部视为可看
    return bool(set(strategy_regimes or []) & _REGIME_GROUPS.get(sentiment_regime, set()))


async def scan_all(top_n: int = 10, min_trend_score: float = 65.0,
                   strategies: list[str] | None = None,
                   fetch_history: bool = False) -> dict[str, Any]:
    """全链：15 策略并行跑 L1→L2（每跑一次存档 funnel_runs）→ 返回各策略榜单。

    返回 {sentiment, strategies{name:{fits_today, market_regime, candidates[]}},
          stats, funnel_run_ids}。candidates 保持各策略自身排序，互不合并。
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
    boards: dict[str, dict] = {}
    funnel_ids: list[int] = []
    errors: dict[str, str] = {}
    for name in names:
        try:
            r = await run_pipeline(name, top_n=top_n,
                                   min_trend_score=min_trend_score,
                                   fetch_history=fetch_history)
        except Exception as e:
            errors[name] = f"{type(e).__name__}: {e}"
            continue
        if "error" in r:
            errors[name] = r["error"]
            continue
        # 存档由 pipeline 内部完成（funnel_runs 唯一规范存档点），此处只收集 id
        if r.get("run_id"):
            funnel_ids.append(r["run_id"])
        boards[name] = {
            "market_regime": all_strats[name].get("market_regime") or [],
            "fits_today": strategy_fits_today(
                all_strats[name].get("market_regime") or [], regime),
            "candidates": r["candidates"],
        }

    n_cands = sum(len(b["candidates"]) for b in boards.values())
    return {
        "sentiment": {"label": label, "regime": regime,
                      "rationale": senti.get("rationale")} if label else None,
        "strategies": boards,
        "stats": {"strategies_run": len(names), "with_candidates": len(boards),
                  "errors": errors or None,
                  "total_candidates": n_cands,
                  "fits_today": sum(1 for b in boards.values() if b["fits_today"])},
        "funnel_run_ids": funnel_ids,
        "next_step": "按当日情绪选道（fits_today 的策略优先看）→ 各策略榜内选优"
                     "→ 层3/4 深评 → 精选 ≤3 → save_recommendation",
    }
