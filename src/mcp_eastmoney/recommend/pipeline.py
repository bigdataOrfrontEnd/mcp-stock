"""策略漏斗编排器 — 只做 L1 → L2，产出"该策略的标的池"。

职责边界：
  - L1    screener.screen   全市场截面硬过滤 + 因子打分 + 计算层因子过滤 → Top N
  - L2    trend_score       单股趋势终审（<min_score 淘汰；一票否决淘汰）
  - 出口  candidates + excluded（各级淘汰留痕）+ stages（收窄计数），
          run_pipeline 返回后由调用方存档 funnel_runs（可回溯）

L2 之后的动作（打分配置、排雷、仓位、落表、后验）不在本模块：
它们在其他编排层/工具中实施（scan_all → 聚合 → 终审 → save_recommendation）。

数据注入（依赖倒置，便于离线单测）：rows/bars_map 缺省时自动读 db。
"""
from __future__ import annotations

from typing import Any

from . import screener
from ..compute import trend_score


async def run_pipeline(
    strategy: str,
    top_n: int = 10,
    fetch_history: bool = True,
    min_trend_score: float = 65.0,
    rows: list[dict] | None = None,
    bars_map: dict[str, list[dict]] | None = None,
) -> dict[str, Any]:
    """执行 L1 → L2 漏斗，返回标的池 + 各级淘汰名单（含原因）。

    bars_map: {code: 升序bars}，缺省从 db 读取。
    """
    from ..data import db

    strategies = screener.load_strategies()
    s = strategies.get(strategy)
    if not s:
        return {"error": f"未知策略 {strategy!r}", "available": list(strategies)}

    if rows is None:
        rows = db.query_market_snapshot()
    if not rows:
        return {"error": "无市场快照，先运行 refresh daily_market"}

    # ---- L1 硬过滤 + 两段打分（快照段 + 计算层因子段，bars 注入） ----
    def _bars_get(code: str) -> list[dict] | None:
        bars = (bars_map or {}).get(code)
        return bars if bars is not None else screener.local_bars_get(code)

    scr = screener.screen(rows, s, top_n, bars_get=_bars_get)
    # L1 淘汰的是全市场，逐条列出无意义，只留统计进 stages
    excluded: list[dict] = []

    # ---- L2 趋势终审（缺K线/过期当场补拉，一遍收敛） ----
    snap_date = (db.snapshot_dates(1) or [None])[0]  # 截面日期 = 新鲜度基准

    async def _fresh_bars(code: str, bars: list[dict]) -> list[dict]:
        """新鲜度保障：K线末根日期须 ≥ 截面日期，否则增量补拉重读。"""
        if not snap_date or not bars:
            return bars
        if bars[-1].get("date", "") >= snap_date:
            return bars
        if not fetch_history:
            return bars
        try:
            from ..refresh import refresh_valuation as _rv
            await _rv([code])
            refreshed = list(reversed(db.query_klines_daily(code, limit=320)))
            return refreshed if refreshed else bars
        except Exception:
            return bars

    candidates: list[dict] = []
    for r in scr["ranking"]:
        code = r["code"]
        bars = (bars_map or {}).get(code)
        if bars is None:
            bars = list(reversed(db.query_klines_daily(code, limit=320)))
        if len(bars) < 30 and fetch_history:
            # 按需补拉（增量逻辑对稀疏历史会误判 up-to-date，故走全量重拉）
            try:
                from ..refresh import refresh_valuation as _rv
                await _rv([code])
                bars = list(reversed(db.query_klines_daily(code, limit=320)))
            except Exception:
                pass
        bars = await _fresh_bars(code, bars)
        if len(bars) < 30:
            excluded.append({"code": code, "stage": "L2",
                             "reason": f"本地K线不足({len(bars)}根)"})
            continue
        if snap_date and bars[-1].get("date", "") < snap_date:
            excluded.append({"code": code, "stage": "L2",
                             "reason": f"K线过期（末根 {bars[-1].get('date')} < 截面 {snap_date}，补拉失败）"})
            continue

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

        candidates.append({
            "code": code, "name": r.get("name"),
            "close": float(bars[-1]["close"]),
            "screen_score": r.get("composite"),
            "trend_score": ts["score"], "trend_grade": ts["grade"],
            "trend_items": ts["items"],
            "snapshot": {k: r.get(k) for k in
                         ("change_pct", "amount", "turnover_rate",
                          "volume_ratio", "pe", "pb")},
        })

    candidates.sort(key=lambda x: -(x["trend_score"] or 0))
    n_l2_out = len([e for e in excluded if e["stage"] == "L2"])
    stages = {"L1_screen_top": len(scr["ranking"]),
              "L2_trend_in": len(scr["ranking"]),
              "L2_trend_out": n_l2_out,
              "final": len(candidates)}
    # 唯一存档点：每次 L1→L2 运行落 funnel_runs（收窄计数 + 出口标的 + 淘汰明细）
    run_id = None
    try:
        run_id = db.insert_funnel_run(
            s["name"], snap_date, "L2", scr["universe"], scr["filtered"],
            stages, candidates, excluded)
    except Exception:
        pass
    return {
        "strategy": s["name"], "display_name": s.get("display_name"),
        "style": s.get("style"), "snapshot_date": snap_date,
        "universe": scr["universe"], "filtered": scr["filtered"],
        "stages": stages,
        "mode": "L2",
        "run_id": run_id,
        "candidates": candidates,
        "excluded": excluded,
        "next_step": "策略标的池（L2 出口）→ 聚合/终审/落表在其它编排层实施",
    }
