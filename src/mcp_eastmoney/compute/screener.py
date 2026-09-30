"""兼容转发 — screener 已迁移至 recommend/ 层（2026-09 分层重构）。"""
from ..recommend.screener import (  # noqa: F401
    load_strategies, screen, _passes, _percentile,
)
