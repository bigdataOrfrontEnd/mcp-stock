"""仓位计算 — 风险预算法（ATR 健全性校验）。纯计算，零网络零落表。

复用最优部分：止损位直接接受 chan.analyze() 买卖点自带的 stop_loss
（或调用方指定的任何止损价）；ATR14 来自 indicators.analyze()。
本模块只做一件事：入场价、止损价、资金、风险预算 → 股数与仓位。
"""
from __future__ import annotations

import math
from typing import Any


def position_size(entry: float, stop_loss: float, capital: float = 100_000.0,
                  risk_pct: float = 1.0, max_position_pct: float = 30.0,
                  atr14: float | None = None,
                  atr_mult_cap: float = 3.0) -> dict[str, Any]:
    """风险预算法定仓位（做多）。

    - 单笔风险金额 = capital * risk_pct%
    - 股数 = 风险金额 / (entry - stop_loss)，按 100 股取整
    - 上限 = capital * max_position_pct% 对应股数
    - ATR 校验：止损距离 < 0.5*ATR 警告过近（易被噪音打掉）；
      > atr_mult_cap*ATR 警告过远（单笔风险实际超预算口径）。
    """
    entry, stop_loss = float(entry), float(stop_loss)
    capital, risk_pct, max_position_pct = float(capital), float(risk_pct), float(max_position_pct)
    if entry <= 0 or capital <= 0:
        return {"error": "entry/capital 必须为正"}
    if stop_loss >= entry:
        return {"error": f"做多止损价({stop_loss})必须低于入场价({entry})"}

    stop_dist = entry - stop_loss
    stop_dist_pct = round(stop_dist / entry * 100, 2)
    risk_amount = round(capital * risk_pct / 100, 2)

    shares = math.floor(risk_amount / stop_dist / 100) * 100
    cap_shares = math.floor(capital * max_position_pct / 100 / entry / 100) * 100
    capped = shares > cap_shares
    shares = max(shares, 0)
    if capped:
        shares = cap_shares

    notes: list[str] = []
    if atr14:
        if stop_dist < 0.5 * atr14:
            notes.append(f"止损距离{stop_dist:.2f} < 0.5*ATR14({0.5 * atr14:.2f})，"
                         "过近易被日内噪音打掉，建议放宽止损或降低预期")
        if stop_dist > atr_mult_cap * atr14:
            notes.append(f"止损距离{stop_dist:.2f} > {atr_mult_cap}*ATR14"
                         f"({atr_mult_cap * atr14:.2f})，过远，触发时亏损将超出预算口径")
    if capped:
        notes.append(f"已按最大仓位 {max_position_pct}% 封顶")
    if shares == 0:
        notes.append("风险预算不足以买入 1 手（100 股），建议放弃或放宽预算")

    cost = round(shares * entry, 2)
    return {
        "entry": entry, "stop_loss": stop_loss,
        "stop_dist": round(stop_dist, 2), "stop_dist_pct": stop_dist_pct,
        "capital": capital, "risk_pct": risk_pct,
        "risk_amount": risk_amount,
        "shares": shares, "cost": cost,
        "position_pct": round(cost / capital * 100, 2),
        "max_loss": round(shares * stop_dist, 2),
        "atr14": atr14,
        "notes": notes,
        "note": "风险预算法定仓位，供参考不构成投资建议",
    }
