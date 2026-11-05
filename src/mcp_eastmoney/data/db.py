"""SQLite 落库模块 — 唯一写库入口。

职责边界：本模块只做建表/upsert/查询，不发任何网络请求。
库内代码统一为 6 位纯数字码（见 codes.py）。

数据库规范：
- 唯一正式库路径：项目内 <project>/data/stock.db（STOCK_DB_PATH 可覆盖，支持 ~ 前缀）
- DSH 挂载（cordis.yml）与 CLI 默认值一致，杜绝分脑
- 查看库状态：uv run python -m mcp_eastmoney.refresh dbinfo
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

_PROJECT_ROOT = Path(__file__).resolve().parents[3]  # src/mcp_eastmoney/data/db.py -> 项目根

DB_PATH = os.path.expanduser(os.environ.get(
    "STOCK_DB_PATH",
    str(_PROJECT_ROOT / "data" / "stock.db"),
))

DDL = """
CREATE TABLE IF NOT EXISTS stock_basic (
    code TEXT PRIMARY KEY, name TEXT,
    ipo_date TEXT, out_date TEXT,
    type INTEGER, status INTEGER,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS stock_industry (
    code TEXT PRIMARY KEY,
    industry TEXT, classification TEXT,
    update_date TEXT, updated_at TEXT
);



CREATE TABLE IF NOT EXISTS quotes_daily (
    code TEXT NOT NULL, trade_date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, amount REAL,
    change_pct REAL, turnover_rate REAL,
    pe REAL, pb REAL, ps REAL,
    total_mv REAL, float_mv REAL,     -- 亿元（v3：由全市场行情摄入；K线源不提供）
    source TEXT, updated_at TEXT,
    PRIMARY KEY (code, trade_date)
);

CREATE TABLE IF NOT EXISTS concept_members (
    trade_date TEXT NOT NULL,          -- 成分快照日（同花顺）
    concept_code TEXT NOT NULL,        -- 同花顺概念代码
    concept_name TEXT,
    code TEXT NOT NULL,                -- 成分股 6 位代码
    name TEXT,
    updated_at TEXT,
    PRIMARY KEY (trade_date, concept_code, code)
);
CREATE INDEX IF NOT EXISTS idx_concept_members_code ON concept_members(code, trade_date);

CREATE TABLE IF NOT EXISTS quotes_15min (
    code TEXT NOT NULL, datetime TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, amount REAL,
    PRIMARY KEY (code, datetime)
);

CREATE TABLE IF NOT EXISTS fetch_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task TEXT, api TEXT, param TEXT,
    provider TEXT, status TEXT,
    latency_ms INTEGER, error TEXT, created_at TEXT
);

CREATE TABLE IF NOT EXISTS watchlist (
    code TEXT PRIMARY KEY, note TEXT, added_at TEXT
);

CREATE TABLE IF NOT EXISTS zt_pool (
    trade_date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    change_pct REAL,
    close REAL,
    amount REAL,
    turnover_rate REAL,
    seal_amount REAL,
    first_seal_time TEXT,
    last_seal_time TEXT,
    blast_count INTEGER,
    zt_stat TEXT,
    lian_ban INTEGER,
    industry TEXT, updated_at TEXT,
    PRIMARY KEY (trade_date, code)
);

