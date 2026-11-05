# L3 深度分析与生态协同 PRD

> 形态：mcp-eastmoney 漏斗之后的"深度层 + 第二意见层"（不改 L1-L2 语义，不动策略定义）
> 参考：AlphaSift L3（本地 scorecard 全量 + DSA_API_URL 前 N 只）× DSA（单股决策报告）
> 前置：《alphasift-dsa-协同流程.md》《路径A下一步规划.md》（P0 聚合层已于 v0.4.0 落地）
> 边界：个人自用研究工具，输出不构成投资建议

---

## 1. 背景与问题

L1→L2 闭环已通（15 策略漏斗 + 聚合层 + AI 终审 + 落表 + 后验），但漏斗出口之后的深度分析仍是**临时拼装**的：

1. **无固定产物**：AI 终审时临时调缠论/筹码/龙虎榜/新闻工具，调用哪些、看什么、输出什么结构全凭模型当次发挥——同票两次深评结论可能不可比。
2. **无第二意见**：所有判断来自单一模型单一数据视角，没有外部交叉验证机制（alphasift 用 DSA 作外部 L3 分析器正是为此）。
3. **候选断档**：落表推荐进入 T+N 后验，但"持续跟踪"止于收益数字——没有 DSA 式的"持有期间每日风险警报/催化兑现/操作清单"形态。
4. **深评不落档**：深度分析结论没有结构化存储，无法进入归因（"缠论二买推荐的票 vs 突破推荐的票，谁的胜率高？"目前回答不了）。

## 2. 目标 / 非目标

**目标**
- G1：对聚合榜 Top N 一键生成**结构统一、可比较、可归档**的深度分析报告（本地零成本，复用现有层3/4 工具）。
- G2：提供**可插拔外部第二意见**通道（默认关闭；开启后仅对 Top N 调外部 HTTP 分析器，失败降级不阻塞）。
- G3：深评结论**结构化落表**并关联 run_id / recommendation，进入归因切片。
- G4：推荐在持有期内获得**每日跟踪快照**（警报/催化兑现），充实 review 维度。

**非目标**
- 不自建 Web 服务/推送系统（DSA 形态不在本项目范围；需要时按"姿势 B"外挂 DSA）。
- 不做多市场（港美股）——保持 A 股单市场。
- 不改 L1/L2/策略 YAML 语义；不引入需要付费 Key 的硬依赖（外部分析器为可选配置）。

## 3. 方案设计

### 3.1 P0-①：深度分析产品化 —— `deep_analysis`（新工具，本地全量）

**定位**：等价 alphasift 本地 scorecard（覆盖全部终审候选，零 LLM 成本），但产物更重——一份结构化单股深评。

```
recommend/deep_analysis.py（新增，纯编排：只调 compute/ 与 data/，零网络计算）
输入: code, context = { hit_strategies, resonance, board_flow_rank, sentiment_fit, strategy_signals }
编排（固定顺序，不凭模型发挥）:
  ① 趋势体检     get_indicators + trend_score        → 趋势结构/关键位/量价配合
  ② 缠论结构     get_chan_analysis                    → 买卖点类型/置信度/止损位/背驰
  ③ 筹码结构     get_chip_distribution                → 获利盘/成本区/套牢压力
  ④ 资金与情绪   龙虎榜(get_lhb) + 板块资金(sector_flow) + 市场情绪标签
  ⑤ 基本面排雷   get_financial_indicator + 业绩预告   → 硬伤否决项
  ⑥ 历史教训     get_decision_history(code)           → 同票过往推荐 α 与教训
输出（固定 schema，落 report）:
  verdict: pass / reject / watch        （+ 每维一票否决项）
  score_breakdown: {trend, chan, chips, flow, fundamental, history}  各 0-100
  risk_flags[] / catalysts[] / key_levels{stop, entry_zone, target}
  one_page_markdown                    （供 AI 终审与 save_report 直接使用）
```

**与 AI 终审的分工**：`deep_analysis` 产出确定性证据页；AI 终审在其上做跨票权衡与最终 ≤3 只精选（等价 alphasift"本地 scorecard 全量 + LLM 只做池内软判断"）。

### 3.2 P0-②：深评落档 —— 表与关联

- 新表 `deep_analyses`：`id / run_id / code / eval_date / verdict / score_breakdown(JSON) / risk_flags / catalysts / key_levels(JSON) / created_at`。
- `recommendations` 增加 `deep_analysis_id` 可空外键：任一推荐可回溯"当时的证据页"。
- 归因扩展：`get_recommendation_stats` 支持 `group_by=verdict_source`（如缠论买点类型/是否共振票），回答"哪类深评信号真的赚钱"。

### 3.3 P0-③：可插拔外部第二意见 —— `external_analyzer`（配置项，默认关闭）

等价 alphasift 的 DSA_API_URL 模式：

