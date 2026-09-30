"""自选股异动监听策略 — 纯计算（借鉴 StockAgent listener/strategies/）。

输入：每股实时行情 quote + 本地日K bars（升序）。输出触发列表。
策略：
- price_change  盘中涨跌幅越限（默认 |change_pct| >= 5%）
- limit_open    昨日涨停今日炸板/开板（昨 change_pct>=9.8 且现 < 7%）
- ma5_break     今日收盘上穿 MA5（昨在下方今在上方）
- gap           开盘跳空（开盘价相对昨收 |gap| >= 2%）

quote 字段: change_pct, price, open, prev_close（缺字段则跳过对应策略）。
"""
from __future__ import annotations

from typing import Any

DEFAULTS = {
    "price_change_pct": 5.0,
    "gap_pct": 2.0,
    "zt_threshold": 9.8,   # 视为涨停的最低涨幅
    "open_board_pct": 7.0,  # 涨停后回落到该涨幅以下视为开板
    "volume_ratio_spike": 3.0,  # 量比异动阈值（借鉴 DSA listener 量能策略）
}


def _ma5(closes: list[float]) -> float | None:
    if len(closes) < 5:
        return None
    return sum(closes[-5:]) / 5


def check_stock(code: str, quote: dict[str, Any], bars: list[dict] | None,
                cfg: dict | None = None) -> list[dict]:
    """对单只股票跑全部策略，返回触发的告警列表。"""
    c = {**DEFAULTS, **(cfg or {})}
    alerts: list[dict] = []

    chg = quote.get("change_pct")
    if isinstance(chg, (int, float)):
        if abs(chg) >= c["price_change_pct"]:
            alerts.append({
                "type": "price_change",
                "level": "warn" if abs(chg) < 8 else "critical",
                "detail": f"盘中涨跌幅 {chg:+.2f}% 越限 ±{c['price_change_pct']}%",
            })

    prev_chg = None
    if bars and len(bars) >= 2:
        pc = bars[-1].get("change_pct")
        prev_chg = float(pc) if isinstance(pc, (int, float)) else None
        if prev_chg is not None and prev_chg >= c["zt_threshold"] \
                and isinstance(chg, (int, float)) and chg < c["open_board_pct"]:
            alerts.append({
                "type": "limit_open",
                "level": "critical",
                "detail": f"昨日涨停({prev_chg:+.2f}%) 现涨幅回落至 {chg:+.2f}%，疑似开板/炸板",
            })

        closes = [float(b["close"]) for b in bars]
        ma5_now, ma5_prev = _ma5(closes), _ma5(closes[:-1]) if len(closes) >= 6 else None
        if ma5_now and ma5_prev:
            px = quote.get("price") or closes[-1]
            crossed = closes[-2] <= ma5_prev and float(px) > ma5_now
            if crossed:
                alerts.append({
                    "type": "ma5_break",
                    "level": "info",
                    "detail": f"现价 {px} 上穿 MA5({ma5_now:.2f})",
                })

    op, pc = quote.get("open"), quote.get("prev_close")
    if isinstance(op, (int, float)) and isinstance(pc, (int, float)) and pc:
        gap = (op / pc - 1) * 100
        if abs(gap) >= c["gap_pct"]:
            alerts.append({
                "type": "gap",
                "level": "info",
                "detail": f"开盘跳空 {gap:+.2f}%（开 {op} / 昨收 {pc}）",
            })

    # 量比异动（资金突然进出的领先信号）
    vr = quote.get("volume_ratio")
    if isinstance(vr, (int, float)) and vr >= c["volume_ratio_spike"]:
        level = "warn" if vr < 5 else "critical"
        direction = "放量" if (chg or 0) > 0 else "放量下杀"
        alerts.append({
            "type": "volume_spike",
            "level": level,
            "detail": f"量比 {vr:.1f} {direction}（阈值 {c['volume_ratio_spike']}）",
        })

    return [{"code": code, **a} for a in alerts]


def check_watchlist(quotes: dict[str, dict[str, Any]],
                    bars_map: dict[str, list[dict]],
                    cfg: dict | None = None) -> dict:
    """对自选股批量检测。quotes/bars_map 均以 code 为键。"""
    triggered: list[dict] = []
    skipped: list[str] = []
    for code in quotes:
        try:
            triggered.extend(check_stock(code, quotes[code], bars_map.get(code), cfg))
        except Exception as e:  # 单只失败不阻断
            skipped.append(f"{code}: {e}")
    triggered.sort(key=lambda a: {"critical": 0, "warn": 1, "info": 2}.get(a["level"], 3))
    return {"triggered": triggered, "total_checked": len(quotes),
            "triggered_count": len(triggered), "skipped": skipped}
