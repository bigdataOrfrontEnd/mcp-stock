"""AkShare / 同花顺 客户端 — 概念板块列表、成分股、个股热度。

数据源现状（2026-09 本机网络实测）：
- 东财 push2 全系 host 被应用层断连（clist/quote/kline），走东财源的接口不可用
- 同花顺源（10jqka.com.cn）可用：概念列表 375 个
- 同花顺概念成分页 q.10jqka.com.cn 无 cookie 只有第一页（每概念 10 只，按涨跌幅
  排序 = 当日最活跃成分），完整分页需 hexin-v 反爬 cookie，暂不实现 → 成分快照
  标记 partial
- 东财 datacenter host 可用：个股热度榜 stock_hot_rank_em 正常

注意：
- AkShare 为同步库，全部 asyncio.to_thread 包装
- import 前禁用系统代理（Clash Verge 等会干扰国内财经接口）
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Any

# 必须在 import akshare 之前设置
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

import akshare as ak
import httpx
import pandas as pd

THS_MEMBER_URL = "http://q.10jqka.com.cn/gn/detail/code/{code}/"  # 带 field/order 路径会被 401
_THS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "http://q.10jqka.com.cn/gn/",
}


def _t(fn, /, *args, **kwargs):
    return asyncio.to_thread(fn, *args, **kwargs)


# ---- 概念列表（同花顺源） ---------------------------------------------------------


async def concept_list() -> pd.DataFrame:
    """同花顺概念板块列表。列: name, code（如 AI制药 / 308614）。"""
    return await _t(ak.stock_board_concept_name_ths)


async def concept_summary() -> pd.DataFrame:
    """同花顺概念板块总结（含驱动事件，用于'新闻→概念'场景）。列: 日期, 概念名称, 驱动事件, 龙头股, 成分股数量。"""
    return await _t(ak.stock_board_concept_summary_ths)


# ---- 概念成分股（同花顺页面，部分快照） ----------------------------------------------

_MEMBER_ROW = re.compile(r'<td><a[^>]*>(\d{6})</a></td>\s*<td><a[^>]*>([^<]+)</a></td>')


async def concept_cons(concept_code: str) -> dict:
    """同花顺概念成分股（无 cookie 首页，按涨跌幅排序的 Top 10 活跃成分）。

    Returns:
        {"members": [{"code","name"},...], "partial": True, "note": "..."}
    """
    def _sync() -> dict:
        url = THS_MEMBER_URL.format(code=concept_code)
        r = httpx.get(url, headers=_THS_HEADERS, trust_env=False,
                      timeout=15, follow_redirects=True)
        if r.status_code != 200:
            raise RuntimeError(f"THS 页面请求失败: HTTP {r.status_code}")
        rows = [{"code": c, "name": n.strip()} for c, n in _MEMBER_ROW.findall(r.text)]
        if not rows:
            raise RuntimeError("THS 页面未解析到成分股（页面结构可能变更）")
        return {"members": rows, "partial": True,
                "note": "无cookie仅首页Top10（按涨跌幅排序的活跃成分），完整成分待升级"}

    return await asyncio.to_thread(_sync)


# ---- 个股热度（东财 datacenter host，可用） ------------------------------------------


async def hot_rank(top_n: int = 100) -> pd.DataFrame:
    """东财个股人气榜。列: 当前排名, 代码, 股票名称, 最新价, 涨跌幅, ..."""
    df = await _t(ak.stock_hot_rank_em)
    return df.head(top_n)


async def zt_pool(date: str) -> pd.DataFrame:
    """东财涨停池。date 格式 YYYYMMDD。列: 代码, 名称, 涨跌幅, 最新价, 成交额, 换手率,
    封板资金, 首次封板时间, 最后封板时间, 炸板次数, 涨停统计, 连板数, 所属行业。"""
    return await _t(ak.stock_zt_pool_em, date=date)


# ---- 个股板块/概念（东财 F10 emweb host，可用） ---------------------------------------

_EMWEB_URL = "https://emweb.securities.eastmoney.com/PC_HSF10/CoreConception/PageAjax?code={secucode}"
_EMWEB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Referer": "https://emweb.securities.eastmoney.com/",
}


async def core_conception(code: str) -> dict:
    """东财 F10 核心题材：完整所属板块（行业链 + 地域 + 概念标签）。

    Returns:
        {"industry": [...行业链], "region": str|None, "concepts": [板块名...]}
    """
    secucode = f"{code}.{'SH' if code.startswith(('6', '5')) else 'SZ'}"

    def _sync() -> dict:
        r = httpx.get(_EMWEB_URL.format(secucode=secucode),
                      headers=_EMWEB_HEADERS, trust_env=False, timeout=15)
        r.raise_for_status()
        ssbk = r.json().get("ssbk") or []
        # BOARD_RANK 1-3 为行业链（一级/二级/三级），其余按语义归类
        industry, region, concepts = [], None, []
        for b in sorted(ssbk, key=lambda x: x.get("BOARD_RANK") or 99):
            name = b.get("BOARD_NAME")
            rank = b.get("BOARD_RANK") or 99
            if rank <= 3 and name:
                industry.append(name)
            elif name and name.endswith("板块") and len(name) <= 5:
                region = name
            elif name:
                concepts.append(name)
        return {"industry": industry, "region": region, "concepts": concepts}

    return await asyncio.to_thread(_sync)


# ---- 龙虎榜 / 业绩预告 / 同花顺行业指数（2026-09 实测可用，datacenter+THS 源） ------------


async def lhb_detail(start: str, end: str) -> pd.DataFrame:
    """东财龙虎榜个股明细（区间）。start/end: YYYYMMDD。"""
    return await _t(ak.stock_lhb_detail_em, start_date=start, end_date=end)


async def earnings_forecast(period: str) -> pd.DataFrame:
    """东财业绩预告。period: 报告期 YYYYMMDD（如 20260630）。"""
    return await _t(ak.stock_yjyg_em, date=period)


async def industry_index_list() -> pd.DataFrame:
    """同花顺行业板块列表。列: name, code。"""
    return await _t(ak.stock_board_industry_name_ths)


async def industry_index_kline(symbol: str, start: str, end: str) -> pd.DataFrame:
    """同花顺行业指数日K。symbol: 行业名（如 '半导体'）。列: 日期/开盘价/.../成交额。"""
    return await _t(ak.stock_board_industry_index_ths, symbol=symbol,
                    start_date=start, end_date=end)


# ---- 板块资金流（同花顺源；东财 push2 断连时的 fallback） ---------------------------


def _fnum(v) -> float | None:
    """安全数值转换（'--'/空/异常 → None）。"""
    try:
        s = str(v).replace(",", "").strip()
        if not s or s in {"--", "-", "nan", "None"}:
            return None
        return float(s)
    except (TypeError, ValueError):
        return None


async def sector_flow_raw(kind: str = "industry") -> list[dict]:
    """同花顺行业/概念资金流（即时），对齐东财 sector_flow_raw 的行结构。

    口径差异（重要）：
    - "净额"为**全单口径**（无主力/超大/大/中/小单分解）→ 只填 main_net，
      分解字段留 None；单位亿 → 换算成元（与东财一致）
    - 同花顺无 BK 板块代码 → code 用行业/概念名（幂等键仍成立；
      行业名与 sector_rotation 的同花顺行业体系一致，排名标注可直接对上）
    """
    def _sync() -> list[dict]:
        df = (ak.stock_fund_flow_industry("即时") if kind == "industry"
              else ak.stock_fund_flow_concept("即时"))
        out: list[dict] = []
        for _, r in df.iterrows():
            name = str(r.get("行业") or "").strip()
            if not name:
                continue
            net = _fnum(r.get("净额"))
            out.append({
                "code": name, "name": name,
                "index_value": _fnum(r.get("行业指数")),
                "change_pct": _fnum(r.get("行业-涨跌幅")),
                "main_net": net * 1e8 if net is not None else None,
                "main_net_pct": None,
                "super_large_net": None, "super_large_pct": None,
                "large_net": None, "large_pct": None,
                "medium_net": None, "medium_pct": None,
                "small_net": None, "small_pct": None,
                "leader_name": str(r.get("领涨股") or "") or None,
                "leader_code": None,
                "data_ts": None,
            })
        return out

    return await _t(_sync)
