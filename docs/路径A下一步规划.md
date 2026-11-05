# 路径 A 下一步规划 v2（单车道模型，2026-11-04）

> 前提：L1 → L1.5 → L2 已通，15 策略**各自排序、各自落表**（funnel_runs）。
> 参考：AlphaSift 单策略车道模型（L1 硬筛 → L2 排序 → 风险覆盖 → L3 后析 → 存 run → T+N 评估）。
> 原则：**漏斗内部不动；引擎不做跨策略处理；最终名单由 AI 终审决定。**

---

## 〇、架构决策记录（v1 → v2 演变）

| 日期 | 决策 | 理由 |
|---|---|---|
| 11-02 | v1 规划：建聚合层（合并总榜，共振排序） | 对标 AlphaSift screen_score |
| 11-03 | v0.4.0 落地 → 当日重构为分族聚合（族内共振） | 跨族比共振是范畴错误（regime 互斥），且结构性偏向大族 |
| **11-04** | **彻底移除聚合层，确立单车道模型** | 策略互不相关，任何跨策略合并缺乏依据；对齐 AlphaSift"每策略一条道"。AI 终审拥有全策略视野，这是引擎层给不了的 |

现行架构（确定性链路零 AI，AI 只在终审出场）：

```
eod 每日（⚙️）: 刷数据 → 15 策略 L1→L2 各自排序各自落表 funnel_runs（唯一存档点）
AI 终审（🤖）:   读 scan_all 各道榜单 + fits_today + 情绪 → 精选 ≤3
落表（🤖→⚙️）:  save_recommendation(run_id → funnel_runs, risk_flags, 入场价)
后验（⚙️）:     eod 每日 T+N 评估（pnl/α/浮盈浮亏）→ 归因 → decision_history 教训回注
```

## 一、与 AlphaSift 的能力对照（2026-11-04 实况）

| AlphaSift | 我们现状 | 状态 |
|---|---|---|
| L1 硬筛 + 日K增强 | L1 快照 + L1.5 计算层（K 线全量本地、16 因子） | ✅ 更强 |
| L2 排序（其 L2 即 LLM screen_score） | 策略 composite + trend_score（确定性）；LLM 判断挪到道外 AI 终审 | ✅ 等价（省 LLM 成本，换全策略视野） |
| 每策略一条道、run 可回溯 | funnel_runs 唯一规范存档（pipeline 自写，含淘汰明细——比它厚） | ✅ 更强 |
| 风险覆盖层 veto+penalty → risk_flags | L3 硬 veto 有；软标注无 | ◐ → 第二批 P1-① |
| 组合分散层（行业桶） | 无（已决定不进引擎，改为 AI 终审输入信息） | ◐ → 第二批 P1-② |
| L3 后置分析（scorecard 全量 + DSA 前 N） | 层3/4 工具散装（缠论/筹码/龙虎榜/新闻/决策历史），未产品化 | ◐ → 第三批 |
| 保存价 → T+N 评估 | 真实可成交入场价 + α + 决策档案回注 | ✅ 更强 |
| failure_review → 策略 patch | 无 | ❌ → P2 |

## 二、P0：打通推荐闭环 —— ✅ 完成（11-04）

- ~~① 聚合层~~：**作废**（见架构决策记录；scan_all 替代：批量入口 + fits_today 标注，零合并）。
- ② 落表：✅ `save_recommendation` 带 `run_id`/`risk_flags`（v8 迁移），`decision_history` 可回溯"哪次运行、什么风险标记"；旧格式兼容。
- ③ eod 编排：✅ `refresh_eod` 串 `scan_all`（snapshot 之后）+ fetch_log 审计。
- 附带修复：run_pipeline 双写 screen_runs 的 bug —— 现存档秩序为 `screen → screen_runs`（L1 快筛）、`run_pipeline → funnel_runs`（全量漏斗，唯一规范点）。

**待点火（验收收尾）**：
- [ ] 跑一次 `refresh eod`（或接入定时 preset），验收 fetch_log 可见 scan_all 链路、当日 15 条 funnel_runs
- [ ] 第一次带 run_id 的真实终审落表（≥1 条推荐进入 recommendations，status=open）——此后飞轮开始积累样本

## 三、P1：风险标注与终审输入（第二批，已重定位）

> 原设计"penalty 影响聚合排序"的消费点已随聚合层移除，两项均重新定位。

### ① 风险标注下沉 L2 出口（pipeline 内）
- `run_pipeline` 在 L2 出口依据**当时快照**给候选标注 `risk_flags[]`：涨幅过热、量比异常、高换手、负 PE、日K信号弱、数据降级
- **只标注不否决**（硬 veto 保留在 L3）；随 candidates 一起写入 funnel_runs —— **point-in-time 冻结**，这是 AlphaSift"可评估上下文"纪律的落点（标记必须与决策所见数据同毫秒，终审/事后补算都会污染归因）
- 验收：funnel_runs 候选 JSON 含 risk_flags；`save_recommendation` 的 risk_flags 参数可直接引用漏斗标记

### ② 终审行业分布提示
- scan_all 返回附一份轻量摘要：候选行业分布 + 同行业超限提示（如同 3 只以上同板块）
- 不进引擎配额（那是聚合层还魂），AI 终审自行取舍
- 验收：scan_all 返回含 industry_distribution；AI 终审 prompt 可直接引用

### ③ 推荐上下文补全
- recommendations 补存 `sentiment_label`（落表时当日情绪，一列 + 迁移）
- 验收：任一推荐可回溯"哪次运行、什么情绪、哪些风险标记"（run_id ✅ risk_flags ✅ sentiment ←本项）

## 四、L3：深评产品化（第三批，详见《L3深度分析与生态协同PRD.md》）

- `deep_analysis` 六维证据页：趋势体检/缠论结构/筹码/资金情绪/基本面排雷/同票历史教训 → 固定 schema + 一页 markdown
- 作用对象 = AI 终审精选 ≤3 只（原料：其 funnel_run 证据 + 层3/4 工具）
- 可插拔外部第二意见 `external_analyzer`（默认关闭，仅 Top N，失败降级不阻塞）
- 验收：任一终审候选可一条命令出证据页；`recommendations.deep_analysis_id` 可回溯

## 五、P2：后验评估升级（样本积累后）

- ① review 增加交易成本扣减（双边 bp 可配）+ `shape_status`（突破跟随/失败突破/回踩反弹/回踩失败）
- ② failure_review：按**策略车道**/行业/风险标记聚合失败样本（原"共振数"维度已随聚合层作废）→ prefer/avoid/watch + 策略 patch 建议 YAML 片段（只建议不改文件）
- ③ 学习型权重：每策略 ≥10 条终评样本前禁止拍权重（纪律不变）
- 验收：`get_recommendation_stats` 支持 shape_status / risk_flags 分组；每周自动失败复盘

## 六、纪律（不变）

1. 归因 <5 条样本不结论；修订需引用绩效依据
2. 确定性的事交给代码，LLM 只碰终审（成本分级）
3. 一切输出落盘带 point-in-time 上下文（版本/截面/标记/价格），评估永远用快照不用事后数据
4. 降级显式化：源切换记 fetch_log provider（如 sector_flow 的东财→同花顺降级链）

---

*附：相关文档——《alphasift-dsa-协同流程.md》（生态参考蓝图）、《L3深度分析与生态协同PRD.md》（第三批详设，其中聚合层引用已过时以本文档为准）。*
