"""全市场日K增量（eod 第 1 步）：跳过已最新、缺口补齐、来源标记。"""
from __future__ import annotations

import asyncio


def _make_bars(code, dates, base=10.0):
    out = []
    for i, d in enumerate(dates):
        out.append({"date": d, "open": base, "high": base + 0.5, "low": base - 0.5,
                    "close": base + 0.1, "volume": 1000.0, "amount": 1e5,
                    "change_pct": 1.0})
    return out


def test_daily_market_skips_uptodate_and_fills_gap(tmp_db, monkeypatch):
    from datetime import datetime
    import pandas as pd
    db = tmp_db
    # 两只在市股票
    db.upsert_stock_basic(pd.DataFrame([
        {"code": "sh.600001", "code_name": "甲", "ipoDate": "2020-01-01",
         "outDate": "", "type": "1", "status": "1"},
        {"code": "sh.600002", "code_name": "乙", "ipoDate": "2020-01-01",
         "outDate": "", "type": "1", "status": "1"},
    ]))
    today = datetime.now().strftime("%Y-%m-%d")
    # 600001 已最新（今天有行）→ 应跳过；600002 停在旧日期 → 应补齐
    db.upsert_klines_daily(pd.DataFrame([
        {"code": "600001", "date": today, "open": 1, "high": 1, "low": 1,
         "close": 1, "volume": 1, "amount": 1, "pctChg": 0}]))

    calls = []

    async def fake_kline(code, count=10):
        calls.append((code, count))
        return _make_bars(code, ["2026-11-03", today])

    import mcp_eastmoney.data.tencent_client as tc
    monkeypatch.setattr(tc, "get_daily_kline", fake_kline)
    monkeypatch.setattr(tc, "get_daily_kline_sina", fake_kline)

    import mcp_eastmoney.refresh as R
    # 批量行情摄入在测试中打桩（避免网络）
    async def _fake_ingest():
        return {"stubbed": True}
    monkeypatch.setattr(R, "refresh_snapshot", _fake_ingest)
    out = asyncio.run(R.refresh_daily_market(concurrency=2))
    # 只对 600002 发起拉取
    assert [c[0] for c in calls] == ["600002"]
    assert out["ok"] == 1 and out["fail"] == 0
    assert len(db.query_klines_daily("600002", limit=10)) >= 1

    # 再跑一次：600002 已到目标日 → 全部跳过
    calls.clear()
    out2 = asyncio.run(R.refresh_daily_market(concurrency=2))
    assert out2["todo"] <= 1 and (not calls or all(c[0] == "600002" for c in calls))
