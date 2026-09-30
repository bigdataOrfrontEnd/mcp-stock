"""单股趋势评分 — 规则制 100 分 + 一票否决。纯计算，零网络零落表。

复用最优部分，不重复计算：
- 指标值全部来自 indicators.analyze() 的结果（MA/MACD/KDJ/RSI/BOLL/ATR）
- 动量/量比复用 factors 的 _pct / _vol_ratio（同一计算层，避免二次实现）
- 缠论结构（买卖点/背驰/中枢位置）接受 chan.analyze() 结果作为可选输入

评分结构（100 分制）：
  均线趋势 25 + MACD 20 + 动量 15 + 量能配合 10 + RSI 位置 10 + 缠论结构 20
  无缠论输入时，按已评权重归一化到 100。

一票否决（触发即 0 分，并列出原因）：趋势/结构/动能三重空头、
  MACD 死叉叠加缠论卖点或顶背驰、K线不足。
"""
from __future__ import annotations

from typing import Any

from . import indicators
from .factors import _pct, _vol_ratio


def _closes_and_vols(bars: list[dict]) -> tuple[list[float], list[float]]:
    closes = [float(b["close"]) for b in bars]
    vols = [float(b["volume"] or 0) for b in bars]
    return closes, vols


def _veto_checks(ind: dict, chan: dict | None) -> list[str]:
    """一票否决规则。返回原因列表（空 = 通过）。"""
    vetoes: list[str] = []
    ma_align = ind.get("ma_alignment")
    m = ind.get("macd") or {}
    dif_below = m.get("dif") is not None and m.get("dea") is not None \
        and m["dif"] < m["dea"]

    # 1) 三重空头：均线空头排列 + MACD 零下死叉 + 收盘在 MA20 下方
    if ma_align == "空头排列" and dif_below and m.get("dif", 0) < 0 \
            and ind.get("close") and (ind.get("ma") or {}).get("ma20") \
            and ind["close"] < ind["ma"]["ma20"]:
        vetoes.append("三重空头：均线空头排列+MACD零下死叉+收盘低于MA20")

    # 2) MACD 死叉信号 叠加 缠论卖点或顶背驰
    if chan:
        recent_types = "".join(s.get("type", "") for s in chan.get("signals", [])[-3:])
        beichi = chan.get("beichi") or {}
        top_div = beichi.get("type") == "顶背驰"
        if (m.get("signal") or "").startswith("死叉") and ("卖" in recent_types or top_div):
            vetoes.append("MACD死叉叠加缠论卖点/顶背驰")
    return vetoes


def _item(name: str, got: float, mx: float, reason: str) -> dict:
    return {"item": name, "score": round(got, 1), "max": mx, "reason": reason}