```yaml
# config（~/.dsh-stock/config.yaml）
external_analyzer:
  enabled: false
  url: http://127.0.0.1:8000/api/analyze   # 如指向 DSA 或任意 HTTP 分析器
  top_n: 3                                  # 仅对聚合榜前 N 只调用
  timeout_sec: 30
  on_failure: degrade                       # degrade=记降级继续 / fail=任务失败（显式请求时）
```

- 调用时机：AI 终审前，对 Top N 并发请求；结果作为 `second_opinion` 字段并入证据页，**不覆盖本地结论**。
- 成本纪律：top_n ≤ 5；本地 `deep_analysis` 全量兜底，外部失败绝不阻塞主流程（对齐 alphasift"非核心依赖"原则）。
- 对象可以是 DSA（已有部署时），也可以是任何遵循简单 JSON 约定的自写服务——本项目只定义请求/响应契约，不绑定实现。

### 3.4 P1-①：持有期跟踪快照（"迷你 DSA 雷达"）

- eod 链路新增：对 `status=open` 的推荐，每日追加跟踪记录到 `recommendation_reviews`（已有）+ 新增轻量字段 `alerts[]`（预警：跌破止损位/放量滞涨/题材退潮[板块排名滑落]）、`catalyst_status`（兑现/落空/待验证，AI 复盘时回填）。
- `check_alerts` 扩展为同时读 watchlist 与 open 推荐——推荐票自动进雷达，无需人工维护两份名单。

### 3.5 P1-②：热点→候选反查（对齐 alphasift hotspot 闭环）

- 现有 `sector_rotation`（90 行业 N 日排行）+ `sector_flow` 之上，新增反查：当日热度 Top 板块 → `get_concept_members / 行业成分` → 交集聚合榜候选 → 输出 `hotspot_candidates`（板块热度锚点作为 AI 终审的显式输入，对齐 `board_heat_score` 用作 LLM 判断锚点的做法）。

### 3.6 P2：评估升级（对齐 alphasift evaluate 细节）

- review 增加交易成本扣减（双边 bp 可配）与 `shape_status` 标签（路径A P2-① 已规划，此处维持）。
- failure_review 输出"分深评信号"的失败切片：哪类 verdict/缠论买点/风险标记下推荐失败率最高 → 反哺 deep_analysis 权重（样本 ≥10 才允许调，规则先于学习）。

## 4. 工具与数据变更清单

| 类型 | 名称 | 说明 |
|---|---|---|
| 新工具 | `deep_analysis` | 单股结构化深评（P0） |
| 新工具 | `get_hotspot_candidates` | 热点板块→候选反查（P1） |
| 改工具 | `save_recommendation` | 增加 `deep_analysis_id`（P0） |
| 改工具 | `get_recommendation_stats` | 支持 verdict/信号维度切片（P0） |
| 改工具 | `check_alerts` | 覆盖 open 推荐的每日警报（P1） |
| 新表 | `deep_analyses` | 深评证据页存档（P0） |
| 改表 | `recommendation_reviews` | alerts / catalyst_status（P1） |
| 新配置 | `external_analyzer` | 外部第二意见，默认关闭（P0） |
| 编排 | `refresh eod` | 链路追加：聚合 → Top N 深评存档 →（可选）外部分析器 → 跟踪快照 |

## 5. 每日流程（目标态）

```
16:10 eod：刷数据 → 15 策略 L2 扫描(screen_runs) → 聚合榜
   → deep_analysis Top N（本地全量证据页 + 可选外部第二意见）→ 落 deep_analyses
   → 报告候选榜（含证据页摘要）
次日早盘：AI 终审（证据页之上跨票权衡）→ 精选 ≤3 → save_recommendation(带 run_id + deep_analysis_id)
每日 eod：真实入场价后验 + 持有期警报/催化状态 → 归因（策略×信号×风险标记切片）
```

## 6. 验收标准

- P0：任一聚合榜候选可一条命令产出六维证据页并落 `deep_analyses`；任一推荐可回溯其证据页；`external_analyzer` 关闭时零行为差异，开启后仅影响 Top N 且故障不阻塞（fetch_log 记降级）。
- P1：open 推荐每日自动进 `check_alerts`；热点反查结果出现在终审输入中。
- P2：`get_recommendation_stats` 可按 shape_status / verdict 信号分组；每周自动产出失败复盘。

## 7. 风险与边界

- 深评编排顺序固定是为可比性，牺牲灵活性——维度增减走版本号（`deep_analysis_version`），归因时区分版本。
- 外部分析器响应质量不可控：仅作 `second_opinion` 展示，永不参与本地 score 计算。
- alerts 规则先做最小集（止损/滞涨/板块退潮三项），避免告警噪音。
- 与路径A P1（风险软扣分/组合桶）并行不冲突：风险覆盖影响聚合排序，本 PRD 影响漏斗之后的证据与跟踪。
