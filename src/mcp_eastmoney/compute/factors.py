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
    # 形态（布尔 100/0，借鉴 InStock myhhub/stock 策略库，2026-10 移植）
    "new_high_60": {"name": "60日新高(海龟)", "category": "形态", "input": "daily",
                    "desc": "收盘价≥近60日最高收盘价：100是/0否（compute_filters min:100 复刻海龟入场）"},
    "recent_crash": {"name": "近期灾害K线", "category": "形态", "input": "daily",
                     "desc": "近20日单日跌幅≤-7%或两日累计≤-10%：100有/0无（compute_filters max:0 排雷）"},
    "ma30_slope": {"name": "MA30斜率", "category": "技术", "input": "daily",
                   "desc": "MA30相对30日前变化率%（趋势稳定性）"},
    "breakout_recent": {"name": "近期放量平台突破", "category": "形态", "input": "daily",
                        "desc": "近20日存在 open<MA60≤close 且量比≥2 的突破日：100有/0无"},
    # 形态二期（InStock 停机坪/回踩年线/高紧旗/低ATR，2026-10-29 移植）
    "parking_apron": {"name": "停机坪", "category": "形态", "input": "daily",
                      "desc": "近15日放量涨停后连续3日高开横盘不破涨停价（0.97<收/开<1.03，±5%内）：100有/0无"},
    "backtrace_ma250": {"name": "回踩年线", "category": "形态", "input": "daily",
                        "desc": "上破250日线后高位缩量回踩（后段缩量>2倍、回撤<20%、间隔10-50日）：100有/0无（需310+根K线）"},
    "high_tight_flag": {"name": "高而窄的旗形", "category": "形态", "input": "daily",
                        "desc": "收盘/近24~10日最低价≥1.9 且期间连续两日涨幅≥9.5%：100有/0无"},
    "range_10": {"name": "10日区间振幅", "category": "波动", "input": "daily",
                 "desc": "近10日最高收盘/最低收盘-1（%），InStock 低ATR成长判定用"},
}


def _new_high(closes: list[float], n: int = 60) -> float:
    """海龟入场（InStock turtle_trade）：收盘创 n 日新高。100/0 布尔。"""
    win = closes[-n:]
    return 100.0 if win and win[-1] >= max(win) else 0.0


def _recent_crash(bars: list[dict], n: int = 20,
                  day: float = -7.0, two_day: float = -10.0) -> float:
    """灾害K线（InStock low_backtrace_increase 的排除项）。100=有灾害（应排雷）。

    判定：近 n 日存在单日涨跌幅 ≤ day%，或相邻两日累计 ≤ two_day%。
    """
    win = bars[-n:]
    chgs = [float(b.get("change_pct") or 0) for b in win]
    for i, c in enumerate(chgs):
        if c <= day:
            return 100.0
        if i > 0 and chgs[i - 1] + c <= two_day:
            return 100.0
    return 0.0


def _ma_slope(closes: list[float], n: int = 30) -> float | None:
    """均线斜率（InStock keep_increasing）：MA_n 今值相对 30 日前变化率%。"""
    if len(closes) < n + 30:
        return None
    ma_now = sum(closes[-n:]) / n
    ma_then = sum(closes[-n - 30:-30]) / n
    return (ma_now / ma_then - 1) * 100 if ma_then else None


def _breakout_recent(bars: list[dict], n: int = 20) -> float:
    """放量平台突破（InStock breakthrough_platform + enter.check_volume）。100/0。

    近 n 日存在某日 open<MA60≤close（当日上穿60日线收盘站稳）且 量/5日均量≥2。
    """
    if len(bars) < 80:
        return 0.0
    closes = _closes(bars)
    vols = _vols(bars)
    for i in range(len(bars) - n, len(bars)):
        o = bars[i].get("open")
        if not isinstance(o, (int, float)):
            continue
        ma60 = sum(closes[i - 59:i + 1]) / 60
        if not (float(o) < ma60 <= closes[i]):
            continue
        avg5 = sum(vols[i - 5:i]) / 5 if i >= 5 and sum(vols[i - 5:i]) else None
        if avg5 and vols[i] / avg5 >= 2:
            return 100.0
    return 0.0


