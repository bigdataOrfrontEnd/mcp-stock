"""代码格式统一 — SQLite 内部统一存 6 位纯数字码。

边界转换规则：
- Baostock 用 `sh.600519` 格式
- 东财 secid 用 `1.600519` 格式
- 本项目库内与 MCP 工具入参统一用 6 位码 `600519`
"""
from __future__ import annotations

import re

_CODE_RE = re.compile(r"^(\d{6})$")


def normalize(code: str) -> str:
    """任意输入 -> 6 位码。去前缀、去空格；非法输入抛 ValueError。"""
    c = str(code).strip().lower()
    if "." in c:
        c = c.split(".", 1)[1]
    c = c.zfill(6) if c.isdigit() and len(c) <= 6 else c
    if not _CODE_RE.match(c):
        raise ValueError(f"非法股票代码: {code!r}（期望 6 位数字，如 600519）")
    return c


def to_bs(code: str) -> str:
    """'600519' -> 'sh.600519'（Baostock 格式）。已是 sh./sz. 格式（如指数）直接返回。"""
    c = str(code).strip().lower()
    if "." in c:
        return c
    c = normalize(c)
    prefix = "sh" if market_prefix(c) == "1" else "sz"
    return f"{prefix}.{c}"


def from_bs(code: str) -> str:
    """'sh.600519' -> '600519'"""
    return normalize(code)


def to_secid(code: str) -> str:
    """'600519' -> '1.600519'（东财 secid 格式）"""
    c = normalize(code)
    return f"{market_prefix(c)}.{c}"


def market_prefix(code: str) -> str:
    """Return 0 (深市) or 1 (沪市) prefix for a 6-digit A-share code."""
    code = str(code).strip()
    if "." in code:  # 容错 sh.600519
        code = code.split(".", 1)[1]
    if code.startswith(("60", "68", "11", "13", "5")):
        return "1"  # 沪市 主板/科创/转债/ETF
    return "0"  # 深市 主板/创业板/北交所
