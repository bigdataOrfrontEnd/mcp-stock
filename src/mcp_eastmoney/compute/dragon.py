"""龙头战法五维评分器 — 纯计算，零网络（移植自 dragon-quant 识别真龙体系）。

原版权重/门槛（scorers/registry.py）：
  带动性30% / 领涨性25% / 抗跌性15% / 流动性20% / 资金承接10%
  四大特征门槛 40/40/35/35，任一低于即一票否决；承接无门槛。

日线级近似说明（原版用分钟K的部分）：
- 带动性"脉冲跟随因果" → 用「板块当日涨停家数 + 板块指数涨幅」的板块共鸣近似
- 抗跌性"跳水段横盘/率先反弹" → 用「当日涨幅 - 基准涨幅」的相对强弱近似
- 资金承接"跨板块虹吸" → 用「所属行业指数近10日累计涨幅」分位近似
"""
from __future__ import annotations

from typing import Any

# ── 权重与门槛（与 dragon-quant registry 对齐） ──
DIM_WEIGHTS = {"drive": 0.30, "leadership": 0.25, "anti_drop": 0.15,
               "liquidity": 0.20, "absorption": 0.10}
DIM_FLOORS = {"drive": 40.0, "leadership": 40.0, "anti_drop": 35.0,
              "liquidity": 35.0}  # absorption 无门槛

# 子因子权重
EARLY_W, LEAD_W, VOICE_W = 0.40, 0.35, 0.25      # 带动性：封板早/板块带动/共鸣
BOARD_W, PCT_W = 0.50, 0.50                        # 领涨性：连板/5日涨幅
MARKET_W, SECTOR_W = 0.6, 0.4                      # 抗跌性：大盘/板块双基准
TURN_W, SEAL_W = 0.55, 0.45                        # 流动性：换手/封板质量


def _pct_rank(values: list[float | None]) -> list[float]:
    """横截面分位 0-100（None → 50 中性）。"""
    present = sorted(v for v in values if v is not None)
    n = len(present)
    if n == 0:
        return [50.0] * len(values)

    def one(v: float | None) -> float:
        if v is None:
            return 50.0
        cnt = sum(1 for p in present if p <= v)
        return (cnt - 1) / (n - 1) * 100 if n > 1 else 50.0

    return [one(v) for v in values]


def _to_minutes(t: str | None) -> float | None:
    """'093125' / '09:31:25' → 分钟数。"""
    if not t:
        return None
    s = str(t).replace(":", "").split(".")[0]
    if len(s) < 4 or not s[:4].isdigit():
        return None
    return int(s[:2]) * 60 + int(s[2:4])