def _parking_apron(bars: list[dict], n: int = 15) -> float:
    """停机坪（InStock parking_apron）。100/0。

    近 n 日存在放量涨停日（涨幅≥9.5% 且 量/前5日均量≥2），
    其后连续 3 日：均高开于涨停价上方（open>涨停价），收盘上涨（首日），
    收/开偏离 <3%（0.97<收/开<1.03），第2/3日涨跌幅 ±5% 内且不破涨停价。
    """
    if len(bars) < n + 5:
        return 0.0
    vols = _vols(bars)
    win = bars[-n:]
    for i, b in enumerate(win):
        if float(b.get("change_pct") or 0) < 9.5:
            continue
        idx = len(bars) - n + i
        if idx < 5 or not vols[idx - 5:idx] or sum(vols[idx - 5:idx]) == 0:
            continue
        avg5 = sum(vols[idx - 5:idx]) / 5
        if vols[idx] / avg5 < 2:  # 涨停须放量
            continue
        after = win[i + 1:i + 4]
        if len(after) < 3:
            return 0.0
        try:
            zt = float(b["close"])
            d1 = after[0]
            if not (float(d1["open"]) > zt and float(d1["close"]) > zt
                    and float(d1.get("change_pct") or 0) > 0
                    and 0.97 < float(d1["close"]) / float(d1["open"]) < 1.03):
                continue
            ok = all(
                float(d["open"]) > zt and float(d["close"]) > zt
                and 0.97 < float(d["close"]) / float(d["open"]) < 1.03
                and -5 < float(d.get("change_pct") or 0) < 5
                for d in after[1:])
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if ok:
            return 100.0
    return 0.0


def _backtrace_ma250(bars: list[dict], n: int = 60) -> float:
    """回踩年线（InStock backtrace_ma250）。100/0。

    需 310+ 根（250日均线稳定 + 60日观察窗）：
    ① 近 n 日最高收盘日前段自下而上穿越 MA250；② 最高价日后段全程 ≥ MA250；
    ③ 后段最低日与最高日相差 10~50 个自然日；④ 缩量回踩（量比>2，回撤<20%）。
    """
    if len(bars) < 310:
        return 0.0
    closes = _closes(bars)
    vols = _vols(bars)
    dates = [str(b.get("date") or "") for b in bars]
    win = bars[-n:]
    hi_i = max(range(len(win)), key=lambda k: float(win[k]["close"]))
    hi_abs = len(bars) - n + hi_i
    front, end = bars[:hi_abs], bars[hi_abs:]
    if not front:  # 最高价日即窗口首日，无前段
        return 0.0

    def _ma250(idx: int) -> float:
        return sum(closes[idx - 249:idx + 1]) / 250

    ma_f0, ma_f1 = _ma250(0), _ma250(len(front) - 1)
    if not (closes[0] < ma_f0 and closes[len(front) - 1] > ma_f1):  # 前段上穿年线
        return 0.0
    lo_price, lo_vol, lo_i = None, None, hi_abs
    for k in range(hi_abs, len(bars)):
        ma = _ma250(k)
        if closes[k] < ma:  # 后段跌回年线下
            return 0.0
        if lo_price is None or closes[k] < lo_price:
            lo_price, lo_vol, lo_i = closes[k], vols[k], k
    try:
        from datetime import datetime
        d_hi = datetime.strptime(dates[hi_abs][:10], "%Y-%m-%d")
        d_lo = datetime.strptime(dates[lo_i][:10], "%Y-%m-%d")
        gap = (d_lo - d_hi).days
    except ValueError:
        gap = lo_i - hi_abs  # 日期不可解析时退化为交易日间隔
    if not (10 <= gap <= 50):
        return 0.0
    if vols[lo_i] and vols[hi_abs] / vols[lo_i] > 2 and lo_price / closes[hi_abs] < 0.8:
        return 100.0
    return 0.0


