"""腾讯/新浪行情客户端 — 免鉴权、当日数据即时可得（Baostock 盘后 17:30+ 才发布的兜底源）。

本机网络实测（2026-09-28）：
- 腾讯实时行情 qt.gtimg.cn/q=sh600519            ✅（GBK 编码）
- 腾讯日K web.ifzq.gtimg.cn fqkline（个股+指数，qfq）✅ 收盘后即时含当日
- 新浪15分钟 quotes.sina.cn scale=15               ✅ 含当日（带成交额）
- 东财 push2 全系仍被断连，本客户端就是它的替代

职责：实时行情、当日日K兜底、当日15分钟兜底。落库仍走 db.py。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

import httpx

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Referer": "https://gu.qq.com/",
}


def _symbol(code: str) -> str:
    """'600519' / 'sh.000001' -> 'sh600519'。"""
    c = str(code).strip().lower()
    if "." in c:
        p, n = c.split(".", 1)
        return f"{p}{n}"
    p = "sh" if c.startswith(("6", "5", "9")) else "sz"
    return f"{p}{c}"


async def get_daily_kline(code: str, count: int = 320) -> list[dict]:
    """个股前复权日K（腾讯 fqkline，单只 ~0.1s，全市场回填用）。

    返回升序 [{date, open, high, low, close, volume(股), amount(估算), change_pct}]。
    行格式: [日期, 开, 收, 高, 低, 量(手)]；amount 用 close×量估算（腾讯日K不带成交额）。
    """
    c = str(code).strip().lower()
    if "." in c:
        sym = "".join(c.split("."))
    elif c.startswith(("6", "5", "9")):
        sym = "sh" + c
    elif c.startswith(("4", "8")):
        sym = "bj" + c
    else:
        sym = "sz" + c
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    async with _client() as cli:
        r = await cli.get(url, params={"param": f"{sym},day,,,{count},qfq"})
        r.raise_for_status()
        d = r.json()
    node = (d.get("data") or {}).get(sym) or {}
    rows = node.get("qfqday") or node.get("day") or []
    out: list[dict] = []
    prev_close = None
    for row in rows:
        try:
            date, o, cl, hi, lo, vol = row[0], float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])
        except (IndexError, TypeError, ValueError):
            continue
        chg = round((cl / prev_close - 1) * 100, 2) if prev_close else 0.0
        prev_close = cl
        vol_shares = vol * 100  # 手 → 股（与 Baostock 对齐）
        out.append({"date": date, "open": o, "high": hi, "low": lo, "close": cl,
                    "volume": vol_shares, "amount": round(cl * vol_shares, 2),
                    "change_pct": chg})
    return out


async def get_daily_kline_sina(code: str, count: int = 320) -> list[dict]:
    """个股日K（新浪 jsonp，不复权）。腾讯被限流时的备用源。

    返回升序 [{date, open, high, low, close, volume(股), amount(估算), change_pct}]。
    """
    c = str(code).strip().lower()
    if "." in c:
        sym = "".join(c.split("."))
    elif c.startswith(("6", "5", "9")):
        sym = "sh" + c
    elif c.startswith(("4", "8")):
        sym = "bj" + c
    else:
        sym = "sz" + c
    url = "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_d/CN_MarketDataService.getKLineData"
    async with _client() as cli:
        r = await cli.get(url, params={"symbol": sym, "scale": "240",
                                       "ma": "no", "datalen": str(count)})
        r.raise_for_status()
        text = r.text
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end < 0:
        return []
    import json
    raw = json.loads(text[start:end + 1])
    out: list[dict] = []
    prev = None
    for row in raw:
        try:
            o, h, l, cl = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
            vol = float(row["volume"])
        except (KeyError, TypeError, ValueError):
            continue
        chg = round((cl / prev - 1) * 100, 2) if prev else 0.0
        prev = cl
        out.append({"date": row["day"][:10], "open": o, "high": h, "low": l, "close": cl,
                    "volume": vol, "amount": round(cl * vol, 2), "change_pct": chg})
    return out


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers=_HEADERS, trust_env=False, timeout=10)


# ---- 实时行情 -----------------------------------------------------------------------


async def get_quote(code: str) -> dict:
    """腾讯实时行情（个股/指数/ETF 通用）。字段对齐原 eastmoney.get_quote。"""
    sym = _symbol(code)

    def _sync() -> dict:
        with httpx.Client(headers=_HEADERS, trust_env=False, timeout=10) as c:
            r = c.get(f"https://qt.gtimg.cn/q={sym}")
            r.raise_for_status()
            text = r.content.decode("gbk", errors="replace")
        body = text.split('="', 1)[1].rsplit('"', 1)[0]
        f = body.split("~")
        if len(f) < 45 or not f[3]:
            return {"error": f"无行情数据: {code}"}
        cur, prev = float(f[3]), float(f[4])
        return {
            "code": f[2], "name": f[1],
            "price": cur,
            "change": round(cur - prev, 2),
            "change_pct": round((cur - prev) / prev * 100, 2) if prev else 0.0,
            "open": float(f[5]), "high": float(f[33]), "low": float(f[34]),
            "prev_close": prev,
            "volume": int(float(f[6])),          # 手
            "amount": float(f[37]) * 1e4 if f[37] else None,  # 万元 -> 元
            "turnover_rate": float(f[38]) if f[38] else None,
            "pe": float(f[39]) if f[39] else None,
            "time": f[30],
            "source": "tencent",
        }

    return await asyncio.to_thread(_sync)


# ---- 批量行情（全市场快照采集用） ------------------------------------------------------


def _f(f: list[str], i: int) -> float | None:
    try:
        v = f[i]
        return float(v) if v not in ("", "-") else None
    except (IndexError, ValueError):
        return None


async def get_batch_quotes(codes: list[str], batch: int = 60) -> list[dict]:
    """腾讯批量实时行情（~60只/请求）。返回 [{code,name,price,change_pct,...}]。

    额外字段（快照表用）：volume_ratio 量比 / amplitude 振幅 / total_mv 总市值(亿) /
    float_mv 流通市值(亿) / pb 市净率。单批失败不阻断。
    """

    def _sync_batch(syms: list[str]) -> list[dict]:
        with httpx.Client(headers=_HEADERS, trust_env=False, timeout=15) as c:
            r = c.get("https://qt.gtimg.cn/q=" + ",".join(syms))
            r.raise_for_status()
        text = r.content.decode("gbk", errors="replace")
        out = []
        for chunk in text.strip().split(";"):
            if '="' not in chunk:
                continue
            f = chunk.split('="', 1)[1].rsplit('"', 1)[0].split("~")
            if len(f) < 50 or not f[3]:
                continue  # 停牌/无数据
            price, prev = _f(f, 3), _f(f, 4)
            if price is None or prev in (None, 0):
                continue
            out.append({
                "code": f[2], "name": f[1],
                "price": price, "prev_close": prev,
                "change_pct": round((price - prev) / prev * 100, 2),
                "open": _f(f, 5), "high": _f(f, 33), "low": _f(f, 34),
                "volume": _f(f, 6),                    # 手
                "amount": (_f(f, 37) or 0) * 1e4,       # 万元 -> 元
                "turnover_rate": _f(f, 38),
                "pe": _f(f, 39),
                "amplitude": _f(f, 43),
                "float_mv": _f(f, 44),                 # 亿
                "total_mv": _f(f, 45),                 # 亿
                "pb": _f(f, 46),
                "volume_ratio": _f(f, 49),
            })
        return out

    out: list[dict] = []
    for i in range(0, len(codes), batch):
        syms = [_symbol(c) for c in codes[i:i + batch]]
        try:
            out.extend(await asyncio.to_thread(_sync_batch, syms))
        except Exception:
            continue  # 单批失败不阻断
        await asyncio.sleep(0.15)  # 限速
    return out


# ---- 日K（收盘后即时含当日） ----------------------------------------------------------


async def get_kline_daily(code: str, limit: int = 30, qfq: bool = True) -> list[dict]:
    """腾讯日K。返回升序 [{date, open, close, high, low, volume(手)}]，含当日（收盘后）。"""
    sym = _symbol(code)
    key = "qfqday" if qfq else "day"

    def _sync() -> list[dict]:
        with httpx.Client(headers=_HEADERS, trust_env=False, timeout=10) as c:
            r = c.get(f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                      f"?param={sym},day,,,{limit},{'qfq' if qfq else ''}")
            r.raise_for_status()
            d = (r.json().get("data") or {}).get(sym) or {}
        rows = d.get(key) or d.get("day") or []
        out = []
        for x in rows:
            out.append({
                "date": x[0], "open": float(x[1]), "close": float(x[2]),
                "high": float(x[3]), "low": float(x[4]), "volume": float(x[5]),
            })
        return out

    return await asyncio.to_thread(_sync)


# ---- 15分钟线（新浪，含当日） ---------------------------------------------------------


async def get_kline_15m(code: str, datalen: int = 500) -> list[dict]:
    """新浪15分钟线。返回升序 [{datetime, open, high, low, close, volume, amount}]。"""

    def _sync() -> list[dict]:
        with httpx.Client(headers={"User-Agent": _HEADERS["User-Agent"],
                                   "Referer": "https://finance.sina.com.cn"},
                          trust_env=False, timeout=10) as c:
            r = c.get("https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData",
                      params={"symbol": _symbol(code), "scale": 15, "ma": "no",
                              "datalen": datalen})
            r.raise_for_status()
            data = r.json()
        return [
            {"datetime": x["day"], "open": float(x["open"]), "high": float(x["high"]),
             "low": float(x["low"]), "close": float(x["close"]),
             "volume": float(x["volume"]), "amount": float(x.get("amount") or 0)}
            for x in data
        ]

    return await asyncio.to_thread(_sync)
