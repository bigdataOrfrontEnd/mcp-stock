"""sector_flow（行业/概念板块资金流）表读写与工具输出。"""
from __future__ import annotations

import asyncio
import json


def _rows():
    return [
        {"code": "BK0420", "name": "航空机场", "index_value": 3830.07,
         "change_pct": 0.65, "main_net": -4.22e7, "main_net_pct": -1.72,
         "super_large_net": 5.0e7, "super_large_pct": 2.05,
         "large_net": -9.2e7, "large_pct": -3.77,
         "medium_net": -9.4e7, "medium_pct": -3.82,
         "small_net": 1.36e8, "small_pct": 5.54,
         "leader_name": "海南机场", "leader_code": "600515"},
        {"code": "BK0421", "name": "铁路公路", "index_value": 8410.75,
         "change_pct": -1.15, "main_net": 3.0e8, "main_net_pct": 6.61,
         "leader_name": "京沪高铁", "leader_code": "601816"},
    ]


def test_sector_flow_roundtrip(tmp_db):
    db = tmp_db
    assert db.upsert_sector_flow(_rows(), "2026-10-30", "industry") == 2
    rows = db.query_sector_flow("industry", limit=10)
    assert len(rows) == 2
    # 按主力净流入降序
    assert rows[0]["name"] == "铁路公路"
    assert rows[0]["main_net"] == 3.0e8
    assert rows[1]["leader_name"] == "海南机场"
    assert db.sector_flow_dates() == ["2026-10-30"]
    # 幂等：重复写入不增行
    db.upsert_sector_flow(_rows(), "2026-10-30", "industry")
    assert len(db.query_sector_flow("industry", limit=10)) == 2
    assert db.query_sector_flow("concept", limit=10) == []


def test_sector_flow_tool(tmp_db):
    from mcp_eastmoney import server
    db = tmp_db
    db.upsert_sector_flow(_rows(), "2026-10-30", "industry")
    db.upsert_sector_flow(_rows(), "2026-10-30", "concept")
    out = json.loads(asyncio.run(server.get_sector_flow("both", limit=5)))
    assert out["date"] == "2026-10-30"
    assert len(out["industry"]) == 2 and len(out["concept"]) == 2
    assert "error" not in out
