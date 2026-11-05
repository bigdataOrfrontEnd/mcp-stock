# DSH 定时任务配置（eod 每日 / weekly 每周 / history 一次性）

> 数据层跟随 DSH 生命周期：`python -m mcp_eastmoney.refresh <task>`。
> 写入 DSH 的 profile patch（`$DSH_HOME/cordis.patch.yml` 或你的 preset），改配置需重启 dsh 并新建会话。

## 任务总览

| 任务 | 频率 | 内容 |
|---|---|---|
| **`eod`** | 每交易日 16:10 | **纯数据 8 步**（见下）：行情/情绪/指数/板块/榜单原料，幂等可重跑 |
| `scan_all` / `review` | **按需/手动** | 15 策略 L2 扫描存档；推荐后验（不进定时，由报告流程或手动触发） |
| `weekly` | 每周一 08:30 | 股票池/行业/业绩预告/自选池财务（变化慢的数据） |
| `history` | 一次性（手动） | 全市场日K回填（断点续跑，已有 ≥200 根自动跳过） |

### eod 的 8 步（纯数据，16:10）

```
1 daily_market  全市场行情摄入（批量：OHLCV+PE/PB/市值/换手/量比）
                + 日K缺口修复（已最新自动跳过）
2 ztpool        涨停池（情绪定性/龙头/主线分布）
3 index         大盘指数（时间基准 + 后验 α）
4 ind_index     90 个同花顺行业指数（板块轮动/复盘主线）
5 lhb           龙虎榜（层4 资金验证）
6 concepts      概念成分（东财 BK 体系，与 sector_flow 同码）
7 sector        板块资金流（行业+概念；聚合层"资金流排名"维度，须每日）
8 prune         库瘦身（旧日志/旧分时）
```

## 方案 A（推荐）：@weibaohui/dsh-tasks

```yaml
- insert:
    - id: stock-refresh-tasks
      name: '@weibaohui/dsh-tasks'
      config:
        tasks:
          # 每交易日 16:10 收盘后全套（含补跑自查与库瘦身）
          - id: stock-eod
            schedule: "10 16 * * 1-5"
            command: |
              cd /Users/bianyingjie/Desktop/mcp-eastmoney &&
              .venv/bin/python -m mcp_eastmoney.refresh eod
            env:
              STOCK_DB_PATH: /Users/bianyingjie/Desktop/mcp-eastmoney/data/stock.db
          # 每周一 08:30：基础信息 + 行业分类
          - id: stock-weekly
            schedule: "30 8 * * 1"
            command: |
              cd /Users/bianyingjie/Desktop/mcp-eastmoney &&
              .venv/bin/python -m mcp_eastmoney.refresh weekly
            env:
              STOCK_DB_PATH: /Users/bianyingjie/Desktop/mcp-eastmoney/data/stock.db
```

> `history` 回填**不进定时**——需要时手动跑一次（30-45 分钟，可中断续跑）：
> `.venv/bin/python -m mcp_eastmoney.refresh history`
> 回填完成后 15 个策略的 L1.5/L2 即对全市场有效（验收：`uv run python -m mcp_eastmoney.audit`）。

## 方案 B：chicheng-cron

```yaml
- insert:
    - id: stock-refresh-cron
      name: 'chicheng-cron'
      config:
        jobs:
          - name: stock-eod
            cron: "10 16 * * 1-5"
            command: "cd /Users/bianyingjie/Desktop/mcp-eastmoney && .venv/bin/python -m mcp_eastmoney.refresh eod"
          - name: stock-weekly
            cron: "30 8 * * 1"
            command: "cd /Users/bianyingjie/Desktop/mcp-eastmoney && .venv/bin/python -m mcp_eastmoney.refresh weekly"
```

## 注意事项

1. **新建空白 Session** 才能加载新配置。
2. **错过窗口的补跑**：eod 开头读 `fetch_log` 自查；日K/daily_all 增量天然补齐缺失日期；**15 分钟线与概念快照错过不可补**。
3. 审计：`sqlite3 data/stock.db "SELECT task,status,created_at FROM fetch_log ORDER BY id DESC LIMIT 10"`
4. 手动验证：`.venv/bin/python -m mcp_eastmoney.refresh eod`
5. eod 第 1/2/7 步作用于**自选池**——用 `manage_watchlist(action="add", code=...)` 添加；`screen(add_watchlist=true)` 也会自动入池。
6. prune 是 eod 最后一步，只删"可再生数据"（旧日志/旧分时），**quotes_daily 与证据类表（screen_runs/recommendations/reviews/zt_pool/lhb）永不删**。