def score_trend(bars: list[dict], ind: dict | None = None,
                chan: dict | None = None) -> dict[str, Any]:
    """规则制 100 分趋势评分。bars 升序日K；ind/chan 可传入已算好的
    indicators.analyze / chan.analyze 结果避免重复计算。"""
    if len(bars) < 26:
        return {"error": f"K线不足（{len(bars)} 根，至少 26 根）"}
    ind = ind or indicators.analyze(bars)
    if "error" in ind:
        return ind

    closes, vols = _closes_and_vols(bars)
    i = len(bars) - 1
    close = closes[i]
    items: list[dict] = []

    # ---- 1) 均线趋势 25 ----
    align = ind.get("ma_alignment")
    ma20 = (ind.get("ma") or {}).get("ma20")
    if align == "多头排列":
        items.append(_item("均线趋势", 25, 25, "MA5>MA10>MA20>MA60 多头排列"))
    elif ma20 and close > ma20:
        items.append(_item("均线趋势", 12, 25, "均线纠缠但站稳MA20"))
    elif align == "纠缠":
        items.append(_item("均线趋势", 5, 25, "均线纠缠且未站上MA20"))
    else:
        items.append(_item("均线趋势", 0, 25, "空头排列"))

    # ---- 2) MACD 20 ----
    m = ind.get("macd") or {}
    dif, dea, bar = m.get("dif"), m.get("dea"), m.get("bar")
    if dif is not None and dea is not None and dif > dea and dif > 0:
        s, r = 20, "金叉且DIF在零轴上方（多头动能）"
    elif dif is not None and dea is not None and dif > dea:
        s, r = 13, "金叉但DIF在零轴下方（修复中）"
    elif bar is not None and bar > 0:
        s, r = 7, "死叉但柱状收红（动能转暖）"
    else:
        s, r = 0, "死叉且绿柱（空头动能）"
    if (m.get("signal") or "").startswith("金叉"):
        r += f"，近5日{m['signal']}"
    items.append(_item("MACD", s, 20, r))

    # ---- 3) 动量 15（复用 factors._pct）----
    mom20 = _pct(closes, 20)
    mom5 = _pct(closes, 5)
    if mom20 is None:
        s, r = 7.5, "数据不足按中值"
    elif mom20 > 10:
        s, r = 15, f"20日动量{mom20:.1f}% 强势"
    elif mom20 > 0:
        s, r = 10, f"20日动量{mom20:.1f}% 偏多"
    elif mom5 is not None and mom5 > 0:
        s, r = 4, f"20日动量{mom20:.1f}% 但5日{mom5:.1f}% 回暖"
    else:
        s, r = 0, f"20日动量{mom20:.1f}% 空头"
    items.append(_item("动量", s, 15, r))

    # ---- 4) 量能配合 10（复用 factors._vol_ratio）----
    vr = _vol_ratio(vols)
    if vr is None:
        s, r = 5, "数据不足按中值"
    elif 1.2 <= vr <= 3.0:
        s, r = 10, f"量比{vr:.1f} 温和放量"
    elif vr > 3.0:
        s, r = 6, f"量比{vr:.1f} 放量过热（追高风险）"
    elif 0.8 <= vr < 1.2:
        s, r = 4, f"量比{vr:.1f} 量能平平"
    else:
        s, r = 0, f"量比{vr:.1f} 明显缩量"
    items.append(_item("量能", s, 10, r))

    # ---- 5) RSI 位置 10 ----
    r6 = (ind.get("rsi") or {}).get("rsi6")
    if r6 is None:
        s, r = 5, "数据不足按中值"
    elif 40 <= r6 <= 70:
        s, r = 10, f"RSI6={r6} 健康多头区"
    elif 15 <= r6 < 40:
        s, r = 6, f"RSI6={r6} 低位（反弹空间但需右侧确认）"
    elif r6 < 15:
        s, r = 3, f"RSI6={r6} 超卖（可能继续下杀）"
    else:
        s, r = 0, f"RSI6={r6} 超买（追高风险）"
    items.append(_item("RSI位置", s, 10, r))

    # ---- 6) 缠论结构 20 ----
    got_max: tuple[float, float] | None = None
    if chan and "error" not in chan:
        recent = chan.get("signals", [])[-3:]
        s, r = 0, "无近期买卖点"
        conf = 0.0
        sell_recent = False
        for sig in recent:
            t = sig.get("type", "")
            if "卖" in t:
                sell_recent = True
            if "买" in t and sig.get("confidence", 0) > conf:
                conf = sig["confidence"]
                s = 12 + round(8 * min(conf, 1.0), 1)  # 12~20，置信度加成
                r = f"近期{t}（置信度{conf}，止损{sig.get('stop_loss')}）"
        if sell_recent:
            # 对称处理：近期卖点直接归零（原版只靠否决兜底，评分不对称）
            s, r = 0, "近期出现缠论卖点，结构分归零"
        pos = chan.get("position") or ""
        if "中枢上方" in pos:
            s += 5
            r += " + 收盘在中枢上方强势区"
        elif "中枢下方" in pos:
            r += "（中枢下方弱势区）"
        s = min(s, 20.0)
        items.append(_item("缠论结构", s, 20, r))
        got_max = (s, 20)

    # ---- 否决与汇总 ----
    vetoes = _veto_checks(ind, chan)
    total = sum(x["score"] for x in items)
    max_all = sum(x["max"] for x in items)
    score = 0.0 if vetoes else round(total / max_all * 100, 1)
    grade = (
        "一票否决" if vetoes
        else "A(强趋势)" if score >= 80
        else "B(偏多)" if score >= 65
        else "C(中性观察)" if score >= 50
        else "D(弱势)"
    )
    return {
        "date": ind.get("date"), "close": close, "bars": len(bars),
        "score": score, "grade": grade,
        "chan_used": bool(got_max),
        "items": items,
        "vetoes": vetoes,
        "note": "无缠论输入时按已评权重归一化；规则制评分供粗筛后复核，不构成投资建议",
    }
