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
                # Baostock 当日未发布/无数据 → 腾讯兜底（零历史拉全量160根）
                if db.get_last_trade_date(code) is None:
                    n = await _fill_history_tencent(code)
                    results[code] = f"ok({n},tencent_history)"
                else:
                    n = await _fill_today_tencent(code)
                    results[code] = f"ok({n},tencent_fallback)"
            else:
                results[code] = f"ok({n})"
            db.log_fetch("daily", "query_kline", "baostock", "success",
                         int((time.monotonic() - t0) * 1000), param=f"{code}:{start}")
        except Exception as e:
            # Baostock 异常（连接失败/断连）→ 腾讯兜底；零历史股票拉160根全量
            try:
                if db.get_last_trade_date(code) is None:
                    n = await _fill_history_tencent(code)
                    results[code] = (f"ok({n},tencent_history)" if n else f"failed: {e}")
                else:
                    n = await _fill_today_tencent(code)
                    results[code] = f"ok({n},tencent_fallback_after_error)" if n else f"failed: {e}"
            except Exception:
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


async def refresh_concepts(fetch_all: bool = True) -> dict:
    """概念板块成分股（东财 BK 体系，全量）→ concept_members 每日快照。

    v6.1 换源：同花顺无 cookie 只能拿 Top10 部分成分；东财板块成分接口
    （fs=b:BKxxxx）可拿全量，且板块代码与 sector_flow 同码，可直接 join。
    限速 0.35s/板块（东财对密集请求会断连），断点续跑：当日已有成分的概念跳过。
    """
    from .data.eastmoney import EastmoneyClient
    t0 = time.monotonic()
    today = datetime.now().strftime("%Y-%m-%d")
    cli = EastmoneyClient()
    results: dict[str, str] = {}
    try:
        boards = None
        for attempt in range(1, 6):     # 东财限流时退避重试（限流窗口通常几分钟）
            try:
                boards = await cli.sector_boards("concept")
                break
            except Exception as e:
                wait = 30 * attempt
                log.warning("板块清单失败(%s)，%ds 后重试 %d/5", type(e).__name__, wait, attempt)
                await asyncio.sleep(wait)
        if not boards:
            raise RuntimeError("东财板块清单拉取失败（限流未恢复），稍后重跑即可续传")
        log.info("东财概念板块清单: %d 个", len(boards))
        done: set[str] = set()
        if not fetch_all:
            boards = boards[:TOP_CONCEPTS]
        # 断点续跑：今日已落库的概念跳过（只补缺口）
        c = db._conn()
        try:
            done = {r[0] for r in c.execute(
                "SELECT DISTINCT concept_code FROM concept_members WHERE trade_date=?",
                (today,)).fetchall()}
        finally:
            c.close()
        consec_fail = 0
        for i, b in enumerate(boards, 1):
            code, name = b["code"], b["name"]
            if code in done:
                results[name] = "skip(已有快照)"
                continue
            t1 = time.monotonic()
            try:
                members = await cli.sector_members(code)
                n = db.upsert_concept_members(code, name, members, today)
                results[name] = f"ok({n})"
                consec_fail = 0
                db.log_fetch("concepts", "sector_members", "eastmoney", "success",
                             int((time.monotonic() - t1) * 1000), param=name)
            except Exception as e:
                results[name] = f"failed: {e}"
                consec_fail += 1
                db.log_fetch("concepts", "sector_members", "eastmoney", "failed",
                             int((time.monotonic() - t1) * 1000), error=str(e), param=name)
                if consec_fail >= 6:
                    log.warning("连续 %d 个板块失败（东财限流），提前结束本次（已落库部分保留，稍后重跑续传）",
                                consec_fail)
                    break
            if i % 20 == 0:
                log.info("concepts 进度 %d/%d（%.0fs）", i, len(boards),
                         time.monotonic() - t0)
            await asyncio.sleep(0.35)
    finally:
        await cli.aclose()
    ok = sum(1 for v in results.values() if v.startswith(("ok", "skip")))
    total_members = sum(int(v.split("(")[1].rstrip(")")) for v in results.values()
                        if v.startswith("ok(") and v.split("(")[1].rstrip(")").isdigit())
    log.info("concepts 快照: %d/%d 成功，成分 %d 行", ok, len(results), total_members)
    return {"boards": len(results), "ok": ok, "members": total_members,
            "elapsed_s": round(time.monotonic() - t0, 1)}


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


