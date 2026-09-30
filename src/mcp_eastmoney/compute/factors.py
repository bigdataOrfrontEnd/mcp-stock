"""因子库与横截面排名 — 纯计算，零网络（借鉴 StockAgent factor_library.py）。

因子分类（对齐 StockAgent 七分类中本地数据可支撑的五类）：
- 动量   mom5/mom20/mom60          ← 日K close
- 波动   vol20/vol60（低波动=高分） ← 日K close
- 流动性 turnover_20d/amount_20d/vol_ratio ← 日K turnover_rate/amount/volume
- 技术   ma20_dev/rsi14/price_pos_60 ← 日K
- 估值   pe/pb（低估值=高分）        ← 日K pe/pb（Baostock peTTM/pbMRQ）
- 质量   roe/gross_margin/net_margin ← 财务指标最新报告期（fin 输入，可选）

横截面 percentile 排名 → 加权合成综合分。数据不足的因子置 None 并跳过合成。
所有函数纯函数，可独立测试。
"""
from __future__ import annotations

import math
from typing import Any


def _closes(bars: list[dict]) -> list[float]:
    return [float(b["close"]) for b in bars]


def _vols(bars: list[dict]) -> list[float]:
    return [float(b["volume"] or 0) for b in bars]


def _avg(bars: list[dict], key: str, n: int) -> float | None:
    vals = [b.get(key) for b in bars[-n:]]
    nums = [float(v) for v in vals if isinstance(v, (int, float))]
    return sum(nums) / len(nums) if nums else None


def _pct(vals: list[float], n: int) -> float | None:
    if len(vals) < n + 1:
        return None
    return (vals[-1] / vals[-1 - n] - 1) * 100


def _volatility(vals: list[float], n: int = 20) -> float | None:
    if len(vals) < n + 1:
        return None
    rets = [(vals[i] / vals[i - 1] - 1) * 100
            for i in range(len(vals) - n, len(vals))]
    mean = sum(rets) / len(rets)
    return math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets))


def _ma_dev(vals: list[float], n: int = 20) -> float | None:
    if len(vals) < n:
        return None
    m = sum(vals[-n:]) / n
    return (vals[-1] / m - 1) * 100


def _rsi(vals: list[float], n: int = 14) -> float | None:
    if len(vals) < n + 1:
        return None
    diffs = [vals[i] - vals[i - 1] for i in range(len(vals) - n, len(vals))]
    gains = sum(d for d in diffs if d > 0) / len(diffs)
    losses = -sum(d for d in diffs if d < 0) / len(diffs)
    if losses == 0:
        return 100.0
    return 100 - 100 / (1 + gains / losses)


def _price_pos(vals: list[float], n: int = 60) -> float | None:
    if len(vals) < n:
        n = len(vals)
    if n < 5:
        return None
    hi, lo = max(vals[-n:]), min(vals[-n:])
    if hi == lo:
        return 50.0
    return (vals[-1] - lo) / (hi - lo) * 100


def _vol_ratio(vols: list[float]) -> float | None:
    if len(vols) < 6:
        return None
    avg5 = sum(vols[-6:-1]) / 5
    return vols[-1] / avg5 if avg5 else None


def _last_float(bars: list[dict], key: str) -> float | None:
    for b in reversed(bars):
        v = b.get(key)
        if isinstance(v, (int, float)) and v == v:  # 非 NaN
            return float(v)
    return None


# ---- 因子注册表（含元数据目录） -------------------------------------------------------

FACTOR_META: dict[str, dict[str, Any]] = {
    # 动量
    "mom5":  {"name": "5日动量", "category": "动量", "input": "daily",
              "desc": "近5个交易日收益率%"},
    "mom20": {"name": "20日动量", "category": "动量", "input": "daily",
              "desc": "近20个交易日收益率%"},
    "mom60": {"name": "60日动量", "category": "动量", "input": "daily",
              "desc": "近60个交易日收益率%"},
    # 波动
    "vol20": {"name": "20日波动率", "category": "波动", "input": "daily",
              "desc": "近20日日收益率标准差%（低波动得分高）"},
    "vol60": {"name": "60日波动率", "category": "波动", "input": "daily",
              "desc": "近60日日收益率标准差%（低波动得分高）"},
    # 流动性
    "turnover_20d": {"name": "20日换手率均值", "category": "流动性", "input": "daily",
                     "desc": "近20日换手率均值%"},
    "amount_20d": {"name": "20日成交额均值", "category": "流动性", "input": "daily",
                   "desc": "近20日成交额均值（元）"},
    "vol_ratio": {"name": "量比", "category": "流动性", "input": "daily",
                  "desc": "最新成交量/近5日均量"},
    # 技术
    "ma20_dev": {"name": "MA20偏离", "category": "技术", "input": "daily",
                 "desc": "收盘价相对20日均线偏离度%"},
    "rsi14": {"name": "RSI(14)", "category": "技术", "input": "daily",
              "desc": "Cutler RSI 14"},
    "price_pos_60": {"name": "60日价格位置", "category": "技术", "input": "daily",
                     "desc": "收盘价在近60日高低区间位置 0-100"},
    # 估值（低估值得分高）
    "pe": {"name": "市盈率TTM", "category": "估值", "input": "daily",
           "desc": "最新PE_TTM（低估值得分高）"},
    "pb": {"name": "市净率", "category": "估值", "input": "daily",
           "desc": "最新PB_MRQ（低估值得分高）"},
    # 质量（来自财务指标最新报告期）
    "roe": {"name": "ROE", "category": "质量", "input": "fin",
            "desc": "净资产收益率%（最新报告期）"},
    "gross_margin": {"name": "毛利率", "category": "质量", "input": "fin",
                     "desc": "销售毛利率%（最新报告期）"},
    "net_margin": {"name": "净利率", "category": "质量", "input": "fin",
                   "desc": "销售净利率%（最新报告期）"},
}


