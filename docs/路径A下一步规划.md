# 路径 A 下一步规划（参考 AlphaSift 分层，2026-11-02）

> 前提：L1 → L1.5 → L2 已通（10/15 策略出标的），结构不再改动。
> 参考：AlphaSift（L1 硬筛 → L2 横向排序 → 风险覆盖 → 组合分散 → L3 后置分析 → 评估闭环）。
> 原则：**漏斗内部不动；所有新增能力放在漏斗"之后"（聚合层/覆盖层/评估层）**，不改策略定义、不改 L1-L2 语义。

---

## 一、与 AlphaSift 的能力对照

| AlphaSift | 我们现状 | 差距 → 本规划对应项 |
|---|---|---|
| L1 硬筛 + 日K增强 | L1 快照 + L1.5 计算层（K线已全量本地） | 我们更强，不改 |
| L2 screen_score（横向、选股专用） | 策略内部 composite（快照分+K线分） | 缺**跨策略汇总层** → P0-① |
| LLM 重排（仅池内） | skill 层 AI 终审 | 等价，保持 |
| 风险覆盖层（penalty/veto/flags） | L3 排雷（纯 veto） | 缺软扣分与 risk_flags → P1-① |
| 组合分散层（行业风险桶） | 无 | → P1-② |
| L3 scorecard + DSA | 层3/4 深评（缠论/筹码/新闻） | 等价，保持 |
| 形态后验 + failure_review + patch 建议 | review 仅 pnl/α | → P2 |
| 可评估上下文（版本/降级/分数/保存价） | strategy_def + reviews | 缺 run_id/降级/风险字段关联 → P1-③ |

---

## 二、P0：打通推荐闭环（本周）

### ① 聚合层 `recommend/aggregate.py`（新增，漏斗外）
- 输入：15 个策略的 `stop_after="L2"` 输出（候选池）
- 处理（**规则制，不拍权重**）：
  - 合并去重，按 code 聚合
  - 标注 `hit_strategies[]`（命中策略名）+ `resonance`（≥2 策略命中）
  - 标注 `board_flow_rank`（所属板块在 `sector_flow` 的主力净流入排名，行业/概念各取最好）
  - 标注 `sentiment_fit`（策略 market_regime × 当日 `market_sentiment` 标签）
  - 排序规则（透明）：`resonance` 降序 → `board_flow_rank` 升序 → `trend_score` 降序
- 输出：`aggregated[]` + `stats`（策略命中数、共振数、板块分布）
- 对应 AlphaSift：`screen_score` 的统一横向排序（但先用规则，不学权重）

### ② 落表（飞轮起点，当前最大缺口：本周 0 条新增推荐）
- `save_recommendation` 增加可选参数：`run_id`（关联 screen_runs）、`risk_flags`、`portfolio_flags`
- 终审流程：聚合榜 → 层3/4 深评（缠论/筹码/新闻/该股历史决策）→ 精选 ≤3 → 落表
- 验收：当日 ≥1 次 run、≥1 条推荐进入 `recommendations`，status=open

### ③ eod 编排
- 16:10 后：刷新数据 → 15 策略 L2 扫描（存档 screen_runs）→ 聚合 → 报告候选榜
- 验收：`fetch_log` 可见 eod → aggregate 链路；次日可重跑

---

## 三、P1：风险与组合覆盖层（下周）

### ① 风险覆盖层（veto + penalty 双轨）
- 保留现有硬 veto（业绩预减/一字板）
- 新增软扣分：涨跌幅过热、量比异常、高换手、负 PE、日K信号弱、数据降级
- 输出 `risk_score / risk_level / risk_flags`，策略 YAML 可配 `risk_profile` 覆盖阈值
- 对应 AlphaSift：risk overlay；**不改变 L2 判定，只影响聚合排序与展示**

### ② 组合分散层（行业/主题风险桶）
- 桶来源：`stock_industry`（证监会行业）+ `concept_members`（概念）+ 手工桶映射（金融=银行/券商/保险…）
- 同桶超限 → `portfolio_penalty` + `portfolio_flags`（温和扣分，非硬配额）
- 对应 AlphaSift：portfolio diversification overlay

### ③ 推荐上下文完善
- recommendations 关联 `run_id` + 保存当时的 `risk_flags/portfolio_flags/sentiment_label`
- 验收：任一条推荐可回溯到"哪次运行、什么情绪、哪些风险标记"

---

## 四、P2：后验评估升级（两周后，样本积累后）

### ① 交易成本与形态标签
- review 增加：成本扣减（如双边 20bp）、`shape_status`（突破跟随/失败突破/回踩反弹/回踩失败）
- 验收：`get_recommendation_stats` 能按 `shape_status` 分组

### ② failure_review / event_signal_review
- 按策略/行业/共振数/风险标签聚合失败样本
- 输出：`prefer/avoid/watch` 信号 + **策略 patch 建议 YAML 片段（append_unique，只建议不改文件）**
- 验收：每周生成一份失败复盘报告

### ③ 学习型权重（数据门槛）
- 每策略 ≥10 条终评样本后，才允许把规则排序升级为学习权重
- 在此之前：禁止拍权重（避免归因污染）

---

## 五、数据前置（并行，不阻塞）

| 项 | 状态 | 影响 |
|---|---|---|
| 复权口径统一（959 只 sina_raw） | 进行中（腾讯限流窗口重拉） | 因子精度 |
| 概念成分全量（4/150） | 东财限流，自动重试 | 组合桶精度（P1-② 需要） |
| 财务全量 | ✅ 5,221 只 | — |
| 行情全量 | ✅ 5,223 只 | — |

> 概念成分未补齐前，P1-② 的桶退化为"证监会行业"单维度，仍可用。

---

## 六、里程碑验收

| 阶段 | 时间 | 验收标准 |
|---|---|---|
| P0 | 本周 | 每天自动产出候选榜；推荐落表 ≥1 条/日；飞轮开始有数据 |
| P1 | 下周 | 报告展示 risk_flags/portfolio_flags；同桶集中度约束生效；推荐可回溯 run_id |
| P2 | 两周后 | 形态标签统计可用；failure_review 输出 patch 建议；每策略样本 ≥10 后启动权重学习 |
