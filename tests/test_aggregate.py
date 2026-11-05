

def test_funnel_candidates_view(tmp_db):
    """候选级视图：funnel_runs.candidates 展开成一只票一行；latest 去重。"""
    db = tmp_db
    c = lambda code, trend: {"code": code, "name": f"股{code}", "close": 10.0,
                             "screen_score": 88.0, "trend_score": trend,
                             "trend_grade": "B",
                             "snapshot": {"change_pct": 3.0, "amount": 2e8,
                                          "turnover_rate": 5.0, "volume_ratio": 2.0,
                                          "pe": 20.0, "pb": 2.0}}
    # 同一策略两次运行：最新一次才应默认返回
    db.insert_funnel_run("s1", "2026-11-03", "L2", 5000, 300,
                         {"final": 2}, [c("600001", 80), c("600002", 70)], [])
    db.insert_funnel_run("s1", "2026-11-04", "L2", 5223, 319,
                         {"final": 1}, [c("600003", 90)], [])
    rows = db.query_funnel_candidates(strategy="s1")
    assert [r["code"] for r in rows] == ["600003"]        # 默认只取最新运行
    assert rows[0]["trend_score"] == 90 and rows[0]["pe"] == 20.0

    all_rows = db.query_funnel_candidates(strategy="s1", latest=False)
    assert {r["code"] for r in all_rows} == {"600001", "600002", "600003"}


def test_funnel_candidates_tool(tmp_db):
    import asyncio, json
    from mcp_eastmoney import server
    db = tmp_db
    db.insert_funnel_run("s1", "2026-11-04", "L2", 5223, 319, {"final": 1},
                         [{"code": "600003", "name": "股600003", "close": 10.0,
                           "trend_score": 90.0, "trend_grade": "A", "snapshot": {}}], [])
    out = json.loads(asyncio.run(server.funnel_candidates()))
    assert out["count"] == 1 and out["candidates"][0]["code"] == "600003"
