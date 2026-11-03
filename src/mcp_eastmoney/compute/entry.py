"""真实可成交入场价计算 — 纯函数（后验体系的核心数学）。

从 refresh.py 下沉至此（2026-10 分层修正：一切公式归 compute）。
规则：
- 推荐日未涨停收盘 → 收盘价入场
- 推荐日涨停收盘（含一字）→ 顺延到下一个非一字日开盘价入场
- 连续 ≥2 日一字 → 该单作废（不计入胜率，防纸面收益污染）
"""
from __future__ import annotations

LIMIT_PCT = 9.8  # 视为涨停收盘的最低涨幅（20cm 板同样覆盖）


def sealed_limit(bar: dict) -> bool:
    """当日涨停收盘。"""
    chg = bar.get("change_pct")
    return isinstance(chg, (int, float)) and chg >= LIMIT_PCT


def fill_change_pct(bars: list[dict], ref_price: float | None = None) -> list[dict]:
    """腾讯兜底行缺 change_pct → 用前收盘补算（涨停检测依赖此字段）。

    首根无前收盘时用 ref_price（推荐日参考价=前一收盘）兜底。
    """
    prev_close = ref_price
    for b in bars:
        if b.get("change_pct") is None:
            pc = prev_close or b.get("prev_close")
            if pc:
                b["change_pct"] = round((float(b["close"]) / float(pc) - 1) * 100, 2)
        prev_close = b["close"]
    return bars


def yizi(bar: dict) -> bool:
    """一字板（全天高低同价，无成交机会）。"""
    hi, lo = bar.get("high"), bar.get("low")
    return (hi is not None and lo is not None and float(hi) == float(lo)
            and float(hi) > 0)


def effective_entry(bars: list[dict], rec_date: str,
                    ref_price: float | None) -> tuple[float | None, str | None, str | None]:
    """真实可成交入场价。返回 (entry, fill_date, note)。"""
    rec_bar = next((b for b in bars if b["date"] == rec_date), None)
    if not rec_bar:
        return ref_price, rec_date, None
    if not sealed_limit(rec_bar):
        return ref_price, rec_date, None
    later = [b for b in bars if b["date"] > rec_date]
    consec = 0
    for b in later:
        if sealed_limit(b) and yizi(b):
            consec += 1
            continue
        op = b.get("open") or b.get("close")
        if op is None:
            continue
        note = (f"推荐日涨停收盘无法成交，顺延至{b['date']}开盘价入场"
                + ("（跨过连续一字）" if consec > 0 else ""))
        return float(op), b["date"], note
    if not later:
        return None, None, None  # 次日行情未出 → 等待，不作废
    if consec >= 2:
        return None, None, "连续一字涨停无法入场，该单作废（不计入胜率）"
    return None, None, None  # 仅1日一字 → 继续等待开板
