"""共享横截面引擎 — 分位打分与加权合成的唯一实现。

所有横截面数学（因子排名/策略打分/龙头评分）统一经本模块，
杜绝各层自带 percentile 重复实现（2026-10 分层修正）。
"""
from __future__ import annotations


def percentile_rank(values: list[float | None]) -> list[float | None]:
    """横截面分位 0-100。None 保持 None（调用方决定中性值语义）。"""
    present = sorted(v for v in values if v is not None)
    n = len(present)
    if n == 0:
        return [None] * len(values)

    def one(v: float | None) -> float | None:
        if v is None:
            return None
        cnt = sum(1 for p in present if p <= v)
        return (cnt - 1) / (n - 1) * 100 if n > 1 else 50.0

    return [one(v) for v in values]


def neutral_fill(scores: list[float | None], neutral: float = 50.0) -> list[float]:
    """None → 中性值。"""
    return [neutral if s is None else s for s in scores]


def weighted_composite(score_rows: list[dict[str, float | None]],
                       weights: dict[str, float]) -> list[float | None]:
    """按权重合成综合分。每行 {因子名: 分}，权重 {因子名: w}。
    缺失因子按剩余权重归一（不惩罚缺数据）。
    """
    out: list[float | None] = []
    for row in score_rows:
        num, den = 0.0, 0.0
        for k, w in weights.items():
            s = row.get(k)
            if s is not None:
                num += s * w
                den += w
        out.append(round(num / den, 1) if den else None)
    return out


def anchor_score(values: list[float | None],
                 full: tuple[float, float],
                 hard: tuple[float, float]) -> list[float | None]:
    """锚点区间软打分（借鉴 AlphaSift ideal/chase 曲线，数学归计算层）。

    full=(lo, hi): 该区间内 100 分；hard=(lo_hard, hi_hard): 出界线性衰减到 0。
    None 保持 None（调用方决定中性语义）。
    """
    lo, hi = full
    lo_h, hi_h = hard
    if not (lo_h <= lo <= hi <= hi_h):
        raise ValueError("anchor 参数需满足 lo_hard ≤ lo ≤ hi ≤ hi_hard")

    def one(v: float | None) -> float | None:
        if v is None:
            return None
        if lo <= v <= hi:
            return 100.0
        if v < lo:
            span = lo - lo_h
            return max(0.0, (v - lo_h) / span * 100) if span > 0 else 0.0
        span = hi_h - hi
        return max(0.0, (hi_h - v) / span * 100) if span > 0 else 0.0

    return [one(v) for v in values]
