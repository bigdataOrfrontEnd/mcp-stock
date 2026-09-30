"""mcp-eastmoney MCP server — exposes Eastmoney A-share data as MCP tools.

Compatible with both mcp v1 (FastMCP) and v2 (MCPServer, the renamed FastMCP).
"""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any, Literal

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP  # type: ignore[no-redef]

from .data import db
from .data.codes import normalize
from .data.eastmoney import EastmoneyClient

app: FastMCP = FastMCP("mcp-eastmoney")

_client: EastmoneyClient | None = None


def _c() -> EastmoneyClient:
    global _client
    if _client is None:
        _client = EastmoneyClient()
    return _client


def _d(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


# ---- Tools -------------------------------------------------------------------


@app.tool(
    name="get_stock_quote",
    description=(
        "获取A股个股实时行情（价格、涨跌幅、成交量、换手率、市盈率等）。"
        "Get real-time quote for an A-share stock — price, change %, volume, "
        "turnover rate, P/E. 数据来源东方财富，延迟约15分钟。"
    ),
)
async def get_stock_quote(code: str) -> str:
    """Get a real-time quote for an A-share stock or index (Tencent source)."""
    try:
        c, _ = _resolve_code(code)
    except ValueError as e:
        return _d({"error": str(e)})
    from .data import tencent_client
    try:
        return _d(await tencent_client.get_quote(c))
    except Exception as e:
        # 兜底：实时拿不到 → 本地库最新日K（标 stale）
        rows = db.query_klines_daily(c, limit=1)
        if rows:
            return _d({"code": c, "stale": True, "source": "local_daily",
                       "close": rows[0]["close"], "date": rows[0]["date"],
                       "error": f"实时源失败改用库内日K: {e}"})
        return _d({"error": str(e)})


@app.tool(
    name="search_stock",
    description=(
        "按名称、代码或拼音搜索A股股票。Search A-share stocks by name / code / "
        "pinyin (e.g. '宁德', 'NDSD', '300750')."
    ),
)
async def search_stock(keyword: str, limit: int = 10) -> str:
    """Search A-share stocks by name / code / pinyin."""
    return _d(await _c().search(keyword, limit))


@app.tool(
    name="main_fund_rank",
    description=(
        "主力资金净流入排行榜。Top stocks ranked by main capital net inflow. "
        "可按市场过滤：all / sh(沪市) / sz(深市) / cyb(创业板) / kcb(科创板)。"
    ),
)
async def main_fund_rank(
    limit: int = 20, market: Literal["all", "sh", "sz", "cyb", "kcb"] = "all"
) -> str:
    """Rank stocks by main capital net inflow, optionally filtered by market."""
    return _d(await _c().main_fund_rank(limit, market))


@app.tool(
    name="sector_fund_flow",
    description=(
        "板块资金流向排行（行业板块或概念板块）。Sector fund flow ranking. "
        "kind: industry(行业) / concept(概念)。返回涨跌幅、主力净流入、领涨股。"
    ),
)
async def sector_fund_flow(
    kind: Literal["industry", "concept"] = "industry", limit: int = 20
) -> str:
    """Rank sectors by main capital net inflow."""
    return _d(await _c().sector_fund_flow(kind, limit))


@app.tool(
    name="get_kline",
    description=(
        "获取个股K线数据（日/周/月/分钟级）。Historical K-line data — daily, "
        "weekly, monthly, or intraday (5/15/30/60 min)。本地库优先（已落库的日K/15min直接回库），"
        "未命中走实时接口。用于趋势分析、回测、技术指标计算。"
    ),
)
async def get_kline(
    code: str,
    period: Literal["daily", "weekly", "monthly", "5min", "15min", "30min", "60min"] = "daily",
    limit: int = 30,
) -> str:
    """Get historical K-line data for a stock or index. Local SQLite first, live API fallback."""
    try:
        c, is_index = _resolve_code(code)
    except ValueError:
        return _d({"error": f"非法股票代码: {code}"})
    # 指数（大盘 sh.000001 式 / 行业 88 开头）：index_daily 库优先
    if is_index and period == "daily":
        rows = db.query_index_daily(c, limit)
        if rows:
            return _d({"code": c, "period": "daily", "source": "local_index",
                       "klines": list(reversed(rows))})
        return _d({"error": f"指数 {code} 无数据，先运行 refresh index / ind_index"})
    # 库优先：日K 和 15min 有本地数据直接回库（含 source 标记）
    if period == "daily":
        rows = db.query_klines_daily(c, limit)
        if rows:
            return _d({"code": c, "period": "daily", "source": "local", "klines": list(reversed(rows))})
    elif period == "15min":
        rows = db.query_klines_15m(c, limit)
        if rows:
            return _d({"code": c, "period": "15min", "source": "local", "klines": list(reversed(rows))})
    return _d(await _c().get_kline(c, period, limit))


# ---- 数据层工具（本地库） -------------------------------------------------------


@app.tool(
    name="refresh_daily",
    description=(
        "拉取股票日K线（可选同时拉15分钟线）写入本地SQLite库。fetch daily K-lines "
        "(optionally 15-min bars) into local SQLite. codes: 6位代码列表。增量拉取，幂等可重复执行。"
    ),
)
async def refresh_daily(codes: list[str], include_15min: bool = False) -> str:
    """Fetch daily K-lines (and optionally 15-min bars) into local SQLite."""
    from .refresh import refresh_daily as _refresh
    try:
        norm = [normalize(c) for c in codes]
    except ValueError as e:
        return _d({"error": str(e)})
    return _d(await _refresh(norm, days=60, include_15min=include_15min))


@app.tool(
    name="get_stock_concepts",
    description=(
        "查询个股所属板块和概念（本地库最新快照优先，未覆盖时自动走东财F10完整数据）。"
        "Which concepts does a stock belong to. 返回行业链、地域、概念标签，"
        "用于'新闻→概念→成分股'关联分析。"
    ),
)
async def get_stock_concepts(code: str) -> str:
    """Get boards & concepts a stock belongs to (local snapshot first, F10 fallback)."""
    try:
        c = normalize(code)
    except ValueError as e:
        return _d({"error": str(e)})
    rows = db.query_stock_concepts(c)
    out: dict = {"code": c}
    if rows:
        out["concepts"] = rows
        out["source"] = "local_snapshot"
    # 本地快照为部分覆盖，始终补充 F10 完整板块数据
    try:
        from .data import akshare_client as akc
        f10 = await akc.core_conception(c)
        out["industry"] = f10["industry"]
        out["region"] = f10["region"]
        if not rows:
            out["concepts"] = f10["concepts"]
            out["source"] = "eastmoney_f10"
        else:
            f10_set = set(f10["concepts"])
            out["concepts_extra_from_ths"] = [
                x["concept_name"] for x in rows if x["concept_name"] not in f10_set
            ]
    except Exception as e:
        if not rows:
            return _d({"code": c, "concepts": [],
                       "hint": "本地无快照且 F10 拉取失败", "error": str(e)})
    return _d(out)


@app.tool(
    name="get_concept_members",
    description=(
        "查询概念板块成分股（本地库最新快照）。List member stocks of a concept. "
        "concept_name 如 'AI制药'、'低空经济'。可对比近 N 天成分变化（diff_days 参数）。"
    ),
)
async def get_concept_members(concept_name: str, diff_days: int = 0) -> str:
    """Get member stocks of a concept (latest snapshot), optionally diff vs N days ago."""
    rows = db.query_concept_members(concept_name)
    if not rows:
        return _d({"concept": concept_name, "members": [],
                   "hint": "本地库无该概念快照，先运行 refresh concepts"})
    out: dict = {"concept": concept_name, "count": len(rows), "members": rows}
    if diff_days > 0:
        out["diff"] = db.concept_member_diff(concept_name, diff_days)
    return _d(out)


@app.tool(
    name="get_stock_industry",
    description="查询个股行业分类（证监会分类，本地库）。Get industry classification of a stock."
)
async def get_stock_industry(code: str) -> str:
    """Get industry classification (CSRC) of a stock from local DB."""
    try:
        c = normalize(code)
    except ValueError as e:
        return _d({"error": str(e)})
    row = db.query_stock_industry(c)
    if not row:
        return _d({"code": c, "hint": "本地库无行业数据，先运行 refresh industry"})
    return _d(row)


@app.tool(
    name="get_zt_pool",
    description=(
        "查询涨停池（每日快照）。Get the limit-up pool for a date. date 格式 YYYYMMDD"
        "（默认今日）。min_lian_ban 过滤最低连板数。本地库优先，未命中现拉并落库。"
        "不传 date 可先用 dates=true 查已有哪些日期的快照。"
    ),
)
async def get_zt_pool(
    date: str = "", min_lian_ban: int = 1, dates: bool = False
) -> str:
    """Get limit-up pool snapshot for a date (local DB first, live fetch + persist fallback)."""
    if dates:
        return _d({"available_dates": db.zt_pool_dates()})
    from datetime import datetime as _dt
    d = date or _dt.now().strftime("%Y%m%d")
    if not (len(d) == 8 and d.isdigit()):
        return _d({"error": f"date 应为 YYYYMMDD，收到: {date!r}"})
    iso = f"{d[:4]}-{d[4:6]}-{d[6:]}"
    rows = db.query_zt_pool(iso, min_lian_ban)
    source = "local"
    if not rows:
        # 现拉并落库
        try:
            from .data import akshare_client as akc
            df = await akc.zt_pool(d)
            db.upsert_zt_pool(df, iso)
            rows = db.query_zt_pool(iso, min_lian_ban)
            source = f"live_fetched({len(rows)} persisted)"
        except Exception as e:
            return _d({"error": f"本地无快照且现拉失败: {e}", "date": iso})
    # 统计摘要
    lian = [r["lian_ban"] or 1 for r in rows]
    summary = {
        "total": len(rows),
        "max_lian_ban": max(lian) if lian else 0,
        "lianban_2plus": sum(1 for x in lian if x >= 2),
        "blasted": sum(1 for r in rows if (r["blast_count"] or 0) > 0),
    }
    return _d({"date": iso, "source": source, "summary": summary, "stocks": rows})


@app.tool(
    name="save_recommendation",
    description=(
        "保存一条AI股票推荐到本地库（推荐后必须调用，用于后续绩效验证与复盘进化）。"
        "Save an AI stock recommendation. code: 6位代码; thesis: 推荐逻辑; "
        "concepts: 概念标签逗号分隔; style: 打板/低吸/趋势/事件驱动; "
        "target_horizon: 预期持有天数(默认5); confidence: 置信度1-5; "
        "skill_version: 方法论版本(默认v1)。入场价自动取推荐日收盘价。"
    ),
)
async def save_recommendation(
    code: str, thesis: str, concepts: str = "", style: str = "事件驱动",
    target_horizon: int = 5, confidence: int = 3, skill_version: str = "v1",
) -> str:
    """Persist an AI recommendation with full context for later review."""
    try:
        c = normalize(code)
    except ValueError as e:
        return _d({"error": str(e)})
    # 入场参考价：推荐日（今日）收盘，库优先，缺则现拉
    entry = None
    name = None
    today_rows = db.query_klines_daily(c, limit=1)
    if today_rows:
        entry = today_rows[0]["close"]
    else:
        from .data.baostock_client import query_kline as bs_kline
        from datetime import datetime as _dt
        try:
            df = await bs_kline(c, "d", start=(_dt.now() - timedelta(days=10)).strftime("%Y-%m-%d"))
            df = df[df["tradestatus"] == "1"]
            if not df.empty:
                db.upsert_klines_daily(df)
                entry = float(df.iloc[-1]["close"])
        except Exception:
            pass
    con = db._conn()
    try:
        row = con.execute("SELECT name FROM stock_basic WHERE code=?", (c,)).fetchone()
        name = row[0] if row else None
    finally:
        con.close()
    rid = db.insert_recommendation(
        c, name, thesis, concepts, style, entry, target_horizon,
        max(1, min(5, confidence)), skill_version,
    )
    return _d({"ok": True, "rec_id": rid, "code": c, "name": name,
               "entry_ref_price": entry, "skill_version": skill_version,
               "note": "eod 任务将每日自动评估绩效（T+1起，到期终评）"})


@app.tool(
    name="get_recommendation_stats",
    description=(
        "查询AI推荐记录与绩效归因。Recommendation track record. "
        "action: history(最近推荐+最新绩效) / stats(归因统计) / review(立即触发一次绩效评估)。"
        "stats 时 group_by: style/concept/confidence/skill_version，days: 统计窗口天数。"
    ),
)
async def get_recommendation_stats(
    action: Literal["history", "stats", "review"],
    group_by: Literal["style", "concept", "confidence", "skill_version"] = "style",
    days: int = 30, limit: int = 20,
) -> str:
    """Get recommendation history / attribution stats / trigger review."""
    if action == "history":
        return _d({"history": db.recommendation_history(limit)})
    if action == "stats":
        return _d({"group_by": group_by, "days": days,
                   "stats": db.recommendation_stats(group_by, days)})
    from .refresh import review_recommendations
    return _d(await review_recommendations())


@app.tool(
    name="manage_watchlist",
    description=(
        "自选股管理。Manage watchlist. action: list(列出) / add(添加) / remove(删除)。"
        "定时任务 eod 每日对自选股落库日K和15分钟线。"
    ),
)
async def manage_watchlist(
    action: Literal["list", "add", "remove"], code: str = "", note: str = ""
) -> str:
    """Manage the watchlist (list / add / remove)."""
    if action == "list":
        return _d({"watchlist": db.watchlist_all()})
    try:
        c = normalize(code)
    except ValueError as e:
        return _d({"error": str(e)})
    if action == "add":
        db.watchlist_add(c, note or None)
        return _d({"ok": True, "code": c})
    db.watchlist_remove(c)
    return _d({"ok": True, "removed": c})


# ---- 计算层工具（技术面/缠论，纯现算不落表） -----------------------------------------


async def _get_bars(code: str, limit: int = 250) -> tuple[list[dict], str]:
    """取升序日K：库优先，不足则 Baostock 现拉落库。返回 (bars, source)。

    指数（'sh.000001' 式大盘码或 88 开头行业码）路由 index_daily，不再混入个股表。
    """
    if "." in code or code.startswith("88"):
        rows = db.query_index_daily(code, limit=limit)
        return list(reversed(rows)), ("local_index" if rows else "local_empty")
    rows = db.query_klines_daily(code, limit=limit)
    bars = list(reversed(rows))  # db 返回倒序 → 转升序
    if len(bars) >= min(limit, 120):
        return bars, "local"
    from datetime import datetime as _dt
    from .data.baostock_client import query_kline as _qk
    start = (_dt.now() - timedelta(days=int(limit * 1.6))).strftime("%Y-%m-%d")
    df = await _qk(code, "d", start=start)
    df = df[df["tradestatus"] == "1"]
    if not df.empty:
        db.upsert_klines_daily(df)
        bars = list(reversed(db.query_klines_daily(code, limit=limit)))
        return bars, "baostock_fetched"
    return bars, "local_insufficient"


def _resolve_code(code: str) -> tuple[str, bool]:
    """返回 (存储键, 是否指数)。

    指数：'sh.000001' 式大盘码（index_daily category=broad）或
    '88' 开头的同花顺行业板块码（category=industry）。
    """
    c = str(code).strip().lower()
    if "." in c:
        return c, True
    if len(c) == 6 and c.startswith("88"):
        return c, True
    return normalize(c), False


def _local_bars_get(code: str) -> list[dict] | None:
    """筛选计算层段用的本地K线注入（同步、只读本地库，不触发网络）。"""
    rows = db.query_klines_daily(code, limit=250)
    bars = list(reversed(rows))
    return bars if len(bars) >= 30 else None


@app.tool(
    name="get_indicators",
    description=(
        "技术指标分析：MA均线排列、MACD(金叉/死叉)、KDJ(超买超卖)、RSI、BOLL位置、ATR。"
        "Technical indicator analysis. 基于日K现算（库优先，不足自动拉取）。"
    ),
)
async def get_indicators(code: str, limit: int = 120) -> str:
    """Compute technical indicators for a stock/index (on-the-fly, not persisted)."""
    try:
        c, _ = _resolve_code(code)
    except ValueError as e:
        return _d({"error": str(e)})
    from .compute import indicators
    bars, source = await _get_bars(c, max(limit, 120))
    result = indicators.analyze(bars)
    result["code"] = c
    result["source"] = source
    return _d(result)


@app.tool(
    name="get_chan_analysis",
    description=(
        "缠论完整结构分析：包含关系合并、分型、笔、线段、中枢、背驰"
        "（MACD面积+RSI/KDJ共振）、三类买卖点（一/二/三买、一/二/三卖，"
        "含置信度与止损止盈）、走势类型（上涨/下跌/盘整）、支撑阻力位与摘要。"
        "Chan theory analysis (full engine). 基于日K现算，"
        "结构会被后续K线回溯修正。适用于趋势结构与背驰判断。"
    ),
)
async def get_chan_analysis(code: str, limit: int = 250) -> str:
    """Chan theory structural analysis for a stock/index (fractal/bi/zhongshu/beichi)."""
    try:
        c, _ = _resolve_code(code)
    except ValueError as e:
        return _d({"error": str(e)})
    from .compute import chan
    bars, source = await _get_bars(c, max(limit, 250))
    result = chan.analyze(bars)
    result["code"] = c
    result["source"] = source
    return _d(result)


@app.tool(
    name="trend_score",
    description=(
        "单股趋势评分（规则制100分+一票否决）：均线趋势25+MACD20+动量15+量能10"
        "+RSI位置10+缠论结构20（买卖点/中枢位置）。触发否决（三重空头、死叉叠加"
        "卖点/顶背驰等）直接0分。>=80 A(强趋势) / >=65 B(偏多) / >=50 C(中性) / D(弱势)。"
        "漏斗粗筛后的单股复核步骤。"
    ),
)
async def trend_score(code: str, limit: int = 250) -> str:
    """Rule-based 100-point trend score with hard vetoes for one stock."""
    try:
        c, _ = _resolve_code(code)
    except ValueError as e:
        return _d({"error": str(e)})
    from .compute import indicators, trend_score as ts
    from .compute import chan
    bars, source = await _get_bars(c, max(limit, 250))
    ind = indicators.analyze(bars)   # 一次计算，评分复用，不重算
    chan_r = chan.analyze(bars)
    result = ts.score_trend(bars, ind=ind, chan=chan_r if "error" not in chan_r else None)
    result["code"] = c
    result["source"] = source
    if "error" not in chan_r:
        result["chan_summary"] = chan_r.get("summary")
    return _d(result)


@app.tool(
    name="position_plan",
    description=(
        "风险预算法定仓位：入场价(当日收盘)、止损价、资金、单笔风险% → 股数/仓位%/最大亏损。"
        "止损价缺省自动取缠论最近买点的 stop_loss；含 ATR 健全性校验（止损过近/过远警告）。"
        "推荐流程最后一步：评分+LLM面通过后再定仓位。"
    ),
)
async def position_plan(
    code: str, stop_loss: float | None = None, capital: float = 100000.0,
    risk_pct: float = 1.0, max_position_pct: float = 30.0,
) -> str:
    """Risk-budget position sizing; stop defaults to latest chan buy-point stop."""
    try:
        c, _ = _resolve_code(code)
    except ValueError as e:
        return _d({"error": str(e)})
    from .compute import indicators, position, chan
    bars, source = await _get_bars(c, 120)
    ind = indicators.analyze(bars)
    if "error" in ind:
        return _d({"error": ind["error"]})
    entry = ind["close"]
    atr14 = ind.get("atr14")
    stop_src = "手动指定"
    if stop_loss is None:
        chan_r = chan.analyze(bars)
        for sig in reversed(chan_r.get("signals", []) if "error" not in chan_r else []):
            if "买" in sig.get("type", "") and sig.get("stop_loss"):
                stop_loss = float(sig["stop_loss"])
                stop_src = f"缠论{sig['type']}止损位({sig.get('date')})"
                break
    if stop_loss is None:
        return _d({"error": "未指定止损价，且缠论无近期买点可自动取止损位，请显式传 stop_loss"})
    result = position.position_size(entry, stop_loss, capital, risk_pct,
                                    max_position_pct, atr14)
    result.update({"code": c, "date": ind.get("date"),
                   "stop_loss_source": stop_src, "source": source})
    return _d(result)


# ---- 新闻舆情 / 财务指标（借鉴 StockAgent 移植） -------------------------------------


@app.tool(
    name="get_news",
    description=(
        "查询财经新闻与舆情（多源：财联社/东财全球/新浪 + 个股新闻）。Get financial news. "
        "code 传 6 位代码查个股新闻（本地库优先，未命中现拉东财并落库）；"
        "keyword 全文关键词过滤；source 按来源过滤（财联社/东财/新浪）；"
        "都不传返回三源聚合的最新市场快讯（现拉并落库）。"
    ),
)
async def get_news(code: str = "", keyword: str = "", source: str = "",
                   limit: int = 20) -> str:
    """Query stock news / market flash news / keyword sentiment."""
    from .data import news_client
    src = source or None
    if code:
        try:
            c = normalize(code)
        except ValueError as e:
            return _d({"error": str(e)})
        rows = db.query_news(code=c, keyword=keyword or None, source=src, limit=limit)
        source_tag = "local"
        if not rows:
            try:
                fetched = await news_client.stock_news(c)
                db.upsert_news(fetched)
                rows = db.query_news(code=c, keyword=keyword or None, source=src, limit=limit)
                source_tag = f"live_fetched({len(fetched)} persisted)"
            except Exception as e:
                return _d({"code": c, "news": [],
                           "hint": "本地无快照且现拉失败", "error": str(e)})
        return _d({"code": c, "source": source_tag, "news": rows})
    if keyword or src:
        rows = db.query_news(keyword=keyword or None, source=src, limit=limit)
        return _d({"keyword": keyword or None, "source": src or "any",
                   "count": len(rows), "news": rows})
    try:
        fetched = await news_client.market_news()
        db.upsert_news(fetched)
    except Exception as e:
        rows = db.query_news(limit=limit)
        return _d({"source": "local", "note": f"多源现拉失败，回退本地: {e}", "news": rows})
    out = fetched[:limit]
    return _d({"source": "multi_live(cls+em+sina)", "count": len(fetched),
               "persisted": len(fetched), "news": out})


@app.tool(
    name="get_financial_indicator",
    description=(
        "获取个股财务指标（EPS、ROE、ROA、毛利率、净利率、每股净资产/现金流、营收/净利）。"
        "Financial indicators by report period. 本地库优先，未命中现拉新浪源并落库，"
        "返回近 limit 期报告期数据（倒序）。"
    ),
)
async def get_financial_indicator(code: str, limit: int = 8) -> str:
    """Get financial indicators for a stock (local DB first, Sina live fallback)."""
    from .data import fin_client
    try:
        c = normalize(code)
    except ValueError as e:
        return _d({"error": str(e)})
    rows = db.query_financial_indicators(c, limit)
    if rows:
        return _d({"code": c, "source": "local", "periods": rows})
    try:
        df = await fin_client.financial_indicators(c)
        n = db.upsert_financial_indicators(c, df)
        rows = db.query_financial_indicators(c, limit)
        return _d({"code": c, "source": f"sina_fetched({n} persisted)", "periods": rows})
    except Exception as e:
        return _d({"code": c, "periods": [],
                   "hint": "本地无数据且现拉失败", "error": str(e)})


# ---- 因子排名 / 异动监听（借鉴 StockAgent 增强） -------------------------------------


def _resolve_universe(universe: str, codes: list[str]) -> tuple[list[str], str]:
    """返回 (代码列表, 说明)。universe: watchlist / zt_pool / concept:<名> / codes。"""
    if universe == "codes":
        return [normalize(c) for c in codes], "codes"
    if universe == "watchlist":
        return [w["code"] for w in db.watchlist_all()], "watchlist"
    if universe == "zt_pool":
        dates = db.zt_pool_dates()
        if not dates:
            raise ValueError("本地无涨停池快照，先 get_zt_pool")
        rows = db.query_zt_pool(dates[0])
        return [r["code"] for r in rows], f"zt_pool({dates[0]})"
    if universe.startswith("concept:"):
        name = universe.split(":", 1)[1]
        rows = db.query_concept_members(name)
        if not rows:
            raise ValueError(f"本地无概念 {name} 快照，先 refresh concepts")
        return [r["code"] for r in rows], f"concept:{name}"
    raise ValueError(f"未知 universe: {universe}")


@app.tool(
    name="factor_catalog",
    description=(
        "列出因子库目录（16个因子：动量/波动/流动性/技术/估值/质量），"
        "含每个因子的分类、数据输入、方向说明。List the factor catalog."
    ),
)
async def factor_catalog() -> str:
    """List factor catalog with metadata."""
    from .compute import factors as F
    return _d({"count": len(F.FACTOR_META), "inverted_factors": sorted(F.INVERTED_FACTORS),
               "catalog": F.catalog()})


@app.tool(
    name="factor_rank",
    description=(
        "因子横截面排名（16因子：动量 mom5/20/60、波动 vol20/60、流动性 换手/成交额/量比、"
        "技术 MA20偏离/RSI/价格位置、估值 pe/pb（低=高分）、质量 roe/毛利率/净利率）。"
        "Factor cross-sectional ranking. universe: watchlist / zt_pool / codes。"
        "基于本地日K+财务指标计算，分位0-100，综合分为所选因子分位均值。"
        "先用 factor_catalog 查看可选因子。"
    ),
)
async def factor_rank(
    universe: Literal["watchlist", "zt_pool", "codes"] = "watchlist",
    codes: list[str] | None = None,
    factors: list[str] | None = None,
    top_n: int = 10,
) -> str:
    """Rank a universe of stocks by cross-sectional factor scores (local data)."""
    try:
        codelist, note = _resolve_universe(universe, codes or [])
    except ValueError as e:
        return _d({"error": str(e)})
    if not codelist:
        return _d({"universe": note, "stocks": [], "hint": "股票池为空"})
    stock_bars: list[tuple[str, dict]] = []
    insufficient: list[str] = []
    for c in codelist:
        rows = db.query_klines_daily(c, limit=120)
        bars = list(reversed(rows))  # 升序
        if len(bars) >= 21:
            stock_bars.append((c, bars))
        else:
            insufficient.append(c)
    from .compute import factors as F
    # 质量因子输入：每股最新报告期财务指标
    try:
        fins = db.latest_financials([c for c, _ in stock_bars]) if stock_bars else {}
    except Exception:
        fins = {}
    ranked = F.rank_stocks(stock_bars, factors=factors, fins=fins, top_n=top_n)
    # 附名称
    name_map: dict[str, str] = {}
    con = db._conn()
    try:
        for c in [r["code"] for r in ranked]:
            row = con.execute("SELECT name FROM stock_basic WHERE code=?", (c,)).fetchone()
            if row:
                name_map[c] = row[0]
    finally:
        con.close()
    for r in ranked:
        r["name"] = name_map.get(r["code"])
    return _d({"universe": note, "universe_size": len(codelist),
               "computed": len(stock_bars), "insufficient_bars": insufficient,
               "factors": factors or list(F.FACTOR_META), "ranking": ranked})


@app.tool(
    name="get_chip_distribution",
    description=(
        "个股筹码分布：获利盘比例、平均成本、90%成本区间、筹码集中度、"
        "上方套牢压力位/下方筹码支撑位、成本峰分布直方图。"
        "Chip distribution analysis. 基于本地日K+换手率推演（三角分布模型，"
        "本地库优先，不足自动拉取）。适合判断套牢区/获利回吐压力。"
    ),
)
async def get_chip_distribution(code: str, limit: int = 1) -> str:
    """Get chip distribution (profit ratio / avg cost / 90% cost zone)."""
    try:
        c = normalize(code)
    except ValueError as e:
        return _d({"error": str(e)})
    from .compute import chips as C
    bars, source = await _get_bars(c, max(limit * 210, 210))  # 筹码需长历史
    result = C.analyze(bars)
    if "error" in result:
        return _d({"code": c, **result})
    result["code"] = c
    result["source"] = source
    # 落库（今日快照），历史趋势可反复查询
    try:
        db.upsert_chip_daily([{
            "code": c, "trade_date": result["date"],
            "profit_ratio_pct": result["profit_ratio_pct"],
            "avg_cost": result["avg_cost"],
            "cost_90_low": result["cost_90_low"],
            "cost_90_high": result["cost_90_high"],
            "concentration_pct": result["concentration_pct"],
        }])
    except Exception:
        pass  # 落库失败不影响返回
    if limit > 1:
        result["history"] = db.query_chip_daily(c, limit)
    return _d(result)


@app.tool(
    name="check_alerts",
    description=(
        "自选股盘中异动检测（实时行情 + 本地日K）。Intraday alert check on watchlist. "
        "策略：price_change(涨跌幅越限±5%) / limit_open(昨涨停今开板) / "
        "ma5_break(上穿MA5) / gap(开盘跳空≥2%)。返回按严重级排序的触发列表。"
        "盘中速览报告应调用本 tool。"
    ),
)
async def check_alerts(
    price_change_pct: float = 5.0, gap_pct: float = 2.0,
) -> str:
    """Run alert strategies on the watchlist (live quotes + local bars)."""
    from .compute import alerts as A
    from .data import tencent_client
    wl = db.watchlist_all()
    if not wl:
        return _d({"error": "watchlist 为空，先 manage_watchlist add"})
    quotes: dict[str, dict] = {}
    bars_map: dict[str, list] = {}
    for w in wl:
        c = w["code"]
        try:
            quotes[c] = await tencent_client.get_quote(c)
        except Exception:
            pass  # 单只失败不阻断
        rows = db.query_klines_daily(c, limit=30)
        bars_map[c] = list(reversed(rows))  # 升序
    result = A.check_watchlist(
        quotes, bars_map,
        cfg={"price_change_pct": price_change_pct, "gap_pct": gap_pct},
    )
    return _d(result)


# ---- 策略筛选层（market_snapshot → 硬过滤+打分 → 候选池） -----------------------------


@app.tool(
    name="list_strategies",
    description=(
        "列出可用选股策略（YAML 定义：放量突破/双低价值/缩量回踩，"
        "data/strategies/ 下可自定义覆盖）。List screening strategies. "
        "返回每个策略的硬过滤条件与因子权重。"
    ),
)
async def list_strategies() -> str:
    """List all screening strategies (packaged + user overrides)."""
    from .compute import screener as S
    strategies = S.load_strategies()
    return _d({"count": len(strategies),
               "strategies": [
                   {"name": s["name"], "display_name": s.get("display_name"),
                    "description": s.get("description"), "style": s.get("style"),
                    "version": s.get("version"),
                    "hard_filters": s.get("hard_filters"),
                    "factor_weights": s.get("factor_weights"),
                    "inverted": s.get("inverted", []),
                    "compute_filters": s.get("compute_filters"),
                    "compute_weights": s.get("compute_weights")}
                   for s in strategies.values()
               ]})


@app.tool(
    name="screen",
    description=(
        "执行策略筛选：对最新全市场快照(market_snapshot)做硬过滤+因子打分，返回 Top N 候选。"
        "Run a screening strategy. add_watchlist=true 时把 Top N 自动加入自选池"
        "（eod 将自动为其拉取深历史）。save_run=true 存档本次运行（screen_runs 表）。"
        "无快照时自动现拉全市场（约1-2分钟）。策略清单见 list_strategies。"
    ),
)
async def screen(
    strategy: str, top_n: int = 10,
    add_watchlist: bool = False, save_run: bool = True,
) -> str:
    """Screen the full-market snapshot with a strategy; return ranked candidates."""
    from .compute import screener as S
    strategies = S.load_strategies()
    s = strategies.get(strategy)
    if not s:
        return _d({"error": f"未知策略 {strategy!r}", "available": list(strategies)})
    rows = db.query_market_snapshot()
    source = "local"
    if not rows:
        from .refresh import refresh_snapshot
        try:
            r = await refresh_snapshot()
            source = f"live_fetched({r['rows']})"
            rows = db.query_market_snapshot()
        except Exception as e:
            return _d({"error": f"本地无快照且现拉失败: {e}",
                       "hint": "先运行 refresh snapshot"})
    result = S.screen(rows, s, top_n, bars_get=_local_bars_get)
    out: dict = {
        "strategy": s["name"], "display_name": s.get("display_name"),
        "style": s.get("style"), "snapshot_source": source,
        "snapshot_date": db.snapshot_dates(1)[0] if db.snapshot_dates(1) else None,
        "universe": result["universe"], "filtered": result["filtered"],
        "ranking": result["ranking"],
        "compute": result.get("compute"),
        "next_step": "对候选用 get_chan_analysis / get_chip_distribution 深评，"
                     "精选后 save_recommendation（skill_version 建议用策略名）",
    }
    if save_run and result["ranking"]:
        run_id = db.insert_screen_run(s["name"], result["universe"],
                                      result["filtered"], result["ranking"])
        out["run_id"] = run_id
    if add_watchlist and result["ranking"]:
        added = []
        for r in result["ranking"]:
            db.watchlist_add(r["code"], note=f"screen:{s['name']}")
            added.append(r["code"])
        out["added_to_watchlist"] = added
    return _d(out)


@app.tool(
    name="run_pipeline",
    description=(
        "一键执行推荐漏斗：screen策略筛选 → trend_score趋势终审(<65分淘汰/一票否决) → "
        "排雷(业绩预减/一字板买不进) → ATR风险预算定仓。Run the full recommendation "
        "pipeline. 输出候选包(含证据链+建议仓位)与各级淘汰名单。"
        "AI 只需对候选包终审 ≤3 只 → save_recommendation(skill_version=策略名)。"
        "早盘推荐报告的首选入口，替代手工串联多工具。"
    ),
)
async def run_pipeline(
    strategy: str, top_n: int = 10, min_trend_score: float = 65.0,
    capital: float = 100_000.0, risk_pct: float = 1.0,
    add_watchlist: bool = False, fetch_history: bool = True,
) -> str:
    """Run the full screening pipeline and return a candidate pack."""
    from .recommend import pipeline as P
    # 预处理：L1 候选中本地无深历史的 → Baostock 现拉落库（top_n 只，~2s/只）
    if fetch_history:
        try:
            from .recommend import screener as S
            strategies = S.load_strategies()
            if strategy in strategies:
                scr = S.screen(db.query_market_snapshot(), strategies[strategy], top_n)
                missing = [r["code"] for r in scr["ranking"]
                           if len(db.query_klines_daily(r["code"], limit=60)) < 30]
                if missing:
                    from .refresh import refresh_daily as _rd
                    await _rd(missing, days=0)
        except Exception:
            pass  # 预取失败不阻断（pipeline 会把无K线的候选标记淘汰）
    result = P.run_pipeline(strategy, top_n=top_n, min_trend_score=min_trend_score,
                            capital=capital, risk_pct=risk_pct)
    if "error" in result:
        return _d(result)
    # 候选落库 screen_runs（复盘可查当时漏斗证据）
    try:
        run_id = db.insert_screen_run(
            result["strategy"], result["universe"], result["filtered"],
            result["candidates"])
        result["run_id"] = run_id
    except Exception:
        pass
    if add_watchlist and result["candidates"]:
        added = []
        for c in result["candidates"]:
            db.watchlist_add(c["code"], note=f"pipeline:{result['strategy']}")
            added.append(c["code"])
        result["added_to_watchlist"] = added
    return _d(result)


@app.tool(
    name="scan_dragons",
    description=(
        "龙头战法扫描：对最新涨停池做五维「识别真龙」评分（带动性30/领涨性25/"
        "抗跌性15/流动性20/资金承接10，门槛40/40/35/35一票否决，移植自dragon-quant）。"
        "Identify true dragon leaders from the limit-up pool. "
        "eod 每日自动扫描存档；打板风格推荐首选入口，入选后 AI 终审 → "
        "save_recommendation(style='打板', skill_version='dragon_v1')。"
    ),
)
async def scan_dragons(top: int = 5, add_watchlist: bool = False) -> str:
    """Scan the latest limit-up pool and identify true dragon leaders."""
    from .recommend import dragon as DG
    r = await DG.scan_dragons(top=top, persist=True)
    if "error" in r:
        return _d(r)
    if add_watchlist and r["dragons"]:
        added = []
        for x in r["dragons"]:
            db.watchlist_add(x["code"], note="dragon:dragon_v1")
            added.append(x["code"])
        r["added_to_watchlist"] = added
    return _d(r)


@app.tool(
    name="screen_runs",
    description=(
        "查询历史筛选运行存档（策略/日期/池规模/Top候选）。Historical screen runs. "
        "配合 get_recommendation_stats(skill_version=策略名) 可看各策略长期战绩。"
    ),
)
async def screen_runs(limit: int = 10) -> str:
    """Query historical screen runs."""
    return _d({"runs": db.query_screen_runs(limit)})


# ---- 龙虎榜 / 业绩预告 / 板块轮动（借鉴 DSA 数据层） ----------------------------------


@app.tool(
    name="get_lhb",
    description=(
        "查询龙虎榜每日明细（净买额排行）。Dragon-Tiger list. date: YYYYMMDD 默认今日，"
        "dates=true 查已有快照日期；传 code 查个股近 N 天上榜史（days 默认30）。"
        "返回净买额/买卖额/上榜原因/解读。"
        "本地库优先，未命中现拉东财并落库。复盘报告'资金面'与个股分析'上榜史'数据源。"
    ),
)
async def get_lhb(date: str = "", min_net_buy: float = 0.0, limit: int = 20,
                  dates: bool = False, code: str = "", days: int = 30) -> str:
    """Get dragon-tiger list for a date, or one stock's recent LHB history."""
    from datetime import datetime as _dt
    if dates:
        return _d({"available_dates": db.lhb_dates()})
    if code:
        try:
            c = normalize(code)
        except ValueError as e:
            return _d({"error": str(e)})
        rows = db.query_lhb_by_code(c, days)
        return _d({"code": c, "days": days, "count": len(rows),
                   "net_buy_sum": round(sum(r["net_buy"] or 0 for r in rows) / 1e8, 2),
                   "history": rows})
    d = date or _dt.now().strftime("%Y%m%d")
    if not (len(d) == 8 and d.isdigit()):
        return _d({"error": f"date 应为 YYYYMMDD，收到: {date!r}"})
    iso = f"{d[:4]}-{d[4:6]}-{d[6:]}"
    rows = db.query_lhb(iso, min_net_buy, limit)
    source = "local"
    if not rows:
        from .refresh import refresh_lhb
        try:
            await refresh_lhb(d)
            rows = db.query_lhb(iso, min_net_buy, limit)
            source = f"live_fetched({len(rows)} persisted)"
        except Exception as e:
            return _d({"error": f"本地无快照且现拉失败: {e}", "date": iso})
    total_net = sum(r["net_buy"] or 0 for r in rows)
    return _d({"date": iso, "source": source, "count": len(rows),
               "total_net_buy": round(total_net / 1e8, 2), "stocks": rows})


@app.tool(
    name="get_earnings_forecast",
    description=(
        "查询业绩预告（预增/预减/扭亏/续盈等）。Earnings forecast. "
        "code 查个股；ftype 按类型过滤（如 预增）；min_change 按最低变动幅度%。"
        "事件驱动推荐的核心素材（避雷预减、捕捉预增）。本地库，refresh forecast 每日更新。"
    ),
)
async def get_earnings_forecast(code: str = "", ftype: str = "",
                                min_change: float | None = None,
                                limit: int = 20) -> str:
    """Query earnings forecasts by code / type / min change pct."""
    c = None
    if code:
        try:
            c = normalize(code)
        except ValueError as e:
            return _d({"error": str(e)})
    rows = db.query_earnings_forecast(code=c, ftype=ftype or None,
                                      min_change=min_change, limit=limit)
    if not rows:
        from .refresh import refresh_forecast
        try:
            await refresh_forecast()
            rows = db.query_earnings_forecast(code=c, ftype=ftype or None,
                                              min_change=min_change, limit=limit)
        except Exception as e:
            return _d({"hint": f"本地无数据且拉取失败: {e}", "rows": []})
    return _d({"filters": {"code": c, "ftype": ftype or None, "min_change": min_change},
               "count": len(rows), "rows": rows})


@app.tool(
    name="sector_rotation",
    description=(
        "板块轮动排行：行业指数近 N 日累计涨幅 Top/Bottom。Sector rotation ranking. "
        "基于 index_daily(category=industry, 同花顺90个行业) 落库数据。"
        "days: 回看天数(默认5)。复盘报告'热点轮动'与早盘'主线判断'的数据源。"
    ),
)
async def sector_rotation(days: int = 5, top_n: int = 10) -> str:
    """Sector rotation ranking over recent N days (industry indices)."""
    uni = db.index_universe("industry")
    if not uni:
        return _d({"error": "本地无行业指数数据，先运行 refresh ind_index"})
    r = db.index_rotation("industry", days=days, top_n=top_n)
    # 大盘基准
    bench_rows = db.query_index_daily("sh.000001", limit=days)
    bench = None
    if len(bench_rows) >= 2:
        first, last = bench_rows[-1]["close"], bench_rows[0]["close"]
        bench = round((last / first - 1) * 100, 2)
    return _d({"days": days, "universe": len(uni), "benchmark_sh000001_pct": bench,
               "top": r["top"], "bottom": r["bottom"]})


# ---- Skill 层工具（报告归档；模板本体在 DSH skill：scratch-plugin/skills/） ----------


@app.tool(
    name="save_report",
    description=(
        "归档生成的报告到 ~/.dsh-stock/reports/。Archive a generated report. "
        "rtype: review/intraday/morning/stock。复盘和早盘报告完成后必须归档。"
    ),
)
async def save_report(rtype: str, content: str) -> str:
    """Archive a generated report; returns the saved path."""
    from . import skill_store
    return _d(skill_store.save_report(rtype, content))


@app.tool(
    name="list_reports",
    description="列出已归档的报告文件。List archived reports."
)
async def list_reports(limit: int = 10) -> str:
    """List archived report files."""
    from . import skill_store
    return _d({"reports": skill_store.list_reports(limit)})


# ---- Entry point -------------------------------------------------------------


def main() -> None:
    db.init_db()
    asyncio.run(app.run_stdio_async())


if __name__ == "__main__":
    main()
