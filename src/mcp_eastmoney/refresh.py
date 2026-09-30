"""定时刷新编排 — CLI 入口。

用法:
  uv run python -m mcp_eastmoney.refresh basic                # 全量 stock_basic
  uv run python -m mcp_eastmoney.refresh industry             # 全量 stock_industry
  uv run python -m mcp_eastmoney.refresh daily --codes 600519,000001 [--days 60]
  uv run python -m mcp_eastmoney.refresh daily --watchlist
  uv run python -m mcp_eastmoney.refresh 15min --codes 600519 [--days 5]
  uv run python -m mcp_eastmoney.refresh concepts [--all]
  uv run python -m mcp_eastmoney.refresh news                # 财联社快讯 + 自选股个股新闻
  uv run python -m mcp_eastmoney.refresh eod                  # 每日收盘后全套（补跑自查）
  uv run python -m mcp_eastmoney.refresh weekly               # 每周全套（basic + industry）

原则：单只失败不阻断整体，记 fetch_log 继续；全部 INSERT OR REPLACE 幂等。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timedelta

from .data import akshare_client as akc
from .data import baostock_client as bc
from .data import db

import pandas as pd
from .data.codes import normalize

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("refresh")

HISTORY_START = "2015-01-01"  # 日K首次全量回补起点
TOP_CONCEPTS = 50             # 默认只刷涨跌幅 Top N 概念


def _codes_from_args(args) -> list[str]:
    if args.watchlist:
        wl = db.watchlist_all()
        codes = [w["code"] for w in wl]
        if not codes:
            log.warning("watchlist 为空，无代码可刷")
        return codes
    if args.codes:
        return [normalize(c) for c in args.codes.split(",")]
    return []


# ---- 各任务 ----------------------------------------------------------------------


async def refresh_basic() -> dict:
    t0 = time.monotonic()
    try:
        df = await bc.query_stock_basic()
        # 只保留股票（type=1）且在市，避免指数/转债塞满基础表
        n = db.upsert_stock_basic(df)
        db.log_fetch("basic", "query_stock_basic", "baostock", "success",
                     int((time.monotonic() - t0) * 1000), param="full")
        log.info("stock_basic 写入 %d 条", n)
        return {"rows": n}
    except Exception as e:
        db.log_fetch("basic", "query_stock_basic", "baostock", "failed",
                     int((time.monotonic() - t0) * 1000), error=str(e))
        raise


async def refresh_industry() -> dict:
    t0 = time.monotonic()
    try:
        df = await bc.query_stock_industry()
        n = db.upsert_stock_industry(df)
        db.log_fetch("industry", "query_stock_industry", "baostock", "success",
                     int((time.monotonic() - t0) * 1000), param="full")
        log.info("stock_industry 写入 %d 条", n)
        return {"rows": n}
    except Exception as e:
        db.log_fetch("industry", "query_stock_industry", "baostock", "failed",
                     int((time.monotonic() - t0) * 1000), error=str(e))
        raise


async def refresh_daily(codes: list[str], days: int = 60, include_15min: bool = False) -> dict:
    """日K增量落库：库中已有则从最后日期+1天增量拉，无则全量回补 HISTORY_START 起。"""
    results: dict[str, str] = {}
    for code in codes:
        t0 = time.monotonic()
        try:
            last = db.get_last_trade_date(code)
            if last:
                start = db.next_trade_day(last)
                # 已是最新则跳过（增量起点在未来说明今天已落过）
                if start > datetime.now().strftime("%Y-%m-%d"):
                    results[code] = "up-to-date"
                    continue
            else:
                # 首次：回补 days 天（days=0 表示回补到 HISTORY_START）
                start = HISTORY_START if days == 0 else (
                    datetime.now() - timedelta(days=days)
                ).strftime("%Y-%m-%d")
            df = await bc.query_kline(code, "d", start=start)
            df = df[df["tradestatus"] == "1"]  # 剔除停牌日空行
            n = db.upsert_klines_daily(df)
            if n == 0:
                # Baostock 当日未发布 → 腾讯兜底
                n = await _fill_today_tencent(code)
                results[code] = f"ok({n},tencent_fallback)"
            else:
                results[code] = f"ok({n})"
            db.log_fetch("daily", "query_kline", "baostock", "success",
                         int((time.monotonic() - t0) * 1000), param=f"{code}:{start}")
        except Exception as e:
            results[code] = f"failed: {e}"
            db.log_fetch("daily", "query_kline", "baostock", "failed",
                         int((time.monotonic() - t0) * 1000), error=str(e), param=code)
        if include_15min:
            try:
                results[code] += ";" + await refresh_15min_one(code)
            except Exception as e:
                results[code] += f";15min failed: {e}"
    log.info("daily 结果: %s", results)
    return {"updated": results, "total": len(codes), "db": db.DB_PATH}


def _last_15m_date(last: str | None) -> str | None:
    """从库内最后 15min 时间戳提取日期（兼容 '2026-09-24 15:00:00' 与 '20260924150000' 两种格式）。"""
    if not last:
        return None
    if "-" in last:
        return last[:10]
    if len(last) >= 8:
        return f"{last[:4]}-{last[4:6]}-{last[6:8]}"
    return None


async def refresh_15min_one(code: str) -> str:
    last = db.get_last_15m_datetime(code)
    ld = _last_15m_date(last)
    start = ld or (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
    df = await bc.query_kline(code, "15", start=start)
    n = db.upsert_klines_15m(df)
    return f"15min ok({n})"


async def refresh_15min(codes: list[str], days: int = 5) -> dict:
    """15 分钟线：只能增量（回溯窗口短），错过不可补。"""
    results: dict[str, str] = {}
    for code in codes:
        t0 = time.monotonic()
        try:
            ld = _last_15m_date(db.get_last_15m_datetime(code))
            start = ((datetime.strptime(ld, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d") if ld
                     else (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d"))
            df = await bc.query_kline(code, "15", start=start)
            n = db.upsert_klines_15m(df)
            results[code] = f"ok({n})"
            db.log_fetch("15min", "query_kline", "baostock", "success",
                         int((time.monotonic() - t0) * 1000), param=f"{code}:{start}")
        except Exception as e:
            results[code] = f"failed: {e}"
            db.log_fetch("15min", "query_kline", "baostock", "failed",
                         int((time.monotonic() - t0) * 1000), error=str(e), param=code)
    log.info("15min 结果: %s", results)
    return {"updated": results, "total": len(codes)}


async def refresh_concepts(fetch_all: bool = False) -> dict:
    """概念板块列表（同花顺源）+ 成分股每日快照。

    同花顺列表无涨跌幅列，Top N 用"概念总结"页（含驱动事件，与新闻场景匹配）。
    成分为部分快照（每概念活跃 Top 10），详见 akshare_client.concept_cons。
    """
    t0 = time.monotonic()
    df = await akc.concept_list()
    n_list = db.upsert_concepts(df)
    log.info("concepts 列表 %d 个", n_list)

    # 排序依据：概念总结页最近出现的概念（带驱动事件）优先，其余按列表序
    priority: list[str] = []
    try:
        sm = await akc.concept_summary()
        priority = sm["概念名称"].tolist()[::-1]  # 最近的在前
    except Exception as e:
        log.warning("概念总结页拉取失败（不阻断）: %s", e)
    ordered = [n for n in priority if n in set(df["name"])] + \
              [n for n in df["name"] if n not in set(priority)]
    if not fetch_all:
        ordered = ordered[:TOP_CONCEPTS]

    today = datetime.now().strftime("%Y-%m-%d")
    code_map = dict(zip(df["name"], df["code"]))
    results: dict[str, str] = {}
    for name in ordered:
        t1 = time.monotonic()
        try:
            cons = await akc.concept_cons(code_map[name])
            n = db.upsert_concept_cons(code_map[name], name, cons["members"], today)
            results[name] = f"ok({n},partial)" if cons.get("partial") else f"ok({n})"
            db.log_fetch("concepts", "concept_cons", "ths", "success",
                         int((time.monotonic() - t1) * 1000), param=name)
        except Exception as e:
            results[name] = f"failed: {e}"
            db.log_fetch("concepts", "concept_cons", "ths", "failed",
                         int((time.monotonic() - t1) * 1000), error=str(e), param=name)
        await asyncio.sleep(0.3)  # 限速
    ok = sum(1 for v in results.values() if v.startswith("ok"))
    log.info("concepts 快照: %d/%d 成功", ok, len(results))
    return {"concepts": n_list, "ok": ok, "total": len(results), "detail": results}


# ---- 新闻舆情 ----------------------------------------------------------------------


async def refresh_news() -> dict:
    """新闻采集：三源市场快讯（财联社/东财全球/新浪）+ 自选股个股新闻。

    多源注册可扩展（news_client.MARKET_SOURCES），幂等落库。
    """
    from .data import news_client
    out: dict = {}
    t0 = time.monotonic()
    try:
        rows = await news_client.market_news()
        n = db.upsert_news(rows)
        by_src: dict[str, int] = {}
        for r in rows:
            by_src[r["source"]] = by_src.get(r["source"], 0) + 1
        out["market"] = f"ok({n}, {by_src})"
        db.log_fetch("news", "market_news_multi", "cls/em/sina", "success",
                     int((time.monotonic() - t0) * 1000), param="market")
    except Exception as e:
        out["market"] = f"failed: {e}"
        db.log_fetch("news", "market_news_multi", "cls/em/sina", "failed",
                     int((time.monotonic() - t0) * 1000), error=str(e), param="market")
    codes = [w["code"] for w in db.watchlist_all()]
    for code in codes:
        t1 = time.monotonic()
        try:
            rows = await news_client.stock_news(code)
            n = db.upsert_news(rows)
            out[code] = f"ok({n})"
            db.log_fetch("news", "stock_news_em", "eastmoney", "success",
                         int((time.monotonic() - t1) * 1000), param=code)
        except Exception as e:
            out[code] = f"failed: {e}"
            db.log_fetch("news", "stock_news_em", "eastmoney", "failed",
                         int((time.monotonic() - t1) * 1000), error=str(e), param=code)
        await asyncio.sleep(0.3)  # 限速
    log.info("news 结果: %s", out)
    return {"updated": out, "total": 1 + len(codes)}


# ---- 估值回补 ----------------------------------------------------------------------


async def refresh_valuation(codes: list[str]) -> dict:
    """全历史重拉日K，回填 pe/pb/ps 估值列（INSERT OR REPLACE 幂等）。

    与 refresh_daily 增量逻辑不同：本任务无视库内最后日期，从 HISTORY_START
    全量重拉，用于老数据补估值列（Baostock 日K自带 peTTM/pbMRQ/psTTM）。
    """
    results: dict[str, str] = {}
    for code in codes:
        t0 = time.monotonic()
        try:
            df = await bc.query_kline(code, "d", start=HISTORY_START)
            df = df[df["tradestatus"] == "1"]
            n = db.upsert_klines_daily(df)
            results[code] = f"ok({n})"
            db.log_fetch("valuation", "query_kline", "baostock", "success",
                         int((time.monotonic() - t0) * 1000), param=code)
        except Exception as e:
            results[code] = f"failed: {e}"
            db.log_fetch("valuation", "query_kline", "baostock", "failed",
                         int((time.monotonic() - t0) * 1000), error=str(e), param=code)
    log.info("valuation 结果: %s", results)
    return {"updated": results, "total": len(codes)}


# ---- 筹码快照 ----------------------------------------------------------------------


async def refresh_chips(codes: list[str]) -> dict:
    """对指定股票计算筹码快照落库（自选股每日积累获利盘趋势）。

    纯本地计算（quotes_daily 日K+换手率 → 三角分布模型），不发网络请求。
    """
    from .compute import chips as C
    results: dict[str, str] = {}
    for code in codes:
        try:
            bars = list(reversed(db.query_klines_daily(code, limit=210)))
            r = C.analyze(bars)
            if "error" in r:
                results[code] = f"skip({r['error']})"
                continue
            db.upsert_chip_daily([{
                "code": code, "trade_date": r["date"],
                "profit_ratio_pct": r["profit_ratio_pct"],
                "avg_cost": r["avg_cost"], "cost_90_low": r["cost_90_low"],
                "cost_90_high": r["cost_90_high"],
                "concentration_pct": r["concentration_pct"],
            }])
            results[code] = f"ok(profit={r['profit_ratio_pct']}%)"
        except Exception as e:
            results[code] = f"failed: {e}"
    log.info("chips 结果: %s", results)
    return {"updated": results, "total": len(codes)}


# ---- 全市场快照 ----------------------------------------------------------------------


def _active_stock_codes() -> list[str]:
    """在市普通股票代码（type=1, status=1）。"""
    con = db._conn()
    try:
        rows = con.execute(
            "SELECT code FROM stock_basic WHERE type=1 AND status=1 AND length(code)=6"
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        con.close()


async def refresh_snapshot() -> dict:
    """全市场当日截面：腾讯批量行情 ~60只/请求 → market_snapshot。

    依赖 stock_basic（weekly 刷新）；快照错过不可补，eod 每日执行。
    """
    from .data import tencent_client
    t0 = time.monotonic()
    codes = _active_stock_codes()
    if not codes:
        raise RuntimeError("stock_basic 为空，先运行 refresh basic")
    rows = await tencent_client.get_batch_quotes(codes)
    if not rows:
        raise RuntimeError("腾讯批量行情全部失败")
    today = datetime.now().strftime("%Y-%m-%d")
    n = db.upsert_market_snapshot(rows, today)
    db.log_fetch("snapshot", "qt.gtimg.cn batch", "tencent", "success",
                 int((time.monotonic() - t0) * 1000), param=f"{len(codes)}只/{n}行")
    log.info("snapshot %s 写入 %d 行（池 %d 只）", today, n, len(codes))
    return {"date": today, "rows": n, "universe": len(codes)}


# ---- 组合任务 --------------------------------------------------------------------


def _is_trading_day_now() -> bool:
    """周一~周五视为交易日（粗判；节假日由 Baostock 返回空数据自然跳过）。"""
    return datetime.now().weekday() < 5


async def refresh_ztpool(date: str | None = None) -> dict:
    """涨停池每日快照。date 格式 YYYYMMDD，默认今日。"""
    d = date or datetime.now().strftime("%Y%m%d")
    t0 = time.monotonic()
    try:
        df = await akc.zt_pool(d)
        n = db.upsert_zt_pool(df, f"{d[:4]}-{d[4:6]}-{d[6:]}")
        db.log_fetch("ztpool", "zt_pool", "akshare/em", "success",
                     int((time.monotonic() - t0) * 1000), param=d)
        log.info("zt_pool %s 写入 %d 条", d, n)
        return {"date": d, "rows": n}
    except Exception as e:
        db.log_fetch("ztpool", "zt_pool", "akshare/em", "failed",
                     int((time.monotonic() - t0) * 1000), error=str(e), param=d)
        raise


# ---- 推荐绩效评估 -------------------------------------------------------------------


async def _bars_for_review(code: str, start: str) -> list[dict]:
    """取推荐日起的日K：库优先 → Baostock 现拉 → 腾讯兜底。"""
    bars = db.query_klines_daily(code, limit=500)
    bars = [b for b in reversed(bars) if b["date"] >= start]
    if bars:
        return bars
    # 库里没有 → 现拉（从推荐日到今天）
    try:
        df = await bc.query_kline(code, "d", start=start)
        df = df[df["tradestatus"] == "1"]
        if not df.empty:
            db.upsert_klines_daily(df)
            return [{"date": r["date"], "close": float(r["close"])}
                    for _, r in df.iterrows()]
    except Exception:
        pass  # Baostock 抖动 → 走腾讯兜底
    # 腾讯兜底（含当日，量纲对齐后落库）
    from .data import tencent_client
    rows = await tencent_client.get_kline_daily(code, limit=120)
    if rows:
        df2 = pd.DataFrame([{
            "date": r["date"], "code": code,
            "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"],
            "volume": r["volume"] * 100, "amount": None, "turn": None, "pctChg": None,
        } for r in rows])
        db.upsert_klines_daily(df2, source="tencent")
        # 腾讯无涨跌幅列 → 用前收盘推算（首根缺前收为 None）
        out = []
        prev = None
        for r in rows:
            chg = (r["close"] / prev - 1) * 100 if prev else None
            out.append({**r, "change_pct": round(chg, 2) if chg is not None else None})
            prev = r["close"]
        return out
    return []


def _sealed_limit(bar: dict) -> bool:
    """当日涨停收盘（涨幅>=9.8 近似，20cm 板同样覆盖）。"""
    chg = bar.get("change_pct")
    return isinstance(chg, (int, float)) and chg >= 9.8


def _yizi(bar: dict) -> bool:
    """一字板（全天高低同价，无成交机会）。"""
    hi, lo = bar.get("high"), bar.get("low")
    return (hi is not None and lo is not None and hi == lo
            and float(hi) > 0)


def _effective_entry(bars: list[dict], rec_date: str,
                     ref_price: float | None) -> tuple[float | None, str | None, str | None]:
    """真实可成交入场价。

    - 推荐日未涨停 → 收盘价入场（原逻辑）
    - 推荐日涨停收盘（含一字）→ 顺延到下一个非一字日的开盘价入场
    - 连续一字无法入场 → (None, None, note) 放弃该单（不计入胜率）
    返回 (entry, fill_date, note)。
    """
    rec_bar = next((b for b in bars if b["date"] == rec_date), None)
    if not rec_bar:
        return ref_price, rec_date, None
    if not _sealed_limit(rec_bar):
        return ref_price, rec_date, None
    later = [b for b in bars if b["date"] > rec_date]
    consec = 0
    for b in later:
        if _sealed_limit(b) and _yizi(b):
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


async def review_recommendations() -> dict:
    """对所有 open 状态的推荐做绩效评估（eod 每日自动跑）。

    入场价采用真实可成交价：推荐日涨停收盘的单子顺延到下一可买日开盘（见
    _effective_entry）。对每条推荐：拉推荐日起日K → 从成交日起逐日写/更新
    review 行（累计收益、最大浮盈浮亏）→ 达到 target_horizon 时终评。
    """
    recs = db.recommendations_open()
    results: dict[str, str] = {}
    for rec in recs:
        rid, code = rec["id"], rec["code"]
        t0 = time.monotonic()
        try:
            bars = await _bars_for_review(code, rec["rec_date"])
            entry, fill_date, note = _effective_entry(bars, rec["rec_date"],
                                                      rec["entry_ref_price"])
            if entry is None and note is None:
                results[code] = "open(待下一交易日入场确认)"  # 涨停/一字后行情未出
            elif entry is None and note:
                # 无法入场：终评作废，不写 pnl 行（stats 只 join 有 review 行的）
                db.upsert_review(rid, datetime.now().strftime("%Y-%m-%d"), 0,
                                 rec["entry_ref_price"], 0.0, 0.0, 0.0, None, note)
                db.finalize_recommendation(rid, 0, note)
                results[code] = f"void({note})"
            elif entry and bars and fill_date:
                if note:
                    # 顺延入场（开盘价成交）→ 当日收盘即 T+1
                    closes = [(b["date"], float(b["close"])) for b in bars
                              if b["date"] >= fill_date]
                else:
                    # 推荐日收盘价入场 → 次一交易日起算 T+1
                    closes = [(b["date"], float(b["close"])) for b in bars
                              if b["date"] > fill_date]
                max_gain, max_dd = 0.0, 0.0
                finalized = False
                for i, (d, close) in enumerate(closes, start=1):
                    pnl = (close - entry) / entry * 100
                    max_gain = max(max_gain, pnl)
                    max_dd = min(max_dd, pnl)
                    days_held = i
                    hit: int | None = None
                    if days_held >= rec["target_horizon"]:
                        hit = 1 if pnl > 0 else 0
                    final_note = None
                    if hit is not None:
                        final_note = f"终评: {'赢' if pnl > 0 else '输'} {pnl:+.2f}%"
                    elif i == 1 and note:
                        final_note = note  # 首行记录入场说明
                    db.upsert_review(rid, d, days_held, close, round(pnl, 2),
                                     round(max_gain, 2), round(max_dd, 2), hit, final_note)
                    if hit is not None:
                        db.finalize_recommendation(rid, hit, "")
                        finalized = True
                        results[code] = f"final(T+{days_held} {pnl:+.2f}%)"
                        break
                if not finalized:
                    if closes:
                        results[code] = f"open(T+{len(closes)} 最新{closes[-1][1]:.2f} 入场{entry:.2f})"
                    else:
                        results[code] = "open(无后续行情)"
                db.log_fetch("review", "kline", "baostock/local", "success",
                             int((time.monotonic() - t0) * 1000), param=code)
            else:
                results[code] = "open(缺入场价或行情)"
        except Exception as e:
            results[code] = f"failed: {e}"
            db.log_fetch("review", "kline", "baostock/local", "failed",
                         int((time.monotonic() - t0) * 1000), error=str(e), param=code)
    log.info("review 结果: %s", results)
    return {"reviewed": results, "total": len(recs)}


# 核心指数（Baostock 代码 -> 名称）。独立存 index_daily（category=broad）
INDEXES = {
    "sh.000001": "上证指数",
    "sz.399001": "深证成指",
    "sz.399006": "创业板指",
    "sh.000300": "沪深300",
    "sh.000905": "中证500",
    "sh.000016": "上证50",
    "sh.000852": "中证1000",
    "sz.399303": "国证2000",
}

INDUSTRY_INDEX_START = "2024-01-01"  # 行业指数回补起点（轮动分析够用）


async def _fill_today_tencent_index(code_key: str) -> int:
    """当日兜底：index_daily 缺今日时用腾讯源补。"""
    from .data import tencent_client
    today = datetime.now().strftime("%Y-%m-%d")
    if datetime.now().weekday() >= 5:
        return 0
    last = db.get_last_index_date(code_key)
    if last and last >= today:
        return 0
    rows = await tencent_client.get_kline_daily(code_key, limit=5)
    if not rows:
        return 0
    name = INDEXES.get(code_key, code_key)
    return db.upsert_index_daily(
        code_key, name, "broad",
        [{"date": r["date"], "open": r["open"], "high": r["high"], "low": r["low"],
          "close": r["close"], "volume": r["volume"], "amount": None, "change_pct": None}
         for r in rows],
        source="tencent",
    )


async def refresh_index(start: str = "2015-01-01") -> dict:
    """大盘指数日K → index_daily（独立表，不再混入 quotes_daily）。增量续拉。"""
    results: dict[str, str] = {}
    for bs_code, name in INDEXES.items():
        t0 = time.monotonic()
        try:
            last = db.get_last_index_date(bs_code)
            st = db.next_trade_day(last) if last else start
            if st > datetime.now().strftime("%Y-%m-%d"):
                results[name] = "up-to-date"
                continue
            df = await bc.query_kline(bs_code, "d", start=st)
            df = df[df["tradestatus"] == "1"]
            n = db.upsert_index_daily(
                bs_code, name, "broad",
                [{"date": r["date"], "open": r["open"], "high": r["high"], "low": r["low"],
                  "close": r["close"], "volume": r["volume"], "amount": r["amount"],
                  "change_pct": r["pctChg"]}
                 for _, r in df.iterrows()],
            ) if not df.empty else 0
            if n == 0:
                n = await _fill_today_tencent_index(bs_code)
                results[name] = f"ok({n},tencent_fallback)"
            else:
                results[name] = f"ok({n})"
            db.log_fetch("index", "query_kline", "baostock", "success",
                         int((time.monotonic() - t0) * 1000), param=f"{bs_code}:{st}")
        except Exception as e:
            results[name] = f"failed: {e}"
            db.log_fetch("index", "query_kline", "baostock", "failed",
                         int((time.monotonic() - t0) * 1000), error=str(e), param=bs_code)
    log.info("index 结果: %s", results)
    return {"updated": results, "total": len(INDEXES)}


async def refresh_industry_index(start: str = INDUSTRY_INDEX_START) -> dict:
    """同花顺 90 个行业指数日K → index_daily(category=industry)。

    支持板块轮动分析（sector_rotation）。全量首拉约 3-5 分钟，之后增量。
    """
    results: dict[str, str] = {}
    try:
        df = await akc.industry_index_list()
        industries = list(zip(df["name"], df["code"]))
    except Exception as e:
        log.error("行业列表拉取失败: %s", e)
        return {"error": str(e)}
    today = datetime.now().strftime("%Y-%m-%d")
    for name, code in industries:
        t0 = time.monotonic()
        try:
            last = db.get_last_index_date(code)
            st = db.next_trade_day(last) if last else start
            if st > today:
                results[name] = "up-to-date"
                continue
            end = today.replace("-", "")
            k = await akc.industry_index_kline(
                name, st.replace("-", ""), end)
            rows = [{
                "date": str(r["日期"]), "open": r.get("开盘价"), "high": r.get("最高价"),
                "low": r.get("最低价"), "close": r.get("收盘价"),
                "volume": r.get("成交量"), "amount": r.get("成交额"), "change_pct": None,
            } for _, r in k.iterrows()] if not k.empty else []
            n = db.upsert_index_daily(code, name, "industry", rows, source="ths")
            results[name] = f"ok({n})"
            db.log_fetch("ind_index", "stock_board_industry_index_ths", "ths", "success",
                         int((time.monotonic() - t0) * 1000), param=name)
        except Exception as e:
            results[name] = f"failed: {e}"
            db.log_fetch("ind_index", "stock_board_industry_index_ths", "ths", "failed",
                         int((time.monotonic() - t0) * 1000), error=str(e), param=name)
        await asyncio.sleep(0.3)
    ok = sum(1 for v in results.values() if v.startswith("ok"))
    log.info("industry_index: %d/%d 成功", ok, len(results))
    return {"ok": ok, "total": len(results), "detail": results}


# ---- 龙虎榜 / 业绩预告 ----------------------------------------------------------------


async def refresh_lhb(date: str | None = None) -> dict:
    """当日龙虎榜明细落库。date: YYYYMMDD 默认今日。"""
    d = date or datetime.now().strftime("%Y%m%d")
    t0 = time.monotonic()
    try:
        df = await akc.lhb_detail(d, d)
        if "上榜日" in df and not df.empty:
            iso = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            df = df[df["上榜日"].astype(str) == iso]
        n = db.upsert_lhb(df, f"{d[:4]}-{d[4:6]}-{d[6:]}")
        db.log_fetch("lhb", "stock_lhb_detail_em", "akshare/em", "success",
                     int((time.monotonic() - t0) * 1000), param=d)
        log.info("lhb %s 写入 %d 条", d, n)
        return {"date": d, "rows": n}
    except Exception as e:
        db.log_fetch("lhb", "stock_lhb_detail_em", "akshare/em", "failed",
                     int((time.monotonic() - t0) * 1000), error=str(e), param=d)
        raise


def _forecast_periods() -> list[str]:
    """近两个报告期（YYYYMMDD）：季末日期。"""
    now = datetime.now()
    y, m = now.year, now.month
    quarters = [(y, 3), (y, 6), (y, 9), (y, 12), (y - 1, 12), (y - 1, 9)]
    qs = [f"{yy}{mm:02d}30" if mm in (3, 6, 9) else f"{yy}1231"
          for yy, mm in quarters]
    # 只取已过去的
    past = [q for q in qs if q <= now.strftime("%Y%m%d")]
    return past[:2]


async def refresh_forecast() -> dict:
    """近两个报告期的业绩预告落库（幂等覆盖）。"""
    results: dict[str, str] = {}
    for period in _forecast_periods():
        t0 = time.monotonic()
        try:
            df = await akc.earnings_forecast(period)
            n = db.upsert_earnings_forecast(df)
            results[period] = f"ok({n})"
            db.log_fetch("forecast", "stock_yjyg_em", "akshare/em", "success",
                         int((time.monotonic() - t0) * 1000), param=period)
        except Exception as e:
            results[period] = f"failed: {e}"
            db.log_fetch("forecast", "stock_yjyg_em", "akshare/em", "failed",
                         int((time.monotonic() - t0) * 1000), error=str(e), param=period)
    log.info("forecast 结果: %s", results)
    return {"updated": results}


async def _fill_today_tencent(code_key: str) -> int:
    """当日兜底：若库内缺今日日K（Baostock 未发布），用腾讯源补（收盘后即时可得）。

    返回补入行数。腾讯 volume 单位是手，转换为股以对齐 Baostock。
    """
    from .data import tencent_client
    today = datetime.now().strftime("%Y-%m-%d")
    if datetime.now().weekday() >= 5:
        return 0  # 周末
    last = db.get_last_trade_date(code_key)
    if last and last >= today:
        return 0
    rows = await tencent_client.get_kline_daily(code_key, limit=10)
    if not rows:
        return 0
    df = pd.DataFrame([{
        "date": r["date"], "code": code_key,
        "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"],
        "volume": r["volume"] * 100, "amount": None, "turn": None, "pctChg": None,
    } for r in rows])
    return db.upsert_klines_daily(df, source="tencent")


def migrate_index_from_quotes() -> dict:
    """一次性迁移：quotes_daily 中的 8 个大盘指数 → index_daily，迁移后删除旧行。"""
    con = db._conn()
    try:
        keys = list(INDEXES.keys())
        ph = ",".join("?" * len(keys))
        rows = con.execute(
            f"SELECT code, trade_date, open, high, low, close, volume, amount, "
            f"change_pct FROM quotes_daily WHERE code IN ({ph})", keys,
        ).fetchall()
        if not rows:
            return {"migrated": 0, "hint": "quotes_daily 中无指数数据（可能已迁移）"}
        import sqlite3 as _sq
        con.executemany(
            "INSERT OR REPLACE INTO index_daily (code, name, category, trade_date, "
            "open, high, low, close, volume, amount, change_pct, source, updated_at) "
            "VALUES (?, ?, 'broad', ?, ?, ?, ?, ?, ?, ?, ?, 'migrated', datetime('now'))",
            [(r[0], INDEXES[r[0]], *r[1:]) for r in rows],
        )
        con.execute(f"DELETE FROM quotes_daily WHERE code IN ({ph})", keys)
        con.commit()
        return {"migrated": len(rows), "deleted_from_quotes": len(rows)}
    finally:
        con.close()


# ---- 龙头战法数据供给 ----------------------------------------------------------------


async def refresh_zt_kline(days: int = 30) -> dict:
    """为当日涨停池股票增量补近 N 日K（龙头评分的 5日涨幅/量能输入）。

    涨停股多数不在 watchlist，此任务保证每天入池的涨停股有短历史可用。
    """
    from datetime import datetime as _dt
    today = _dt.now().strftime("%Y-%m-%d")
    dates = db.zt_pool_dates(1)
    if not dates or dates[0] != today:
        # 允许用最新快照日（手动补历史日期的龙扫描）
        if not dates:
            return {"skipped": "无涨停池数据"}
        today = dates[0]
    con = db._conn()
    try:
        codes = [r[0] for r in con.execute(
            "SELECT code FROM zt_pool WHERE trade_date=?", (today,)).fetchall()]
    finally:
        con.close()
    if not codes:
        return {"skipped": f"{today} 涨停池为空"}
    need = [c for c in codes
            if not db.get_last_trade_date(c) or db.get_last_trade_date(c) < today]
    if not need:
        return {"skipped": "全部已有K线"}
    out = await refresh_daily(need, days=days)
    return {"date": today, "pool": len(codes), "fetched": len(need),
            "detail": out.get("updated", {})}


async def refresh_eod() -> dict:
    """每日收盘后全套：日K + 15min + 概念快照（针对自选股）。

    补跑自查：若 fetch_log 中 daily 最后 success 早于今天且今天是交易日，
    说明上次错过了，本次照常执行（增量逻辑天然补齐日K；15min 只能尽力补近日）。
    """
    if not _is_trading_day_now():
        log.info("非交易日，跳过 eod")
        return {"skipped": "non-trading-day"}
    last = db.last_success_at("daily")
    log.info("上次 daily success: %s", last or "无")

    codes = [w["code"] for w in db.watchlist_all()]
    if not codes:
        log.warning("watchlist 为空，eod 只刷概念")
    out: dict = {}
    if codes:
        out["daily"] = await refresh_daily(codes, days=0, include_15min=True)
        try:
            out["chips"] = await refresh_chips(codes)
        except Exception as e:
            out["chips"] = {"error": str(e)}
    try:
        out["index"] = await refresh_index()
    except Exception as e:
        out["index"] = {"error": str(e)}
    try:
        out["industry_index"] = await refresh_industry_index()
    except Exception as e:
        out["industry_index"] = {"error": str(e)}
    try:
        out["lhb"] = await refresh_lhb()
    except Exception as e:
        out["lhb"] = {"error": str(e)}
    try:
        out["forecast"] = await refresh_forecast()
    except Exception as e:
        out["forecast"] = {"error": str(e)}
    try:
        out["concepts"] = await refresh_concepts()
    except Exception as e:
        out["concepts"] = {"error": str(e)}
    try:
        out["ztpool"] = await refresh_ztpool()
    except Exception as e:
        out["ztpool"] = {"error": str(e)}
    try:
        out["zt_kline"] = await refresh_zt_kline()
    except Exception as e:
        out["zt_kline"] = {"error": str(e)}
    try:
        from .recommend.dragon import scan_dragons as _dragon_scan
        out["dragon"] = await _dragon_scan(persist=True)
    except Exception as e:
        out["dragon"] = {"error": str(e)}
    try:
        out["review"] = await review_recommendations()
    except Exception as e:
        out["review"] = {"error": str(e)}
    try:
        out["news"] = await refresh_news()
    except Exception as e:
        out["news"] = {"error": str(e)}
    try:
        out["snapshot"] = await refresh_snapshot()
    except Exception as e:
        out["snapshot"] = {"error": str(e)}
    return out


async def refresh_weekly() -> dict:
    """每周任务：基础信息 + 行业分类。"""
    return {
        "basic": await refresh_basic(),
        "industry": await refresh_industry(),
    }


# ---- CLI --------------------------------------------------------------------------


TASKS = {
    "basic": lambda a: refresh_basic(),
    "industry": lambda a: refresh_industry(),
    "daily": lambda a: refresh_daily(_codes_from_args(a), a.days),
    "15min": lambda a: refresh_15min(_codes_from_args(a), a.days),
    "concepts": lambda a: refresh_concepts(a.all),
    "eod": lambda a: refresh_eod(),
    "weekly": lambda a: refresh_weekly(),
    "ztpool": lambda a: refresh_ztpool(a.date),
    "news": lambda a: refresh_news(),
    "valuation": lambda a: refresh_valuation(_codes_from_args(a)),
    "chips": lambda a: refresh_chips(_codes_from_args(a) or
                                     [w["code"] for w in db.watchlist_all()]),
    "snapshot": lambda a: refresh_snapshot(),
    "ind_index": lambda a: refresh_industry_index(),
    "lhb": lambda a: refresh_lhb(a.date),
    "zt_kline": lambda a: refresh_zt_kline(a.days),
    "forecast": lambda a: refresh_forecast(),
    "migrate_index": lambda a: migrate_index_from_quotes(),
    "index": lambda a: refresh_index(),
    "review": lambda a: review_recommendations(),
    "dbinfo": lambda a: db_info(),
}


def db_info() -> dict:
    """库状态：路径 + 各表行数 + 关键范围。随时可查，验证数据是否落地。"""
    import sqlite3
    tables = ["stock_basic", "stock_industry", "concepts", "stock_concepts",
              "quotes_daily", "quotes_15min", "index_daily", "watchlist", "fetch_log",
              "news", "financial_indicators", "zt_pool", "lhb_daily",
              "earnings_forecast", "market_snapshot", "chip_daily"]
    c = db._conn()
    info: dict = {"db_path": db.DB_PATH, "tables": {}}
    try:
        for t in tables:
            try:
                info["tables"][t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.OperationalError:
                info["tables"][t] = "table missing"
        try:
            info["quotes_daily_range"] = c.execute(
                "SELECT MIN(trade_date), MAX(trade_date) FROM quotes_daily"
            ).fetchone()
            info["watchlist_codes"] = [r[0] for r in c.execute("SELECT code FROM watchlist")]
            info["last_tasks"] = c.execute(
                "SELECT task, status, created_at FROM fetch_log ORDER BY id DESC LIMIT 5"
            ).fetchall()
        except sqlite3.OperationalError:
            pass
        return info
    finally:
        c.close()


def main() -> None:
    p = argparse.ArgumentParser(description="股票数据层定时刷新")
    p.add_argument("task", choices=list(TASKS))
    p.add_argument("--codes", help="逗号分隔的 6 位代码，如 600519,000001")
    p.add_argument("--watchlist", action="store_true", help="对自选股执行")
    p.add_argument("--days", type=int, default=60, help="首次回补天数（0=全量到 2015）")
    p.add_argument("--all", action="store_true", help="concepts: 全量刷（默认 Top 50）")
    p.add_argument("--date", help="ztpool: 指定日期 YYYYMMDD，默认今日")
    args = p.parse_args()

    db.init_db()
    try:
        result = TASKS[args.task](args)
        if asyncio.iscoroutine(result):
            result = asyncio.run(result)
        print(result)
    except Exception as e:
        log.error("task %s 失败: %s", args.task, e)
        sys.exit(1)


if __name__ == "__main__":
    main()
