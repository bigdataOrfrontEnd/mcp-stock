"""新闻舆情客户端 — 多源采集（个股新闻 + 三源全球快讯）。

数据源（2026-09 本机实测可用）：
- ak.stock_news_em(symbol)        东财个股新闻 Top10
- ak.stock_info_global_cls()      财联社电报快讯（最新一页 ~20 条）
- ak.stock_info_global_em()       东财全球财经快讯（~200 条/次）
- ak.stock_info_global_sina()     新浪财经环球快讯（~20 条）

借鉴 StockAgent 的多源新闻采集框架（collectors/news/sources/）：采集与存储
分离、多源注册可扩展、统一 scope 落库，来源用 source 列区分。
AkShare 同步库 → asyncio.to_thread 包装。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

import akshare as ak


def _t(fn, /, *args, **kwargs):
    return asyncio.to_thread(fn, *args, **kwargs)


async def stock_news(code: str) -> list[dict]:
    """东财个股新闻。返回标准化 rows（供 db.upsert_news）。"""

    def _sync() -> list[dict]:
        df = ak.stock_news_em(symbol=code)
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "url": str(r.get("新闻链接") or "").strip(),
                "scope": f"stock:{code}",
                "code": code,
                "title": str(r.get("新闻标题") or "").strip(),
                "content": str(r.get("新闻内容") or "").strip()[:500],
                "source": str(r.get("文章来源") or "东财"),
                "pub_time": str(r.get("发布时间") or "").strip(),
            })
        return [r for r in rows if r["url"] and r["title"]]

    return await asyncio.to_thread(_sync)


# ---- 市场快讯多源采集器（统一注册，可扩展） -------------------------------------------


async def _fetch_cls() -> list[dict]:
    """财联社电报。列: 标题, 内容, 发布日期, 发布时间"""

    def _sync() -> list[dict]:
        df = ak.stock_info_global_cls(symbol="全部")
        rows = []
        for _, r in df.iterrows():
            d = str(r.get("发布日期") or "").strip()
            t = str(r.get("发布时间") or "").strip()
            title = str(r.get("标题") or "").strip()
            rows.append({
                "url": f"cls:{d} {t} {title[:50]}",  # 无独立链接，时间+标题做去重键
                "scope": "market",
                "code": None,
                "title": title,
                "content": str(r.get("内容") or "").strip()[:500],
                "source": "财联社电报",
                "pub_time": f"{d} {t}".strip() or None,
            })
        return [r for r in rows if r["title"]]

    return await asyncio.to_thread(_sync)


async def _fetch_em_global() -> list[dict]:
    """东财全球财经快讯。列: 标题, 摘要, 发布时间, 链接"""

    def _sync() -> list[dict]:
        df = ak.stock_info_global_em()
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "url": str(r.get("链接") or "").strip(),
                "scope": "market",
                "code": None,
                "title": str(r.get("标题") or "").strip(),
                "content": str(r.get("摘要") or "").strip()[:500],
                "source": "东财全球",
                "pub_time": str(r.get("发布时间") or "").strip(),
            })
        return [r for r in rows if r["url"] and r["title"]]

    return await asyncio.to_thread(_sync)


async def _fetch_sina_global() -> list[dict]:
    """新浪财经环球快讯。列: 时间, 内容"""

    def _sync() -> list[dict]:
        df = ak.stock_info_global_sina()
        rows = []
        for _, r in df.iterrows():
            t = str(r.get("时间") or "").strip()
            content = str(r.get("内容") or "").strip()
            rows.append({
                "url": f"sina:{t} {content[:60]}",
                "scope": "market",
                "code": None,
                "title": content[:60],  # 无标题列，取内容前段
                "content": content[:500],
                "source": "新浪财经",
                "pub_time": t or None,
            })
        return [r for r in rows if r["content"]]

    return await asyncio.to_thread(_sync)


# 源注册表：新源加一项即可被 refresh_news / get_news 覆盖
MARKET_SOURCES: dict[str, Any] = {
    "cls": {"fetch": _fetch_cls, "label": "财联社电报"},
    "em": {"fetch": _fetch_em_global, "label": "东财全球"},
    "sina": {"fetch": _fetch_sina_global, "label": "新浪财经"},
}


async def market_news(sources: list[str] | None = None,
                      per_source: int = 20) -> list[dict]:
    """聚合多源市场快讯（单源失败不阻断），按 pub_time 倒序、url 去重。"""
    names = sources or list(MARKET_SOURCES)
    rows: list[dict] = []
    for name in names:
        src = MARKET_SOURCES.get(name)
        if not src:
            continue
        try:
            rows.extend(await src["fetch"]())
        except Exception:
            continue  # 单源失败不阻断
        await asyncio.sleep(0.3)  # 限速
    # 去重 + 排序
    seen, uniq = set(), []
    for r in rows:
        if r["url"] not in seen:
            seen.add(r["url"])
            uniq.append(r)
    uniq.sort(key=lambda r: r.get("pub_time") or "", reverse=True)
    return uniq[: per_source * max(len(names), 1)]
