"""龙头扫描编排 — 汇集 zt_pool/snapshot/index_daily/bars 喂给 compute.dragon。

职责：只做数据汇集（db 读取 + 缺K线自动补），评分逻辑全在 compute/dragon.py。
eod 每日自动扫描存档（screen_runs, strategy=dragon_v1），tool 实时调用复用本模块。
"""
from __future__ import annotations

import asyncio
from typing import Any

from ..compute import dragon as D
from ..compute.factors import _pct
from ..data import db


def _sector_context(date_iso: str) -> dict[str, dict]:
    """行业上下文：板块内涨停家数（zt_pool）+ 行业指数当日涨幅 + 10日累计。"""
    ctx: dict[str, dict] = {}
    # 板块内涨停家数
    con = db._conn()
    try:
        for ind, cnt in con.execute(
            "SELECT industry, COUNT(*) FROM zt_pool WHERE trade_date=? GROUP BY industry",
            (date_iso,),
        ).fetchall():
            if ind:
                ctx.setdefault(ind, {})["zt_count"] = cnt
    finally:
        con.close()
    # 行业指数当日涨幅 + 10日累计（THS 行业，名称直接匹配，缺失留空）
    con = db._conn()
    try:
        for name, chg, cum10 in con.execute(
            """
            SELECT name,
                   SUM(CASE WHEN rn=1 THEN change_pct END),
                   (SUM(CASE WHEN rn=1 THEN close END) /
                    SUM(CASE WHEN rn=10 THEN close END) - 1) * 100
            FROM (SELECT code, name, change_pct, close,
                         ROW_NUMBER() OVER (PARTITION BY code ORDER BY trade_date DESC) rn
                  FROM index_daily WHERE category='industry'
                      AND trade_date<=?)
            WHERE rn<=10 GROUP BY code, name
            """,
            (date_iso,),
        ).fetchall():
            if name:
                c = ctx.setdefault(name, {})
                if chg is not None:
                    c["sector_pct"] = chg
                if cum10 is not None:
                    c["cum10"] = round(cum10, 2)
    finally:
        con.close()
    return ctx


def _sector_context_for(zt_rows: list[dict], ctx: dict[str, dict]) -> dict[str, dict]:
    """给涨停池行业名做 THS 指数名模糊对齐（东财简称常截断，如"汽车零部"→"汽车零部件"）。"""
    def match(ind: str) -> dict:
        if not ind:
            return {}
        if ind in ctx:
            return ctx[ind]
        for name, c in ctx.items():
            if len(ind) >= 3 and (ind[:3] in name or name[:3] in ind):
                return c
        return {}
    return {r.get("industry") or "": match(r.get("industry") or "")
            for r in zt_rows if r.get("industry")}


async def _m15_map_for(codes: list[str]) -> dict[str, list[dict]]:
    """涨停池 15 分钟线：库优先，缺则新浪现拉并落库（错过不可补，盘中抓到就是赚到）。

    返回 {code: 升序 bars（datetime/open/high/low/close/volume）}。
    """
    from ..data import tencent_client
    from datetime import datetime as _dt
    today = _dt.now().strftime("%Y-%m-%d")
    out: dict[str, dict] = {}
    missing = []
    for c in codes:
        rows = db.query_klines_15m(c, limit=16)   # 16根=全天
        bars = list(reversed(rows))
        # 新鲜度检查：库内 16 根须含今日（否则"早盘冲击力"用的是旧数据）
        fresh = len(bars) >= 5 and str(bars[-1]["datetime"]).startswith(today)
        if fresh:
            out[c] = [{"datetime": b["datetime"], "open": b["open"], "high": b["high"],
                       "low": b["low"], "close": b["close"], "volume": b["volume"]}
                      for b in bars]
        else:
            missing.append(c)
    for c in missing:
        try:
            bars = await tencent_client.get_kline_15m(c, datalen=16)
            if len(bars) >= 5:
                out[c] = bars
                db.upsert_klines_15m([{"code": c, **b} for b in bars])
            await asyncio.sleep(0.2)
        except Exception:
            continue  # 单只失败不阻断
    return out


# 早盘冲击力的数学实现已下沉 compute/dragon.py（分层修正：推荐层不含公式）
from ..compute.dragon import early_impulse as _early_impulse  # noqa: F401


