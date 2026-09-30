"""推荐评估的真实可成交入场价逻辑测试（_effective_entry）。"""
from __future__ import annotations


def _bar(d, o, h, l, c, chg):
    return {"date": d, "open": o, "high": h, "low": l, "close": c, "change_pct": chg}


def test_normal_entry_uses_close():
    from mcp_eastmoney.refresh import _effective_entry
    bars = [_bar("2026-09-28", 9.9, 10.2, 9.8, 10.0, 2.1),
            _bar("2026-09-29", 10.1, 10.5, 10.0, 10.4, 4.0)]
    entry, fill, note = _effective_entry(bars, "2026-09-28", 10.0)
    assert entry == 10.0 and fill == "2026-09-28" and note is None


def test_zt_close_defers_to_next_open():
    from mcp_eastmoney.refresh import _effective_entry
    bars = [
        _bar("2026-09-28", 15.5, 16.9, 15.4, 16.9, 10.0),   # 涨停收盘（非一字：有下影）
        _bar("2026-09-29", 17.2, 18.5, 17.0, 18.3, 8.3),    # 次日高开可买
    ]
    entry, fill, note = _effective_entry(bars, "2026-09-28", 16.9)
    assert entry == 17.2 and fill == "2026-09-29"
    assert "顺延" in note and "开盘价" in note


def test_consecutive_yizi_voids_trade():
    from mcp_eastmoney.refresh import _effective_entry
    bars = [
        _bar("2026-09-28", 16.9, 16.9, 16.9, 16.9, 10.0),   # 一字板
        _bar("2026-09-29", 18.59, 18.59, 18.59, 18.59, 10.0),  # 又一字
        _bar("2026-09-30", 20.45, 20.45, 20.45, 20.45, 10.0),  # 还一字
    ]
    entry, fill, note = _effective_entry(bars, "2026-09-28", 16.9)
    assert entry is None and fill is None
    assert "无法入场" in note


def test_yizi_rec_day_then_openable_next_day():
    from mcp_eastmoney.refresh import _effective_entry
    bars = [
        _bar("2026-09-28", 16.9, 16.9, 16.9, 16.9, 10.0),   # 推荐日一字（买不进）
        _bar("2026-09-29", 17.5, 19.0, 17.3, 18.8, 11.2),   # 次日开板可买
    ]
    entry, fill, note = _effective_entry(bars, "2026-09-28", 16.9)
    assert entry == 17.5 and fill == "2026-09-29"


def test_missing_rec_day_bar_falls_back():
    from mcp_eastmoney.refresh import _effective_entry
    bars = [_bar("2026-09-29", 10.0, 10.5, 9.9, 10.4, 4.0)]
    entry, fill, note = _effective_entry(bars, "2026-09-28", 9.8)
    assert entry == 9.8 and note is None
