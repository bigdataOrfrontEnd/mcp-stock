"""买卖点判定：一/二/三类买卖点、走势类型、支撑阻力位。

自 stock-chanlun backend/chanlun/signals.py 移植为纯 Python。
索引均基于「合并后K线序列」，时间比较一律用索引。
"""
from __future__ import annotations

from typing import Optional

from .structure import Bi, Segment, Zhongshu


class SignalDetector:
    """三类买卖点判定：

    一买: 下跌趋势背驰点 → 最后一个下跌段终点，价格新低但力度不创新低
    二买: 一买后回调低点（不破一买前低，±3% 容差）
    三买: 向上笔突破中枢后回踩，低点不跌入中枢上沿
    一卖/二卖/三卖: 对称逻辑（上涨趋势）
    """

    def __init__(self, bis: list[Bi], segments: list[Segment],
                 zhongshus: list[Zhongshu], level: str = "daily"):
        self.bis = bis
        self.segments = segments
        self.zhongshus = zhongshus
        self.level = level

    # ---- 汇总 ------------------------------------------------------------------

    def detect_all(self) -> list[dict]:
        signals: list[dict] = []
        first_buys = self._detect_1st_buy()
        signals += first_buys
        signals += self._detect_2nd_buy(first_buys)
        signals += self._detect_3rd_buy()
        first_sells = self._detect_1st_sell()
        signals += first_sells
        signals += self._detect_2nd_sell(first_sells)
        signals += self._detect_3rd_sell()
        return sorted(signals, key=lambda s: s["_idx"])

    # ---- 买点 ------------------------------------------------------------------

    def _detect_1st_buy(self) -> list[dict]:
        """一买：连续下跌段价格创新低但力度（幅度/K线数）减弱 20% 以上。"""
        signals = []
        down_segments = [s for s in self.segments if s.direction == "down"]
        for prev, curr in zip(down_segments, down_segments[1:]):
            if curr.low >= prev.low:
                continue
            prev_power = (prev.high - prev.low) / max(1, prev.bars)
            curr_power = (curr.high - curr.low) / max(1, curr.bars)
            if curr_power < prev_power * 0.8:
                signals.append({
                    "type": "一买", "level": self.level,
                    "price": round(curr.low, 2), "_idx": curr.e_idx,
                    "date": curr.e_date,
                    "confidence": round(min(1.0, abs(1 - curr_power / prev_power) + 0.5), 2),
                    "stop_loss": round(curr.low * 0.97, 2),
                    "description": (f"背驰一买: 当前段力度{curr_power:.3f} "
                                    f"< 前段力度{prev_power:.3f}"),
                })
        return signals

    def _detect_2nd_buy(self, first_buys: list[dict]) -> list[dict]:
        """二买：一买后结束的下探笔，回调低点不破一买价（含 ±3% 容差）。"""
        signals = []
        if not first_buys:
            return signals
        for fb in first_buys:
            for b in self.bis:
                if b.direction != "down" or b.e_idx <= fb["_idx"]:
                    continue
                if b.low >= fb["price"] * 0.97:
                    signals.append({
                        "type": "二买", "level": self.level,
                        "price": round(b.low, 2), "_idx": b.e_idx,
                        "date": b.e_date, "confidence": 0.70,
                        "stop_loss": round(min(b.low * 0.97, fb["price"] * 0.95), 2),
                        "description": f"回调二买: 回踩{b.low:.2f}不破一买{fb['price']:.2f}",
                    })
                    break
        return signals

    def _detect_3rd_buy(self) -> list[dict]:
        """三买：向上笔突破中枢后首个下探笔回踩，低点不跌入中枢上沿。"""
        signals = []
        if not self.zhongshus:
            return signals
        for zs in reversed(self.zhongshus):
            break_up: Optional[Bi] = None
            for b in self.bis:
                if b.direction == "up" and b.e_idx > zs.e_idx and b.high > zs.upper:
                    break_up = b
                    break
            if break_up is None:
                continue
            retests = [b for b in self.bis
                       if b.direction == "down" and b.s_idx > break_up.e_idx]
            if not retests:
                continue
            retest = retests[0]
            if retest.low > zs.upper:
                signals.append({
                    "type": "三买", "level": self.level,
                    "price": round(retest.low, 2), "_idx": retest.e_idx,
                    "date": retest.e_date, "confidence": 0.80,
                    "stop_loss": round(zs.lower, 2),
                    "take_profit": round(break_up.high * 1.05, 2),
                    "description": f"三买: 回踩{retest.low:.2f}不破中枢上沿{zs.upper:.2f}",
                })
            break  # 只取最近被突破中枢的三买
        return signals

    # ---- 卖点 ------------------------------------------------------------------

    def _detect_1st_sell(self) -> list[dict]:
        """一卖：连续上涨段价格创新高但力度减弱 20% 以上。"""
        signals = []
        up_segments = [s for s in self.segments if s.direction == "up"]
        for prev, curr in zip(up_segments, up_segments[1:]):
            if curr.high <= prev.high:
                continue
            prev_power = (prev.high - prev.low) / max(1, prev.bars)
            curr_power = (curr.high - curr.low) / max(1, curr.bars)
            if curr_power < prev_power * 0.8:
                signals.append({
                    "type": "一卖", "level": self.level,
                    "price": round(curr.high, 2), "_idx": curr.e_idx,
                    "date": curr.e_date,
                    "confidence": round(min(1.0, abs(1 - curr_power / prev_power) + 0.5), 2),
                    "stop_loss": round(curr.high * 1.03, 2),
                    "description": (f"背驰一卖: 当前段力度{curr_power:.3f} "
                                    f"< 前段力度{prev_power:.3f}"),
                })
        return signals

    def _detect_2nd_sell(self, first_sells: list[dict]) -> list[dict]:
        """二卖：一卖后结束的上探笔，反弹高点不破一卖价（含 ±2% 容差）。"""
        signals = []
        if not first_sells:
            return signals
        for fs in first_sells:
            for b in self.bis:
                if b.direction != "up" or b.e_idx <= fs["_idx"]:
                    continue
                if b.high <= fs["price"] * 1.03:
                    signals.append({
                        "type": "二卖", "level": self.level,
                        "price": round(b.high, 2), "_idx": b.e_idx,
                        "date": b.e_date, "confidence": 0.65,
                        "description": f"二卖: 反弹{b.high:.2f}不破一卖{fs['price']:.2f}",
                    })
                    break
        return signals

    def _detect_3rd_sell(self) -> list[dict]:
        """三卖：向下笔跌破中枢后首个上探笔反弹，高点不突破中枢下沿。"""
        signals = []
        if not self.zhongshus:
            return signals
        for zs in reversed(self.zhongshus):
            break_down: Optional[Bi] = None
            for b in self.bis:
                if b.direction == "down" and b.e_idx > zs.e_idx and b.low < zs.lower:
                    break_down = b
                    break
            if break_down is None:
                continue
            retests = [b for b in self.bis
                       if b.direction == "up" and b.s_idx > break_down.e_idx]
            if not retests:
                continue
            retest = retests[0]
            if retest.high < zs.lower:
                signals.append({
                    "type": "三卖", "level": self.level,
                    "price": round(retest.high, 2), "_idx": retest.e_idx,
                    "date": retest.e_date, "confidence": 0.75,
                    "description": f"三卖: 反弹{retest.high:.2f}不破中枢下沿{zs.lower:.2f}",
                })
            break
        return signals

    # ---- 走势与支撑阻力 ----------------------------------------------------------

    def detect_trend(self) -> str:
        """判断当前走势类型：中枢多于 1 个视为盘整，否则按最近 3 段方向投票。"""
        if not self.segments:
            return "未知"
        recent = self.segments[-3:]
        downs = sum(1 for s in recent if s.direction == "down")
        ups = sum(1 for s in recent if s.direction == "up")
        if len(self.zhongshus) > 1:
            return "盘整"
        if downs > ups:
            return "下跌"
        if ups > downs:
            return "上涨"
        return "盘整"

    def detect_support_resistance(self, merged: list[dict],
                                  signals: list[dict]) -> list[dict]:
        """计算支撑/阻力位：中枢上下沿、近 10 笔高低点、近 6 段高低点、
        买卖点价位、近 20 根K线极值；±0.5% 价格去重后按强度降序。"""
        levels: list[dict] = []

        def add(kind: str, price: float, source: str, date, strength: float):
            levels.append({"type": kind, "price": round(float(price), 2),
                           "source": source, "date": date,
                           "strength": round(float(strength), 2)})

        for zs in self.zhongshus:
            add("support", zs.lower, "zhongshu", zs.s_date, 0.85)
            add("resistance", zs.upper, "zhongshu", zs.s_date, 0.85)

        for b in self.bis[-10:]:
            if b.direction == "down":
                add("support", b.low, "bi_low", b.e_date, 0.6)
            else:
                add("resistance", b.high, "bi_high", b.e_date, 0.6)

        for x in self.segments[-6:]:
            if x.direction == "down":
                add("support", x.low, "segment_low", x.e_date, 0.75)
            else:
                add("resistance", x.high, "segment_high", x.e_date, 0.75)

        for sig in signals:
            kind = "support" if "买" in sig["type"] else "resistance"
            add(kind, sig["price"], "signal", sig["date"], sig["confidence"])

        if merged:
            recent = merged[-20:]
            lo = min(recent, key=lambda k: float(k["low"]))
            hi = max(recent, key=lambda k: float(k["high"]))
            add("support", lo["low"], "kline_low", lo["date"], 0.5)
            add("resistance", hi["high"], "kline_high", hi["date"], 0.5)

        # ±0.5% 价格去重：保留更强者
        deduped: list[dict] = []
        for lvl in levels:
            replaced = False
            for d in deduped:
                if abs(d["price"] - lvl["price"]) <= max(0.01, d["price"] * 0.005):
                    if lvl["strength"] > d["strength"]:
                        deduped[deduped.index(d)] = lvl
                    replaced = True
                    break
            if not replaced:
                deduped.append(lvl)
        return sorted(deduped, key=lambda x: -x["strength"])
