"""策略筛选引擎 — 两段式筛选：快照截面(数据层字段) + 计算层因子。

分层边界（数据层→计算层→策略层）：
- 快照段：全市场截面行（db.query_market_snapshot 从 quotes_daily 派生），
  硬过滤(hard_filters) + 截面分位打分(factor_weights)，便宜、覆盖全市场。
- 计算层段：compute_filters / compute_weights 引用的因子名必须来自
  compute/factors.py 的 FACTOR_META 注册表（动量/波动/流动性/技术/估值/质量）。
  因子公式与反向方向的唯一来源在计算层；本层只做过滤与加权，不实现公式。
  K线经 bars_get 回调注入（依赖倒置，便于离线单测；缺K线候选该段记 None）。

策略定义：src/mcp_eastmoney/recommend/strategies/*.yaml（包内置），
         data/strategies/*.yaml（用户自定义，同名覆盖内置）。
策略改 YAML 不改代码。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

import yaml

from ..compute import crosssec
from ..compute import factors as F

_PACKAGED_DIR = Path(__file__).resolve().parent / "strategies"
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_USER_DIR = Path(os.environ.get(
    "STOCK_STRATEGY_DIR", str(_PROJECT_ROOT / "data" / "strategies")))


def load_strategies() -> dict[str, dict]:
    """加载策略：用户版覆盖包内置版（同名）。

    同时同步到 SQLite strategies 注册表（数据治理单写入口：注册经 db.py），
    DB 不可用时静默降级为纯文件模式。
    """
    out: dict[str, dict] = {}
    sources: dict[str, str] = {}
    for label, d in (("packaged", _PACKAGED_DIR), ("user", _USER_DIR)):
        if not d.exists():
            continue
        for p in sorted(d.glob("*.yaml")):
            try:
                s = yaml.safe_load(p.read_text(encoding="utf-8"))
                if isinstance(s, dict) and s.get("name"):
                    out[s["name"]] = s
                    sources[s["name"]] = label
            except yaml.YAMLError:
                continue  # 坏文件跳过
    try:
        from ..data import db
        db.sync_strategy_registry(
            {n: (s, sources.get(n, "packaged")) for n, s in out.items()})
    except Exception:
        pass
    return out


def _passes(row: dict, hf: dict) -> bool:
    """硬过滤：字段缺失（None）视为不过滤通过（交由打分惩罚/缺权处理）。"""
    if hf.get("exclude_st", True) and row.get("name"):
        n = str(row["name"]).upper()
        if "ST" in n or "退" in n:
            return False
    checks = [
        ("amount_min", "amount"), ("amount_max", "amount"),
        ("change_pct_min", "change_pct"), ("change_pct_max", "change_pct"),
        ("price_min", "close"), ("price_max", "close"),
        ("pe_min", "pe"), ("pe_max", "pe"),
        ("pb_min", "pb"), ("pb_max", "pb"),
        ("turnover_min", "turnover_rate"), ("turnover_max", "turnover_rate"),
        ("volume_ratio_min", "volume_ratio"), ("volume_ratio_max", "volume_ratio"),
        ("amplitude_max", "amplitude"),
        ("total_mv_min", "total_mv"), ("total_mv_max", "total_mv"),
        ("float_mv_min", "float_mv"), ("float_mv_max", "float_mv"),
    ]
    for fk, rk in checks:
        limit = hf.get(fk)
        if limit is None:
            continue
        v = row.get(rk)
        if v is None:
            return False  # 关键过滤字段缺失 → 直接淘汰
        if fk.endswith("_min") and v < limit:
            return False
        if fk.endswith("_max") and v > limit:
            return False
    return True


def _percentile(values: list[float], invert: bool) -> list[float]:
    """分位数学统一走 compute/crosssec（本层不自带实现）。"""
    scores = crosssec.percentile_rank(values)
    return [100 - s if (s is not None and invert) else s
            for s in crosssec.neutral_fill(scores)]


def _compute_stage(strategy: dict, pool: list[dict],
                   bars_get: Callable[[str], list[dict] | None] | None,
                   ) -> tuple[dict[str, dict[str, float | None]],
                              dict[str, dict[str, float | None]], dict[str, bool]]:
    """计算层段：对 pool 内候选计算 FACTOR_META 因子。

    返回 (values, scores, has_bars)：code → {factor: 原始值/分位分}。
    无K线 → 该候选因子全 None 且 has_bars=False（compute_filters 对其
    降级放行，融合退化为快照段分，与快照段"缺数据不判死"口径一致）。
    """
    cf: dict[str, dict] = strategy.get("compute_filters") or {}
    cw: dict[str, float] = strategy.get("compute_weights") or {}
    keys = sorted(set(cf) | set(cw))
    unknown = [k for k in keys if k not in F.FACTOR_META]
    if unknown:
        raise ValueError(f"策略引用了计算层未注册的因子: {unknown}（见 factor_catalog）")
    values: dict[str, dict[str, float | None]] = {}
    has_bars: dict[str, bool] = {}
    # 财务类因子（roe 等）需要财务指标输入：从 financial_indicators 取最新报告期
    fin_keys = {k for k in keys if k in F.FACTOR_META and F.FACTOR_META[k]["input"] == "fin"}
    fins: dict[str, dict] = {}
    if fin_keys:
        try:
            from ..data import db
            fins = db.latest_financials([r["code"] for r in pool])
        except Exception:
            fins = {}
    for r in pool:
        code = r["code"]
        bars = bars_get(code) if bars_get else None
        has_bars[code] = bool(bars)
        values[code] = (F.compute_factors(bars, keys, fin=fins.get(code))
                        if bars else {k: None for k in keys})

    scores: dict[str, dict[str, float | None]] = {}
    for k in keys:
        col = F._percentile_scores([values[r["code"]][k] for r in pool],
                                   invert=k in F.INVERTED_FACTORS)
        for i, r in enumerate(pool):
            scores.setdefault(r["code"], {})[k] = (
                round(col[i], 1) if col[i] is not None else None)
    return values, scores, has_bars


def screen(rows: list[dict], strategy: dict, top_n: int = 10,
           bars_get: Callable[[str], list[dict] | None] | None = None,
           pool_size: int | None = None) -> dict:
    """执行两段式筛选。返回 {universe, filtered, ranking[, compute]}。

    - 快照段：hard_filters + factor_weights（截面分位，缺字段跳过该因子）。
    - 计算层段（YAML 含 compute_filters/compute_weights 时启用）：对快照段
      幸存者的前 pool_size（默认 max(top_n*5, 50)）名，用计算层因子二次
      过滤+打分，最终 composite 为两段加权融合。缺K线候选计算层分记 None，
      融合退化为快照段分。
    """
    hf = strategy.get("hard_filters") or {}
    weights: dict[str, float] = strategy.get("factor_weights") or {}
    inverted = set(strategy.get("inverted") or [])
    cf: dict[str, dict] = strategy.get("compute_filters") or {}
    cw: dict[str, float] = strategy.get("compute_weights") or {}
    use_compute = bool(cf or cw) and bars_get is not None

    kept = [r for r in rows if _passes(r, hf)]
    if not kept or not (weights or use_compute):
        return {"universe": len(rows), "filtered": len(kept), "ranking": []}

    # ---- 快照段打分（无 snapshot 权重时仅排序占位 50 分） ----
    col_scores: dict[str, list[float]] = {}
    for k in weights:
        vals_raw = [r.get(k) for r in kept]
        idx_ok = [i for i, v in enumerate(vals_raw) if isinstance(v, (int, float))]
        if not idx_ok:
            col_scores[k] = [None] * len(kept)
            continue
        scores = _percentile([vals_raw[i] for i in idx_ok], k in inverted)
        col = [None] * len(kept)
        for j, i in enumerate(idx_ok):
            col[i] = scores[j]
        col_scores[k] = col

    # ---- 软打分段（soft_scores: 锚点区间，借鉴 AlphaSift ideal/chase 曲线） ----
    # 格式: {字段: {full: [lo,hi], hard: [lo_h,hi_h], weight: w}}
    #   —— 与 weights 并入同一加权合成（百分比/量比等"过热惩罚"类字段的正确姿势）
    soft: dict[str, dict] = strategy.get("soft_scores") or {}
    for k, cfg in soft.items():
        vals_raw = [r.get(k) for r in kept]
        scores = crosssec.anchor_score(vals_raw,
                                       full=tuple(cfg["full"]),
                                       hard=tuple(cfg["hard"]))
        col_scores[k] = crosssec.neutral_fill(scores)
        weights = {**weights, k: float(cfg.get("weight", 1.0))}

    snap_ranking = []
    # 加权合成统一走 compute/crosssec（缺失因子按剩余权重归一）
    score_rows = [{k: col_scores.get(k, [None] * len(kept))[i] for k in weights}
                  for i in range(len(kept))]
    composites = crosssec.weighted_composite(score_rows, weights)
    for i, r in enumerate(kept):
        composite = composites[i]
        snap_ranking.append({
            "code": r["code"], "name": r.get("name"), "close": r.get("close"),
            "change_pct": r.get("change_pct"), "amount": r.get("amount"),
            "turnover_rate": r.get("turnover_rate"), "volume_ratio": r.get("volume_ratio"),
            "pe": r.get("pe"), "pb": r.get("pb"), "total_mv": r.get("total_mv"),
            "composite": composite,
        })
    snap_ranking.sort(key=lambda x: (x["composite"] is None, -(x["composite"] or 0)))

    if not use_compute:
        return {"universe": len(rows), "filtered": len(kept),
                "ranking": snap_ranking[:top_n]}

    # ---- 计算层段 ----
    # 计算层池：K线已本地全量，扩大池子避免"合格但排名靠后"的票被漏掉
    pool = snap_ranking[: (pool_size or max(top_n * 5, 200))]
    values, f_scores, has_bars = _compute_stage(strategy, pool, bars_get)

    # compute_filters 硬过滤：有K线但因子值缺失/越界 → 淘汰；
    # 无K线（基础设施缺口，非个股问题）→ 降级放行，计算层分记 None。
    dropped: list[dict] = []
    survivors = []
    for x in pool:
        v = values[x["code"]]
        ok = True
        if has_bars.get(x["code"]):
            for k, lim in cf.items():
                val = v.get(k)
                if val is None:
                    ok = False
                    break
                if "min" in lim and val < lim["min"]:
                    ok = False
                    break
                if "max" in lim and val > lim["max"]:
                    ok = False
                    break
        (survivors if ok else dropped).append(x)

    w_snap, w_comp = sum(weights.values()), sum(cw.values()) or 1.0
    comp_rows = [{k: f_scores[x["code"]].get(k) for k in cw} for x in survivors]
    comp_scores = crosssec.weighted_composite(comp_rows, cw)
    out = []
    for x, comp_score in zip(survivors, comp_scores):
        code = x["code"]
        snap = x["composite"]
        if comp_score is not None and snap is not None and w_snap > 0:
            final = round((snap * w_snap + comp_score * w_comp) / (w_snap + w_comp), 1)
        else:
            final = comp_score if comp_score is not None else snap
        out.append({**x, "compute_score": comp_score,
                    "compute_factors": {k: values[code][k] for k in cw},
                    "composite": final})
    out.sort(key=lambda x: (x["composite"] is None, -(x["composite"] or 0)))
    return {"universe": len(rows), "filtered": len(kept),
            "ranking": out[:top_n],
            "compute": {"pool": len(pool), "dropped_by_filters": len(dropped),
                        "factor_keys": sorted(set(cf) | set(cw)),
                        "note": "因子来自计算层 FACTOR_META，bars 经 bars_get 注入"}}