def score_dragons(zt_rows: list[dict],
                  mom5_map: dict[str, float | None],
                  sector_ctx: dict[str, dict],
                  bench: dict[str, float]) -> list[dict]:
    """对当日涨停池打分。

    zt_rows: zt_pool 查询行（code/name/lian_ban/first_seal_time/blast_count/
             turnover_rate/industry/change_pct/amount...）
    mom5_map: {code: 5日涨幅%}（bars 现算）
    sector_ctx: {行业名: {"zt_count": n, "sector_pct": 板块指数当日涨幅,
                "cum10": 行业指数近10日累计涨幅%}}
    bench: {"broad": 大盘当日涨幅%, "sector_default": 板块缺失时的中性行业涨幅}

    返回按综合分降序的评分列表（含维度分/否决原因/is_true_dragon）。
    """
    if not zt_rows:
        return []
    n = len(zt_rows)
    codes = [r["code"] for r in zt_rows]

    # ---- 预计算各子因子原始值 ----
    seal_min = [_to_minutes(r.get("first_seal_time")) for r in zt_rows]
    max_lian = max((r.get("lian_ban") or 1) for r in zt_rows) or 1
    mom5 = [mom5_map.get(c) for c in codes]

    def sector_of(r: dict) -> dict:
        return sector_ctx.get(r.get("industry") or "", {}) or {}

    # ---- 维度1 带动性（封板早 + 板块带动 + 板块共鸣） ----
    early_rank = _pct_rank(seal_min)  # 早封 → 值小 → 分位反转
    early_rank = [100 - x for x in early_rank]
    lead_raw = [(sector_of(r).get("zt_count") or 0) * 2
                + (sector_of(r).get("sector_pct") or 0) for r in zt_rows]
    lead_rank = _pct_rank(lead_raw)
    voice_raw = [(sector_of(r).get("zt_count") or 0) for r in zt_rows]
    voice_rank = _pct_rank(voice_raw)
    drive = [EARLY_W * e + LEAD_W * l + VOICE_W * v
             for e, l, v in zip(early_rank, lead_rank, voice_rank)]

    # ---- 维度2 领涨性（连板 + 5日涨幅分位） ----
    board = [(r.get("lian_ban") or 1) / max_lian * 100 for r in zt_rows]
    mom_rank = _pct_rank(mom5)
    leadership = [BOARD_W * b + PCT_W * m for b, m in zip(board, mom_rank)]

    # ---- 维度3 抗跌性（双基准相对强弱，近似原版跳水段分析） ----
    broad = bench.get("broad")
    mkt_diff = [(r.get("change_pct") or 0) - broad if broad is not None else None
                for r in zt_rows]
    sec_diff = [((r.get("change_pct") or 0) - (sector_of(r).get("sector_pct")
                if sector_of(r).get("sector_pct") is not None
                else bench.get("sector_default")))
                if (sector_of(r).get("sector_pct") is not None
                    or bench.get("sector_default") is not None) else None
                for r in zt_rows]
    mkt_rank = _pct_rank(mkt_diff)
    sec_rank = _pct_rank(sec_diff)
    anti_drop = [MARKET_W * m + SECTOR_W * s for m, s in zip(mkt_rank, sec_rank)]

    # ---- 维度4 流动性（换手 + 封板质量，一字不罚） ----
    turn = [r.get("turnover_rate") for r in zt_rows]
    turn_rank = _pct_rank(turn)
    seal_q = []
    for r in zt_rows:
        blast = r.get("blast_count") or 0
        q = 100.0 - blast * 25.0          # 每炸板一次扣25
        if (r.get("close") and r.get("open") and r["close"] == r["open"]):
            pass  # 一字板不罚（原版规则）
        seal_q.append(max(q, 0.0))
    liquidity = [TURN_W * t + SEAL_W * q for t, q in zip(turn_rank, seal_q)]

    # ---- 维度5 资金承接（行业10日累计涨幅分位，无门槛） ----
    cum10 = [sector_of(r).get("cum10") for r in zt_rows]
    cum_rank = _pct_rank(cum10)
    absorption = cum_rank

    # ---- 聚合：门槛否决 + 加权 ----
    dims = list(zip(drive, leadership, anti_drop, liquidity, absorption))
    keys = ["drive", "leadership", "anti_drop", "liquidity", "absorption"]
    out = []
    for i, r in enumerate(zt_rows):
        d = {k: round(dims[i][j], 1) for j, k in enumerate(keys)}
        vetoes = [f"{k}={d[k]:.0f} < {DIM_FLOORS[k]:.0f}"
                  for k in DIM_FLOORS if d[k] < DIM_FLOORS[k]]
        composite = round(sum(DIM_WEIGHTS[k] * d[k] for k in keys), 1)
        out.append({
            "code": r["code"], "name": r.get("name"), "industry": r.get("industry"),
            "lian_ban": r.get("lian_ban"), "first_seal_time": r.get("first_seal_time"),
            "blast_count": r.get("blast_count"),
            "mom5": round(mom5[i], 2) if mom5[i] is not None else None,
            "dims": d, "composite": composite,
            "is_true_dragon": not vetoes,
            "vetoes": vetoes,
        })
    out.sort(key=lambda x: (not x["is_true_dragon"], -x["composite"]))
    return out
