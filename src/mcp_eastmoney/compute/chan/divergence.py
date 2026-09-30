"""背驰检测：MACD 同向柱面积对比 + RSI/KDJ 背离共振加成。

自 stock-chanlun backend/ai/divergence.py 移植为纯 Python。
- 向上笔比较红柱（bar>0）累计面积，向下笔比较绿柱（bar<0）绝对值面积
- 面积过小回退为全绝对值面积
- 后段价格新高/新低 且 MACD 面积 < 前段 × 0.85 → 背驰
- RSI / KDJ(J) 同向背离各 +0.07 概率
"""
from __future__ import annotations

from typing import Optional

from ..indicators import macd as _macd, rsi as _rsi, kdj as _kdj
from .structure import Bi

MACD_WEAKEN_RATIO = 0.85      # 后段面积低于前段该比例以下视为力度显著减弱
MACD_AREA_EPS = 1e-12
MIN_BARS_PER_SEGMENT = 2
OSC_CONFIRM_BOOST = 0.07


def _directional_area(bars: list[float], direction: str) -> float:
    """与走势同向的 MACD 柱累计面积（up 取红柱，down 取绿柱绝对值）。"""
    if direction == "up":
        return sum(max(b, 0.0) for b in bars)
    return sum(abs(min(b, 0.0)) for b in bars)


def _abs_area(bars: list[float]) -> float:
    return sum(abs(b) for b in bars)


def check_beichi(merged: list[dict], bis: list[Bi]) -> Optional[dict]:
    """比较最近两个同向笔的力度。顶、底同时满足时取概率更高者。"""
    if len(bis) < 4:
        return None

    closes = [float(b["close"]) for b in merged]
    m = _macd(closes)
    r = _rsi(closes, 14)
    j_line = _kdj(merged)["j"]

    up_segments = [b for b in bis if b.direction == "up"]
    down_segments = [b for b in bis if b.direction == "down"]
    candidates: list[dict] = []

    if len(up_segments) >= 2:
        res = _compare(up_segments[-2], up_segments[-1], "top", m, r, j_line)
        if res:
            candidates.append(res)
    if len(down_segments) >= 2:
        res = _compare(down_segments[-2], down_segments[-1], "bottom", m, r, j_line)
        if res:
            candidates.append(res)
    if not candidates:
        return None
    return max(candidates, key=lambda c: c["probability"])


def _compare(seg1: Bi, seg2: Bi, seg_type: str,
             macd_data: dict, rsi_vals: list[float], j_vals: list[float]) -> Optional[dict]:
    i0, i1 = seg1.s_idx, seg1.e_idx
    j0, j1 = seg2.s_idx, seg2.e_idx
    if j1 - j0 + 1 < MIN_BARS_PER_SEGMENT or i1 - i0 + 1 < MIN_BARS_PER_SEGMENT:
        return None

    direction = "up" if seg_type == "top" else "down"
    b1 = macd_data["bar"][i0:i1 + 1]
    b2 = macd_data["bar"][j0:j1 + 1]

    d1 = _directional_area(b1, direction)
    d2 = _directional_area(b2, direction)
    if d1 > MACD_AREA_EPS and d2 > MACD_AREA_EPS:
        macd1, macd2, force_kind = d1, d2, "directional"
    else:
        macd1, macd2, force_kind = _abs_area(b1), _abs_area(b2), "abs"
    if macd1 <= MACD_AREA_EPS:
        return None

    if seg_type == "bottom":
        price1, price2 = seg1.low, seg2.low
        if not (price2 < price1 and macd2 < macd1 * MACD_WEAKEN_RATIO):
            return None
        prefix = "价格新低"
    else:
        price1, price2 = seg1.high, seg2.high
        if not (price2 > price1 and macd2 < macd1 * MACD_WEAKEN_RATIO):
            return None
        prefix = "价格新高"

    ratio = macd2 / macd1
    prob = min(1.0, (1 - ratio) + 0.5)

    # RSI / KDJ(J) 背离共振
    rsi1 = rsi_vals[i0:i1 + 1]
    rsi2 = rsi_vals[j0:j1 + 1]
    j1v = j_vals[i0:i1 + 1]
    j2v = j_vals[j0:j1 + 1]
    if seg_type == "top":
        rsi_ok = bool(rsi2) and bool(rsi1) and max(rsi2) < max(rsi1)
        kdj_ok = bool(j2v) and bool(j1v) and max(j2v) < max(j1v)
    else:
        rsi_ok = bool(rsi2) and bool(rsi1) and min(rsi2) > min(rsi1)
        kdj_ok = bool(j2v) and bool(j1v) and min(j2v) > min(j1v)
    if rsi_ok:
        prob = min(1.0, prob + OSC_CONFIRM_BOOST)
    if kdj_ok:
        prob = min(1.0, prob + OSC_CONFIRM_BOOST)

    tags = [t for t, ok in (("RSI背离", rsi_ok), ("KDJ背离", kdj_ok)) if ok]
    extra = f"，{'/'.join(tags)}共振" if tags else ""
    delta = abs(price2 - price1)

    return {
        "type": "底背驰" if seg_type == "bottom" else "顶背驰",
        "compare": f"{seg1.s_date}~{seg1.e_date} vs {seg2.s_date}~{seg2.e_date}",
        "probability": round(prob, 2),
        "macd_ratio": round(ratio, 2),
        "macd_force": force_kind,
        "rsi_confirm": rsi_ok,
        "kdj_confirm": kdj_ok,
        "description": f"{prefix}{delta:.2f}但MACD力度减弱至{ratio:.0%}{extra}",
    }
