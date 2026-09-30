"""缠论结构识别：包含关系合并 → 分型 → 笔 → 线段 → 中枢。

自 stock-chanlun backend/chanlun 移植为纯 Python（无 pandas/numpy 依赖）。
所有索引均基于「合并后K线序列」；日期仅用于展示，比较一律用索引。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


# ---- 数据结构 ------------------------------------------------------------------------


@dataclass
class Fenxing:
    idx: int                 # 合并K线索引
    type: Literal["top", "bottom"]
    high: float
    low: float
    date: object


@dataclass
class Bi:
    s_idx: int               # 起点分型在合并序列中的索引
    e_idx: int
    s_date: object
    e_date: object
    direction: Literal["up", "down"]
    high: float
    low: float
    start_price: float
    end_price: float

    @property
    def range(self) -> float:
        return abs(self.end_price - self.start_price)

    @property
    def bars(self) -> int:
        return self.e_idx - self.s_idx + 1


@dataclass
class Segment:
    """线段（段）— 由笔构成"""
    s_idx: int
    e_idx: int
    s_date: object
    e_date: object
    direction: Literal["up", "down"]
    high: float
    low: float
    start_price: float
    end_price: float
    bi_ids: list[int] = field(default_factory=list)

    @property
    def bars(self) -> int:
        return self.e_idx - self.s_idx + 1


@dataclass
class Zhongshu:
    """中枢 — 价格重叠区域 [lower, upper]"""
    s_idx: int
    e_idx: int
    s_date: object
    e_date: object
    upper: float             # 区间上沿 = min(各段 high)
    lower: float             # 区间下沿 = max(各段 low)
    xiang_ids: list[int] = field(default_factory=list)
    level: int = 2


# ---- 1. 包含关系合并 ------------------------------------------------------------------


def merge_klines(bars: list[dict]) -> list[dict]:
    """合并包含关系K线（方向感知）：
    - 相邻两根一根完全包含另一根 → 合并
    - 向上方向：高高取高（high=max, low=max）；向下方向：低低取低（high=min, low=min）
    - 合并K线的日期取后一根（沿用被包含新K线的时间，便于时间轴映射）
    """
    if not bars:
        return []
    out: list[dict] = [dict(bars[0])]
    for b in bars[1:]:
        last = out[-1]
        hi, lo = float(b["high"]), float(b["low"])
        l_hi, l_lo = float(last["high"]), float(last["low"])
        contained = (l_lo <= lo and l_hi >= hi) or (lo <= l_lo and hi >= l_hi)
        if not contained:
            out.append(dict(b))
            continue
        # 判断方向：与前一根比较
        if len(out) >= 2:
            before = out[-2]
            up = l_hi >= float(before["high"]) and l_lo >= float(before["low"])
        else:
            up = hi >= l_hi and lo >= l_lo
        if up:
            last["high"], last["low"] = max(hi, l_hi), max(lo, l_lo)
        else:
            last["high"], last["low"] = min(hi, l_hi), min(lo, l_lo)
        last["close"] = b.get("close", last.get("close"))
        last["date"] = b["date"]  # 日期取新K线
    return out


# ---- 2. 分型 -------------------------------------------------------------------------


def find_fractals(merged: list[dict]) -> list[Fenxing]:
    """严格三根K线分型：
    顶：中间K线 high/low 均高于两侧；底：均低于两侧。
    """
    out: list[Fenxing] = []
    for i in range(1, len(merged) - 1):
        h0, h1, h2 = (float(merged[k]["high"]) for k in (i - 1, i, i + 1))
        l0, l1, l2 = (float(merged[k]["low"]) for k in (i - 1, i, i + 1))
        if h1 > h0 and h1 > h2 and l1 > l0 and l1 > l2:
            out.append(Fenxing(i, "top", h1, l1, merged[i]["date"]))
        elif h1 < h0 and h1 < h2 and l1 < l0 and l1 < l2:
            out.append(Fenxing(i, "bottom", h1, l1, merged[i]["date"]))
    return out


def compress_fractals(fenxings: list[Fenxing]) -> list[Fenxing]:
    """压缩分型序列：连续同类型仅保留更极值者（顶取更高，底取更低）。"""
    if not fenxings:
        return []
    out = [fenxings[0]]
    for fx in fenxings[1:]:
        last = out[-1]
        if fx.type != last.type:
            out.append(fx)
        elif fx.type == "top":
            if fx.high >= last.high:
                out[-1] = fx
        else:
            if fx.low <= last.low:
                out[-1] = fx
    return out


# ---- 3. 笔 ---------------------------------------------------------------------------


def build_bis(merged: list[dict], min_bars: int = 5) -> list[Bi]:
    """笔 = 相邻的顶/底分型连接，方向交替，且间隔 >= min_bars 根合并K线。"""
    fenxings = compress_fractals(find_fractals(merged))
    bis: list[Bi] = []
    for fx1, fx2 in zip(fenxings, fenxings[1:]):
        if fx1.type == fx2.type:
            continue
        if fx2.idx - fx1.idx + 1 < min_bars:
            continue
        if fx1.type == "bottom":        # 向上笔：底 → 顶
            bis.append(Bi(
                s_idx=fx1.idx, e_idx=fx2.idx, s_date=fx1.date, e_date=fx2.date,
                direction="up", high=fx2.high, low=fx1.low,
                start_price=fx1.low, end_price=fx2.high,
            ))
        else:                            # 向下笔：顶 → 底
            bis.append(Bi(
                s_idx=fx1.idx, e_idx=fx2.idx, s_date=fx1.date, e_date=fx2.date,
                direction="down", high=fx1.high, low=fx2.low,
                start_price=fx1.high, end_price=fx2.low,
            ))
    return bis


# ---- 4. 线段 -------------------------------------------------------------------------


def _bi_overlap(group: list[Bi]) -> bool:
    """一组笔是否存在公共重叠区间"""
    high = min(b.high for b in group)
    low = max(b.low for b in group)
    return high > low


def detect_segments(bis: list[Bi], min_overlap_bis: int = 3) -> list[Segment]:
    """线段：连续 min_overlap_bis 笔重叠构成；同向后续笔与区间重叠则延伸。"""
    segments: list[Segment] = []
    i = 0
    while i <= len(bis) - min_overlap_bis:
        group = bis[i:i + min_overlap_bis]
        if not _bi_overlap(group):
            i += 1
            continue
        direction = group[0].direction
        high = max(b.high for b in group)
        low = min(b.low for b in group)
        used = list(group)
        # 尝试延伸同向笔
        while True:
            nxt_i = i + len(used)
            if nxt_i >= len(bis):
                break
            nxt = bis[nxt_i]
            if nxt.direction == direction and nxt.high > low and nxt.low < high:
                high = max(high, nxt.high)
                low = min(low, nxt.low)
                used.append(nxt)
            else:
                break
        last = used[-1]
        segments.append(Segment(
            s_idx=group[0].s_idx, e_idx=last.e_idx,
            s_date=group[0].s_date, e_date=last.e_date,
            direction=direction, high=high, low=low,
            start_price=group[0].start_price, end_price=last.end_price,
            bi_ids=list(range(i, i + len(used))),
        ))
        i += len(used)
    return segments


# ---- 5. 中枢 -------------------------------------------------------------------------


def detect_zhongshus(segments: list[Segment]) -> list[Zhongshu]:
    """中枢：连续 3 段重叠区间；后续段重叠则并入（区间收窄取交集）。
    相邻中枢之间至少间隔 3 段，不会重复。
    """
    zhongshus: list[Zhongshu] = []
    i = 0
    while i <= len(segments) - 3:
        group = segments[i:i + 3]
        upper = min(s.high for s in group)
        lower = max(s.low for s in group)
        if upper > lower:
            used = list(group)
            j = i + 3
            while j < len(segments):
                nxt = segments[j]
                if nxt.high > lower and nxt.low < upper:
                    upper = min(upper, nxt.high)
                    lower = max(lower, nxt.low)
                    used.append(nxt)
                    j += 1
                else:
                    break
            zhongshus.append(Zhongshu(
                s_idx=group[0].s_idx, e_idx=used[-1].e_idx,
                s_date=group[0].s_date, e_date=used[-1].e_date,
                upper=float(upper), lower=float(lower),
                xiang_ids=list(range(i, i + len(used))),
                level=2,
            ))
            i = j
        else:
            i += 1
    return zhongshus