def _high_tight_flag(bars: list[dict]) -> float:
    """高而窄的旗形（InStock high_tight_flag，去掉龙虎榜机构门槛的形态近似）。100/0。"""
    if len(bars) < 60:
        return 0.0
    seg = bars[-24:-9]  # 之前24~10日
    lows = [float(b["low"]) for b in seg if isinstance(b.get("low"), (int, float))]
    if not lows or min(lows) <= 0:
        return 0.0
    if float(bars[-1]["close"]) / min(lows) < 1.9:
        return 0.0
    chgs = [float(b.get("change_pct") or 0) for b in bars[-24:]]
    for i in range(len(chgs) - 1):
        if chgs[i] >= 9.5 and chgs[i + 1] >= 9.5:
            return 100.0
    return 0.0


def _range_n(closes: list[float], n: int = 10) -> float | None:
    """近 n 日收盘区间振幅 %（InStock 低ATR成长的区间判定）。"""
    win = closes[-n:]
    if len(win) < n or not win or min(win) <= 0:
        return None
    return (max(win) - min(win)) / min(win) * 100


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
    if name == "new_high_60":
        return _new_high(closes, 60)
    if name == "recent_crash":
        return _recent_crash(bars, 20)
    if name == "ma30_slope":
        return _ma_slope(closes, 30)
    if name == "breakout_recent":
        return _breakout_recent(bars, 20)
    if name == "parking_apron":
        return _parking_apron(bars, 15)
    if name == "backtrace_ma250":
        return _backtrace_ma250(bars, 60)
    if name == "high_tight_flag":
        return _high_tight_flag(bars)
    if name == "range_10":
        return _range_n(closes, 10)
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


# 低值=高分的因子（波动、估值、灾害标志）
INVERTED_FACTORS = {"vol20", "vol60", "pe", "pb", "recent_crash"}


def catalog() -> list[dict]:
    """因子目录（factor_catalog tool 用）：时序因子 + 快照截面因子统一列出。"""
    out = [
        {"factor": k, **{kk: vv for kk, vv in v.items()},
         "direction": "低值得分高" if k in INVERTED_FACTORS else "高值得分高"}
        for k, v in FACTOR_META.items()
    ]
    out.extend({"factor": k, **v} for k, v in SNAP_FACTOR_META.items())
    return out


# 快照截面因子（策略 factor_weights 引用的截面字段，来自 quotes_daily 派生截面）。
# 计算无公式（字段即值），仅注册元数据供目录统一展示与 screener 校验。
SNAP_FACTOR_META: dict[str, dict[str, Any]] = {
    "change_pct":  {"name": "当日涨幅", "category": "截面", "input": "snapshot",
                    "desc": "策略层打分字段（快照段）"},
    "amount":      {"name": "成交额", "category": "截面", "input": "snapshot",
                    "desc": "策略层打分字段（快照段）"},
    "turnover_rate": {"name": "换手率", "category": "截面", "input": "snapshot",
                      "desc": "策略层打分字段（快照段）"},
    "volume_ratio": {"name": "量比", "category": "截面", "input": "snapshot",
                     "desc": "策略层打分字段（快照段）"},
    "amplitude":   {"name": "振幅", "category": "截面", "input": "snapshot",
                    "desc": "策略层打分字段（快照段）"},
    "pe":          {"name": "市盈率", "category": "截面", "input": "snapshot",
                    "desc": "策略层打分字段（快照段，低值得分高需在策略inverted声明）"},
    "pb":          {"name": "市净率", "category": "截面", "input": "snapshot",
                    "desc": "策略层打分字段（快照段，低值得分高需在策略inverted声明）"},
    "total_mv":    {"name": "总市值", "category": "截面", "input": "snapshot",
                    "desc": "策略层打分字段（快照段）"},
    "float_mv":    {"name": "流通市值", "category": "截面", "input": "snapshot",
                    "desc": "策略层打分字段（快照段）"},
}


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
    """分位数学统一走 compute/crosssec（唯一实现，invert 反向）。"""
    from .crosssec import percentile_rank
    scores = percentile_rank(values)
    return [100 - s if (s is not None and invert) else s for s in scores]


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
