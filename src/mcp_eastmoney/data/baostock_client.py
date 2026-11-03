"""Baostock 客户端 — 稳定历史数据（基础信息 / 行业 / 日K / 15分钟K）。

已知坑的规避（详见 docs/数据层实施方案.md）：
1. rs.get_data() 在 pandas 2.0+ 崩溃 -> 手动收集行数据
2. Baostock 非线程安全 -> 每次调用独立 login/logout
3. 同步库 -> asyncio.to_thread 包装，避免阻塞 MCP 事件循环
4. 返回全为字符串 -> db 层 _num()/_int() 转换
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Callable

import baostock as bs
import pandas as pd

from .codes import from_bs, normalize, to_bs


def _rs_to_df(rs: Any) -> pd.DataFrame:
    """Baostock 结果集转 DataFrame（pandas 2.0+ 兼容）。"""
    rows = []
    while (rs.error_code == "0") and rs.next():
        rows.append(rs.get_row_data())
    return pd.DataFrame(rows, columns=rs.fields)


def _run(fn: Callable[[], Any]) -> asyncio.Future:
    """独立 login/logout 循环 + 线程池执行。登录失败重试（Baostock 服务端偶发抖动）。"""
    import time as _time

    def _sync() -> Any:
        for attempt in range(3):
            lg = bs.login()
            if lg.error_code == "0":
                break
            if attempt == 2:
                raise RuntimeError(f"baostock login failed: {lg.error_msg}")
            _time.sleep(1.0 + attempt)
        try:
            return fn()
        finally:
            bs.logout()

    return asyncio.to_thread(_sync)


def _check(rs: Any) -> Any:
    if rs.error_code != "0":
        raise RuntimeError(f"baostock error {rs.error_code}: {rs.error_msg}")
    return rs


async def query_stock_basic(code: str | None = None) -> pd.DataFrame:
    """证券基本资料。code 可为 6 位码或 Baostock 格式；为空返回全量（约 5000 条）。"""
    bs_code = to_bs(code) if code else ""

    def _sync() -> pd.DataFrame:
        rs = _check(bs.query_stock_basic(code=bs_code or None))
        return _rs_to_df(rs)

    return await _run(_sync)


async def query_stock_industry(code: str | None = None) -> pd.DataFrame:
    """行业分类（证监会）。Baostock 每周一更新。code 为空返回全量。"""
    bs_code = to_bs(code) if code else ""

    def _sync() -> pd.DataFrame:
        rs = _check(bs.query_stock_industry(code=bs_code or None))
        return _rs_to_df(rs)

    return await _run(_sync)


_DAILY_FIELDS = (
    "date,code,open,high,low,close,preclose,volume,amount,turn,tradestatus,pctChg,"
    "peTTM,pbMRQ,psTTM"
)
_MIN_FIELDS = "time,code,open,high,low,close,volume,amount,adjustflag"


async def query_kline(
    code: str,
    frequency: str = "d",
    start: str = "2024-01-01",
    end: str | None = None,
    adjust: str = "2",
) -> pd.DataFrame:
    """历史 K 线。

    frequency: 'd' 日K（可回补多年） / '15' 15分钟线（回溯窗口仅约几个月，只能增量落）
    adjust: '1' 后复权 '2' 前复权(默认，适合技术分析) '3' 不复权
    """
    bs_code = to_bs(code)
    fields = _DAILY_FIELDS if frequency == "d" else _MIN_FIELDS

    def _sync() -> pd.DataFrame:
        rs = _check(bs.query_history_k_data_plus(
            bs_code, fields,
            start_date=start,
            end_date=end or datetime.now().strftime("%Y-%m-%d"),
            frequency=frequency,
            adjustflag=adjust,
        ))
        return _rs_to_df(rs)

    return await _run(_sync)


async def trade_dates(start: str, end: str) -> list[str]:
    """区间内的交易日列表（补跑判断用）。"""

    def _sync() -> list[str]:
        rs = _check(bs.query_trade_dates(start_date=start, end_date=end))
        df = _rs_to_df(rs)
        return df[df["is_trading_day"] == "1"]["calendar_date"].tolist()

    return await _run(_sync)


__all__ = [
    "query_stock_basic", "query_stock_industry", "query_kline", "trade_dates",
    "to_bs", "from_bs", "normalize",
]
