"""缠论分析引擎 — 串联：合并K线 → 分型 → 笔 → 线段 → 中枢 → 买卖点/背驰 → 摘要。

自 stock-chanlun backend/chanlun/engine.py 移植。
输入：升序K线 list[dict]（date/open/high/low/close/volume），
输出：纯 dict（JSON 可序列化），便于 MCP 工具直接返回。
"""
from __future__ import annotations

from typing import Any

from .structure import merge_klines, build_bis, detect_segments, detect_zhongshus
from .signals import SignalDetector
from .divergence import check_beichi


def analyze(bars: list[dict], level: str = "daily",
            min_bars: int = 30) -> dict[str, Any]:
    """完整缠论分析。bars 需 >= min_bars 根（建议 120+，稳定结构 250+）。"""
    if len(bars) < min_bars:
        return {"error": f"K线不足（{len(bars)} 根，至少 {min_bars} 根）"}

    merged = merge_klines(bars)
    bis = build_bis(merged, min_bars=5)
    segments = detect_segments(bis, min_overlap_bis=3)
    zhongshus = detect_zhongshus(segments)

    detector = SignalDetector(bis, segments, zhongshus, level=level)
    raw_signals = detector.detect_all()
    signals = [
        {k: v for k, v in s.items() if k != "_idx"}
        for s in raw_signals[-10:]          # 只保留最近 10 个信号
    ]
    trend = detector.detect_trend()
    support_resistance = detector.detect_support_resistance(merged, raw_signals)
    beichi = check_beichi(merged, bis)

    close = float(bars[-1]["close"])
    last_zs = zhongshus[-1] if zhongshus else None
    position = None
    if last_zs:
        if close > last_zs.upper:
            position = "中枢上方（强势区，三买观察区）"
        elif close < last_zs.lower:
            position = "中枢下方（弱势区，一买观察区）"
        else:
            position = "中枢内部（震荡区）"

    # 当前未完成笔：最后一笔终点到现在
    current = None
    if bis:
        tail = merged[bis[-1].e_idx:]
        if len(tail) >= 2:
            current = {
                "from": bis[-1].e_date,
                "bars_since": len(tail),
                "tentative_direction": "down" if bis[-1].direction == "up" else "up",
                "tail_high": round(max(float(b["high"]) for b in tail), 2),
                "tail_low": round(min(float(b["low"]) for b in tail), 2),
            }

    return {
        "date": bars[-1]["date"],
        "close": close,
        "merged_bars": len(merged),
        "fractals_recent": _recent_fractals(merged),
        "bis_recent": [
            {"id": i, "dir": b.direction, "from": b.s_date, "to": b.e_date,
             "high": round(b.high, 2), "low": round(b.low, 2),
             "range": round(b.range, 2)}
            for i, b in enumerate(bis[-8:])
        ],
        "current_bi_tentative": current,
        "segments_recent": [
            {"id": i, "dir": s.direction, "from": s.s_date, "to": s.e_date,
             "high": round(s.high, 2), "low": round(s.low, 2),
             "bis": len(s.bi_ids)}
            for i, s in enumerate(segments[-6:])
        ],
        "zhongshu_recent": [
            {"id": i, "from": z.s_date, "to": z.e_date,
             "upper": round(z.upper, 2), "lower": round(z.lower, 2),
             "segments": len(z.xiang_ids)}
            for i, z in enumerate(zhongshus[-3:])
        ],
        "position": position,
        "signals": signals,
        "beichi": beichi,
        "trend": trend,
        "support_resistance": support_resistance[:8],
        "summary": _make_summary(trend, signals, last_zs, beichi),
        "disclaimer": "缠论结构会被后续K线回溯修正，结果仅供参考，不构成投资建议",
    }


def _recent_fractals(merged: list[dict]) -> list[dict]:
    """最近 6 个分型（顶/底），复用 structure 的分型逻辑但输出轻量 dict。"""
    from .structure import find_fractals, compress_fractals
    return [
        {"type": f.type, "price": round(f.high if f.type == "top" else f.low, 2),
         "date": f.date}
        for f in compress_fractals(find_fractals(merged))[-6:]
    ]


def _make_summary(trend: str, signals: list[dict], last_zs, beichi) -> str:
    parts = [f"当前走势: {trend}"]
    if last_zs is not None:
        parts.append(f"中枢区间: [{last_zs.lower:.2f}, {last_zs.upper:.2f}]")
    if beichi:
        parts.append(f"{beichi['type']}（概率{beichi['probability']}）")
    if signals:
        latest = signals[-1]
        parts.append(f"最近信号: {latest['type']} @ {latest['price']:.2f}")
    else:
        parts.append("暂无明确买卖点")
    return " | ".join(parts)
