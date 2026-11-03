"""龙头战法五维评分器 v2 — 纯计算，零网络（移植自 dragon-quant 识别真龙体系）。

v2 优化（相对 v1 的审查修正）：
- 带动性：新增「封板时间领先度」子因子（比同板块其它涨停股平均早封多少分钟）
  —— 日线级近似原版"个股脉冲在前、板块跟随在后"的因果方向；板块涨停家数只计一次
- 领涨性：连板改为原版衰减式 100-(max-lian)×10（线性式对低板过度惩罚）
- 抗跌性：改为「振幅倒数」分位（低振幅=秒板不回头=抗跌）——修复 v1 中
  "全部涨停股涨幅相同导致相对强弱退化"的噪声问题
- 流动性：一字板检测修复（经 snapshot 的 open/high/low），一字=不罚换手
- 资金承接：THS 行业指数缺失时回退自有代理（板块近5日日均涨停家数）

权重/门槛保持原版：30/25/15/20/10，floor 40/40/35/35 一票否决（承接无门槛）。
"""
from __future__ import annotations

from typing import Any

# ── 权重与门槛（与 dragon-quant registry 对齐） ──
DIM_WEIGHTS = {"drive": 0.30, "leadership": 0.25, "anti_drop": 0.15,
               "liquidity": 0.20, "absorption": 0.10}
DIM_FLOORS = {"drive": 40.0, "leadership": 40.0, "anti_drop": 35.0,
              "liquidity": 35.0}  # absorption 无门槛

# 子因子权重（无15min数据时的 v2 三因子）
EARLY_W, LEAD_W, VOICE_W = 0.40, 0.35, 0.25
# 有15min数据时的四因子（带动性升级：加入早盘冲击力）
EARLY_W4, LEAD_W4, IMPULSE_W4, VOICE_W4 = 0.30, 0.25, 0.25, 0.20
BOARD_DECAY = 10.0                                # 领涨：每低于最高板1板扣分
BOARD_W, PCT_W = 0.50, 0.50
TURN_W, SEAL_W = 0.55, 0.45
BLAST_PENALTY = 25.0
ABS_THS_W, ABS_OWN_W = 0.6, 0.4                  # 承接：THS指数 / 自有涨停趋势


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


def _is_yizi(r: dict, snap: dict | None = None) -> bool:
    """一字板判定的唯一公式入口（全项目共用）。

    优先级：行自带 high/low（日K bar）→ snapshot high==low →
    近似（首封 09:25 且零炸板）。
    """
    hi, lo = r.get("high"), r.get("low")
    if hi is not None and lo is not None:
        return float(hi) == float(lo)
    if snap and snap.get("open") is not None:
        o, h, l = snap.get("open"), snap.get("high"), snap.get("low")
        return h is not None and l is not None and float(h) == float(l)
    # zt_pool 无 open 列 → 近似：首封时间为 09:25 且零炸板
    return str(r.get("first_seal_time") or "").startswith("0925") \
        and (r.get("blast_count") or 0) == 0


def _lead_margin(zt_rows: list[dict]) -> list[float | None]:
    """封板领先度（分钟）：比同板块其它涨停股平均封板时间早多少。

    正值 = 它先封（带动者）；负值 = 别人先封（跟随者）。
    板块内独苗 → None（中性）。
    """
    groups: dict[str, list[float]] = {}
    times = [_to_minutes(r.get("first_seal_time")) for r in zt_rows]
    for r, t in zip(zt_rows, times):
        if t is not None and r.get("industry"):
            groups.setdefault(r["industry"], []).append(t)
    out: list[float | None] = []
    for r, t in zip(zt_rows, times):
        peers = groups.get(r.get("industry") or "", [])
        if t is None or len(peers) < 2:
            out.append(None)
            continue
        peer_avg = (sum(peers) - t) / (len(peers) - 1)
        out.append(round(peer_avg - t, 1))   # 正=早于同伴
    return out


