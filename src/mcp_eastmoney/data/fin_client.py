"""财务指标客户端 — 新浪源（ak.stock_financial_analysis_indicator）。

免 API Key、覆盖全 A 股、按报告期返回（2023 起拉取足够近 8 期）。
落库 financial_indicators，字段映射见 db._FIN_COLS。
"""
from __future__ import annotations

import asyncio
import os

os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

import akshare as ak
import pandas as pd


async def financial_indicators(code: str, start_year: str = "2023") -> pd.DataFrame:
    """新浪财务指标原始 DataFrame（中文列名）。"""
    def _sync():
        return ak.stock_financial_analysis_indicator(symbol=code, start_year=start_year)

    return await asyncio.to_thread(_sync)
