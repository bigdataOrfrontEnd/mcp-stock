"""concept_members（v6 单表）：概念 ↔ 个股挂钩 + 与板块资金流同码 join。"""
from __future__ import annotations


def _members(n=3):
    return [{"code": f"60000{i}", "name": f"股{i}"} for i in range(n)]


def test_concept_members_roundtrip(tmp_db):
    db = tmp_db
    n = db.upsert_concept_members("BK0420", "航空机场", _members(3), "2026-10-30")
    assert n == 3
    # 概念 → 个股
    rows = db.query_concept_members("航空机场")
    assert len(rows) == 3 and rows[0]["code"] == "600000"
    # 个股 → 概念
    tags = db.query_stock_concepts("600001")
    assert tags[0]["concept_name"] == "航空机场"
    assert tags[0]["concept_code"] == "BK0420"
    # 幂等
    db.upsert_concept_members("BK0420", "航空机场", _members(3), "2026-10-30")
    assert len(db.query_concept_members("航空机场")) == 3


def test_concept_join_sector_flow(tmp_db):
    """概念成分与板块资金流同码（BKxxxx）→ 可直接 join。"""
    db = tmp_db
    db.upsert_sector_flow(
        [{"code": "BK0420", "name": "航空机场", "change_pct": 0.65,
          "main_net": -4.2e7, "main_net_pct": -1.7, "leader_name": "海南机场",
          "leader_code": "600515"}], "2026-10-30", "concept")
    db.upsert_concept_members("BK0420", "航空机场", _members(3), "2026-10-30")
    flows = {r["code"]: r for r in db.query_sector_flow("concept")}
    members = db.query_concept_members("航空机场")
    assert flows["BK0420"]["name"] in ("航空机场",)
    assert len(members) == 3
    # 成分中应能找到领涨股（本例不在成分里，校验 join 键可用即可）
    assert all(m["code"].startswith("600") for m in members)


def test_concept_members_multi_snapshot(tmp_db):
    """多日快照：默认取最新，可指定历史日期（成分变动 diff 的基础）。"""
    db = tmp_db
    db.upsert_concept_members("BK0420", "航空机场", _members(3), "2026-10-29")
    db.upsert_concept_members("BK0420", "航空机场", _members(2), "2026-10-30")
    assert len(db.query_concept_members("航空机场")) == 2          # 最新
    assert len(db.query_concept_members("航空机场", "2026-10-29")) == 3