def score_dragons(zt_rows: list[dict],
                  mom5_map: dict[str, float | None],
                  sector_ctx: dict[str, dict],
                  bench: dict[str, float],
                  snap_map: dict[str, dict] | None = None,
                  sector_abs: dict[str, float] | None = None,
                  impulse_map: dict[str, float | None] | None = None) -> list[dict]:
    """对当日涨停池打分（v2.1）。

    snap_map: {code: {open/high/low/amplitude}}（派生截面行，一字判定与振幅）
    sector_abs: {行业名: 近5日板块日均涨停家数}（承接的自有代理，缺 THS cum10 时用）
    impulse_map: {code: 早盘冲击力%}（前45分钟累计涨幅，15min线现算；
                 提供时带动性切换为四因子权重，缺失个股该因子取中性50）
    """
    snap_map = snap_map or {}
    sector_abs = sector_abs or {}
    impulse_map = impulse_map or {}
    if not zt_rows:
        return []
    codes = [r["code"] for r in zt_rows]
    max_lian = max((r.get("lian_ban") or 1) for r in zt_rows) or 1
    mom5 = [mom5_map.get(c) for c in codes]

    def sector_of(r: dict) -> dict:
        return sector_ctx.get(r.get("industry") or "", {}) or {}

    # ---- 维度1 带动性：封板早 + 领先同板块 + [早盘冲击力] + 板块共鸣 ----
    seal_min = [_to_minutes(r.get("first_seal_time")) for r in zt_rows]
    early_rank = [100 - x for x in _pct_rank(seal_min)]      # 早封 → 高分
    margin_rank = _pct_rank(_lead_margin(zt_rows))            # 带动者 → 高分
    voice_rank = _pct_rank([(sector_of(r).get("zt_count") or 0)
                            + (sector_of(r).get("sector_pct") or 0)
                            for r in zt_rows])
    impulses = [impulse_map.get(c) for c in codes]
    impulse_available = any(v is not None for v in impulses)
    if impulse_available:
        impulse_rank = _pct_rank(impulses)                    # 早盘率先放量 → 高分
        drive = [EARLY_W4 * e + LEAD_W4 * m + IMPULSE_W4 * p + VOICE_W4 * v
                 for e, m, p, v in zip(early_rank, margin_rank, impulse_rank, voice_rank)]
    else:
        drive = [EARLY_W * e + LEAD_W * m + VOICE_W * v
                 for e, m, v in zip(early_rank, margin_rank, voice_rank)]

    # ---- 维度2 领涨性：连板衰减式 + 5日涨幅分位 ----
    board = [max(100.0 - (max_lian - (r.get("lian_ban") or 1)) * BOARD_DECAY, 0.0)
             for r in zt_rows]
    mom_rank = _pct_rank(mom5)
    leadership = [BOARD_W * b + PCT_W * m for b, m in zip(board, mom_rank)]

    # ---- 维度3 抗跌性：振幅倒数（低振幅=秒板不回头） ----
    amp = [(snap_map.get(c) or {}).get("amplitude") for c in codes]
    amp_inv = [(-a if a is not None else None) for a in amp]
    anti_drop = _pct_rank(amp_inv)

    # ---- 维度4 流动性：换手 + 封板质量（一字不罚换手） ----
    turn = [r.get("turnover_rate") for r in zt_rows]
    turn_rank = _pct_rank(turn)
    liquidity = []
    for i, r in enumerate(zt_rows):
        blast = r.get("blast_count") or 0
        seal_q = max(100.0 - blast * BLAST_PENALTY, 0.0)
        if _is_yizi(r, snap_map.get(r["code"])):
            liquidity.append(seal_q)  # 一字：只看封板质量（换手不罚，归一到0-100）
        else:
            liquidity.append(TURN_W * turn_rank[i] + SEAL_W * seal_q)

    # ---- 维度5 资金承接：THS行业10日累计(0.6) + 板块涨停趋势(0.4) ----
    cum10_rank = _pct_rank([sector_of(r).get("cum10") for r in zt_rows])
    own_rank = _pct_rank([sector_abs.get(r.get("industry") or "") for r in zt_rows])
    absorption = [ABS_THS_W * t + ABS_OWN_W * o for t, o in zip(cum10_rank, own_rank)]

    # ---- 聚合：门槛否决 + 加权 ----
    keys = ["drive", "leadership", "anti_drop", "liquidity", "absorption"]
    out = []
    for i, r in enumerate(zt_rows):
        d = {k: round(v, 1) for k, v in zip(keys, (drive[i], leadership[i],
                anti_drop[i], liquidity[i], absorption[i]))}
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


def early_impulse(m15: list[dict]) -> float | None:
    """早盘冲击力：前 3 根 15min bar（约45分钟）的累计涨幅%。

    龙头通常在板块启动段率先放量拉升——比'封板时间'更早的领先信号。
    """
    if len(m15) < 4:
        return None
    head = m15[:3]
    o = float(head[0]["open"])
    c = float(head[-1]["close"])
    return round((c / o - 1) * 100, 2) if o else None