async def refresh_daily_market(concurrency: int = 4, retries: int = 2) -> dict:
    """全市场日K增量（eod 第 1 步）——quotes_daily 是唯一真相源，必须全量维护。

    对每个在市股票：库内最后交易日 < 目标交易日 → 腾讯取最近 N 根补齐
    （缺口≤5 日取 10 根，更大缺口取 60 根）；腾讯失败降级新浪（source=sina_raw），
    连续 12 失败熔断直走新浪（限流保护）。已最新的票直接跳过（幂等）。
    换手率/PE/PB/市值：当日行由 snapshot 步骤补齐（COALESCE 合并，不覆盖）。
    """
    from .data.tencent_client import get_daily_kline, get_daily_kline_sina
    import pandas as pd
    # ① 批量行情摄入（原 snapshot 步骤合并进来）：一次覆盖全市场当日
    #    OHLCV + PE/PB/总市值/流通市值/换手/量比（K线接口不提供这些字段）
    ingest: dict = {}
    try:
        ingest = await refresh_snapshot()
    except Exception as e:
        ingest = {"error": str(e)}
    codes = _active_stock_codes()
    today = datetime.now().strftime("%Y-%m-%d")
    last_idx = db.get_last_index_date("sh.000001")
    # 目标日：交易日取今天（收盘后），周末取最近交易日
    target = today if datetime.now().weekday() < 5 else (last_idx or today)
    c = db._conn()
    try:
        have = dict(c.execute(
            "SELECT code, MAX(trade_date) FROM quotes_daily GROUP BY code").fetchall())
    finally:
        c.close()
    todo = [x for x in codes if (have.get(x) or "") < target]
    if not todo:
        log.info("daily_market: 全市场已最新（目标 %s），缺口修复跳过", target)
        return {"target": target, "universe": len(codes), "ingest": ingest,
                "todo": 0, "ok": 0, "fail": 0}
    sem = asyncio.Semaphore(concurrency)
    state = {"tencent_fail": 0, "tencent_ok": True}
    ok = fail = 0
    by_source = {"tencent_qfq": 0, "sina_raw": 0}
    t_all = time.monotonic()

    async def one(code: str) -> str | None:
        async with sem:
            last = have.get(code) or ""
            gap_days = 60  # 未知/大缺口
            if last:
                try:
                    gap_days = (datetime.strptime(target, "%Y-%m-%d")
                                - datetime.strptime(last, "%Y-%m-%d")).days
                except ValueError:
                    gap_days = 60
            count = 10 if gap_days <= 5 else 60
            sources = [("tencent_qfq", get_daily_kline), ("sina_raw", get_daily_kline_sina)]
            if not state["tencent_ok"]:
                sources = sources[1:]
            for source, fn in sources:
                for attempt in range(1, retries + 1):
                    try:
                        bars = await fn(code, count)
                        if not bars:
                            break
                        n = db.upsert_klines_daily(pd.DataFrame([{
                            "code": code, "date": b["date"], "open": b["open"],
                            "high": b["high"], "low": b["low"], "close": b["close"],
                            "volume": b["volume"], "amount": b["amount"],
                            "pctChg": b["change_pct"],
                        } for b in bars]), source=source)
                        if source == "tencent_qfq":
                            state["tencent_fail"] = 0
                        return source if n else None
                    except Exception:
                        if source == "tencent_qfq":
                            state["tencent_fail"] += 1
                            if state["tencent_fail"] >= 12:
                                state["tencent_ok"] = False
                                log.warning("daily_market: 腾讯连续失败 %d 次，本次熔断直走新浪",
                                            state["tencent_fail"])
                        if attempt < retries:
                            await asyncio.sleep(0.5 * attempt)
            return None

    for i in range(0, len(todo), 200):
        chunk = todo[i:i + 200]
        res = await asyncio.gather(*(one(x) for x in chunk))
        for x in res:
            if x:
                ok += 1
                by_source[x] = by_source.get(x, 0) + 1
            else:
                fail += 1
        log.info("daily_market 进度 %d/%d（ok=%d %s fail=%d %.0fs）",
                 min(i + 200, len(todo)), len(todo), ok, by_source, fail,
                 time.monotonic() - t_all)
        await asyncio.sleep(0.1)
    out = {"target": target, "universe": len(codes), "ingest": ingest,
           "todo": len(todo), "ok": ok, "by_source": by_source,
           "fail": fail, "elapsed_s": round(time.monotonic() - t_all, 1)}
    db.log_fetch("daily", "market_increment", "tencent/sina", "success", 0,
                 param=f"{target}/{ok}ok/{fail}fail")
    log.info("daily_market 完成: %s", out)
    return out


