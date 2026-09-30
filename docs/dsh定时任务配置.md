# DSH 插件定时任务配置（数据层每日/每周刷新）

> 目标：用 DSH 的定时任务插件（而非 launchd）驱动 `python -m mcp_eastmoney.refresh`，
> 让数据落库跟随 DSH 生命周期管理。
>
> 两个插件二选一。**注意**：以下 YAML 的字段名以各插件 README 为准（社区插件
> Developer Preview 阶段配置结构可能调整），若校验失败，对照插件文档微调 key 即可。

## 方案 A（推荐）：@weibaohui/dsh-tasks — 直接执行命令

写入 DSH 的 profile patch（`$DSH_HOME/cordis.patch.yml` 或你的 preset `agent.cordis.yml`）：

```yaml
- insert:
    - id: stock-refresh-tasks
      name: '@weibaohui/dsh-tasks'
      config:
        tasks:
          # 每个交易日 16:10 收盘后全套：自选股日K+15分钟线+概念快照（含补跑自查）
          - id: stock-eod
            schedule: "10 16 * * 1-5"
            command: |
              cd <项目根目录> &&
              .venv/bin/python -m mcp_eastmoney.refresh eod
            env:
              STOCK_DB_PATH: ~/.dsh-stock/stock.db
          # 每周一 08:30：基础信息 + 行业分类（Baostock 周一更新）
          - id: stock-weekly
            schedule: "30 8 * * 1"
            command: |
              cd <项目根目录> &&
              .venv/bin/python -m mcp_eastmoney.refresh weekly
            env:
              STOCK_DB_PATH: ~/.dsh-stock/stock.db
```

## 方案 B：chicheng-cron — cron 表达式触发

```yaml
- insert:
    - id: stock-refresh-cron
      name: 'chicheng-cron'
      config:
        jobs:
          - name: stock-eod
            cron: "10 16 * * 1-5"
            command: "cd <项目根目录> && .venv/bin/python -m mcp_eastmoney.refresh eod"
          - name: stock-weekly
            cron: "30 8 * * 1"
            command: "cd <项目根目录> && .venv/bin/python -m mcp_eastmoney.refresh weekly"
```

## MCP Server 挂载（同一份 patch 里并列添加）

```yaml
- insert:
    - id: stock-data-mcp
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: stock-data
        transport: stdio
        command: uv
        args: ['run', 'mcp-eastmoney']
        cwd: <项目根目录>
        failOnStartupError: true
        env:
          STOCK_DB_PATH: ~/.dsh-stock/stock.db
```

## 注意事项

1. **必须新建空白 Session** 才能加载新配置；旧 Session 看不到新工具。
2. **DSH 关闭期间错过的窗口不会自动补跑**——`refresh eod` 开头有自查逻辑
   （读 `fetch_log` 最后 success 时间），日 K 增量拉取天然补齐缺失日期；
   但 **15 分钟线错过不可补**，只能补到 Baostock 回溯窗口内的近日。
3. 日志：任务 stdout 会进插件日志；审计记录在 `~/.dsh-stock/stock.db` 的 `fetch_log` 表：
   `sqlite3 ~/.dsh-stock/stock.db "SELECT task,status,created_at FROM fetch_log ORDER BY id DESC LIMIT 10"`
4. 手动触发验证（不等定时）：
   `cd <项目根目录> && .venv/bin/python -m mcp_eastmoney.refresh eod`
5. `eod` 对**自选股**落库——先用 MCP 工具 `manage_watchlist(action="add", code="600519")` 添加，
   否则 eod 只刷概念快照。