CREATE TABLE IF NOT EXISTS news (
    url TEXT PRIMARY KEY,
    scope TEXT NOT NULL,             -- 'stock:<code>' / 'market'
    code TEXT,                       -- 个股新闻才有
    title TEXT NOT NULL,
    content TEXT,
    source TEXT,
    pub_time TEXT,
    fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_news_code ON news(code, pub_time);
CREATE INDEX IF NOT EXISTS idx_news_scope ON news(scope, pub_time);

CREATE TABLE IF NOT EXISTS financial_indicators (
    code TEXT NOT NULL,
    report_date TEXT NOT NULL,       -- 报告期 YYYY-MM-DD
    eps REAL,                        -- 摊薄每股收益
    bps REAL,                        -- 每股净资产（调整后）
    ocfps REAL,                      -- 每股经营性现金流
    roe REAL,                        -- 净资产收益率 %
    roa REAL,                        -- 总资产利润率 %
    gross_margin REAL,               -- 销售毛利率 %
    net_margin REAL,                 -- 销售净利率 %
    revenue REAL,                    -- 主营业务收入（元）
    net_profit REAL,                 -- 净利润（元）
    updated_at TEXT,
    PRIMARY KEY (code, report_date)
);

CREATE TABLE IF NOT EXISTS index_daily (
    code TEXT NOT NULL,               -- broad: 'sh.000001' / industry: 同花顺板块代码
    name TEXT NOT NULL,
    category TEXT NOT NULL,           -- broad(大盘) / industry(行业) / concept(概念)
    trade_date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, amount REAL, change_pct REAL,
    source TEXT, updated_at TEXT,
    PRIMARY KEY (code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_index_daily_cat ON index_daily(category, trade_date);

CREATE TABLE IF NOT EXISTS lhb_daily (
    trade_date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    close REAL, change_pct REAL,
    net_buy REAL,                     -- 龙虎榜净买额(元)
    buy_amount REAL, sell_amount REAL,
    total_amount REAL,                -- 龙虎榜成交额
    ratio_in_total REAL,              -- 净买额占总成交比 %
    turnover_rate REAL, float_mv REAL,
    reason TEXT,                      -- 上榜原因
    interp TEXT,                      -- 解读
    PRIMARY KEY (trade_date, code)
);

CREATE TABLE IF NOT EXISTS earnings_forecast (
    code TEXT NOT NULL,
    report_date TEXT NOT NULL,        -- 报告期 YYYY-MM-DD
    name TEXT,
    indicator TEXT,                   -- 预测指标(净利润等)
    forecast_type TEXT,               -- 预增/预减/扭亏/续盈...
    change_pct REAL,                  -- 业绩变动幅度 %
    forecast_value REAL,              -- 预测数值
    reason TEXT,                      -- 业绩变动原因
    announce_date TEXT,
    updated_at TEXT,
    PRIMARY KEY (code, report_date)
);

CREATE TABLE IF NOT EXISTS screen_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL,
    strategy TEXT NOT NULL,
    universe_size INTEGER,
    filtered_size INTEGER,
    result TEXT,                      -- JSON: Top N 候选（含分数）
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rec_date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    thesis TEXT,
    concepts TEXT,
    style TEXT,
    entry_ref_price REAL,
    target_horizon INTEGER DEFAULT 5,
    confidence INTEGER,
    skill_version TEXT DEFAULT 'v1',
    run_id INTEGER,                    -- 关联 funnel_runs.id（哪次运行选出，v8 可评估上下文）
    risk_flags TEXT,                   -- JSON 数组：落表时风险标记（point-in-time，v8）
    status TEXT DEFAULT 'open',        -- open / final
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_rec_date ON recommendations(rec_date);

CREATE TABLE IF NOT EXISTS recommendation_reviews (
    rec_id INTEGER NOT NULL,
    eval_date TEXT NOT NULL,
    days_held INTEGER,
    close REAL,
    pnl_pct REAL,
    max_gain_pct REAL,
    max_drawdown_pct REAL,
    hit_target INTEGER,               -- 1 赢 0 输 NULL 未到期
    final_note TEXT,
    alpha_pct REAL,                   -- 同期相对上证超额（v2 起 eod review 落库）
    PRIMARY KEY (rec_id, eval_date)
);
CREATE TABLE IF NOT EXISTS strategies (
    name TEXT PRIMARY KEY,            -- 策略名（与 YAML name / skill_version 一致）
    version TEXT,                     -- YAML version 字段，缺省 1.0
    source TEXT,                      -- packaged(内置) / user(data/strategies 覆盖)
    definition TEXT,                  -- JSON 完整参数：归因时精确到参数版本
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS sector_flow (
    trade_date TEXT NOT NULL,
    sector_type TEXT NOT NULL,        -- industry(行业) / concept(概念)
    code TEXT NOT NULL,               -- 东财板块代码 BKxxxx
    name TEXT,                        -- 板块名（航空机场/机器人概念…）
    index_value REAL,                 -- 板块指数点位
    change_pct REAL,                  -- 板块涨跌幅 %
    main_net REAL,                    -- 主力净流入（元）
    main_net_pct REAL,                -- 主力净占比 %
    super_large_net REAL, super_large_pct REAL,
    large_net REAL, large_pct REAL,
    medium_net REAL, medium_pct REAL,
    small_net REAL, small_pct REAL,
    leader_name TEXT, leader_code TEXT,   -- 领涨股
    updated_at TEXT,
    PRIMARY KEY (trade_date, sector_type, code)
);
CREATE INDEX IF NOT EXISTS idx_sector_flow_date ON sector_flow(trade_date, sector_type);
CREATE TABLE IF NOT EXISTS funnel_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL,            -- 运行日（自然日）
    strategy TEXT NOT NULL,
    snapshot_date TEXT,                -- 依据的截面日（T-1/T）
    mode TEXT,                         -- L2（策略边界）/ full（完整漏斗）
    universe INTEGER,                  -- 全市场
    filtered INTEGER,                  -- 硬过滤后
    stages TEXT,                       -- JSON 各级收窄计数
    candidates TEXT,                   -- JSON L2 出口标的（trend_score/快照证据）
    excluded TEXT,                     -- JSON 各级淘汰明细（code/stage/reason）
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_funnel_date ON funnel_runs(run_date, strategy);
-- 展开视图：funnel_runs 的 candidates JSON → 候选级明细（一只票一行）
CREATE VIEW IF NOT EXISTS funnel_candidates AS
SELECT r.id            AS run_id,
       r.run_date      AS run_date,
       r.strategy      AS strategy,
       r.snapshot_date AS snapshot_date,
       json_extract(c.value, '$.code')        AS code,
       json_extract(c.value, '$.name')        AS name,
       json_extract(c.value, '$.close')       AS close,
       json_extract(c.value, '$.screen_score')AS screen_score,
       json_extract(c.value, '$.trend_score') AS trend_score,
       json_extract(c.value, '$.trend_grade') AS trend_grade,
       json_extract(c.value, '$.snapshot.change_pct')    AS change_pct,
       json_extract(c.value, '$.snapshot.amount')        AS amount,
       json_extract(c.value, '$.snapshot.turnover_rate') AS turnover_rate,
       json_extract(c.value, '$.snapshot.volume_ratio')  AS volume_ratio,
       json_extract(c.value, '$.snapshot.pe')            AS pe,
       json_extract(c.value, '$.snapshot.pb')            AS pb
FROM funnel_runs r, json_each(r.candidates) c;

-- aggregate_runs（v7 曾有的跨策略聚合存档）已随聚合层移除（2026-11-04）：
-- 既有库中的表与历史数据保留不删（证据永不删），新库不再创建，代码不再读写。
"""


# ---- schema 版本化迁移（唯一 DDL 演进入口；历史 ad-hoc ALTER 已折叠为 v1） ---------
SCHEMA_VERSION = 8


def _migrate(c: sqlite3.Connection) -> None:
    v = c.execute("PRAGMA user_version").fetchone()[0]
    if v < 1:
        # v1（历史遗留）：quotes_daily 补估值列
        for col in ("pe", "pb", "ps"):
            try:
                c.execute(f"ALTER TABLE quotes_daily ADD COLUMN {col} REAL")
            except sqlite3.OperationalError:
                pass  # 列已存在
        v = 1
    if v < 2:
        # v2（2026-10-29 数据治理）：策略注册表 + 推荐参数快照 + 后验超额落库
        for ddl in (
            "ALTER TABLE recommendations ADD COLUMN strategy_def TEXT",
            "ALTER TABLE recommendation_reviews ADD COLUMN alpha_pct REAL",
        ):
            try:
                c.execute(ddl)
            except sqlite3.OperationalError:
                pass  # 列已存在
        v = 2
    if v < 3:
        # v3（2026-10-30）：market_snapshot 退役——市值并入 quotes_daily，
        # 截面改为 quotes_daily 派生查询（唯一真相源）。
        for col in ("total_mv", "float_mv"):
            try:
                c.execute(f"ALTER TABLE quotes_daily ADD COLUMN {col} REAL")
            except sqlite3.OperationalError:
                pass  # 列已存在
        has_snap = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_snapshot'"
        ).fetchone()
        if has_snap:
            c.execute(
                "UPDATE quotes_daily SET "
                "total_mv=(SELECT m.total_mv FROM market_snapshot m "
                "          WHERE m.code=quotes_daily.code AND m.trade_date=quotes_daily.trade_date), "
                "float_mv=(SELECT m.float_mv FROM market_snapshot m "
                "          WHERE m.code=quotes_daily.code AND m.trade_date=quotes_daily.trade_date) "
                "WHERE EXISTS (SELECT 1 FROM market_snapshot m "
                "              WHERE m.code=quotes_daily.code AND m.trade_date=quotes_daily.trade_date)")
            c.execute("DROP TABLE market_snapshot")
        v = 3
    if v < 4:
        # v4（2026-10-30）：板块资金流表（行业 + 概念），每日 eod 落库
        v = 4
    if v < 5:
        # v5（2026-10-30）：chip_daily 删除——筹码由 quotes_daily 现算，
        # 落库历史无实际消费者（纯派生缓存，与真相源重复）。
        c.execute("DROP TABLE IF EXISTS chip_daily")
        v = 5
    if v < 6:
        # v6（2026-10-30）：概念两表合并为单表 concept_members（清单可由成员派生）
        c.execute("DROP TABLE IF EXISTS stock_concepts")
        c.execute("DROP TABLE IF EXISTS concepts")
        v = 6
    if v < 7:
        # v7（2026-11-03）：L1→L2 可回溯存档（funnel_runs；aggregate_runs 后随聚合层移除）
        v = 7
    if v < 8:
        # v8（2026-11-04）：推荐关联运行上下文（可评估上下文闭环）
        # run_id → funnel_runs.id（哪次运行选出的）；risk_flags → 落表时的风险标记
        for ddl in (
            "ALTER TABLE recommendations ADD COLUMN run_id INTEGER",
            "ALTER TABLE recommendations ADD COLUMN risk_flags TEXT",
        ):
            try:
                c.execute(ddl)
            except sqlite3.OperationalError:
                pass  # 列已存在
        v = 8
    c.execute(f"PRAGMA user_version={v}")


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def init_db() -> None:
    c = _conn()
    try:
        c.executescript(DDL)
        _migrate(c)
        c.commit()
    finally:
        c.close()


def _num(v: Any) -> float | None:
    """Baostock/AkShare 返回的字符串安全转 float；空值转 None。"""
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "-", "None", "nan", "NaN"):
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    if f != f:  # NaN
        return None
    return f


def _int(v: Any) -> int | None:
    f = _num(v)
    return int(f) if f is not None else None


# ---- fetch_log ----------------------------------------------------------------


def log_fetch(
    task: str,
    api: str,
    provider: str,
    status: str,
    latency_ms: int = 0,
    error: str | None = None,
    param: str | None = None,
) -> None:
    c = _conn()
    try:
        c.execute(
            "INSERT INTO fetch_log (task, api, param, provider, status, latency_ms, error, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (task, api, param, provider, status, latency_ms, error[:500] if error else None, _now()),
        )
        c.commit()
    finally:
        c.close()


def last_success_at(task: str) -> str | None:
    """补跑判断：某任务最后一次 success 的时间。"""
    c = _conn()
    try:
        row = c.execute(
            "SELECT created_at FROM fetch_log WHERE task=? AND status='success' ORDER BY id DESC LIMIT 1",
            (task,),
        ).fetchone()
        return row[0] if row else None
    finally:
        c.close()


# ---- upsert: 基础信息 -----------------------------------------------------------


def upsert_stock_basic(df: pd.DataFrame) -> int:
    """Baostock query_stock_basic 结果 -> stock_basic。code 需为 6 位码。"""
    rows = []
    for _, r in df.iterrows():
        code = str(r.get("code", "")).split(".")[-1]
        if not code.isdigit():
            continue
        rows.append((
            code, r.get("code_name"),
            r.get("ipoDate") or None, r.get("outDate") or None,
            _int(r.get("type")), _int(r.get("status")),
            _now(),
        ))
    c = _conn()
    try:
        c.executemany(
            "INSERT OR REPLACE INTO stock_basic (code, name, ipo_date, out_date, type, status, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        c.commit()
        return len(rows)
    finally:
        c.close()


def upsert_stock_industry(df: pd.DataFrame) -> int:
    rows = []
    for _, r in df.iterrows():
        code = str(r.get("code", "")).split(".")[-1]
        if not code.isdigit():
            continue
        rows.append((
            code, r.get("industry"), r.get("industryClassification"),
            r.get("updateDate") or None, _now(),
        ))
    c = _conn()
    try:
        c.executemany(
            "INSERT OR REPLACE INTO stock_industry (code, industry, classification, update_date, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        c.commit()
        return len(rows)
    finally:
        c.close()


# ---- upsert: K 线 ---------------------------------------------------------------


def upsert_klines_daily(df: pd.DataFrame, source: str = "baostock") -> int:
    """Baostock 日K结果 -> quotes_daily（仅个股；指数走 upsert_index_daily）。

    字段: date, open, high, low, close, volume, amount, turn, pctChg, peTTM, pbMRQ, psTTM。
    """
    rows = []
    for _, r in df.iterrows():
        code = str(r.get("code", "")).split(".")[-1]
        if not code.isdigit() or not r.get("date"):
            continue
        rows.append((
            code, str(r["date"]),
            _num(r.get("open")), _num(r.get("high")), _num(r.get("low")), _num(r.get("close")),
            _num(r.get("volume")), _num(r.get("amount")),
            _num(r.get("pctChg")), _num(r.get("turn")),
            _num(r.get("peTTM")), _num(r.get("pbMRQ")), _num(r.get("psTTM")),
            source, _now(),
        ))
    c = _conn()
    try:
        c.executemany(
            "INSERT INTO quotes_daily (code, trade_date, open, high, low, close, volume, amount, "
            "change_pct, turnover_rate, pe, pb, ps, source, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(code, trade_date) DO UPDATE SET "
            "open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, "
            "volume=excluded.volume, amount=excluded.amount, "
            "change_pct=COALESCE(excluded.change_pct, change_pct), "
            "turnover_rate=COALESCE(excluded.turnover_rate, turnover_rate), "
            "pe=COALESCE(excluded.pe, pe), pb=COALESCE(excluded.pb, pb), "
            "ps=COALESCE(excluded.ps, ps), "
            "total_mv=COALESCE(excluded.total_mv, total_mv), "
            "float_mv=COALESCE(excluded.float_mv, float_mv), "
            "source=excluded.source, updated_at=excluded.updated_at",
            rows,
        )
        c.commit()
        return len(rows)
    finally:
        c.close()


def upsert_klines_15m(df: "pd.DataFrame | list[dict]") -> int:
    """15 分钟线 -> quotes_15min（Baostock time / 新浪 datetime 双兼容，
    接受 DataFrame 或 list[dict]）。"""
    if isinstance(df, list):
        df = pd.DataFrame(df)
    rows = []
    for _, r in df.iterrows():
        code = str(r.get("code", "")).split(".")[-1]
        # 兼容 Baostock(time) 与新浪(datetime) 两种时间字段
        dt = r.get("time") or r.get("datetime")
        if not code.isdigit() or not dt:
            continue
        rows.append((
            code, str(dt),
            _num(r.get("open")), _num(r.get("high")), _num(r.get("low")), _num(r.get("close")),
            _num(r.get("volume")), _num(r.get("amount")),
        ))
    c = _conn()
    try:
        c.executemany(
            "INSERT OR REPLACE INTO quotes_15min (code, datetime, open, high, low, close, volume, amount) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        c.commit()
        return len(rows)
    finally:
        c.close()


# ---- upsert: 概念 ----------------------------------------------------------------


def upsert_concept_members(concept_code: str, concept_name: str,
                           members: list[dict], snapshot_date: str) -> int:
    """概念成分股 -> concept_members 每日快照（单概念概念表，v6 合并）。"""
    rows = [
        (snapshot_date, concept_code, concept_name, m["code"], m.get("name"), _now())
        for m in members if str(m.get("code", "")).isdigit()
    ]
    c = _conn()
    try:
        c.executemany(
            "INSERT OR REPLACE INTO concept_members "
            "(trade_date, concept_code, concept_name, code, name, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        c.commit()
        return len(rows)
    finally:
        c.close()

# ---- 查询 ------------------------------------------------------------------------


def get_last_trade_date(code: str) -> str | None:
    """增量拉取起点：某股库中最新交易日。"""
    c = _conn()
    try:
        row = c.execute(
            "SELECT MAX(trade_date) FROM quotes_daily WHERE code=?", (code,)
        ).fetchone()
        return row[0] if row else None
    finally:
        c.close()


def get_last_15m_datetime(code: str) -> str | None:
    c = _conn()
    try:
        row = c.execute(
            "SELECT MAX(datetime) FROM quotes_15min WHERE code=?", (code,)
        ).fetchone()
        return row[0] if row else None
    finally:
        c.close()


def query_klines_daily(code: str, limit: int = 60) -> list[dict]:
    c = _conn()
    try:
        rows = c.execute(
            "SELECT trade_date, open, high, low, close, volume, amount, change_pct, turnover_rate, pe, pb, ps "
            "FROM quotes_daily WHERE code=? ORDER BY trade_date DESC LIMIT ?",
            (code, limit),
        ).fetchall()
        return [
            {"date": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4],
             "volume": r[5], "amount": r[6], "change_pct": r[7], "turnover_rate": r[8],
             "pe": r[9], "pb": r[10], "ps": r[11]}
            for r in rows
        ]
    finally:
        c.close()


def query_klines_15m(code: str, limit: int = 500) -> list[dict]:
    c = _conn()
    try:
        rows = c.execute(
            "SELECT datetime, open, high, low, close, volume, amount "
            "FROM quotes_15min WHERE code=? ORDER BY datetime DESC LIMIT ?",
            (code, limit),
        ).fetchall()
        return [
            {"datetime": r[0], "open": r[1], "high": r[2], "low": r[3],
             "close": r[4], "volume": r[5], "amount": r[6]}
            for r in rows
        ]
    finally:
        c.close()


def query_stock_concepts(code: str) -> list[dict]:
    """反查：个股属于哪些概念（取最新快照日期）。"""
    c = _conn()
    try:
        rows = c.execute(
            "SELECT concept_code, concept_name, trade_date FROM concept_members "
            "WHERE code=? AND trade_date=(SELECT MAX(trade_date) FROM concept_members)",
            (code,),
        ).fetchall()
        return [{"concept_code": r[0], "concept_name": r[1], "snapshot_date": r[2]} for r in rows]
    finally:
        c.close()


def query_concept_members(concept_name: str, snapshot_date: str | None = None) -> list[dict]:
    """正查：概念的成分股（默认最新快照）。"""
    c = _conn()
    try:
        if snapshot_date is None:
            row = c.execute(
                "SELECT MAX(trade_date) FROM concept_members WHERE concept_name=?",
                (concept_name,),
            ).fetchone()
            if not row or not row[0]:
                return []
            snapshot_date = row[0]
        rows = c.execute(
            "SELECT code, name FROM concept_members WHERE concept_name=? AND trade_date=? ORDER BY code",
            (concept_name, snapshot_date),
        ).fetchall()
        return [{"code": r[0], "name": r[1], "snapshot_date": snapshot_date} for r in rows]
    finally:
        c.close()


def query_stock_industry(code: str) -> dict | None:
    c = _conn()
    try:
        row = c.execute(
            "SELECT industry, classification, update_date FROM stock_industry WHERE code=?", (code,)
        ).fetchone()
        if not row:
            return None
        return {"code": code, "industry": row[0], "classification": row[1], "update_date": row[2]}
    finally:
        c.close()


def concept_member_diff(concept_name: str, days: int = 7) -> dict:
    """对比快照：该概念近 N 天新增/移除了哪些成分股。"""
    c = _conn()
    try:
        dates = [r[0] for r in c.execute(
            "SELECT DISTINCT snapshot_date FROM stock_concepts WHERE concept_name=? "
            "ORDER BY snapshot_date DESC LIMIT ?", (concept_name, days),
        ).fetchall()]
        if len(dates) < 2:
            return {"error": f"快照不足（{len(dates)} 份），无法对比"}
        new_d, old_d = dates[0], dates[-1]
        q = "SELECT code FROM stock_concepts WHERE concept_name=? AND snapshot_date=?"
        cur = {r[0] for r in c.execute(q, (concept_name, new_d)).fetchall()}
        old = {r[0] for r in c.execute(q, (concept_name, old_d)).fetchall()}
        return {
            "concept": concept_name,
            "from": old_d, "to": new_d,
            "added": sorted(cur - old),
            "removed": sorted(old - cur),
        }
    finally:
        c.close()


# ---- watchlist --------------------------------------------------------------------


def watchlist_add(code: str, note: str | None = None) -> None:
    c = _conn()
    try:
        c.execute(
            "INSERT OR REPLACE INTO watchlist (code, note, added_at) VALUES (?, ?, ?)",
            (code, note, _now()),
        )
        c.commit()
    finally:
        c.close()


def watchlist_remove(code: str) -> None:
    c = _conn()
    try:
        c.execute("DELETE FROM watchlist WHERE code=?", (code,))
        c.commit()
    finally:
        c.close()


def watchlist_all() -> list[dict]:
    c = _conn()
    try:
        rows = c.execute("SELECT code, note, added_at FROM watchlist ORDER BY added_at").fetchall()
        return [{"code": r[0], "note": r[1], "added_at": r[2]} for r in rows]
    finally:
        c.close()


def next_trade_day(date_str: str) -> str:
    """粗略的增量起点：最后日期 + 1 天（Baostock 自动跳过非交易日）。"""
    d = datetime.strptime(date_str[:10], "%Y-%m-%d") + timedelta(days=1)
    return d.strftime("%Y-%m-%d")


# ---- zt_pool（涨停池每日快照） -------------------------------------------------------


def upsert_zt_pool(df: pd.DataFrame, trade_date: str) -> int:
    """东财涨停池结果 -> zt_pool。df 列为 AkShare 中文列名。"""
    rows = []
    for _, r in df.iterrows():
        code = str(r.get("代码") or "").strip()
        if not code.isdigit():
            continue
        rows.append((
            trade_date, code, r.get("名称"),
            _num(r.get("涨跌幅")), _num(r.get("最新价")),
            _num(r.get("成交额")), _num(r.get("换手率")),
            _num(r.get("封板资金")),
            str(r.get("首次封板时间") or "").zfill(6),
            str(r.get("最后封板时间") or "").zfill(6),
            _int(r.get("炸板次数")), str(r.get("涨停统计") or ""),
            _int(r.get("连板数")), r.get("所属行业"), _now(),
        ))
    c = _conn()
    try:
        c.executemany(
            "INSERT OR REPLACE INTO zt_pool (trade_date, code, name, change_pct, close, amount, "
            "turnover_rate, seal_amount, first_seal_time, last_seal_time, blast_count, zt_stat, "
            "lian_ban, industry, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        c.commit()
        return len(rows)
    finally:
        c.close()


def query_zt_pool(trade_date: str, min_lian_ban: int = 1) -> list[dict]:
    """按日期查涨停池，可过滤最低连板数，按连板数降序。"""
    c = _conn()
    try:
        rows = c.execute(
            "SELECT code, name, change_pct, close, amount, turnover_rate, first_seal_time, "
            "blast_count, zt_stat, lian_ban, industry FROM zt_pool "
            "WHERE trade_date=? AND lian_ban>=? ORDER BY lian_ban DESC, first_seal_time",
            (trade_date, min_lian_ban),
        ).fetchall()
        return [
            {"code": r[0], "name": r[1], "change_pct": r[2], "close": r[3], "amount": r[4],
             "turnover_rate": r[5], "first_seal_time": r[6], "blast_count": r[7],
             "zt_stat": r[8], "lian_ban": r[9], "industry": r[10]}
            for r in rows
        ]
    finally:
        c.close()


def zt_pool_dates() -> list[str]:
    """已有快照的日期列表（跨天分析用）。"""
    c = _conn()
    try:
        return [r[0] for r in c.execute(
            "SELECT DISTINCT trade_date FROM zt_pool ORDER BY trade_date DESC LIMIT 60"
        ).fetchall()]
    finally:
        c.close()


def zt_pool_daily_stats(limit: int = 10) -> list[dict]:
    """逐日涨停池聚合（情绪定量用）：总数/最高连板/二板以上/炸板股数，按日期降序。

    @param limit 返回最近 N 个有快照的交易日。
    """
    c = _conn()
    try:
        rows = c.execute(
            "SELECT trade_date, COUNT(*) AS total, MAX(lian_ban) AS max_lian_ban,"
            " SUM(CASE WHEN lian_ban >= 2 THEN 1 ELSE 0 END) AS lianban_2plus,"
            " SUM(CASE WHEN blast_count > 0 THEN 1 ELSE 0 END) AS blasted"
            " FROM zt_pool GROUP BY trade_date ORDER BY trade_date DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {"date": r[0], "total": r[1], "max_lian_ban": r[2] or 0,
             "lianban_2plus": r[3] or 0, "blasted": r[4] or 0}
            for r in rows
        ]
    finally:
        c.close()


# ---- recommendations（AI 推荐记录与绩效） ---------------------------------------------


def sync_strategy_registry(strategies: dict[str, tuple[dict, str]]) -> int:
    """YAML 策略 → strategies 注册表（load_strategies 每次加载时同步）。

    strategies: {name: (definition_dict, source)}，source 为 packaged/user。
    definition 存 JSON 快照，保证"当时用什么参数"可回溯。
    """
    import json as _json
    c = _conn()
    try:
        for name, (definition, source) in strategies.items():
            c.execute(
                "INSERT OR REPLACE INTO strategies (name, version, source, definition, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, str(definition.get("version", "1.0")), source,
                 _json.dumps(definition, ensure_ascii=False), _now()),
            )
        c.commit()
        return len(strategies)
    finally:
        c.close()


def strategy_registry() -> list[dict]:
    """注册表全量（name/version/source/updated_at；definition 展开时用）。"""
    import json as _json
    c = _conn()
    try:
        rows = c.execute(
            "SELECT name, version, source, definition, updated_at FROM strategies "
            "ORDER BY source, name").fetchall()
        return [{"name": r[0], "version": r[1], "source": r[2],
                 "definition": _json.loads(r[3]), "updated_at": r[4]} for r in rows]
    finally:
        c.close()


def delete_quotes_by_source(source: str) -> int:
    """删除指定来源的日K行（回填修复用：如把新浪不复权重拉为腾讯前复权）。"""
    c = _conn()
    try:
        n = c.execute("DELETE FROM quotes_daily WHERE source=?", (source,)).rowcount
        c.commit()
        return n
    finally:
        c.close()


def prune_tables(fetchlog_days: int = 30,
                 min15_days: int = 30) -> dict[str, int]:
    """库瘦身（eod 末步调用）：只删可再生数据，证据类表永不删。

    - fetch_log：审计保留 fetchlog_days 天（补跑自查只需近期）
    - quotes_15min：分时只服务盘中，保留 min15_days 天
    （market_snapshot 已于 v3 退役，quotes_daily 是唯一真相源，不参与清理）
    """
    c = _conn()
    out: dict[str, int] = {}
    try:
        out["fetch_log"] = c.execute(
            "DELETE FROM fetch_log WHERE created_at < datetime('now', ?)",
            (f"-{fetchlog_days} days",)).rowcount
        out["quotes_15min"] = c.execute(
            "DELETE FROM quotes_15min WHERE datetime < datetime('now', ?)",
            (f"-{min15_days} days",)).rowcount
        c.commit()
        return out
    finally:
        c.close()


def benchmark_closes(code: str = "sh.000001") -> dict[str, float]:
    """基准指数 {日期: 收盘}，供后验 α 计算与决策档案复用。"""
    c = _conn()
    try:
        return {r[0]: r[1] for r in c.execute(
            "SELECT trade_date, close FROM index_daily WHERE code=?", (code,))}
    finally:
        c.close()


def insert_recommendation(code: str, name: str | None, thesis: str,
                          concepts: str, style: str, entry_ref_price: float,
                          target_horizon: int, confidence: int,
                          skill_version: str, rec_date: str | None = None,
                          strategy_def: str | None = None,
                          run_id: int | None = None,
                          risk_flags: str | None = None) -> int:
    """写入一条推荐记录，返回自增 id。

    strategy_def：推荐时刻策略参数的 JSON 快照（skill_version 匹配已注册策略时
    由 server 层自动附上）——策略参数后续修改不影响历史归因，飞轮可对比参数版本。
    run_id：关联 funnel_runs.id（该票由哪次策略运行选出，证据链闭合）。
    risk_flags：JSON 数组文本，落表时冻结的风险标记（point-in-time）。
    """
    c = _conn()
    try:
        cur = c.execute(
            "INSERT INTO recommendations (rec_date, code, name, thesis, concepts, style, "
            "entry_ref_price, target_horizon, confidence, skill_version, strategy_def, "
            "run_id, risk_flags, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rec_date or _now()[:10], code, name, thesis, concepts, style,
             entry_ref_price, target_horizon, confidence, skill_version,
             strategy_def, run_id, risk_flags, _now()),
        )
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def recommendations_open() -> list[dict]:
    """所有未终评（status=open）的推荐。"""
    c = _conn()
    try:
        rows = c.execute(
            "SELECT id, rec_date, code, name, thesis, concepts, style, entry_ref_price, "
            "target_horizon, confidence, skill_version, run_id, risk_flags "
            "FROM recommendations WHERE status='open' ORDER BY rec_date"
        ).fetchall()
        import json as _json
        return [
            {"id": r[0], "rec_date": r[1], "code": r[2], "name": r[3], "thesis": r[4],
             "concepts": r[5], "style": r[6], "entry_ref_price": r[7],
             "target_horizon": r[8], "confidence": r[9], "skill_version": r[10],
             "run_id": r[11],
             "risk_flags": _json.loads(r[12]) if r[12] else None}
            for r in rows
        ]
    finally:
        c.close()


def upsert_review(rec_id: int, eval_date: str, days_held: int, close: float,
                  pnl_pct: float, max_gain_pct: float, max_drawdown_pct: float,
                  hit_target: int | None, final_note: str | None = None,
                  alpha_pct: float | None = None) -> None:
    """写入/更新某推荐某评估日的绩效行。alpha_pct：同期相对上证超额。"""
    c = _conn()
    try:
        c.execute(
            "INSERT OR REPLACE INTO recommendation_reviews "
            "(rec_id, eval_date, days_held, close, pnl_pct, max_gain_pct, max_drawdown_pct, "
            "hit_target, final_note, alpha_pct) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rec_id, eval_date, days_held, close, pnl_pct, max_gain_pct,
             max_drawdown_pct, hit_target, final_note, alpha_pct),
        )
        c.commit()
    finally:
        c.close()


def finalize_recommendation(rec_id: int, hit: int, note: str) -> None:
    c = _conn()
    try:
        c.execute("UPDATE recommendations SET status='final' WHERE id=?", (rec_id,))
        c.commit()
    finally:
        c.close()
    _ = note  # note 已写入 review 行


def recommendation_stats(group_by: str = "style", days: int = 30,
                         min_sample: int = 5) -> list[dict]:
    """绩效归因：按 style/concept/confidence/skill_version 切片的胜率统计。

    每条推荐取其最新一次 review 的 pnl_pct 作为当前战绩。
    """
    col_map = {"style": "r.style", "confidence": "r.confidence",
               "skill_version": "r.skill_version", "concept": "r.concepts"}
    col = col_map.get(group_by, "r.style")
    c = _conn()
    try:
        rows = c.execute(
            f"""
            SELECT {col} AS grp,
                   COUNT(*) AS n,
                   SUM(CASE WHEN v.pnl_pct > 0 THEN 1 ELSE 0 END) AS wins,
                   AVG(v.pnl_pct) AS avg_pnl,
                   AVG(v.alpha_pct) AS avg_alpha,
                   MIN(v.pnl_pct) AS worst,
                   MAX(v.pnl_pct) AS best,
                   AVG(v.max_drawdown_pct) AS avg_dd
            FROM recommendations r
            JOIN (
                SELECT rec_id, pnl_pct, max_drawdown_pct, alpha_pct, days_held,
                       ROW_NUMBER() OVER (PARTITION BY rec_id ORDER BY eval_date DESC) rn
                FROM recommendation_reviews
            ) v ON v.rec_id = r.id AND v.rn = 1 AND v.days_held > 0  -- days_held=0 为作废单，不计入
            WHERE r.rec_date >= date('now', ?)
            GROUP BY grp
            ORDER BY avg_pnl DESC
            """,
            (f"-{days} days",),
        ).fetchall()
        return [
            {"group": r[0], "count": r[1],
             "wins": r[2] or 0,
             "win_rate": round((r[2] or 0) / r[1] * 100, 1) if r[1] else 0,
             "avg_pnl_pct": round(r[3], 2) if r[3] is not None else None,
             "avg_alpha_pct": round(r[4], 2) if r[4] is not None else None,
             "worst_pct": round(r[5], 2) if r[5] is not None else None,
             "best_pct": round(r[6], 2) if r[6] is not None else None,
             "avg_max_drawdown_pct": round(r[7], 2) if r[7] is not None else None,
             "reliable": r[1] >= min_sample}
            for r in rows
        ]
    finally:
        c.close()


def recommendation_history(limit: int = 20) -> list[dict]:
    """最近推荐 + 最新绩效。"""
    c = _conn()
    try:
        recs = c.execute(
            "SELECT id, rec_date, code, name, thesis, style, entry_ref_price, "
            "target_horizon, confidence, skill_version, status FROM recommendations "
            "ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()
        out = []
        for r in recs:
            v = c.execute(
                "SELECT eval_date, days_held, close, pnl_pct, hit_target, final_note FROM recommendation_reviews "
                "WHERE rec_id=? ORDER BY eval_date DESC LIMIT 1", (r[0],)
            ).fetchone()
            # 真实可成交入场价：最新 review 的 close 与 pnl 反推（顺延开盘入场时与推荐日参考价不同）
            entry_eff = None
            entry_note = None
            if v and v[3] is not None and (1 + v[3] / 100) != 0:
                entry_eff = round(v[2] / (1 + v[3] / 100), 3)
                entry_note = v[5]
            out.append({
                "id": r[0], "rec_date": r[1], "code": r[2], "name": r[3],
                "thesis": r[4], "style": r[5], "entry": r[6], "horizon": r[7],
                "confidence": r[8], "skill_version": r[9], "status": r[10],
                "entry_effective": entry_eff,
                "entry_note": entry_note,
                "latest_review": ({"eval_date": v[0], "days_held": v[1], "close": v[2],
                                   "pnl_pct": v[3], "hit_target": v[4]} if v else None),
            })
        return out
    finally:
        c.close()


def decision_history(code: str, limit: int = 20) -> dict:
    """同一股票的历史决策档案（TradingAgents 决策日志范式）。

    每条历史推荐附最新后验盈亏与相对基准（sh.000001 上证指数）同期 alpha，
    汇总胜负与教训（final 终评 note），供分析/推荐前注入"该股历史战绩与教训"。
    """
    import json as _json
    c = _conn()
    try:
        recs = c.execute(
            "SELECT id, rec_date, code, name, thesis, style, entry_ref_price, "
            "target_horizon, confidence, skill_version, status, run_id, risk_flags "
            "FROM recommendations WHERE code=? ORDER BY id DESC LIMIT ?", (code, limit),
        ).fetchall()
        bench = {r[0]: r[1] for r in c.execute(
            "SELECT trade_date, close FROM index_daily WHERE code='sh.000001'")}
        items, lessons, pnls, alphas, wins = [], [], [], [], 0
        for r in recs:
            v = c.execute(
                "SELECT eval_date, days_held, close, pnl_pct, hit_target, final_note "
                "FROM recommendation_reviews WHERE rec_id=? "
                "ORDER BY eval_date DESC LIMIT 1", (r[0],),
            ).fetchone()
            pnl = v[3] if v else None
            alpha = None
            if pnl is not None and v and v[0] in bench and r[1] in bench \
                    and bench[r[1]] and r[1] < v[0]:
                bench_chg = (bench[v[0]] / bench[r[1]] - 1) * 100
                alpha = round(pnl - bench_chg, 2)
                alphas.append(alpha)
            if pnl is not None:
                pnls.append(pnl)
                wins += 1 if pnl > 0 else 0
            if r[10] == "final" and v and v[5]:
                lessons.append(f"{r[1]} {r[5]}: {v[5]}")
            items.append({
                "id": r[0], "rec_date": r[1], "code": r[2], "name": r[3],
                "thesis": r[4], "style": r[5], "entry": r[6], "horizon": r[7],
                "confidence": r[8], "skill_version": r[9], "status": r[10],
                "run_id": r[11],
                "risk_flags": (_json.loads(r[12]) if r[12] else None),
                "latest_review": ({"eval_date": v[0], "days_held": v[1],
                                   "close": v[2], "pnl_pct": pnl,
                                   "hit_target": v[4]} if v else None),
                "alpha_pct": alpha,
            })
        n = len(pnls)
        return {
            "code": code,
            "count": len(items),
            "summary": {
                "evaluated": n,
                "wins": wins,
                "win_rate_pct": round(wins / n * 100, 1) if n else None,
                "avg_pnl_pct": round(sum(pnls) / n, 2) if n else None,
                "avg_alpha_pct": round(sum(alphas) / len(alphas), 2) if alphas else None,
                "reliable": n >= 3,
            },
            "lessons": lessons[:10],
            "decisions": items,
        }
    finally:
        c.close()


# ---- news（新闻舆情） ----------------------------------------------------------------


def upsert_news(rows: list[dict]) -> int:
    """写新闻。rows: [{url, scope, code?, title, content?, source?, pub_time?}, ...]"""
    data = []
    for r in rows:
        url = (r.get("url") or "").strip()
        title = (r.get("title") or "").strip()
        if not url or not title:
            continue
        data.append((
            url, r.get("scope") or "market", r.get("code"), title,
            r.get("content"), r.get("source"), r.get("pub_time"), _now(),
        ))
    c = _conn()
    try:
        c.executemany(
            "INSERT OR REPLACE INTO news (url, scope, code, title, content, source, pub_time, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            data,
        )
        c.commit()
        return len(data)
    finally:
        c.close()


def query_news(code: str | None = None, keyword: str | None = None,
               source: str | None = None, limit: int = 20) -> list[dict]:
    """查新闻：按个股/全文关键词/来源过滤，按发布时间倒序。"""
    c = _conn()
    try:
        sql = "SELECT title, content, source, pub_time, url, code FROM news"
        conds: list[str] = []
        params: list[Any] = []
        if code:
            conds.append("code=?")
            params.append(code)
        if keyword:
            conds.append("(title LIKE ? OR content LIKE ?)")
            params.extend([f"%{keyword}%", f"%{keyword}%"])
        if source:
            conds.append("source LIKE ?")
            params.append(f"%{source}%")
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY pub_time DESC, rowid DESC LIMIT ?"
        params.append(limit)
        rows = c.execute(sql, params).fetchall()
        return [
            {"title": r[0], "content": r[1], "source": r[2],
             "pub_time": r[3], "url": r[4], "code": r[5]}
            for r in rows
        ]
    finally:
        c.close()


def news_last_fetch(scope: str) -> str | None:
    """某 scope 最后一次落库时间（采集节流用）。"""
    c = _conn()
    try:
        row = c.execute(
            "SELECT MAX(fetched_at) FROM news WHERE scope=?", (scope,)
        ).fetchone()
        return row[0] if row else None
    finally:
        c.close()


# ---- financial_indicators（财务指标） -------------------------------------------------


_FIN_COLS = (
    ("eps", "摊薄每股收益(元)"),
    ("bps", "每股净资产_调整后(元)"),
    ("ocfps", "每股经营性现金流(元)"),
    ("roe", "净资产收益率(%)"),
    ("roa", "总资产利润率(%)"),
    ("gross_margin", "销售毛利率(%)"),
    ("net_margin", "销售净利率(%)"),
    ("revenue", "主营业务收入(元)"),
    ("net_profit", "净利润(元)"),
)


def upsert_financial_indicators(code: str, df: pd.DataFrame) -> int:
    """新浪财务指标结果 -> financial_indicators。df 列为中文列名。"""
    rows = []
    for _, r in df.iterrows():
        d = str(r.get("日期") or "").strip()[:10]
        if not d:
            continue
        vals = tuple(_num(r.get(cn)) for _, cn in _FIN_COLS)
        rows.append((code, d, *vals, _now()))
    c = _conn()
    try:
        c.executemany(
            "INSERT OR REPLACE INTO financial_indicators "
            "(code, report_date, eps, bps, ocfps, roe, roa, gross_margin, net_margin, "
            "revenue, net_profit, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        c.commit()
        return len(rows)
    finally:
        c.close()


def query_financial_indicators(code: str, limit: int = 8) -> list[dict]:
    """按报告期倒序返回财务指标。"""
    keys = [k for k, _ in _FIN_COLS]
    c = _conn()
    try:
        rows = c.execute(
            f"SELECT report_date, {', '.join(keys)} FROM financial_indicators "
            "WHERE code=? ORDER BY report_date DESC LIMIT ?",
            (code, limit),
        ).fetchall()
        return [
            {"report_date": r[0], **dict(zip(keys, r[1:]))} for r in rows
        ]
    finally:
        c.close()


def latest_financials(codes: list[str]) -> dict[str, dict]:
    """批量取每股最新报告期财务指标（因子库的质量因子输入）。"""
    if not codes:
        return {}
    c = _conn()
    try:
        ph = ",".join("?" * len(codes))
        rows = c.execute(
            f"""
            SELECT f.code, f.report_date, f.eps, f.roe, f.roa, f.gross_margin,
                   f.net_margin, f.bps, f.ocfps
            FROM financial_indicators f
            JOIN (SELECT code, MAX(report_date) md FROM financial_indicators
                  WHERE code IN ({ph}) GROUP BY code) m
              ON f.code = m.code AND f.report_date = m.md
            """,
            codes,
        ).fetchall()
        return {
            r[0]: {"report_date": r[1], "eps": r[2], "roe": r[3], "roa": r[4],
                   "gross_margin": r[5], "net_margin": r[6], "bps": r[7], "ocfps": r[8]}
            for r in rows
        }
    finally:
        c.close()


# ---- chip_daily（筹码分布每日快照） --------------------------------------------------


# ---- 全市场截面（quotes_daily 派生；market_snapshot 已于 v3 退役） ------------------


def upsert_snapshot_as_daily(rows: list[dict], trade_date: str) -> int:
    """全市场行情（腾讯批量）→ quotes_daily 当日行（唯一真相源的唯一摄入口）。

    rows 由 tencent_client.get_batch_quotes 产出；volume 手→股 与 Baostock 对齐，
    市值（total_mv/float_mv，亿元）一并落列，供策略 total_mv_min 硬过滤使用。
    """
    data = []
    for r in rows:
        if not r.get("code") or r.get("price") is None:
            continue
        data.append((
            r["code"], trade_date,
            _num(r.get("open")), _num(r.get("high")), _num(r.get("low")),
            _num(r.get("price")), (r.get("volume") or 0) * 100,  # 手→股
            _num(r.get("amount")), _num(r.get("change_pct")),
            _num(r.get("turnover_rate")), _num(r.get("pe")), _num(r.get("pb")),
            _num(r.get("total_mv")), _num(r.get("float_mv")),
            "tencent_snapshot", _now(),
        ))
    c = _conn()
    try:
        c.executemany(
            "INSERT INTO quotes_daily (code, trade_date, open, high, low, "
            "close, volume, amount, change_pct, turnover_rate, pe, pb, total_mv, "
            "float_mv, source, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(code, trade_date) DO UPDATE SET "
            "open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, "
            "volume=excluded.volume, amount=excluded.amount, "
            "change_pct=COALESCE(excluded.change_pct, change_pct), "
            "turnover_rate=COALESCE(excluded.turnover_rate, turnover_rate), "
            "pe=COALESCE(excluded.pe, pe), pb=COALESCE(excluded.pb, pb), "
            "total_mv=COALESCE(excluded.total_mv, total_mv), "
            "float_mv=COALESCE(excluded.float_mv, float_mv), "
            "source=excluded.source, updated_at=excluded.updated_at",
            data,
        )
        c.commit()
        return len(data)
    finally:
        c.close()


def query_market_snapshot(trade_date: str | None = None) -> list[dict]:
    """全市场截面（默认 quotes_daily 最新交易日）。

    派生字段：volume_ratio（当日量/前5日均量）、amplitude（振幅，需前收）、
    name（stock_basic）。与旧 market_snapshot 返回结构完全兼容。
    """
    c = _conn()
    try:
        if trade_date is None:
            row = c.execute("SELECT MAX(trade_date) FROM quotes_daily").fetchone()
            if not row or not row[0]:
                return []
            trade_date = row[0]
        rows = c.execute(
            """
            SELECT q.code,
                   COALESCE(b.name, q.code) AS name,
                   q.close, q.change_pct, q.volume, q.amount, q.turnover_rate,
                   CASE WHEN avg5.avg_vol > 0 THEN q.volume / avg5.avg_vol END AS volume_ratio,
                   CASE WHEN prev.close > 0
                        THEN (q.high - q.low) / prev.close * 100 END AS amplitude,
                   q.pe, q.pb, q.total_mv, q.float_mv, q.open, q.high, q.low,
                   prev.close AS prev_close
            FROM quotes_daily q
            LEFT JOIN stock_basic b ON b.code = q.code
            LEFT JOIN (SELECT code, trade_date, close FROM quotes_daily) prev
                   ON prev.code = q.code AND prev.trade_date = (
                        SELECT MAX(trade_date) FROM quotes_daily p
                        WHERE p.code = q.code AND p.trade_date < q.trade_date)
            LEFT JOIN (
                SELECT code, AVG(volume) AS avg_vol FROM (
                    SELECT code, volume, ROW_NUMBER() OVER (
                        PARTITION BY code ORDER BY trade_date DESC) rn
                    FROM quotes_daily WHERE trade_date < ?
                ) WHERE rn <= 5 GROUP BY code
            ) avg5 ON avg5.code = q.code
            WHERE q.trade_date = ?
            """,
            (trade_date, trade_date),
        ).fetchall()
        return [
            {"code": r[0], "name": r[1], "close": r[2], "change_pct": r[3],
             "volume": r[4], "amount": r[5], "turnover_rate": r[6],
             "volume_ratio": (round(r[7], 2) if r[7] is not None else None),
             "amplitude": (round(r[8], 2) if r[8] is not None else None),
             "pe": r[9], "pb": r[10], "total_mv": r[11], "float_mv": r[12],
             "open": r[13], "high": r[14], "low": r[15], "prev_close": r[16]}
            for r in rows
        ]
    finally:
        c.close()


def snapshot_dates(limit: int = 30) -> list[str]:
    """截面日期 = quotes_daily 的交易日（倒序）。"""
    c = _conn()
    try:
        return [r[0] for r in c.execute(
            "SELECT DISTINCT trade_date FROM quotes_daily ORDER BY trade_date DESC LIMIT ?",
            (limit,),
        ).fetchall()]
    finally:
        c.close()


# ---- sector_flow（板块资金流：行业 + 概念） -----------------------------------------


def upsert_sector_flow(rows: list[dict], trade_date: str, sector_type: str) -> int:
    """东财板块资金流 → sector_flow（行业/概念同表，按 sector_type 区分）。"""
    data = [
        (trade_date, sector_type, r["code"], r.get("name"),
         _num(r.get("index_value")), _num(r.get("change_pct")),
         _num(r.get("main_net")), _num(r.get("main_net_pct")),
         _num(r.get("super_large_net")), _num(r.get("super_large_pct")),
         _num(r.get("large_net")), _num(r.get("large_pct")),
         _num(r.get("medium_net")), _num(r.get("medium_pct")),
         _num(r.get("small_net")), _num(r.get("small_pct")),
         r.get("leader_name"), r.get("leader_code"), _now())
        for r in rows if r.get("code")
    ]
    c = _conn()
    try:
        c.executemany(
            "INSERT OR REPLACE INTO sector_flow (trade_date, sector_type, code, name, "
            "index_value, change_pct, main_net, main_net_pct, super_large_net, "
            "super_large_pct, large_net, large_pct, medium_net, medium_pct, "
            "small_net, small_pct, leader_name, leader_code, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            data,
        )
        c.commit()
        return len(data)
    finally:
        c.close()


def query_sector_flow(sector_type: str | None = None,
                      trade_date: str | None = None,
                      limit: int = 20) -> list[dict]:
    """板块资金流（默认最新交易日，按主力净流入降序）。"""
    c = _conn()
    try:
        if trade_date is None:
            row = c.execute("SELECT MAX(trade_date) FROM sector_flow").fetchone()
            if not row or not row[0]:
                return []
            trade_date = row[0]
        sql = ("SELECT trade_date, sector_type, code, name, index_value, change_pct, "
               "main_net, main_net_pct, super_large_net, large_net, medium_net, "
               "small_net, leader_name, leader_code FROM sector_flow "
               "WHERE trade_date=?")
        args = [trade_date]
        if sector_type:
            sql += " AND sector_type=?"
            args.append(sector_type)
        sql += " ORDER BY main_net DESC LIMIT ?"
        args.append(limit)
        keys = ("trade_date", "sector_type", "code", "name", "index_value",
                "change_pct", "main_net", "main_net_pct", "super_large_net",
                "large_net", "medium_net", "small_net", "leader_name", "leader_code")
        return [dict(zip(keys, r)) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


def sector_flow_dates(limit: int = 10) -> list[str]:
    c = _conn()
    try:
        return [r[0] for r in c.execute(
            "SELECT DISTINCT trade_date FROM sector_flow ORDER BY trade_date DESC LIMIT ?",
            (limit,)).fetchall()]
    finally:
        c.close()


# ---- funnel_runs（L1→L2 可回溯存档，v7） ---------------------------------------------


def insert_funnel_run(strategy: str, snapshot_date: str | None, mode: str,
                      universe: int, filtered: int, stages: dict,
                      candidates: list[dict], excluded: list[dict]) -> int:
    """一次 L1→L2 运行的完整证据存档：收窄计数 + 出口标的 + 各级淘汰明细。"""
    import json as _json
    c = _conn()
    try:
        cur = c.execute(
            "INSERT INTO funnel_runs (run_date, strategy, snapshot_date, mode, universe, "
            "filtered, stages, candidates, excluded, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_now()[:10], strategy, snapshot_date, mode, universe, filtered,
             _json.dumps(stages, ensure_ascii=False),
             _json.dumps(candidates, ensure_ascii=False),
             _json.dumps(excluded, ensure_ascii=False), _now()),
        )
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def query_funnel_runs(strategy: str | None = None, run_date: str | None = None,
                      limit: int = 20) -> list[dict]:
    """历史漏斗运行（默认最近）。含收窄计数与候选（淘汰明细 limit 内附带）。"""
    import json as _json
    c = _conn()
    try:
        sql = ("SELECT id, run_date, strategy, snapshot_date, mode, universe, filtered, "
               "stages, candidates, excluded, created_at FROM funnel_runs")
        conds, args = [], []
        if strategy:
            conds.append("strategy=?"); args.append(strategy)
        if run_date:
            conds.append("run_date=?"); args.append(run_date)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        out = []
        for r in c.execute(sql, args).fetchall():
            out.append({"id": r[0], "run_date": r[1], "strategy": r[2],
                        "snapshot_date": r[3], "mode": r[4], "universe": r[5],
                        "filtered": r[6], "stages": _json.loads(r[7]),
                        "candidates": _json.loads(r[8]),
                        "excluded": _json.loads(r[9])[:50],  # 明细截断防爆
                        "created_at": r[10]})
        return out
    finally:
        c.close()


# aggregate_runs 写入/查询函数已随聚合层移除（2026-11-04）。
# 既有库中的历史数据保留（证据永不删），需要时用 SQL 直接查 aggregate_runs 表。


def query_funnel_candidates(strategy: str | None = None,
                            run_date: str | None = None,
                            code: str | None = None,
                            latest: bool = True,
                            limit: int = 100) -> list[dict]:
    """候选级明细（funnel_candidates 视图）：一只票一行，含所属运行/策略/趋势分。

    latest=True（默认）：每个策略只取**最近一次**运行，避免同日多次扫描重复。
    """
    c = _conn()
    try:
        sql = ("SELECT run_id, run_date, strategy, snapshot_date, code, name, close, "
               "screen_score, trend_score, trend_grade, change_pct, amount, "
               "turnover_rate, volume_ratio, pe, pb FROM funnel_candidates")
        conds, args = [], []
        if strategy:
            conds.append("strategy=?"); args.append(strategy)
        if run_date:
            conds.append("run_date=?"); args.append(run_date)
        if code:
            conds.append("code=?"); args.append(code)
        if latest:
            conds.append("run_id IN (SELECT MAX(id) FROM funnel_runs GROUP BY strategy)")
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY run_date DESC, trend_score DESC LIMIT ?"
        args.append(limit)
        keys = ("run_id", "run_date", "strategy", "snapshot_date", "code", "name",
                "close", "screen_score", "trend_score", "trend_grade",
                "change_pct", "amount", "turnover_rate", "volume_ratio", "pe", "pb")
        return [dict(zip(keys, r)) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


# ---- screen_runs（策略筛选运行存档） --------------------------------------------------


def insert_screen_run(strategy: str, universe_size: int, filtered_size: int,
                      result: list[dict]) -> int:
    import json as _json
    c = _conn()
    try:
        cur = c.execute(
            "INSERT INTO screen_runs (run_date, strategy, universe_size, filtered_size, "
            "result, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (_now()[:10], strategy, universe_size, filtered_size,
             _json.dumps(result, ensure_ascii=False), _now()),
        )
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def query_screen_runs(limit: int = 20) -> list[dict]:
    import json as _json
    c = _conn()
    try:
        rows = c.execute(
            "SELECT id, run_date, strategy, universe_size, filtered_size, created_at "
            "FROM screen_runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            codes = c.execute(
                "SELECT json_extract(value, '$.code') FROM json_each("
                "(SELECT result FROM screen_runs WHERE id=?)) LIMIT 10", (r[0],)
            ).fetchall()
            out.append({"id": r[0], "run_date": r[1], "strategy": r[2],
                        "universe": r[3], "filtered": r[4], "created_at": r[5],
                        "top_codes": [x[0] for x in codes]})
        return out
    finally:
        c.close()


# ---- index_daily（大盘/行业/概念指数，独立于个股K线） ----------------------------------


def upsert_index_daily(code: str, name: str, category: str,
                       rows: list[dict], source: str = "baostock") -> int:
    """指数日K upsert。rows: [{date, open, high, low, close, volume, amount, change_pct}, ...] 升序。

    change_pct 缺失时自动补算：库内该指数前一交易日收盘 → 批内前收双兜底
    （THS行业K线与腾讯兜底行均不带涨幅，2026-10 修复 5.9万行 NULL 空洞）。
    """
    c = _conn()
    try:
        # 库内该指数早于本批首日的最近收盘（增量场景的批外前收）
        first_date = next((str(r["date"]) for r in rows if r.get("date")), None)
        prev_close = None
        if first_date:
            row = c.execute(
                "SELECT close FROM index_daily WHERE code=? AND trade_date<? "
                "ORDER BY trade_date DESC LIMIT 1", (code, first_date),
            ).fetchone()
            prev_close = row[0] if row else None
        data = []
        for r in rows:
            if not r.get("date"):
                continue
            close = _num(r.get("close"))
            chg = _num(r.get("change_pct"))
            if chg is None and close is not None and prev_close:
                chg = round((close / prev_close - 1) * 100, 2)
            prev_close = close or prev_close
            data.append((
                code, name, category, str(r["date"]),
                _num(r.get("open")), _num(r.get("high")), _num(r.get("low")),
                close, _num(r.get("volume")), _num(r.get("amount")),
                chg, source, _now(),
            ))
        c.executemany(
            "INSERT OR REPLACE INTO index_daily (code, name, category, trade_date, "
            "open, high, low, close, volume, amount, change_pct, source, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            data,
        )
        c.commit()
        return len(data)
    finally:
        c.close()


def query_index_daily(code: str, limit: int = 60) -> list[dict]:
    """按指数代码取日K（倒序，同 query_klines_daily 形状）。"""
    c = _conn()
    try:
        rows = c.execute(
            "SELECT trade_date, open, high, low, close, volume, amount, change_pct "
            "FROM index_daily WHERE code=? ORDER BY trade_date DESC LIMIT ?",
            (code, limit),
        ).fetchall()
        return [
            {"date": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4],
             "volume": r[5], "amount": r[6], "change_pct": r[7]}
            for r in rows
        ]
    finally:
        c.close()


def get_last_index_date(code: str) -> str | None:
    c = _conn()
    try:
        row = c.execute(
            "SELECT MAX(trade_date) FROM index_daily WHERE code=?", (code,)
        ).fetchone()
        return row[0] if row else None
    finally:
        c.close()


def index_universe(category: str) -> list[dict]:
    """某类别全部指数（code+name，取最新出现过的）。"""
    c = _conn()
    try:
        rows = c.execute(
            "SELECT code, MAX(name) FROM index_daily WHERE category=? "
            "GROUP BY code ORDER BY code", (category,),
        ).fetchall()
        return [{"code": r[0], "name": r[1]} for r in rows]
    finally:
        c.close()


def index_rotation(category: str = "industry", days: int = 5,
                   top_n: int = 10) -> dict:
    """板块轮动：近 N 日累计涨幅排行（含首尾日期与区间高低）。"""
    c = _conn()
    try:
        rows = c.execute(
            """
            SELECT code, MAX(name) AS name,
                   SUM(CASE WHEN rn=1 THEN close END) AS last_close,
                   SUM(CASE WHEN rn=:d THEN close END) AS first_close,
                   MAX(high) AS hi, MIN(low) AS lo,
                   SUM(change_pct) AS cum_chg,
                   COUNT(*) AS n
            FROM (
                SELECT code, name, close, high, low, change_pct,
                       ROW_NUMBER() OVER (PARTITION BY code ORDER BY trade_date DESC) rn
                FROM index_daily WHERE category=:cat
            )
            WHERE rn <= :d
            GROUP BY code HAVING n = :d
            """,
            {"cat": category, "d": days},
        ).fetchall()
        out = [
            {"code": r[0], "name": r[1], "last_close": r[2],
             "chg_pct": round((r[2] / r[3] - 1) * 100, 2) if r[3] else None,
             "high": r[4], "low": r[5],
             "daily_cum_pct": round(r[6], 2) if r[6] is not None else None}
            for r in rows
        ]
        out.sort(key=lambda x: -(x["chg_pct"] if x["chg_pct"] is not None else -999))
        return {"top": out[:top_n], "bottom": list(reversed(out[-top_n:]))}
    finally:
        c.close()


# ---- lhb_daily（龙虎榜每日明细） -------------------------------------------------------


def upsert_lhb(df: "pd.DataFrame", trade_date: str) -> int:
    """东财龙虎榜明细 -> lhb_daily。df 为 akshare 中文列名。"""
    rows = []
    for _, r in df.iterrows():
        code = str(r.get("代码") or "").strip()
        if not code.isdigit():
            continue
        rows.append((
            trade_date, code, r.get("名称"), _num(r.get("收盘价")), _num(r.get("涨跌幅")),
            _num(r.get("龙虎榜净买额")), _num(r.get("龙虎榜买入额")), _num(r.get("龙虎榜卖出额")),
            _num(r.get("龙虎榜成交额")), _num(r.get("净买额占总成交比")),
            _num(r.get("换手率")), _num(r.get("流通市值")),
            str(r.get("上榜原因") or ""), str(r.get("解读") or ""),
        ))
    c = _conn()
    try:
        c.executemany(
            "INSERT OR REPLACE INTO lhb_daily (trade_date, code, name, close, change_pct, "
            "net_buy, buy_amount, sell_amount, total_amount, ratio_in_total, "
            "turnover_rate, float_mv, reason, interp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        c.commit()
        return len(rows)
    finally:
        c.close()


def query_lhb(trade_date: str, min_net_buy: float = 0.0, limit: int = 20) -> list[dict]:
    """按日查龙虎榜，净买额降序。min_net_buy 单位元。"""
    c = _conn()
    try:
        rows = c.execute(
            "SELECT code, name, close, change_pct, net_buy, buy_amount, sell_amount, "
            "ratio_in_total, turnover_rate, reason, interp FROM lhb_daily "
            "WHERE trade_date=? AND net_buy>=? ORDER BY net_buy DESC LIMIT ?",
            (trade_date, min_net_buy, limit),
        ).fetchall()
        return [
            {"code": r[0], "name": r[1], "close": r[2], "change_pct": r[3],
             "net_buy": r[4], "buy_amount": r[5], "sell_amount": r[6],
             "ratio_in_total": r[7], "turnover_rate": r[8],
             "reason": r[9], "interp": r[10]}
            for r in rows
        ]
    finally:
        c.close()


def lhb_dates(limit: int = 30) -> list[str]:
    c = _conn()
    try:
        return [r[0] for r in c.execute(
            "SELECT DISTINCT trade_date FROM lhb_daily ORDER BY trade_date DESC LIMIT ?",
            (limit,),
        ).fetchall()]
    finally:
        c.close()


def query_lhb_by_code(code: str, days: int = 30) -> list[dict]:
    """按代码查个股近 N 天龙虎榜上榜史，日期降序。"""
    c = _conn()
    try:
        rows = c.execute(
            "SELECT trade_date, name, close, change_pct, net_buy, buy_amount, "
            "sell_amount, ratio_in_total, turnover_rate, reason, interp "
            "FROM lhb_daily WHERE code=? AND trade_date>=date('now', ?) "
            "ORDER BY trade_date DESC",
            (code, f"-{days} day"),
        ).fetchall()
        return [
            {"trade_date": r[0], "name": r[1], "close": r[2], "change_pct": r[3],
             "net_buy": r[4], "buy_amount": r[5], "sell_amount": r[6],
             "ratio_in_total": r[7], "turnover_rate": r[8],
             "reason": r[9], "interp": r[10]}
            for r in rows
        ]
    finally:
        c.close()


# ---- earnings_forecast（业绩预告） -----------------------------------------------------


def upsert_earnings_forecast(df: "pd.DataFrame") -> int:
    """东财业绩预告 -> earnings_forecast。df 列: 股票代码/报告期或公告日期等。"""
    rows = []
    for _, r in df.iterrows():
        code = str(r.get("股票代码") or "").strip()
        if not code.isdigit():
            continue
        rd = str(r.get("最新报告期") or r.get("报告期") or "")[:10]
        if not rd:
            # 无报告期列时用公告日期所在季度末近似
            ad = str(r.get("公告日期") or "")
            rd = f"{ad[:4]}-06-30" if ad and ad[5:7] <= "06" else f"{ad[:4]}-12-31" if ad else ""
        if not rd:
            continue
        rows.append((
            code, rd, r.get("股票简称"), r.get("预测指标"), r.get("预告类型"),
            _num(str(r.get("业绩变动幅度") or "").replace("%", "")),
            _num(r.get("预测数值")), str(r.get("业绩变动原因") or "")[:300],
            str(r.get("公告日期") or "")[:10], _now(),
        ))
    c = _conn()
    try:
        c.executemany(
            "INSERT OR REPLACE INTO earnings_forecast (code, report_date, name, indicator, "
            "forecast_type, change_pct, forecast_value, reason, announce_date, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        c.commit()
        return len(rows)
    finally:
        c.close()


def query_earnings_forecast(code: str | None = None, ftype: str | None = None,
                            min_change: float | None = None, limit: int = 20) -> list[dict]:
    """查业绩预告：按个股 / 按类型（预增/预减/扭亏...）/ 按最低变动幅度。"""
    c = _conn()
    try:
        sql = ("SELECT code, name, report_date, indicator, forecast_type, change_pct, "
               "forecast_value, reason, announce_date FROM earnings_forecast")
        conds: list[str] = []
        params: list[Any] = []
        if code:
            conds.append("code=?")
            params.append(code)
        if ftype:
            conds.append("forecast_type LIKE ?")
            params.append(f"%{ftype}%")
        if min_change is not None:
            conds.append("change_pct>=?")
            params.append(min_change)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY change_pct DESC LIMIT ?"
        params.append(limit)
        rows = c.execute(sql, params).fetchall()
        return [
            {"code": r[0], "name": r[1], "report_date": r[2], "indicator": r[3],
             "forecast_type": r[4], "change_pct": r[5], "forecast_value": r[6],
             "reason": r[7], "announce_date": r[8]}
            for r in rows
        ]
    finally:
        c.close()


def migrate_index_from_quotes(index_names: dict[str, str]) -> dict:
    """一次性迁移：quotes_daily 中的大盘指数 → index_daily，迁移后删除旧行。

    写库 SQL 唯一入口在本模块；index_names: {code: name}（由 refresh 提供）。
    """
    keys = list(index_names.keys())
    ph = ",".join("?" * len(keys))
    c = _conn()
    try:
        rows = c.execute(
            f"SELECT code, trade_date, open, high, low, close, volume, amount, "
            f"change_pct FROM quotes_daily WHERE code IN ({ph})", keys,
        ).fetchall()
        if not rows:
            return {"migrated": 0, "hint": "quotes_daily 中无指数数据（可能已迁移）"}
        c.executemany(
            "INSERT OR REPLACE INTO index_daily (code, name, category, trade_date, "
            "open, high, low, close, volume, amount, change_pct, source, updated_at) "
            "VALUES (?, ?, 'broad', ?, ?, ?, ?, ?, ?, ?, ?, 'migrated', datetime('now'))",
            [(r[0], index_names[r[0]], *r[1:]) for r in rows],
        )
        c.execute(f"DELETE FROM quotes_daily WHERE code IN ({ph})", keys)
        c.commit()
        return {"migrated": len(rows), "deleted_from_quotes": len(rows)}
    finally:
        c.close()
