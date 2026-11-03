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
from ..compute.dragon import _is_yizi  # 一字判定公式唯一来源在计算层
from ..compute.factors import _pct, _vol_ratio  # noqa: F401  (保持 trend_score 同源)
from ..compute.position import atr_stop


async def run_pipeline(
    strategy: str,
    top_n: int = 10,
    fetch_history: bool = True,
    min_trend_score: float = 65.0,
    capital: float = 100_000.0,
    risk_pct: float = 1.0,
    stop_after: str = "full",
    rows: list[dict] | None = None,
    bars_map: dict[str, list[dict]] | None = None,
    forecasts: dict[str, dict] | None = None,
) -> dict[str, Any]:
    """执行推荐漏斗。返回候选包 + 各级淘汰名单（含原因）。

    stop_after:
      "L2"   —— 策略边界模式：到 L2 趋势终审出口为止，产出"该策略的标的池"
                （L2.5 策略补考/L3 排雷/L4 定仓不执行，供 15 策略并行分析用；
                 打分排序优化后续在 L2.5 统一做）。
      "full" —— 完整漏斗（默认，含 L2.5/L3/L4，产出可落表候选包）。
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
            bars = list(reversed(db.query_klines_daily(code, limit=320)))
        # 有行即返回：历史过短（次新股）是"真实不足"，交由 compute_filters 判死；
        # 完全无行（从未入库）才是"基础设施缺口"，返回 None 走降级放行。
        return bars if bars else None

    scr = screener.screen(rows, s, top_n, bars_get=_bars_get)
    # L1 淘汰的是 5000+ 全市场，逐条列出无意义，只留统计进 excluded 的说明位
    excluded: list[dict] = []

    # ---- L2-L4 逐候选深评（缺K线/ K线过期 当场补拉，一遍收敛） ----
    snap_date = (db.snapshot_dates(1) or [None])[0]  # 快照日期 = 新鲜度基准

    async def _fresh_bars(code: str, bars: list[dict]) -> list[dict]:
        """新鲜度保障：K线末根日期须 ≥ 快照日期，否则增量补拉重读。

        与 skill 层"候选股日K必须为 T-1"规则对齐，服务端强制执行：
        陈旧K线的 trend_score/缠论结论视为无效（修复前会静默放行/误杀）。
        无快照基准（snap_date 为 None，离线/测试注入场景）时不动数据。
        """
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
            return refreshed if refreshed else bars  # 补拉失败保留原K线，交由过期判定淘汰
        except Exception:
            return bars

    candidates: list[dict] = []
    cf = s.get("compute_filters") or {}  # L2.5 策略条件复核用（延迟审判）
    for r in scr["ranking"]:
        code = r["code"]
        bars = (bars_map or {}).get(code)
        if bars is None:
            bars = list(reversed(db.query_klines_daily(code, limit=320)))
        if len(bars) < 30 and fetch_history:
            # 按需全历史补拉（增量逻辑对稀疏历史会误判 up-to-date，故走全量重拉）
            try:
                from ..refresh import refresh_valuation as _rv
                await _rv([code])
                bars = list(reversed(db.query_klines_daily(code, limit=320)))
            except Exception:
                pass
        bars = await _fresh_bars(code, bars)
        if len(bars) < 30:
            excluded.append({"code": code, "stage": "L2", "reason": f"本地K线不足({len(bars)}根)"})
            continue
        if snap_date and bars[-1].get("date", "") < snap_date:
            excluded.append({"code": code, "stage": "L2",
                             "reason": f"K线过期（末根 {bars[-1].get('date')} < 快照 {snap_date}，补拉失败）"})
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

        if stop_after == "L2":
            # 策略边界模式：L2 出口即该策略的标的池（后续 L2.5 打分优化/L3/L4 另行编排）
            candidates.append({
                "code": code, "name": r.get("name"),
                "close": float(bars[-1]["close"]),
                "screen_score": r.get("composite"),
                "trend_score": ts["score"], "trend_grade": ts["grade"],
                "trend_items": ts["items"],
                "snapshot": {k: r.get(k) for k in
                             ("change_pct", "amount", "turnover_rate", "volume_ratio", "pe", "pb")},
            })
            continue

        # L2.5 策略条件复核（延迟审判的最后一环）：L1.5 缺K线降级放行的票，
        # 在此用补齐后的 K 线补考 compute_filters——策略核心条件不允许被数据缺口跳过
        # （例：no_retrace_uptrend 的 mom60≥60，不能因为本地没K线就免试）。
        if cf:
            from ..compute import factors as _F
            fin = None
            if set(cf) & {"roe", "gross_margin", "net_margin"}:
                fin = db.latest_financials([code]).get(code)
            vals = _F.compute_factors(bars, list(cf.keys()), fin=fin)
            fail = None
            for k, lim in cf.items():
                v = vals.get(k)
                if v is None or ("min" in lim and v < lim["min"]) \
                        or ("max" in lim and v > lim["max"]):
                    fail = (k, v, lim)
                    break
            if fail:
                k, v, lim = fail
                excluded.append({"code": code, "stage": "L2",
                                 "reason": f"策略条件复核未过: {k}={v} 需 {lim}（L1.5缺K线降级，补数据后补考）"})
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

        # L4 仓位建议（止损公式在 compute/position.atr_stop）
        ind = indicators.analyze(bars)
        entry = float(last["close"])
        atr14 = ind.get("atr14")
        stop = atr_stop(entry, atr14)  # 公式在 compute/position（分层约定）
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
    # 漏斗存档（与 screen 工具一致）：screen_runs 是"策略 → 推荐"归因的对照数据
    run_id = None
    try:
        run_id = db.insert_screen_run(
            s["name"], scr["universe"], scr["filtered"],
            [{"code": c["code"], "trend_score": c["trend_score"],
              "close": c["close"], "stage": "pipeline_final"}
             for c in candidates] or scr["ranking"])
    except Exception:
        pass
    n_l2_out = len([e for e in excluded if e["stage"] == "L2"])
    n_l3_out = len([e for e in excluded if e["stage"] == "L3"])
    return {
        "strategy": s["name"], "display_name": s.get("display_name"),
        "style": s.get("style"), "snapshot_date": snap_date,
        "universe": scr["universe"], "filtered": scr["filtered"],
        "stages": {"L1_screen_top": len(scr["ranking"]),
                   "L2_trend_in": len(scr["ranking"]),
                   "L2_trend_out": n_l2_out,
                   "L3_veto_out": n_l3_out,
                   "final": len(candidates)},
        "mode": stop_after,
        "run_id": run_id,
        "candidates": candidates,
        "excluded": excluded,
        "next_step": (
            "策略标的池（L2 出口）→ 供 15 策略并行分析/共振筛选"
            if stop_after == "L2" else
            "AI 终审候选包（≤3只）→ save_recommendation(skill_version=策略名)"),
    }
