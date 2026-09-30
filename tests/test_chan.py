"""缠论完整引擎测试 — 合成K线验证结构识别，零网络。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_eastmoney.compute.chan import analyze
from mcp_eastmoney.compute.chan.structure import (
    merge_klines, find_fractals, build_bis, detect_segments, detect_zhongshus,
)


def _synthetic_bars(n: int = 260) -> list[dict]:
    """合成一段带涨跌循环的K线：先上涨、再震荡下跌、再反弹，产生分型/笔/段/中枢。"""
    bars = []
    price = 10.0
    for i in range(n):
        # 用分段趋势 + 宽幅震荡制造摆动：大周期趋势 + 小周期震荡
        cycle = (i % 50)
        if cycle < 25:
            drift = 0.08
        else:
            drift = -0.08
        wave = 0.35 if (i % 10) < 5 else -0.35
        price = max(1.0, price + drift + wave)
        o = price - wave / 2
        c = price + wave / 2
        h = max(o, c) + 0.15
        low = min(o, c) - 0.15
        bars.append({
            "date": f"2024-{(i // 28) % 12 + 1:02d}-{i % 28 + 1:02d}",
            "open": round(o, 2), "high": round(h, 2),
            "low": round(low, 2), "close": round(c, 2), "volume": 1000 + i,
        })
    return bars


def test_merge_klines_inclusion_removed():
    bars = [
        {"date": "1", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1},
        {"date": "2", "open": 10.2, "high": 12, "low": 8, "close": 10, "volume": 1},  # 包含前后
        {"date": "3", "open": 8, "high": 9.2, "low": 7, "close": 8.5, "volume": 1},
    ]
    merged = merge_klines(bars)
    assert len(merged) == 2  # 第2根被并入第1根，第3根独立保留


def test_structure_chain():
    bars = _synthetic_bars()
    merged = merge_klines(bars)
    assert len(merged) > 30
    fenxings = find_fractals(merged)
    assert any(f.type == "top" for f in fenxings)
    assert any(f.type == "bottom" for f in fenxings)
    bis = build_bis(merged)
    assert len(bis) >= 5
    # 笔方向必须交替
    for a, b in zip(bis, bis[1:]):
        assert a.direction != b.direction
    segments = detect_segments(bis)
    assert len(segments) >= 1
    zhongshus = detect_zhongshus(segments)


def test_analyze_full_output():
    bars = _synthetic_bars()
    result = analyze(bars)
    assert "error" not in result
    for key in ("merged_bars", "fractals_recent", "bis_recent", "segments_recent",
                "zhongshu_recent", "position", "signals", "beichi", "trend",
                "support_resistance", "summary"):
        assert key in result, f"missing key: {key}"
    assert result["trend"] in ("上涨", "下跌", "盘整", "未知")
    # JSON 可序列化
    import json
    json.dumps(result, ensure_ascii=False)


def test_analyze_insufficient_bars():
    assert "error" in analyze(_synthetic_bars(20))
