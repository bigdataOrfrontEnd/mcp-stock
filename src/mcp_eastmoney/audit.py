"""策略 × 数据源支撑度审计（只读，不触发任何网络拉取）。

对每个策略的三类依赖逐一检查本地覆盖：
  ① 截面字段（hard_filters/factor_weights/soft_scores 引用的 quotes_daily 派生列）
  ② K线因子（compute_* 引用的日K因子，按所需最少K线根数抽查活跃池覆盖率）
  ③ 财务因子（roe/gross_margin/net_margin ← financial_indicators 覆盖面）

用法：UV_CACHE_DIR=.uvcache uv run python -m mcp_eastmoney.audit
"""
from __future__ import annotations

import asyncio

from .data import db
from .recommend import screener
from .compute.factors import FACTOR_META

# 因子所需最少K线根数（粗粒度：60日因子→60，ma250 类→310，含斜率→60+30）
def _bars_needed(factor: str) -> int:
    if factor == "backtrace_ma250":
        return 310
    if factor == "ma30_slope":
        return 60
    if "60" in factor:
        return 60
    if "20" in factor:
        return 20
    return 30


async def audit(verbose: bool = True) -> list[dict]:
    strategies = screener.load_strategies()
    rows = db.query_market_snapshot()
    if not rows:
        print("!! 无截面数据（quotes_daily 为空），先 refresh snapshot")
        return []
    snap_date = db.snapshot_dates(1)[0]

    # 截面字段非空覆盖率（quotes_daily 派生截面）
    snap_cols = {}
    for col in rows[0]:
        if col in ("code", "name"):
            continue
        ok = sum(1 for r in rows if r.get(col) is not None)
        snap_cols[col] = round(ok / len(rows) * 100, 1)

    # K线覆盖抽查池：最新截面成交额 Top 200（策略候选最可能出自这里）
    rows.sort(key=lambda r: -(r.get("amount") or 0))
    pool = rows[:200]
    pool_codes = [r["code"] for r in rows[:200]]
    bar_len: dict[str, int] = {}
    for code in pool_codes:
        bars = db.query_klines_daily(code, limit=1)
        bar_len[code] = 0  # 占位，下面用 count 查询更准
    c = db._conn()
    try:
        counts = dict(c.execute(
            "SELECT code, COUNT(*) FROM quotes_daily WHERE code IN "
            f"({','.join('?' * len(pool_codes))}) GROUP BY code", pool_codes).fetchall())
    finally:
        c.close()

    fin_codes = {r[0] for r in db._conn().execute(
        "SELECT DISTINCT code FROM financial_indicators").fetchall()}

    def _hf_fields(hf: dict) -> set[str]:
        # hard_filters 键名 → 快照列名映射（与 compute/screener._passes 的映射一致）
        alias = {"price": "close", "turnover": "turnover_rate"}
        out = set()
        for k in hf:
            if k == "exclude_st":
                out.add("name")
            elif k.endswith(("_min", "_max")):
                f = k.rsplit("_", 1)[0]
                out.add(alias.get(f, f))
        return out

    # 快照段只认快照字段；K线因子误写进 factor_weights 属于策略 bug，单独检测
    SNAP_FIELDS = set(snap_cols) | {"name"}

    report = []
    for name, s in sorted(strategies.items()):
        hf = s.get("hard_filters") or {}
        fw = s.get("factor_weights") or {}
        ss = s.get("soft_scores") or {}
        cf = s.get("compute_filters") or {}
        cw = s.get("compute_weights") or {}
        verdict, issues = "OK", []

        # ① 快照字段（含"K线因子误写进快照段"检测）
        snap_fields = _hf_fields(hf) | set(fw) | set(ss)
        snap_gaps = {f: snap_cols.get(f) for f in snap_fields
                     if f not in snap_cols or (snap_cols.get(f) or 0) < 90}
        misplaced = [k for k in (set(fw) | set(ss))
                     if k in FACTOR_META and FACTOR_META[k]["input"] == "daily"
                     and k not in snap_cols]
        if misplaced:
            verdict = "BROKEN"
            issues.append(f"K线因子误写进快照段 factor_weights（静默失效）: {misplaced} → 应移到 compute_weights")

        # ② K线因子
        daily_factors = [k for k in (set(cf) | set(cw))
                         if k in FACTOR_META and FACTOR_META[k]["input"] == "daily"]
        fin_factors = [k for k in (set(cf) | set(cw))
                       if k in FACTOR_META and FACTOR_META[k]["input"] == "fin"]
        unknown = [k for k in (set(cf) | set(cw)) if k not in FACTOR_META]
        kline_cov = None
        if daily_factors:
            need = max(_bars_needed(k) for k in daily_factors)
            ok = sum(1 for code in pool_codes if counts.get(code, 0) >= need)
            kline_cov = {"need_bars": need, "covered": ok, "pool": len(pool_codes),
                         "pct": round(ok / len(pool_codes) * 100, 1)}

        # ③ 财务覆盖（对抽查池）
        fin_cov = None
        if fin_factors:
            hit = sum(1 for code in pool_codes if code in fin_codes)
            fin_cov = {"covered": hit, "pool": len(pool_codes),
                       "pct": round(hit / len(pool_codes) * 100, 1)}

        # 判定（verdict/issues 已在 ① 前初始化，此处只累加）
        if unknown:
            verdict = "BROKEN"
            issues.append(f"未注册因子: {unknown}")
        if snap_gaps:
            verdict = "DEGRADED" if verdict == "OK" else verdict
            issues.append(f"快照字段缺口: { {k: v for k, v in snap_gaps.items()} }")
        if kline_cov and kline_cov["pct"] < 50:
            verdict = "DEGRADED" if verdict == "OK" else verdict
            issues.append(f"K线覆盖率 {kline_cov['pct']}%（需≥{kline_cov['need_bars']}根的仅 {kline_cov['covered']}/{kline_cov['pool']}）")
        if fin_cov and fin_cov["pct"] < 10:
            verdict = "STARVED" if verdict == "OK" else verdict
            issues.append(f"财务覆盖 {fin_cov['pct']}%（{fin_cov['covered']}/{fin_cov['pool']}）——策略核心条件近乎全灭")
        report.append({"strategy": name, "verdict": verdict,
                       "kline": kline_cov, "fin": fin_cov, "issues": issues})
        if verbose:
            print(f"[{verdict:8s}] {name:22s} " + ("; ".join(issues) if issues else "数据支撑完整"))
    return report


if __name__ == "__main__":
    asyncio.run(audit())
