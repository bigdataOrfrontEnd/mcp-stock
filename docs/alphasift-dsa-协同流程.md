# AlphaSift × DSA 协同流程（参考蓝图）

> 调研对象：[ZhuLinsen/alphasift](https://github.com/ZhuLinsen/alphasift) 与 [ZhuLinsen/daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis)（DSA）。
> 目的：说明两者如何配合使用，作为 mcp-eastmoney（已实现 L1→L2）后续演进的参考蓝图。
> 日期：2026-11-03。仅描述公开 README/docs 中的机制，未核实源码细节。

---

## 一、三个项目的一句话定位

| 项目 | 定位 | 形态 |
|---|---|---|
| **alphasift** | AI 原生的**广度发现**引擎：全市场 → 硬筛 → LLM 排序 → 风险/组合覆盖 → 后置分析 → T+N 回评估 | Python CLI + 本地只读 JSON API（`alphasift serve`），agent 通过 SKILL.md 驱动 |
| **daily_stock_analysis (DSA)** | **单股深度分析**服务：自选股每日 AI 决策报告（评分/趋势/买卖点位/风险警报/催化/操作清单）+ 大盘复盘 + 多渠道推送 | FastAPI Web/桌面工作台（:8000），GitHub Actions / Docker 每交易日 18:00 定时 |
| **mcp-eastmoney（本项目）** | A 股投研数据底座 + 策略筛选引擎，MCP 形态，已实现 L1→L2 可回溯闭环 | MCP Server（35 tools / 19 表），AI 终审由客户端（Claude/dsh skill 层）承担 |

**关系本质**：alphasift 负责"从 5000 只里找到值得看的 5 只"（广度、漏斗、控成本），DSA 负责"把这 5 只讲透"（深度、报告、推送）。两者通过 `DSA_API_URL` 解耦集成，DSA 不是 alphasift 的核心依赖，而是 L3 层可插拔分析器之一。

---

## 二、核心协同思想：漏斗 + 成本控制

整个生态的设计哲学是 **LLM 调用次数沿漏斗指数收敛**：

```
全市场 ~5,000 只
   │  L1 确定性硬筛（纯代码，零 LLM 成本）           → ~20-50 只
   │  L2 LLM 横向排序（仅池内，--no-llm 可关）        → Top 10-20
   │  风险覆盖 + 组合分散（veto/penalty，纯规则）      → 最终 Top 3-5
   │  L3 后置分析：本地 scorecard 全量 + DSA 仅前 N 只 → 深度报告
   ▼
T+N 回评估（保存价 vs 最新价，纯代码）
```

关键原则：
1. **确定性的事交给代码**：估值/流动性/涨跌幅/技术阈值等硬条件全部在策略 YAML 的 `screening:` 段由 L1 执行，不烧 token。
2. **LLM 只做软判断**：L2 排序输出论点/催化/风险/置信度，且只在候选池内横向比较。
3. **深度分析按需付费**：L3 默认本地 scorecard 覆盖全部最终候选；DSA 这类重型 LLM 分析只对"最终入围的前 N 只"调用。
4. **一切可回溯**：每次 run 保存策略版本、数据源降级记录、各层分数、候选保存价——评估与复盘基于快照而非事后数据。

---

## 三、alphasift 内部三层流水线

### L1 确定性筛选
- 输入：全市场快照（数据源降级链 sina → efinance → akshare_em → em_datacenter，配 token 时 tushare 优先）+ 日 K 增强（均线/MACD/RSI 等，tushare/tencent/sina/akshare/baostock 多源）。
- 处理：策略 YAML 硬过滤 + 因子打分。全局日 K 增强不可用则任务直接失败（不允许静默降级）。
- 输出：候选池（代码/名称/评分/价格/PE/PB 等）。

### L2 可选 LLM 排序
- 对候选池做横向结构化推理：相对排序（`screen_score`，与单股诊断用的 `signal_score`/`sentiment_score` 职责分离）、行业/主题标签、催化与风险摘要、排序理由。
- 经 LiteLLM 支持 Gemini/OpenAI/DeepSeek/Ollama，含多通道、fallback 模型、token 上限；可 `--no-llm` 跳过。
- 可注入市场上下文（`--context`）与候选股新闻/公告/资金流上下文。

### 风险覆盖层与组合分散层（L2 之后、L3 之前）
- 风险层：alpha 排序与风险约束分离，单独做 veto / penalty，输出可审计的 `risk_score` 等。
- 组合层：行业/主题风险桶，同桶超限输出 `portfolio_penalty`（软扣分）。

### L3 可插拔后置分析
- 分析器框架：默认**本地 scorecard** 覆盖全部最终候选（零额外成本）。
- 可选追加 **DSA** 或任意 HTTP 分析器：通过环境变量 `DSA_API_URL` 解耦部署，**只对最终入围候选的前 N 只调用**；L3 分析器失败时，任务失败还是记 degradation 取决于该分析器是否用户显式请求（非核心依赖不阻塞主流程）。

### 评估闭环
- 保存：策略名与 `strategy_version`、数据源与降级记录、L1/L2/L3 分数与风险字段、候选**保存价**。
- 评估（`evaluate`）：保存价对比最新快照价，计算收益、胜率、缺失报价、交易成本扣减、突破/回踩后验标签；可选价格路径估算最大回撤与最大浮盈。定位为"工程闭环，不是完整复权回测"。

### 其他能力
- 热点发现（hotspot，JSON 缓存 schema_version=2 含审计元数据）；行业/概念板块热度锚点（本地 CSV/JSON 映射 `board_heat_score` 供 LLM 判断锚定）。
- 本地只读 JSON API：`/overview`（策略建议 + 数据源健康 + 最近 runs + next actions 组合 payload）、`/strategies` 等。
- 报告：`alphasift report <run_id>` 生成 Markdown。

---

## 四、DSA（daily_stock_analysis）侧

- 输入：自选股列表（`STOCK_LIST` 环境变量，如 `600519,hk00700,AAPL`），支持 A/港/美/日/韩/台股 + ETF。
- 数据：AkShare/Tushare/Pytdx/Baostock/YFinance/Longbridge 行情 + Anspire/SerpAPI/Tavily/博查/Brave 等新闻搜索。
- LLM：多渠道（Gemini/Claude/OpenAI 兼容/Ollama 等，各自环境变量配 Key），支持高级路由。
- 输出：
  - **每日决策仪表盘**：每只自选股的买入/观望/卖出评级 + 摘要 + 风险警报 + 利好催化 + 操作清单（买卖点位）。
  - **大盘复盘**：指数、涨跌统计、板块表现。
  - 推送：企业微信/飞书/Telegram/Discord/Slack/邮箱。
- 定时：默认每工作日北京时间 18:00（GitHub Actions / Docker / 本地 cron），非交易日自动跳过，支持手动触发、dry-run、断点续传。
- Agent 能力：策略问股（多轮追问，15 种内置策略：均线/缠论/波浪/趋势/热点/事件驱动/成长质量/预期重估等），可调实时行情/K线/指标/新闻；多 Agent 编排为实验性功能。

---

## 五、两者协同的端到端流程

```
┌─────────────────────────────────────────────────────────────────────┐
│ ① 盘后（如 15:30+）alphasift 广度发现                                │
│    snapshot（多源降级）→ L1 硬筛（策略YAML，零LLM）→ L1.5 日K增强      │
│    → L2 LLM 排序（screen_score + 论点/催化/风险/置信度）               │
│    → 风险覆盖 + 组合分散 → 最终小名单 Top 3-5                         │
├─────────────────────────────────────────────────────────────────────┤
│ ② L3 后置分析（成本分档）                                             │
│    a. 本地 scorecard：全量最终候选（免费）                            │
│    b. DSA 深度分析：仅前 N 只，经 DSA_API_URL 调用                    │
│       → 买卖点位/风险警报/催化/操作清单级别的单股决策报告               │
├─────────────────────────────────────────────────────────────────────┤
│ ③ 持久化                                                             │
│    alphasift 保存 run：strategy_version / 数据源降级 / 各层分数 /     │
│    保存价 —— 供评估与审计                                             │
├─────────────────────────────────────────────────────────────────────┤
│ ④ DSA 独立主线（并行运行）                                            │
│    每工作日 18:00 对人工维护的自选股出决策仪表盘 + 大盘复盘 → 推送     │
│    （alphasift 的最终候选可人工/脚本合入 DSA 的 STOCK_LIST，           │
│      次日起获得持续的每日跟踪与点位提醒）                              │
├─────────────────────────────────────────────────────────────────────┤
│ ⑤ T+N 回评估（alphasift evaluate）                                   │
│    保存价 vs 最新价 → 收益/胜率/成本扣减/突破回踩标签                  │
│    → 失败复盘 → 策略 patch 建议 → 下一轮筛选进化                      │
└─────────────────────────────────────────────────────────────────────┘
```

**协同要点总结**：
1. 单一集成点：`DSA_API_URL` 环境变量，HTTP 解耦，互不侵入代码。
2. 成本分级：本地 scorecard 全量兜底，DSA 只吃漏斗末端的 Top N。
3. 容错：DSA 失败不阻塞 alphasift 主流程（记 degradation 或按显式请求处理）。
4. 两条互补主线：alphasift 是"发现漏斗"（每日新候选），DSA 是"跟踪雷达"（存量自选股持续盯盘），最终候选流入 DSA 获得连续跟踪。
5. 闭环归 alphasift：run 快照 + T+N 评估保证"筛出来的票后来怎么样"永远可回答。

---

## 六、mcp-eastmoney 在此生态中的位置

### 能力映射

| 生态环节 | alphasift / DSA | mcp-eastmoney 现状 | 差距 |
|---|---|---|---|
| L1 硬筛 + 日K增强 | 策略 YAML `screening:` + 多源快照 | L1 快照 + L1.5 计算层（K 线全量本地、16 因子） | **等价或更强** |
| L2 LLM 排序 | screen_score（LiteLLM，池内横向） | 15 策略 composite + 聚合层（resonance/board_flow_rank/sentiment_fit）+ skill 层 AI 终审 | **等价**（AI 终审由 MCP 客户端承担，免费复用宿主模型） |
| 风险/组合覆盖 | veto/penalty/portfolio_penalty | L3 排雷（纯 veto）+ P1 规划软扣分与风险桶 | 软扣分/风险桶在 P1 路上 |
| L3 深度分析 | 本地 scorecard 全量 + DSA 前 N | 层3/4 工具（缠论/筹码/龙虎榜/新闻/决策历史）可被 AI 终审临时调用 | **缺产品化**：没有"对小名单一键出深度报告"的固定产物与外部第二意见机制 |
| 每日跟踪雷达 | DSA 决策仪表盘 + 推送 | check_alerts + watchlist + dsh 定时报告 | 形态不同（MCP 被动 vs 服务主动推送） |
| T+N 回评估 | 保存价 vs 最新价 + 成本扣减 + 标签 | **真实可成交入场价后验**（涨停顺延/一字作废）+ 分策略胜率归因 + 决策档案回注 | **更强**（入场价真实性优于保存价对比） |
| 热点发现 | hotspot + board_heat_score 锚点 | sector_rotation（90 行业 vs 大盘）+ sector_flow | 缺"热点→候选反查"闭环 |
| Agent 接入 | SKILL.md + CLI + JSON API | MCP 35 tools + dsh skill 层 | **等价**（MCP 原生） |

### 定位结论

mcp-eastmoney ≈ **alphasift 的 L1+L2（MCP 化重实现，且后验更精细）+ 部分 L3 工具 + 推荐飞轮**，当前缺口恰好是 DSA 承担的角色：**对最终小名单的深度分析产品化**（固定报告产物 + 可选外部第二意见）与**存量候选的持续跟踪形态**。演进 PRD 见《L3深度分析与生态协同PRD.md》。

### 三种接入姿势（供决策）

| 姿势 | 做法 | 适用 |
|---|---|---|
| A. 独立闭环（推荐起点） | 不引入 DSA，把层3/4 工具编排产品化为"深度分析报告"skill/工具，AI 终审即 L3 | 保持单体、免费零 Key 哲学 |
| B. DSA 作外挂第二意见 | 按 alphasift 模式加 `external_analyzer` 配置，对 Top N 调 DSA HTTP API，失败降级不阻塞 | 已有 DSA 部署与 LLM Key，要交叉验证 |
| C. 上游替换 | 用 mcp-eastmoney 的 run_pipeline 聚合榜替代 alphasift L1/L2，喂给 DSA 跟踪 | 想复用 DSA 的推送/多市场能力 |