async def refresh_history_fast(concurrency: int = 2, retries: int = 2) -> dict:
    """全市场日K快速回填（腾讯 qfq 主 + 新浪备用，幂等断点续跑）。

    腾讯 ~0.1s/只但会限流（501）；失败自动降级新浪（不复权，source=sina_raw）。
    历史行无换手率/PE/PB（最新交易日由 snapshot 摄入补齐）。腾讯恢复后
    重跑本任务会按缺口补齐（已有 ≥200 根的跳过；如需重拉 sina_raw 行，
    先 DELETE 对应行再跑）。
    """
    from .data.tencent_client import get_daily_kline, get_daily_kline_sina
    codes = _active_stock_codes()
    c = db._conn()
    try:
        have = dict(c.execute(
            "SELECT code, COUNT(*) FROM quotes_daily GROUP BY code").fetchall())
    finally:
        c.close()
    todo = [x for x in codes if have.get(x, 0) < 200]
    ok = fail = 0
    by_source = {"tencent_qfq": 0, "sina_raw": 0}
    t_all = time.monotonic()
    sem = asyncio.Semaphore(concurrency)
    state = {"tencent_fail": 0, "tencent_ok": True}

    async def one(code: str) -> str | None:
        async with sem:
            sources = [("tencent_qfq", get_daily_kline), ("sina_raw", get_daily_kline_sina)]
            if not state["tencent_ok"]:
                sources = sources[1:]   # 熔断：腾讯限流期间直接走新浪
            for source, fn in sources:
                for attempt in range(1, retries + 1):
                    try:
                        bars = await fn(code, 320)
                        if not bars:
                            break   # 该源无数据，换下一个
                        n = db.upsert_klines_daily(
                            __import__("pandas").DataFrame([{
                                "code": code, "date": b["date"], "open": b["open"],
                                "high": b["high"], "low": b["low"], "close": b["close"],
                                "volume": b["volume"], "amount": b["amount"],
                                "pctChg": b["change_pct"],
                            } for b in bars]), source=source)
                        if source == "tencent_qfq":
                            state["tencent_fail"] = 0
                        return source if n > 0 else None
                    except Exception:
                        if source == "tencent_qfq":
                            state["tencent_fail"] += 1
                            if state["tencent_fail"] >= 12:
                                state["tencent_ok"] = False
                                log.warning("腾讯连续失败 %d 次，本次运行熔断（直走新浪）",
                                            state["tencent_fail"])
                        if attempt < retries:
                            await asyncio.sleep(0.5 * attempt)
            return None

    for i in range(0, len(todo), 100):
        chunk = todo[i:i + 100]
        res = await asyncio.gather(*(one(c) for c in chunk))
        for x in res:
            if x:
                ok += 1
                by_source[x] = by_source.get(x, 0) + 1
            else:
                fail += 1
        if (i // 100 + 1) % 5 == 0 or i + 100 >= len(todo):
            log.info("history_fast 进度 %d/%d（ok=%d %s fail=%d %.0fs）",
                     min(i + 100, len(todo)), len(todo), ok, by_source, fail,
                     time.monotonic() - t_all)
        await asyncio.sleep(0.3)
    out = {"universe": len(codes), "todo": len(todo), "ok": ok,
           "by_source": by_source, "fail": fail,
           "elapsed_s": round(time.monotonic() - t_all, 1)}
    log.info("history_fast 完成: %s", out)
    return out


async def refresh_repull_sina() -> dict:
    """把新浪（不复权）回填的行重拉为腾讯前复权（口径统一）。

    腾讯限流窗口过去后运行：删 source='sina_raw' 的历史行 → history_fast 重拉
    （当日行由 snapshot 摄入，COALESCE 合并保留 PE/市值等字段）。
    """
    n = db.delete_quotes_by_source("sina_raw")
    log.info("repull_sina: 删除新浪行 %d，开始腾讯重拉", n)
    out = await refresh_history_fast()
    out["deleted_sina_rows"] = n
    return out


async def refresh_fin_all() -> dict:
    """全市场财务指标回填（新浪源，幂等可断点续跑）。

    financial_indicators 平时只有自选股；value_quality 的 ROE 条件依赖全量覆盖。
    已有一行以上的代码跳过（只补缺口），每只限速 0.3s。
    """
    from .data import fin_client
    codes = _active_stock_codes()
    c = db._conn()
    try:
        have = {r[0] for r in c.execute(
            "SELECT DISTINCT code FROM financial_indicators").fetchall()}
    finally:
        c.close()
    todo = [x for x in codes if x not in have]
    ok = fail = 0
    t_all = time.monotonic()
    sem = asyncio.Semaphore(6)

    async def one(code: str) -> bool:
        async with sem:
            t0 = time.monotonic()
            try:
                df = await fin_client.financial_indicators(code)
                db.upsert_financial_indicators(code, df)
                db.log_fetch("fin", "financial_indicators", "sina", "success",
                             int((time.monotonic() - t0) * 1000), param=code)
                return True
            except Exception as e:
                db.log_fetch("fin", "financial_indicators", "sina", "failed",
                             int((time.monotonic() - t0) * 1000), error=str(e), param=code)
                return False
    for i in range(0, len(todo), 100):
        chunk = todo[i:i + 100]
        res = await asyncio.gather(*(one(c) for c in chunk))
        ok += sum(1 for x in res if x)
        fail += sum(1 for x in res if not x)
        if (i // 100 + 1) % 5 == 0 or i + 100 >= len(todo):
            log.info("fin_all 进度 %d/%d（ok=%d fail=%d %.0fs）",
                     min(i + 100, len(todo)), len(todo), ok, fail,
                     time.monotonic() - t_all)
        await asyncio.sleep(0.3)
    out = {"universe": len(codes), "todo": len(todo), "ok": ok, "fail": fail,
           "elapsed_s": round(time.monotonic() - t_all, 1)}
    log.info("fin_all 完成: %s", out)
    return out


# ---- 板块资金流（行业 + 概念；主线判断的数据底座） ------------------------------------

async def refresh_sector_flow() -> dict:
    """板块资金流落库：行业 + 概念各一份，按 (trade_date, type, code) 幂等。

    源降级链：东财 push2（主力单分解全）→ 同花顺（全单口径，仅 main_net；
    行业体系与 sector_rotation 同源，行业名可直接对上）。fetch_log 的 source
    字段记录实际命中源，事后可审计口径。
    """
    from .data import akshare_client as akc
    from .data.eastmoney import EastmoneyClient
    date = datetime.now().strftime("%Y-%m-%d")
    cli = EastmoneyClient()
    out: dict = {"date": date}
    try:
        for kind in ("industry", "concept"):
            t0 = time.monotonic()
            rows: list[dict] | None = None
            source = "eastmoney"
            em_err: Exception | None = None
            try:
                rows = await cli.sector_flow_raw(kind)
            except Exception as e:
                em_err = e
            if not rows:
                try:
                    rows = await akc.sector_flow_raw(kind)
                    source = "ths(同花顺,全单口径)"
                except Exception as ths_err:
                    err = f"eastmoney: {em_err}; ths: {ths_err}" if em_err else f"ths: {ths_err}"
                    out[kind] = f"failed: {err}"
                    db.log_fetch(f"sector_{kind}", "clist/sector", "eastmoney", "failed",
                                 int((time.monotonic() - t0) * 1000), error=err)
                    continue
            try:
                n = db.upsert_sector_flow(rows, date, kind)
                out[kind] = n
                db.log_fetch(f"sector_{kind}", "clist/sector", source, "success",
                             int((time.monotonic() - t0) * 1000), param=f"{n}行")
            except Exception as e:
                out[kind] = f"failed: {e}"
                db.log_fetch(f"sector_{kind}", "clist/sector", source, "failed",
                             int((time.monotonic() - t0) * 1000), error=str(e))
    finally:
        await cli.aclose()
    log.info("sector_flow 结果: %s", out)
    return out


# ---- 全市场日K基础层（P0：策略 L2 出口的数据前提） ----------------------------------

BACKFILL_DAYS = 550          # 自然日 ≈ 370 交易日，覆盖策略最深 320 根需求


async def refresh_history_full(min_bars: int = 200, batch_pause: float = 0.05) -> dict:
    """全市场日K一次性回填（可断点续跑，幂等）。

    目标：每个在市普通股 ≥min_bars 根日K，使 L1.5/L2 对全市场有效
    （策略到 L2 出标的的架构前提）。已有足够根数的票自动跳过，
    中断后重跑只补缺口。窗口 BACKFILL_DAYS（非 2015 全历史，控制库容）。
    """
    codes = _active_stock_codes()
    c = db._conn()
    try:
        have = dict(c.execute(
            "SELECT code, COUNT(*) FROM quotes_daily GROUP BY code").fetchall())
    finally:
        c.close()
    todo = [x for x in codes if have.get(x, 0) < min_bars]
    start = (datetime.now() - timedelta(days=BACKFILL_DAYS)).strftime("%Y-%m-%d")
    ok = fail = 0
    t_all = time.monotonic()
    for i, code in enumerate(todo, 1):
        t0 = time.monotonic()
        try:
            df = await bc.query_kline(code, "d", start=start)
            df = df[df["tradestatus"] == "1"]
            n = db.upsert_klines_daily(df)
            ok += 1
            db.log_fetch("history", "query_kline", "baostock", "success",
                         int((time.monotonic() - t0) * 1000), param=code)
        except Exception as e:
            fail += 1
            db.log_fetch("history", "query_kline", "baostock", "failed",
                         int((time.monotonic() - t0) * 1000), error=str(e), param=code)
        if i % 100 == 0:
            log.info("history 回填进度 %d/%d（ok=%d fail=%d %.0fs）",
                     i, len(todo), ok, fail, time.monotonic() - t_all)
        await asyncio.sleep(batch_pause)
    out = {"universe": len(codes), "todo": len(todo), "ok": ok, "fail": fail,
           "elapsed_s": round(time.monotonic() - t_all, 1)}
    log.info("history 回填完成: %s", out)
    return out


def refresh_prune(fetchlog_days: int = 30, min15_days: int = 30) -> dict:
    """库瘦身（eod 末步）：删可再生的数据（写入口在 db.prune_tables）。

    证据类表（screen_runs/recommendations/reviews/zt_pool/lhb 等）永不删。
    """
    out = db.prune_tables(fetchlog_days, min15_days)
    db.log_fetch("prune", "internal", "sqlite", "success", 0, param=str(out))
    log.info("prune 清理: %s", out)
    return out


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
    """全市场当日日K：腾讯批量行情 ~60只/请求 → quotes_daily（唯一真相源）。

    market_snapshot 已退役（v3）：本任务既是最便宜的全市场日K增量，
    也是截面数据（量比/振幅/市值由 quotes_daily 派生查询计算）。
    依赖 stock_basic（weekly 刷新）；错过不可补，eod 每日执行。
    日期标签语义（point-in-time，ROADMAP M1）：开盘前（<09:25）或周末刷新时，
    行情实为最近一个已收盘交易日的数据，按该交易日打标签，
    避免"盘前快照被当成当日截面"。
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
    label = today
    now = datetime.now()
    if now.weekday() >= 5 or now.time() < datetime.strptime("09:25", "%H:%M").time():
        # 盘前/周末：数据实为最近已收盘交易日（以库内上证指数最后日期为准）
        last_closed = db.get_last_index_date("sh.000001")
        if last_closed and last_closed < today:
            label = last_closed
    n = db.upsert_snapshot_as_daily(rows, label)
    db.log_fetch("snapshot", "qt.gtimg.cn batch", "tencent", "success",
                 int((time.monotonic() - t0) * 1000), param=f"{len(codes)}只/{n}行")
    log.info("snapshot(=日K增量) 标签 %s（刷新于 %s）写入 %d 行（池 %d 只）",
             label, today, n, len(codes))
    return {"date": label, "refreshed_at": today, "rows": n, "universe": len(codes)}


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


def _latest_expected_trade_date() -> str | None:
    """库内已知最新交易日（以大盘指数快照为准，作为'数据是否过期'的基准）。"""
    d = db.get_last_index_date("sh.000001")
    return d or datetime.now().strftime("%Y-%m-%d")


async def _bars_for_review(code: str, start: str) -> list[dict]:
    """取推荐日起的日K：库优先（须覆盖到最新交易日，否则增量补拉）→ Baostock → 腾讯兜底。"""
    bars = db.query_klines_daily(code, limit=500)
    bars = [b for b in reversed(bars) if b["date"] >= start]
    last_in_db = bars[-1]["date"] if bars else None
    latest = _latest_expected_trade_date()
    if bars and last_in_db and latest and last_in_db >= latest:
        return bars
    # 库内缺失或过期 → 增量现拉（从库内最后日期+1 或推荐日起）
    try:
        pull_from = db.next_trade_day(last_in_db) if last_in_db else start
        df = await bc.query_kline(code, "d", start=pull_from)
        df = df[df["tradestatus"] == "1"]
        if not df.empty:
            db.upsert_klines_daily(df)
            return [b for b in reversed(db.query_klines_daily(code, limit=500))
                    if b["date"] >= start]
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


# 真实入场价数学已下沉 compute/entry.py（分层修正：公式归计算层）
from .compute.entry import (  # noqa: F401  (兼容既有调用/测试)
    effective_entry as _effective_entry,
    fill_change_pct as _fill_change_pct,
    sealed_limit as _sealed_limit,
    yizi as _yizi,
)


async def review_recommendations() -> dict:
    """对所有 open 状态的推荐做绩效评估（eod 每日自动跑）。

    入场价采用真实可成交价：推荐日涨停收盘的单子顺延到下一可买日开盘（见
    _effective_entry）。对每条推荐：拉推荐日起日K → 从成交日起逐日写/更新
    review 行（累计收益、最大浮盈浮亏）→ 达到 target_horizon 时终评。
    """
    recs = db.recommendations_open()
    bench = db.benchmark_closes("sh.000001")  # 后验 α：同期上证涨跌
    results: dict[str, str] = {}
    for rec in recs:
        rid, code = rec["id"], rec["code"]
        t0 = time.monotonic()

        def _alpha(eval_date: str, pnl: float) -> float | None:
            b0, b1 = bench.get(rec["rec_date"]), bench.get(eval_date)
            if b0 and b1 and b1 != b0:
                return round(pnl - (b1 / b0 - 1) * 100, 2)
            return None
        try:
            bars = _fill_change_pct(await _bars_for_review(code, rec["rec_date"]),
                                    rec["entry_ref_price"])
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
                                     round(max_gain, 2), round(max_dd, 2), hit, final_note,
                                     alpha_pct=_alpha(d, round(pnl, 2)))
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
    """当日指数兜底链：腾讯日K → 腾讯实时行情（构造当日bar）→ 放弃。

    Baostock 当日数据 17:30 后才发布；eod 16:10 跑时只能靠这里兜底。
    实时行情接口（qt.gtimg.cn，批量）稳定可用，含 open/high/low/price/成交额。
    """
    from .data import tencent_client
    today = datetime.now().strftime("%Y-%m-%d")
    if datetime.now().weekday() >= 5:
        return 0
    last = db.get_last_index_date(code_key)
    if last and last >= today:
        return 0
    name = INDEXES.get(code_key, code_key)
    # ① 腾讯日K接口（可能 501 限流）
    try:
        rows = await tencent_client.get_kline_daily(code_key, limit=5)
    except Exception:
        rows = []
    if rows:
        return db.upsert_index_daily(
            code_key, name, "broad",
            [{"date": r["date"], "open": r["open"], "high": r["high"], "low": r["low"],
              "close": r["close"], "volume": r["volume"], "amount": None,
              "change_pct": None} for r in rows],
            source="tencent",
        )
    # ② 腾讯实时行情 → 当日 bar（收盘后即为收盘数据）
    try:
        quotes = await tencent_client.get_batch_quotes([code_key])
    except Exception:
        return 0
    if not quotes:
        return 0
    q = quotes[0]
    if not q.get("price"):
        return 0
    return db.upsert_index_daily(
        code_key, name, "broad",
        [{"date": today, "open": q.get("open"), "high": q.get("high"),
          "low": q.get("low"), "close": q.get("price"),
          "volume": q.get("volume"), "amount": q.get("amount"),
          "change_pct": q.get("change_pct")}],
        source="tencent_quote",
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
            # Baostock 异常 → 腾讯兜底当日（降级链不在异常处断掉）
            try:
                n = await _fill_today_tencent_index(bs_code)
                results[name] = f"ok({n},tencent_fallback_after_error)" if n else f"failed: {e}"
            except Exception:
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
    """一次性迁移（SQL 实现在 db.py 唯一写库入口）。"""
    return db.migrate_index_from_quotes(dict(INDEXES))


# ---- 龙头战法数据供给 ----------------------------------------------------------------


async def refresh_zt_kline(days: int = 30) -> dict:
    """为当日涨停池股票增量补近 N 日K（龙头评分的 5日涨幅/量能输入）。

    涨停股多数不在 watchlist，此任务保证每天入池的涨停股有短历史可用。
    """
    from datetime import datetime as _dt
    today = _dt.now().strftime("%Y-%m-%d")
    dates = db.zt_pool_dates()[:1]
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
    """每日收盘后 eod（纯数据，7 步）：行情 + 情绪 + 指数 + 榜单原料。

    顺序：daily_market(批量行情摄入+缺口修复) → ztpool → index → ind_index
          → lhb → concepts → prune
    分析与后验不在此处（morning 任务：推荐后验 + 15 策略 L2 扫描），
    变化慢的数据由 weekly 维护（基础/行业/预告/财务/新闻/板块资金流）。
    """
    if not _is_trading_day_now():
        log.info("非交易日，跳过 eod")
        return {"skipped": "non-trading-day"}
    log.info("上次 daily success: %s", db.last_success_at("daily") or "无")
    out: dict = {}

    async def _step(name: str, coro_factory):
        try:
            out[name] = await coro_factory()
        except Exception as e:
            out[name] = {"error": str(e)}

    # ① 行情（唯一真相源）：批量行情摄入(PE/市值/换手/量比) + 全市场日K缺口修复
    await _step("daily_market", refresh_daily_market)
    # ② 情绪原料（涨停池）：情绪 v2 定性 / 龙头 / 主线分布
    await _step("ztpool", refresh_ztpool)
    # ③ 大盘指数（时间基准 + 后验 α + 大盘环境）
    await _step("index", refresh_index)
    # ④ 行业指数（板块轮动 / 复盘主线）
    await _step("ind_index", refresh_industry_index)
    # ⑤ 龙虎榜（层4 资金验证）
    await _step("lhb", refresh_lhb)
    # ⑥ 概念成分（东财封禁期间失败不阻断）
    await _step("concepts", refresh_concepts)
    # ⑦ 板块资金流（行业+概念：聚合层的"资金流排名"维度依赖它，须每日）
    await _step("sector", refresh_sector_flow)

    # ⑧ 库瘦身（旧日志/旧分时）
    out["prune"] = refresh_prune()
    return out


async def refresh_weekly() -> dict:
    """每周任务：变化慢的数据（股票池/行业/业绩预告/自选池财务）。

    这些数据对 15 策略 L1→L2 无实时依赖，周更即可；失败单项不阻断其他项。
    news 不在此维护（get_news 按需现拉）；sector 已移入 eod（每日数据）。
    """
    out: dict = {}
    for name, fn in (("basic", refresh_basic), ("industry", refresh_industry),
                     ("forecast", refresh_forecast), ("fin", lambda: refresh_fin())):
        try:
            out[name] = await fn()
        except Exception as e:
            out[name] = {"error": str(e)}
    return out


async def refresh_fin(codes: list[str] | None = None) -> dict:
    """自选池（或指定股票）财务指标批量落库（新浪源，按报告期幂等）。

    填补 financial_indicators 仅按需拉取的缺口——质量因子(roe/毛利率)
    依赖此表做横截面。
    """
    from .data import fin_client
    if codes is None:
        codes = [w["code"] for w in db.watchlist_all()]
    results: dict[str, str] = {}
    for code in codes:
        t0 = time.monotonic()
        try:
            df = await fin_client.financial_indicators(code)
            n = db.upsert_financial_indicators(code, df)
            results[code] = f"ok({n})"
            db.log_fetch("fin", "financial_indicators", "sina", "success",
                         int((time.monotonic() - t0) * 1000), param=code)
        except Exception as e:
            results[code] = f"failed: {e}"
            db.log_fetch("fin", "financial_indicators", "sina", "failed",
                         int((time.monotonic() - t0) * 1000), error=str(e), param=code)
        await asyncio.sleep(0.3)
    log.info("fin 结果: %s", results)
    return {"updated": results, "total": len(codes)}


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
    "sector": lambda a: refresh_sector_flow(),
    "valuation": lambda a: refresh_valuation(_codes_from_args(a)),
    "history": lambda a: refresh_history_full(),
    "history_fast": lambda a: refresh_history_fast(),
    "daily_market": lambda a: refresh_daily_market(),
    "prune": lambda a: refresh_prune(),
    "snapshot": lambda a: refresh_snapshot(),
    "ind_index": lambda a: refresh_industry_index(),
    "lhb": lambda a: refresh_lhb(a.date),
    "zt_kline": lambda a: refresh_zt_kline(a.days),
    "forecast": lambda a: refresh_forecast(),
    "fin": lambda a: refresh_fin(_codes_from_args(a) or None),
    "fin_all": lambda a: refresh_fin_all(),
    "repull_sina": lambda a: refresh_repull_sina(),
    "audit": lambda a: __import__("mcp_eastmoney.audit", fromlist=["audit"]).audit(),
    "migrate_index": lambda a: migrate_index_from_quotes(),
    "index": lambda a: refresh_index(),
    "review": lambda a: review_recommendations(),
    "scan_all": lambda a: __import__("mcp_eastmoney.recommend.scan_all",
                                     fromlist=["scan_all"]).scan_all(),
    "dbinfo": lambda a: db_info(),
}


def db_info() -> dict:
    """库状态：路径 + 各表行数 + 关键范围。随时可查，验证数据是否落地。"""
    import sqlite3
    tables = ["stock_basic", "stock_industry", "concept_members",
              "quotes_daily", "quotes_15min", "index_daily", "watchlist", "fetch_log",
              "news", "financial_indicators", "zt_pool", "lhb_daily",
              "earnings_forecast", "strategies", "screen_runs",
              "recommendations", "recommendation_reviews"]
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



async def _fill_history_tencent(code: str, limit: int = 160) -> int:
    """零历史股票的腾讯全量兜底（Baostock 故障时的最后防线）。

    160 根 ≈ 8个月，覆盖 trend_score(≥26) / price_pos_60(≥65) / mom60(≥61)。
    """
    from .data import tencent_client
    rows = await tencent_client.get_kline_daily(code, limit=limit)
    if not rows:
        return 0
    df = pd.DataFrame([{
        "date": r["date"], "code": code,
        "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"],
        "volume": r["volume"] * 100, "amount": None, "turn": None, "pctChg": None,
    } for r in rows])
    return db.upsert_klines_daily(df, source="tencent")
