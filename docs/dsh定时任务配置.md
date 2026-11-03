# DSH 定时任务配置（eod 每日 / weekly 每周 / history 一次性）

> 数据层跟随 DSH 生命周期：`python -m mcp_eastmoney.refresh <task>`。
> 写入 DSH 的 profile patch（`$DSH_HOME/cordis.patch.yml` 或你的 preset），改配置需重启 dsh 并新建会话。

## 任务总览

| 任务 | 频率 | 内容 |
|---|---|---|
| `eod` | 每交易日 16:10 | 15 步全自动（见下），幂等可重跑，含补跑自查 |
| `weekly` | 每周一 08:30 | stock_basic + stock_industry（Baostock 周一更新） |
| `history` | **一次性**（手动） | 全市场日K回填 550 自然日（≈370 交易日），断点续跑，已有 ≥200 根自动跳过 |

### eod 的 15 步（按执行顺序）

```
 1 daily      自选股日K+15分钟线增量
 2 index      大盘指数（时间基准）
 3 ind_index  90 个同花顺行业指数
 4 lhb        龙虎榜
 5 forecast   业绩预告
 6 fin        自选股财务指标
 7 concepts   概念快照
 8 ztpool     涨停池
 9 zt_kline   当日涨停池K线补拉
10 dragon     龙头扫描存档
11 review     推荐后验（pnl + 相对上证 α）
12 sector     板块资金流（行业+概念，东财 push2 直连）
13 news       大盘新闻
14 snapshot   全市场行情 → quotes_daily 当日K线（唯一真相源；截面由它派生；
               point-in-time：盘前刷新打 T-1 标签；含市值/量比所需的量价字段）
15 prune      库瘦身：fetch_log 留 30 天 / 15min 线留 30 天
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
