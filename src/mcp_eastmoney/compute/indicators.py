"""通用技术指标 — 纯计算，零网络零落表。

输入：K 线列表 [{"date","open","high","low","close","volume",...}, ...]（按日期升序）
输出：最新指标值 + 简单信号。所有函数为纯函数，可独立测试。
"""
from __future__ import annotations

from typing import Any


def _closes(bars: list[dict]) -> list[float]:
    return [float(b["close"]) for b in bars]


def _highs(bars: list[dict]) -> list[float]:
    return [float(b["high"]) for b in bars]


def _lows(bars: list[dict]) -> list[float]:
    return [float(b["low"]) for b in bars]


# ---- 单指标 ----------------------------------------------------------------------


def ma(vals: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(vals)
    for i in range(n - 1, len(vals)):
        out[i] = sum(vals[i - n + 1 : i + 1]) / n
    return out


def ema(vals: list[float], n: int) -> list[float]:
    k = 2 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def macd(vals: list[float], fast: int = 12, slow: int = 26, sig: int = 9) -> dict:
    ef, es = ema(vals, fast), ema(vals, slow)
    dif = [a - b for a, b in zip(ef, es)]
    dea = ema(dif, sig)
    bar = [(a - b) * 2 for a, b in zip(dif, dea)]
    return {"dif": dif, "dea": dea, "bar": bar}


def kdj(bars: list[dict], n: int = 9, k_s: int = 3, d_s: int = 3) -> dict:
    k_list, d_list, j_list = [50.0], [50.0], [50.0]
    for i in range(1, len(bars)):
        lo = min(_lows(bars)[max(0, i - n + 1) : i + 1])
        hi = max(_highs(bars)[max(0, i - n + 1) : i + 1])
        rsv = 50.0 if hi == lo else (_closes(bars)[i] - lo) / (hi - lo) * 100
        k = (2 / 3) * k_list[-1] + (1 / 3) * rsv
        d = (2 / 3) * d_list[-1] + (1 / 3) * k
        k_list.append(k)
        d_list.append(d)
        j_list.append(3 * k - 2 * d)
    return {"k": k_list, "d": d_list, "j": j_list}


def rsi(vals: list[float], n: int) -> list[float]:
    """Cutler RSI（窗口简单平均版），无平滑误差。"""
    out = [50.0]
    for i in range(1, len(vals)):
        window = vals[max(0, i - n) : i + 1]
        diffs = [window[j] - window[j - 1] for j in range(1, len(window))]
        gains = sum(d for d in diffs if d > 0) / max(len(diffs), 1)
        losses = -sum(d for d in diffs if d < 0) / max(len(diffs), 1)
        out.append(100 - 100 / (1 + (gains / losses if losses else float("inf"))))
    return out


def boll(vals: list[float], n: int = 20, width: float = 2.0) -> dict:
    mid = ma(vals, n)
    upper: list[float | None] = [None] * len(vals)
    lower: list[float | None] = [None] * len(vals)
    for i in range(n - 1, len(vals)):
        win = vals[i - n + 1 : i + 1]
        m = mid[i]
        sd = (sum((x - m) ** 2 for x in win) / n) ** 0.5  # type: ignore
        upper[i], lower[i] = m + width * sd, m - width * sd  # type: ignore
    return {"mid": mid, "upper": upper, "lower": lower}


def atr(bars: list[dict], n: int = 14) -> list[float | None]:
    trs = [float(bars[0]["high"]) - float(bars[0]["low"])]
    for i in range(1, len(bars)):
        trs.append(max(
            float(bars[i]["high"]) - float(bars[i]["low"]),
            abs(float(bars[i]["high"]) - float(bars[i - 1]["close"])),
            abs(float(bars[i]["low"]) - float(bars[i - 1]["close"])),
        ))
    return ma(trs, n)


def bias(vals: list[float], n: int = 6) -> list[float | None]:
    """乖离率%（收盘 vs MA(n)）。开源量化标配（StockAgent ma_deviation 同源）。"""
    m = ma(vals, n)
    return [(v / mm - 1) * 100 if mm else None for v, mm in zip(vals, m)]


def cci(bars: list[dict], n: int = 14) -> list[float | None]:
    """CCI 顺势指标。（典型价格 - 均TP) / (0.015 × 平均绝对偏差)。"""
    tp = [(float(b["high"]) + float(b["low"]) + float(b["close"])) / 3 for b in bars]
    out: list[float | None] = [None] * len(bars)
    for i in range(n - 1, len(bars)):
        win = tp[i - n + 1 : i + 1]
        m = sum(win) / n
        md = sum(abs(x - m) for x in win) / n
        out[i] = (tp[i] - m) / (0.015 * md) if md else 0.0
    return out


def wr(bars: list[dict], n: int = 14) -> list[float | None]:
    """威廉指标 WR%（0~100，越低越超买）。开源技术库（qstock/DSA）标配。"""
    out: list[float | None] = [None] * len(bars)
    for i in range(n - 1, len(bars)):
        hi = max(float(b["high"]) for b in bars[i - n + 1 : i + 1])
        lo = min(float(b["low"]) for b in bars[i - n + 1 : i + 1])
        c = float(bars[i]["close"])
        out[i] = (hi - c) / (hi - lo) * 100 if hi > lo else 50.0
    return out


# ---- 汇总 ------------------------------------------------------------------------


def analyze(bars: list[dict]) -> dict[str, Any]:
    """输入升序 K 线，输出最新指标值 + 信号判断。bars 需 >= 60 根效果较好。"""
    if len(bars) < 26:
        return {"error": f"K线不足（{len(bars)} 根，至少 26 根）"}
    closes = _closes(bars)
    i = len(bars) - 1
    out: dict[str, Any] = {"date": bars[i]["date"], "close": closes[i], "bars": len(bars)}

    # 均线
    mas = {n: ma(closes, n) for n in (5, 10, 20, 60)}
    out["ma"] = {f"ma{n}": round(mas[n][i], 2) if mas[n][i] else None for n in mas}
    ma_vals = [mas[n][i] for n in (5, 10, 20, 60) if mas[n] and mas[n][i]]
    out["ma_alignment"] = (
        "多头排列" if len(ma_vals) == 4 and all(a >= b for a, b in zip(ma_vals, ma_vals[1:]))
        else "空头排列" if len(ma_vals) == 4 and all(a <= b for a, b in zip(ma_vals, ma_vals[1:]))
        else "纠缠"
    )

    # MACD + 金叉死叉（近 5 根内）
    m = macd(closes)
    out["macd"] = {"dif": round(m["dif"][i], 3), "dea": round(m["dea"][i], 3),
                   "bar": round(m["bar"][i], 3)}
    cross = None
    for j in range(max(1, i - 4), i + 1):
        if m["dif"][j - 1] <= m["dea"][j - 1] and m["dif"][j] > m["dea"][j]:
            cross = f"金叉({bars[j]['date']})"
        elif m["dif"][j - 1] >= m["dea"][j - 1] and m["dif"][j] < m["dea"][j]:
            cross = f"死叉({bars[j]['date']})"
    out["macd"]["signal"] = cross

    # KDJ
    k = kdj(bars)
    out["kdj"] = {"k": round(k["k"][i], 1), "d": round(k["d"][i], 1), "j": round(k["j"][i], 1)}
    out["kdj"]["signal"] = (
        "超买" if k["j"][i] > 100 or (k["k"][i] > 85 and k["d"][i] > 80)
        else "超卖" if k["j"][i] < 0 or (k["k"][i] < 15 and k["d"][i] < 20)
        else "金叉" if k["k"][i] > k["d"][i] and k["k"][i - 1] <= k["d"][i - 1]
        else "死叉" if k["k"][i] < k["d"][i] and k["k"][i - 1] >= k["d"][i - 1]
        else None
    )

    # RSI（cutler）
    out["rsi"] = {f"rsi{n}": round(rsi(closes, n)[i], 1) for n in (6, 12, 24)}
    r6 = out["rsi"]["rsi6"]
    out["rsi"]["signal"] = "超买" if r6 > 85 else "超卖" if r6 < 15 else None

    # BOLL
    b = boll(closes)
    out["boll"] = {
        "upper": round(b["upper"][i], 2) if b["upper"][i] else None,
        "mid": round(b["mid"][i], 2) if b["mid"][i] else None,
        "lower": round(b["lower"][i], 2) if b["lower"][i] else None,
    }
    out["boll"]["position"] = (
        "上轨外(强)" if b["upper"][i] and closes[i] > b["upper"][i]
        else "下轨外(弱)" if b["lower"][i] and closes[i] < b["lower"][i]
        else "中轨上方" if b["mid"][i] and closes[i] > b["mid"][i] else "中轨下方"
    )

    # ATR（波动参考）
    a = atr(bars)
    out["atr14"] = round(a[i], 2) if a[i] else None

    # BIAS 乖离率（追高/超跌参考）
    b6 = bias(closes, 6)[i]
    out["bias6"] = round(b6, 2) if b6 is not None else None
    out["bias6_signal"] = (
        "超跌" if b6 is not None and b6 < -6
        else "乖离过大(追高风险)" if b6 is not None and b6 > 6
        else None
    )

    # CCI（趋势强度/超买超卖，±100 阈值）
    c = cci(bars)[i]
    out["cci14"] = round(c, 1) if c is not None else None
    out["cci_signal"] = (
        "超买" if c is not None and c > 100
        else "超卖" if c is not None and c < -100
        else None
    )

    # WR 威廉（<20 超买，>80 超卖）
    w = wr(bars)[i]
    out["wr14"] = round(w, 1) if w is not None else None
    out["wr_signal"] = (
        "超买" if w is not None and w < 20
        else "超卖" if w is not None and w > 80
        else None
    )

    return out
