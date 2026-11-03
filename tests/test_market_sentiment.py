"""market_sentiment 情绪定量标签测试。

用真实口径的涨停池快照回放，验证判定规则与人工复盘定性一致。
"""
import asyncio
import json

import pandas as pd

from mcp_eastmoney import server
from mcp_eastmoney.data import db


def _seed(rows):
    """写入测试用涨停池快照：rows = [(date, code, lian_ban, blast_count), ...]。"""
    for date, code, lian, blast in rows:
        df = pd.DataFrame([{
            "代码": code, "名称": f"股{code}", "涨跌幅": 10.0, "最新价": 10.0,
            "成交额": 1e8, "换手率": 5.0, "首次封板时间": "093000",
            "炸板次数": blast, "涨停统计": f"{lian}/{lian}", "连板数": lian,
            "所属行业": "测试",
        }])
        db.upsert_zt_pool(df, date)


def test_daily_stats_and_labels(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    db.init_db()

    # 两日对比：总数 50→37、二板以上 16→7 双降 → 退潮期（2026-10-22 实况）
    day1 = [("2026-10-21", f"6{i:05d}", 2 if i < 16 else 1, 0) for i in range(50)]
    day2 = [("2026-10-22", f"7{i:05d}", 2 if i < 7 else 1, 1) for i in range(37)]
    _seed(day1 + day2)

    stats = db.zt_pool_daily_stats()
    assert stats[0]["date"] == "2026-10-22" and stats[0]["total"] == 37
    assert stats[0]["lianban_2plus"] == 7
    assert stats[1]["lianban_2plus"] == 16

    res = json.loads(asyncio.run(server.market_sentiment()))
    assert res["label"] == "退潮期"
    assert "双降" in res["rationale"]
    assert res["date"] == "2026-10-22" and res["prev_date"] == "2026-10-21"

    # 指定历史日期：只对比该日及其前一日
    res2 = json.loads(asyncio.run(server.market_sentiment(date="20261021")))
    assert res2["date"] == "2026-10-21"

    # 非法入参
    res3 = json.loads(asyncio.run(server.market_sentiment(date="bad")))
    assert "error" in res3


def test_height_crash_override(monkeypatch, tmp_path):
    """高度断层优先：总数升但最高连板 10→3，定性分歧期（2026-10-13 实况）。"""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t2.db"))
    db.init_db()
    prev = [("2026-10-12", "699999", 10, 0)] + [
        ("2026-10-12", f"6{i:05d}", 1, 0) for i in range(45)]
    cur = [("2026-10-13", "799999", 3, 0)] + [
        ("2026-10-13", f"7{i:05d}", 2 if i < 9 else 1, 0) for i in range(66)]
    _seed(prev + cur)
    res = json.loads(asyncio.run(server.market_sentiment()))
    assert res["label"] == "分歧期"
    assert "断层" in res["rationale"]


def _seed_with_klines(rows, klines):
    _seed(rows)
    for code, date, chg, close in klines:
        db._conn().execute(
            "INSERT OR REPLACE INTO quotes_daily (code, trade_date, open, high, low, "
            "close, volume, amount, change_pct) VALUES (?,?,?,?,?,?,?,?,?)",
            (code, date, close, close, close, close, 1000, 1e8, chg),
        ).connection.commit()


def test_v2_money_effect_override(monkeypatch, tmp_path):
    """赚钱效应压制：结构双升（高潮）但昨日涨停今日平均大跌 → 压为退潮期。"""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t3.db"))
    db.init_db()
    prev = [("2026-10-21", f"6{i:05d}", 1, 0) for i in range(40)]
    cur = [("2026-10-22", f"7{i:05d}", 2 if i < 20 else 1, 0) for i in range(50)]
    # 昨日 40 只今日平均 -4%（覆盖全部）
    kl = [(f"6{i:05d}", "2026-10-22", -4.0, 9.5) for i in range(40)]
    _seed_with_klines(prev + cur, kl)
    res = json.loads(asyncio.run(server.market_sentiment()))
    assert res["base_label"] == "高潮期"          # 结构双升
    assert res["label"] == "退潮期"               # 赚钱效应压制
    assert res["v2"]["money_effect_pct"] == -4.0
    assert res["v2"]["seal_rate_pct"] == 100.0
    assert any("赚钱效应" in a for a in res["adjustments"])


def test_v2_promote_rate_and_seal(monkeypatch, tmp_path):
    """晋级率与封板率计算；封板率<60% 时高潮降为分歧。"""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t4.db"))
    db.init_db()
    prev = [("2026-10-21", f"6{i:05d}", 1, 0) for i in range(10)]
    # 今日：40 只，其中 3 只是昨日老面孔（晋级率 30%），一半有炸板（封板率 50%）
    cur = [("2026-10-22", f"6{i:05d}", 2, 0) for i in range(3)] + \
          [("2026-10-22", f"7{i:05d}", 2 if i < 18 else 1, 1 if i % 2 == 0 else 0)
           for i in range(37)]
    _seed_with_klines(prev + cur, [])
    res = json.loads(asyncio.run(server.market_sentiment()))
    assert res["v2"]["promote_rate_pct"] == 30.0
    assert res["v2"]["seal_rate_pct"] < 60
    if res["base_label"] == "高潮期":
        assert res["label"] == "分歧期"           # 炸板过半降级