def _compute_one(name: str, bars: list[dict], fin: dict | None) -> float | None:
    closes = _closes(bars)
    if name == "mom5":
        return _pct(closes, 5)
    if name == "mom20":
        return _pct(closes, 20)
    if name == "mom60":
        return _pct(closes, 60)
    if name == "vol20":
        return _volatility(closes, 20)
    if name == "vol60":
        return _volatility(closes, 60)
    if name == "turnover_20d":
        return _avg(bars, "turnover_rate", 20)
    if name == "amount_20d":
        return _avg(bars, "amount", 20)
    if name == "vol_ratio":
        return _vol_ratio(_vols(bars))
    if name == "ma20_dev":
        return _ma_dev(closes, 20)
    if name == "rsi14":
        return _rsi(closes, 14)
    if name == "price_pos_60":
        return _price_pos(closes, 60)
    if name == "pe":
        v = _last_float(bars, "pe")
        return v if v and v > 0 else None  # 亏损 PE 无意义
    if name == "pb":
        v = _last_float(bars, "pb")
        return v if v and v > 0 else None
    if name in ("roe", "gross_margin", "net_margin"):
        v = (fin or {}).get(name)
        return float(v) if isinstance(v, (int, float)) else None
    return None


# 低值=高分的因子（波动、估值）
INVERTED_FACTORS = {"vol20", "vol60", "pe", "pb"}


def catalog() -> list[dict]:
    """因子目录（factor_catalog tool 用）。"""
    return [
        {"factor": k, **{kk: vv for kk, vv in v.items()},
         "direction": "低值得分高" if k in INVERTED_FACTORS else "高值得分高"}
        for k, v in FACTOR_META.items()
    ]


def compute_factors(bars: list[dict], factors: list[str] | None = None,
                    fin: dict | None = None) -> dict:
    """计算单只股票的因子值。fin: 最新报告期财务指标（质量因子输入）。"""
    keys = factors or list(FACTOR_META)
    out: dict = {}
    for k in keys:
        v = _compute_one(k, bars, fin) if k in FACTOR_META else None
        out[k] = round(v, 2) if v is not None else None
    return out


def _percentile_scores(values: list[float | None], invert: bool = False) -> list[float | None]:
    """None 保持 None，其余映射到 0-100 分位。"""
    present = sorted(v for v in values if v is not None)
    if not present:
        return [None] * len(values)
    n = len(present)

    def score(v: float) -> float:
        cnt = sum(1 for p in present if p <= v)
        s = (cnt - 1) / (n - 1) * 100 if n > 1 else 50.0
        return 100 - s if invert else s

    return [score(v) if v is not None else None for v in values]


def rank_stocks(stock_bars: list[tuple[str, dict]], factors: list[str] | None = None,
                fins: dict[str, dict] | None = None, top_n: int = 10) -> list[dict]:
    """横截面因子排名。stock_bars: [(code, bars), ...] 升序 bars。

    fins: {code: 财务指标 dict}，质量因子（roe/毛利率/净利率）依赖此输入。
    返回按综合分降序的前 top_n，每项含单因子值 + 单因子分位 + 综合分。
    """
    keys = [k for k in (factors or list(FACTOR_META)) if k in FACTOR_META]
    if not keys or not stock_bars:
        return []
    fins = fins or {}

    raw: list[tuple[str, dict[str, float | None]]] = []
    for code, bars in stock_bars:
        raw.append((code, compute_factors(bars, keys, fins.get(code))))

    col_scores: dict[str, list[float | None]] = {}
    for k in keys:
        vals = [fv.get(k) for _, fv in raw]
        col_scores[k] = _percentile_scores(vals, invert=k in INVERTED_FACTORS)

    out = []
    for i, (code, fv) in enumerate(raw):
        scores = {k: (round(col_scores[k][i], 1) if col_scores[k][i] is not None else None)
                  for k in keys}
        valid = [s for s in scores.values() if s is not None]
        composite = round(sum(valid) / len(valid), 1) if valid else None
        out.append({"code": code, "factors": fv, "scores": scores, "composite": composite})

    out.sort(key=lambda x: (x["composite"] is None, -(x["composite"] or 0)))
    return out[:top_n]
