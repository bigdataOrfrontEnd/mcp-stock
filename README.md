# mcp-eastmoney

> 🇨🇳 A 股投研数据底座 + 策略筛选引擎 — 让 Claude / Cursor / Codex 等 MCP 客户端拥有完整的"数据 → 计算 → 推荐 → 后验"闭环

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![MCP](https://img.shields.io/badge/MCP-Server-purple.svg)](https://modelcontextprotocol.io)
[![Tests](https://img.shields.io/badge/tests-53%20passed-brightgreen.svg)]()

[English](#english) | [中文](#中文)

---

## 中文

**mcp-eastmoney** 不只是一个行情查询工具——它是一个**分层完整的个人量化研究基础设施**：SQLite 时序+截面双库、缠论/筹码/因子计算引擎、YAML 驱动的策略筛选漏斗、以及"真实可成交入场价"的推荐绩效闭环。全部数据源免费免 Key。

### ✨ 核心特性

- 🆓 **完全免费** — 腾讯/Baostock/AKShare/同花顺/新浪等公开源，零 API Key
- 🐉 **龙头战法** — 五维"识别真龙"评分（带动/领涨/抗跌/流动/承接，门槛一票否决）
- 🧠 **缠论引擎** — 分型/笔/线段/中枢/背驰/三类买卖点（含置信度与止损位）
- 📊 **16 因子库** — 动量/波动/流动性/技术/估值/质量 六类因子横截面排名
- 🎯 **策略漏斗** — YAML 策略筛选 → 趋势终审 → 排雷 → ATR 定仓，一键输出候选包
- 📈 **板块轮动** — 90 个同花顺行业指数 N 日排行 vs 大盘基准
- 🔄 **推荐闭环** — 落库推荐 → 真实入场价后验（涨停顺延开盘、连续一字作废）→ 分策略胜率归因
- 📦 **19 张表 / 35 个 Tool** — eod 定时 15 步全自动，幂等可重跑

### 🏗️ 架构

```
server.py — 35 个 MCP Tool
├── data/       数据层（8 客户端 + 20 张表唯一写入口）
│     tencent / baostock / akshare / 新浪财务 / 三源新闻 / 东财备源
├── compute/    计算层（纯函数零网络：缠论/指标/因子/筹码/异动/趋势/仓位/龙头）
├── recommend/  推荐层（策略YAML筛选 + pipeline漏斗编排 + 龙头扫描）
├── strategies/ 策略定义（放量突破/双低价值/缩量回踩，可自定义覆盖）
└── refresh.py  编排层（eod 每日14步 / weekly / 12个CLI任务）
```

### 🛠️ Tools 一览（35 个）

| 分组 | Tools |
|------|-------|
| 行情 | get_stock_quote / search_stock / main_fund_rank / sector_fund_flow / get_kline |
| 研究计算 | get_indicators / get_chan_analysis / get_chip_distribution / trend_score / position_plan / factor_rank / factor_catalog |
| 推荐漏斗 | list_strategies / screen / **run_pipeline** / scan_dragons / screen_runs |
| 情绪事件 | get_zt_pool / get_lhb / get_earnings_forecast / sector_rotation / get_sector_flow（行业+概念资金流） |
| 基础数据 | get_news / get_stock_concepts / get_concept_members / get_stock_industry / get_financial_indicator |
| 闭环管理 | manage_watchlist / check_alerts / refresh_daily / save_recommendation / get_recommendation_stats / **get_decision_history**（同票决策档案+α+教训回注） / save_report / list_reports |

### 🚀 快速开始

```bash
# 安装运行（uv 推荐）
uvx mcp-eastmoney
# 或
pip install mcp-eastmoney && mcp-eastmoney

# 初始化数据（可选，eod 也可每日定时自动做）
uv run python -m mcp_eastmoney.refresh basic
uv run python -m mcp_eastmoney.refresh index
uv run python -m mcp_eastmoney.refresh eod        # 每日收盘后全套

# 接入 Claude Desktop
```

```json
{
  "mcpServers": {
    "eastmoney": { "command": "uvx", "args": ["mcp-eastmoney"] }
  }
}
```

### 🐉 三十秒看懂推荐漏斗

```
market_snapshot（全市场每日截面 5,222 只）
  → run_pipeline("shrink_pullback")        # 策略筛选+趋势终审+排雷+定仓
  → AI 终审候选包（缠论/筹码/龙虎榜/业绩催化）
  → save_recommendation(skill_version=策略名)
  → eod 每日真实入场价后验 → 分策略胜率归因
```

打板风格另有 `scan_dragons`（龙头识别）入口。

### ⚠️ 数据说明

- 实时行情约延迟 15 分钟（腾讯免费源），仅供研究参考
- 筹码分布为换手衰减三角分布近似模型（东财同思路本地推演，非官方精确值）
- 龙头评分为日线级近似（原版 dragon-quant 的分钟级子因子已降维）
- **不构成任何投资建议**

### 🤝 贡献

欢迎 PR！开发约定：数据层不计算、计算层零网络零落表、写库只走 db.py、全部幂等 upsert。

```bash
git clone https://github.com/<your-username>/mcp-eastmoney.git
cd mcp-eastmoney && uv sync
uv run pytest tests/ -q --ignore=tests/test_live.py   # 53 个离线测试
```

### 📜 License

MIT

---

## English

**mcp-eastmoney** is a full-stack personal equity research infrastructure as an MCP server — not just quotes. It ships a layered architecture (data / compute / recommend / orchestration), a complete Chan-theory engine, a 16-factor cross-sectional ranking library, YAML-driven screening pipelines, a dragon-leader (龙头战法) five-dimension scorer, sector rotation analytics over 90 THS industry indices, and a recommendation feedback loop that evaluates picks at **realistically fillable entry prices** (limit-up deferral to next open, consecutive one-limit boards voided). All data sources are free and keyless.

Key tools: `run_pipeline` (one-shot screening funnel), `scan_dragons` (limit-up leader identification), `get_chan_analysis` (Chan structure), `sector_rotation` (industry momentum vs benchmark), `save_recommendation` → `get_recommendation_stats` (per-strategy win-rate attribution).

Quick start: `uvx mcp-eastmoney`, then add to any MCP client. Daily data via `python -m mcp_eastmoney.refresh eod`. Data delayed ~15 min; not financial advice. MIT License.