async def scan_dragons(top: int = 5, persist: bool = False,
                       fetch_history: bool = True) -> dict[str, Any]:
    """扫描当日（或最新快照日）涨停池，识别真龙。"""
    dates = db.zt_pool_dates()[:1]
    if not dates:
        return {"error": "无涨停池快照，先运行 refresh ztpool"}
    date_iso = dates[0]
    zt_rows = db.query_zt_pool(date_iso, min_lian_ban=1)
    if not zt_rows:
        return {"error": f"{date_iso} 涨停池为空"}

    # 候选K线：库优先，缺则补拉 30 日（5日涨幅够用）
    bars_map: dict[str, list[dict]] = {}
    missing = []
    for r in zt_rows:
        rows = db.query_klines_daily(r["code"], limit=15)
        bars = list(reversed(rows))
        if len(bars) >= 7:
            bars_map[r["code"]] = bars
        else:
            missing.append(r["code"])
    if missing and fetch_history:
        from ..refresh import refresh_daily as _rd
        try:
            await _rd(missing, days=30)
            for c in missing:
                rows = db.query_klines_daily(c, limit=15)
                if len(rows) >= 7:
                    bars_map[c] = list(reversed(rows))
        except Exception:
            pass  # 补拉失败该股 mom5=None → 中性分

    mom5_map = {}
    for c, bars in bars_map.items():
        closes = [float(b["close"]) for b in bars]
        mom5_map[c] = _pct(closes, 5)

    sector_ctx = _sector_context_for(zt_rows, _sector_context(date_iso))

    # 承接的自有代理：板块近5个交易日日均涨停家数（zt_pool 历史聚合，不依赖THS名称匹配）
    sector_abs: dict[str, float] = {}
    con = db._conn()
    try:
        rows_abs = con.execute(
            """
            SELECT industry, AVG(c) FROM (
                SELECT industry, trade_date, COUNT(*) c FROM zt_pool
                WHERE industry IS NOT NULL AND trade_date <= ?
                GROUP BY industry, trade_date
                ORDER BY trade_date DESC LIMIT 200
            ) GROUP BY industry
            """, (date_iso,)).fetchall()
        for ind, cnt in rows_abs:
            if ind:
                sector_abs[ind] = round(cnt, 2)
    finally:
        con.close()

    # 当日行情行（一字判定 + 振幅）：由 quotes_daily 当日行派生（v3 起唯一真相源）
    snap_map: dict[str, dict] = {}
    for r in db.query_market_snapshot(date_iso) if date_iso else []:
        snap_map[r["code"]] = {"open": r.get("open"), "high": r.get("high"),
                               "low": r.get("low"), "amplitude": r.get("amplitude")}

    # 大盘基准：上证指数最新涨幅（腾讯兜底行可能缺 change_pct → 用前收推算）
    bench_rows = db.query_index_daily("sh.000001", limit=2)
    broad = None
    if bench_rows:
        b = bench_rows[0]
        if b["change_pct"] is not None:
            broad = b["change_pct"]
        elif len(bench_rows) >= 2 and bench_rows[1]["close"]:
            broad = round((b["close"] / bench_rows[1]["close"] - 1) * 100, 2)
    bench = {"broad": broad, "sector_default": None}

    # 15分钟线 → 早盘冲击力（带动性的分钟级升级；缺失自动回退 v2 权重）
    m15_map: dict[str, list[dict]] = {}
    if fetch_history:
        try:
            m15_map = await _m15_map_for([r["code"] for r in zt_rows])
        except Exception:
            m15_map = {}
    impulse_map = {c: _early_impulse(b) for c, b in m15_map.items()}

    ranked = D.score_dragons(zt_rows, mom5_map, sector_ctx, bench,
                             snap_map=snap_map, sector_abs=sector_abs,
                             impulse_map=impulse_map)
    dragons = [x for x in ranked if x["is_true_dragon"]][:top]
    result = {
        "date": date_iso, "strategy": "dragon_v1",
        "display_name": "龙头识别（dragon-quant 移植）",
        "universe": len(zt_rows), "true_dragons": len([x for x in ranked if x["is_true_dragon"]]),
        "benchmark_broad_pct": bench["broad"],
        "dragons": dragons,
        "all_ranked_head": ranked[:10],
        "note": "五维日线级近似（带动/领涨/抗跌/流动/承接），门槛40/40/35/35一票否决",
    }
    if persist and dragons:
        try:
            result["run_id"] = db.insert_screen_run(
                "dragon_v1", len(zt_rows),
                result["true_dragons"], dragons)
        except Exception:
            pass
    return result
