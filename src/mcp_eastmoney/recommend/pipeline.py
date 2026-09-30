"""推荐漏斗编排器 — 把"筛选→复核→排雷→定仓"固化成一条可测试的流水线。

职责边界：本模块只做编排（读 db / 注入数据 → 串 compute 层的信号模块），
不做任何新计算；最终"买不买"的判断权保留给 AI/skill 层（save_recommendation）。

漏斗各级（每级淘汰都留痕，支持复盘）：
  L1 screener.screen   全市场快照硬过滤 + 因子打分 → Top N
  L2 trend_score       单股趋势终审（<min_score 淘汰；一票否决淘汰）
  L3 排雷              业绩预告预减 / 推荐日一字板买不进 → 淘汰或标注
  L4 position          ATR 风险预算 → 建议仓位（止损 entry-2×ATR）

数据注入（依赖倒置，便于离线单测）：rows/bars_map/forecasts 缺省时自动读 db。
"""
from __future__ import annotations

from typing import Any, Callable

from . import screener
from ..compute import indicators, position, trend_score
from ..compute.factors import _pct, _vol_ratio  # noqa: F401  (保持 trend_score 同源)


def _is_yizi(bar: dict) -> bool:
    hi, lo = bar.get("high"), bar.get("low")
    chg = bar.get("change_pct")
    return (hi is not None and lo is not None and float(hi) == float(lo)
            and isinstance(chg, (int, float)) and chg >= 9.8)


def run_pipeline(
    strategy: str,
    top_n: int = 10,
    min_trend_score: float = 65.0,
    capital: float = 100_000.0,
    risk_pct: float = 1.0,
    rows: list[dict] | None = None,
    bars_map: dict[str, list[dict]] | None = None,
    forecasts: dict[str, dict] | None = None,
) -> dict[str, Any]:
    """执行完整推荐漏斗。返回候选包 + 各级淘汰名单（含原因）。

    bars_map/forecasts: {code: 升序bars / 预告dict}，缺省从 db 读取。
    """
    from ..data import db

    strategies = screener.load_strategies()
    s = strategies.get(strategy)
    if not s:
        return {"error": f"未知策略 {strategy!r}", "available": list(strategies)}

    if rows is None:
        rows = db.query_market_snapshot()
    if not rows:
        return {"error": "无市场快照，先运行 refresh snapshot"}

    # ---- L1 硬过滤 + 两段打分（快照段 + 计算层因子段，bars 注入） ----
    def _bars_get(code: str) -> list[dict] | None:
        bars = (bars_map or {}).get(code)
        if bars is None:
            bars = list(reversed(db.query_klines_daily(code, limit=250)))
        return bars if len(bars) >= 30 else None

    scr = screener.screen(rows, s, top_n, bars_get=_bars_get)
    excluded: list[dict] = [
        {"code": r["code"], "stage": "L1", "reason": f"未过筛选（filtered {scr['filtered']}/{scr['universe']}）"}
        for r in []  # L1 淘汰的是 5000+ 全市场，逐条列出无意义，只留统计
    ]

    # ---- L2-L4 逐候选深评 ----
    candidates: list[dict] = []
    for r in scr["ranking"]:
        code = r["code"]
        bars = (bars_map or {}).get(code)
        if bars is None:
            bars = list(reversed(db.query_klines_daily(code, limit=250)))
        if len(bars) < 30:
            excluded.append({"code": code, "stage": "L2", "reason": f"本地K线不足({len(bars)}根)"})
            continue

        # L2 趋势终审
        ts = trend_score.score_trend(bars)
        if "error" in ts:
            excluded.append({"code": code, "stage": "L2", "reason": ts["error"]})
            continue
        if ts["vetoes"]:
            excluded.append({"code": code, "stage": "L2",
                             "reason": f"一票否决: {ts['vetoes'][0]}"})
            continue
        if ts["score"] < min_trend_score:
            excluded.append({"code": code, "stage": "L2",
                             "reason": f"趋势分 {ts['score']} < {min_trend_score}（{ts['grade']}）"})
            continue

        # L3 排雷
        flags: list[str] = []
        fc = (forecasts or {}).get(code)
        if fc is None:
            fc_rows = db.query_earnings_forecast(code=code, limit=1)
            fc = fc_rows[0] if fc_rows else {}
        if fc and fc.get("forecast_type") and "减" in str(fc["forecast_type"]):
            excluded.append({"code": code, "stage": "L3",
                             "reason": f"业绩预告{fc['forecast_type']}({fc.get('change_pct')}%)"})
            continue
        if fc and fc.get("forecast_type"):
            flags.append(f"业绩{fc['forecast_type']}({fc.get('change_pct')}%)")
        last = bars[-1]
        if _is_yizi(last):
            excluded.append({"code": code, "stage": "L3",
                             "reason": "最新交易日一字板，无法买入"})
            continue

        # L4 仓位建议（止损 = entry - 2×ATR14）
        ind = indicators.analyze(bars)
        entry = float(last["close"])
        atr14 = ind.get("atr14")
        stop = round(entry - 2 * atr14, 2) if atr14 else round(entry * 0.94, 2)
        pos = position.position_size(entry, stop, capital=capital,
                                     risk_pct=risk_pct, atr14=atr14)

        candidates.append({
            "code": code, "name": r.get("name"), "close": entry,
            "screen_score": r.get("composite"),
            "trend_score": ts["score"], "trend_grade": ts["grade"],
            "trend_items": ts["items"],
            "flags": flags,
            "position": pos,
            "snapshot": {k: r.get(k) for k in
                         ("change_pct", "amount", "turnover_rate", "volume_ratio", "pe", "pb")},
        })

    candidates.sort(key=lambda x: -(x["trend_score"] or 0))
    return {
        "strategy": s["name"], "display_name": s.get("display_name"),
        "style": s.get("style"),
        "universe": scr["universe"], "filtered": scr["filtered"],
        "stages": {"L1_screen": len(scr["ranking"]), "L2_trend": len(candidates) + len(
            [e for e in excluded if e["stage"] == "L2"]),
            "L3_veto": len(candidates), "final": len(candidates)},
        "candidates": candidates,
        "excluded": excluded,
        "next_step": "AI 终审候选包（≤3只）→ save_recommendation(skill_version=策略名)",
    }
