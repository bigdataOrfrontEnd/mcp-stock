"""筹码分布计算 — 纯函数，零网络（复刻东财 stock_cyq_em 的三角分布模型）。

模型（业内通用近似，与东财 F10 筹码同源思路）：
- 逐日推进：历史筹码按换手率衰减（×(1-t)），当日新筹码按三角形分布注入
  —— 三角形底边 [low, high]，顶点在当日均价 avg=(high+low+2*close)/4
- 输出：获利比例、平均成本、90% 成本区间（5%~95% 分位）、筹码集中度

精度说明：东财官方值为精确值，本模型为换手衰减近似（大宗/复权不感知），
筹码数据用于判断套牢区/获利盘压力，近似值足够支撑该场景。
"""
from __future__ import annotations

from typing import Any


def _tri_weight(price: float, low: float, high: float, peak: float) -> float:
    """三角形分布密度（未归一化）。"""
    if price < low or price > high or high <= low:
        return 0.0
    if price == peak:
        return 1.0
    if price < peak:
        return (price - low) / (peak - low) if peak > low else 1.0
    return (high - price) / (high - peak) if high > peak else 1.0


def build_distribution(bars: list[dict], bins_per_yuan: int = 10) -> dict[float, float]:
    """由升序日K（含 turnover_rate）推演最新筹码分布 {价格档: 权重}。"""
    chips: dict[float, float] = {}
    bin_size = 1.0 / bins_per_yuan
    for b in bars:
        low, high = float(b["low"]), float(b["high"])
        close = float(b["close"])
        peak = (high + low + 2 * close) / 4
        t = b.get("turnover_rate")
        t = min(max(float(t) / 100.0, 0.0), 1.0) if isinstance(t, (int, float)) else 0.0
        if t <= 0:
            continue  # 无换手数据（停牌/指数）跳过
        # 历史筹码衰减
        for p in chips:
            chips[p] *= (1.0 - t)
        # 当日新筹码：三角形离散化注入
        n_steps = max(int((high - low) / bin_size), 1)
        raw: list[tuple[float, float]] = []
        for i in range(n_steps + 1):
            p = round(low + (high - low) * i / n_steps, 2)
            raw.append((round(p / bin_size) * bin_size, _tri_weight(p, low, high, peak)))
        denom = sum(w for _, w in raw) or 1.0
        for pb, w in raw:
            key = round(pb, 2)
            chips[key] = chips.get(key, 0.0) + t * w / denom
    total = sum(chips.values())
    if total > 0:
        for p in chips:
            chips[p] /= total
    return chips


def analyze(bars: list[dict], bins_per_yuan: int = 10) -> dict[str, Any]:
    """筹码分析主入口。bars: 升序日K，需含 low/high/close/turnover_rate。

    返回最新交易日的：获利比例、平均成本、90%成本区间、集中度、分布直方图。
    """
    if len(bars) < 10:
        return {"error": "K线不足10根，无法推演筹码"}
    chips = build_distribution(bars, bins_per_yuan)
    if not chips:
        return {"error": "无有效换手数据，无法推演筹码"}
    close = float(bars[-1]["close"])

    prices = sorted(chips)
    cum, cum_map = 0.0, {}
    for p in prices:
        cum += chips[p]
        cum_map[p] = cum

    def quantile(q: float) -> float:
        for p in prices:
            if cum_map[p] >= q:
                return p
        return prices[-1]

    profit = sum(w for p, w in chips.items() if p <= close)
    avg_cost = sum(p * w for p, w in chips.items())
    lo90, hi90 = quantile(0.05), quantile(0.95)
    conc = (hi90 - lo90) / (hi90 + lo90) * 100 if (hi90 + lo90) else None
    # 压力/支撑：上方最近筹码峰 / 下方最近筹码峰
    above = [p for p in prices if p > close * 1.005]
    below = [p for p in prices if p < close * 0.995]
    peaks = sorted(
        [(chips[p], p) for p in prices
         if chips[p] >= max(chips.get(round(q, 2), 0) for q in [p - 0.1, p + 0.1]) and chips[p] > 0.01],
        reverse=True,
    )[:5]

    # 直方图（粗粒度，供展示）
    hist_bins: dict[str, float] = {}
    for p, w in chips.items():
        k = f"{p:.1f}"
        hist_bins[k] = round(hist_bins.get(k, 0) + w * 100, 2)
    hist = dict(sorted(hist_bins.items(), key=lambda x: float(x[0])))

    return {
        "date": bars[-1].get("date"),
        "close": close,
        "profit_ratio_pct": round(profit * 100, 2),          # 获利盘比例
        "avg_cost": round(avg_cost, 2),                       # 平均成本
        "cost_90_low": round(lo90, 2),                        # 90%成本区间下沿(5%分位)
        "cost_90_high": round(hi90, 2),                       # 90%成本区间上沿(95%分位)
        "concentration_pct": round(conc, 2) if conc is not None else None,  # 集中度(越小越集中)
        "pressure_above": round(above[0], 2) if above else None,   # 上方最近套牢价
        "support_below": round(below[-1], 2) if below else None,   # 下方最近筹码价
        "top_peaks": [{"price": p, "weight_pct": round(w * 100, 2)} for w, p in peaks],
        "histogram": hist,
        "note": "换手衰减三角分布近似模型（复刻东财cyq思路），非官方精确值",
    }
