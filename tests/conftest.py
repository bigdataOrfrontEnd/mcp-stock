"""共享 fixture：临时 SQLite 库隔离（多测试模块复用）。"""
from __future__ import annotations

import importlib
import os
import tempfile

import pytest


@pytest.fixture()
def tmp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("STOCK_DB_PATH", os.path.join(d, "t.db"))
        from mcp_eastmoney.data import db
        importlib.reload(db)
        db.init_db()
        yield db
