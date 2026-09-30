# AI 荐股自进化系统 PRD

> 形态：mcp-eastmoney 新增数据层能力（2 张新表 + 4 个新工具）
> 核心思想：**推荐 → 落表 → 市场验证 → 绩效归因 → 方法论迭代** 的闭环飞轮
> 边界：个人自用研究工具，输出不构成投资建议

---

## 1. 要解决什么问题

现在的 AI 推荐是"一次性"的：模型根据当天的涨停池/概念/新闻给出几个代码，说完就散——
**没有记录、没有验证、没有反馈**。你无法回答"上次它推荐得怎么样"，模型也无法从上次的错误中学习。

本 PRD 把推荐变成一个**可积累、可验证、可进化的系统**：

```
        ┌──────────────────────────────────────────────┐
        │                                              │
        ▼                                              │
  ① AI 推荐并落表                                     │
  （用当前 skill 方法论 + 数据层工具）                  │
        │                                              │
        ▼                                              │
  ② 市场自动验证（eod 定时任务）         ④ 方法论迭代    │
  （T+1/3/5 收益、最大回撤、胜负）  ────►  AI 修订 skill ─┘
        │
        ▼
  ③ 绩效归因（按概念/风格/置信度切片）
  （哪类推荐胜率高？哪类是毒药？）
```

**AI 自进化的本质**：skill（方法论文档）不是人写的固定规则，而是 AI 根据③的归因结果**自己修订、自己升版本**，下次推荐用新方法论。绩效数据是训练信号，skill 是参数。

---

## 2. 数据层设计（2 张新表）

### 表 1：`recommendations` — 推荐记录

每次 AI 推荐一只票落一行。**这是整个系统的资产**。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 自增 |
| `rec_date` | TEXT | 推荐日期（YYYY-MM-DD） |
| `code` / `name` | TEXT | 股票代码/名称 |
| `thesis` | TEXT | **推荐逻辑**（概念催化、技术形态、资金信号……模型自由填写） |
| `concepts` | TEXT | 关联概念标签（逗号分隔，供归因切片） |
| `style` | TEXT | 风格标签：`打板/低吸/趋势/事件驱动`（模型自评） |
| `entry_ref_price` | REAL | 参考入场价（推荐日收盘价） |
| `target_horizon` | INTEGER | 预期持有天数（3/5/10） |
| `confidence` | INTEGER | 置信度 1-5（模型自评，归因时验证"自信是否靠谱"） |
| `skill_version` | TEXT | 推荐时使用的 skill 版本号（**归因到方法论版本的钥匙**） |
| `created_at` | TEXT | 写入时间 |

### 表 2：`recommendation_reviews` — 后验绩效

`eod` 定时任务每日自动追加：对 `target_horizon` 未到期的历史推荐，按当日行情更新绩效。

| 字段 | 类型 | 说明 |
|---|---|---|
| `rec_id` | INTEGER | 关联 recommendations.id |
| `eval_date` | TEXT | 评估日 |
| `days_held` | INTEGER | 已持有天数 |
| `close` / `pnl_pct` | REAL / REAL | 当日收盘 / 累计收益率 % |
| `max_gain_pct` / `max_drawdown_pct` | REAL | 期间最大浮盈 / 最大浮亏 |
| `hit_target` | INTEGER | 是否达到预期（T+n 到期时终评：1 赢 0 输 -1 未到期） |
| `final_note` | TEXT | 终评摘要（到期时 AI 复盘写入） |

> 绩效计算数据源：`quotes_daily`（库优先）+ `refresh_daily` 现拉兜底——推荐池的票自动进入日K增量跟踪。

### skill 存储：文件而非表

方法论存 `~/.dsh-stock/skill/recommend.md`（**git 式版本目录** `~/.dsh-stock/skill/versions/v3.md ...`）。
理由：skill 是给模型读的长文本，文件比 SQLite 行合适；版本快照留档即可，不需要表结构。

---

## 3. 工具层设计（4 个新 MCP 工具）

### ① `save_recommendation` — 推荐落表（模型用）

```
输入: code, thesis, concepts, style, target_horizon, confidence
行为: entry_ref_price 自动取当日收盘；skill_version 自动读当前 skill 版本号
```
一次推荐多只 → 模型多次调用。这是"AI 推荐落表"的直接实现。

### ② `review_recommendations` — 触发绩效评估（模型/定时用）

```
输入: 全量未完成评估的推荐（默认）
行为: 对每条推荐拉行情 → 写/更新 recommendation_reviews → 返回评估摘要
```
同时挂入 `eod` 任务（每交易日自动跑）。到期推荐生成终评。

### ③ `get_recommendation_stats` — 绩效归因查询（模型复盘用）

```
输入: group_by=style|concept|confidence|skill_version, days=30
输出: 各切片的推荐数/胜率/平均收益/最大回撤
例: "事件驱动风格近30天胜率45% 平均+2.1%" vs "打板风格胜率28% 平均-1.8%"
```
这是**进化决策的依据**：模型看到归因，才知道该砍掉什么、强化什么。

### ④ `update_recommendation_skill` — 方法论迭代（模型自进化用）

```
输入: new_content（修订后的 skill 全文）, change_note（本次改了什么、依据哪条归因）
行为: 校验当前版本 → 旧版归档 versions/ → 写入新版（版本号+1）→ 返回 diff 摘要
约束: 单次修订必须在 change_note 中引用至少一条绩效数据（防止无依据瞎改）
```

### 完整对话流（你实际怎么用）

> 你："根据这周的情况推荐几只票"
> 模型：读 skill 当前版 → 调涨停池/概念/热度工具分析 → 推荐 3 只 → **自动调 `save_recommendation` 落表**
>
> （三天后）你："复盘一下你的推荐"
> 模型：调 `get_recommendation_stats` → "事件驱动 3 中 2，打板 2 中 0" → 调 `update_recommendation_skill`：砍掉打板追高策略，强化事件驱动低吸 → 版本 v4
>
> 下次推荐自动用 v4 方法论，`skill_version` 字段记录在案，v4 的绩效继续被归因——**飞轮转动**。

---

## 4. 自进化机制的防跑偏设计

AI 自己改自己的方法论有失控风险（越改越玄学），三道保险：

1. **数据强制**：`update_recommendation_skill` 拒绝没有绩效依据的修订（change_note 必须引用 stats）
2. **版本可回滚**：所有历史版本留档，`get_recommendation_stats` 按 skill_version 切片——如果 v5 胜率比 v4 差，模型（或你）能看出来并回退
3. **样本量门槛**：归因统计默认要求每组 ≥5 条推荐才显示结论，避免 2 条数据就推翻方法论

**人的位置**：你随时可以看 skill 当前版（就是一个 md 文件），也可以手动改——AI 的迭代是建议性的，最终方法论你可控。

---

## 5. 实施拆分（两阶段，预计 1.5 天）

### 阶段一：记忆（0.5 天）——先让推荐"留得下来"
- `db.py`：`recommendations` + `recommendation_reviews` 两张表 DDL、upsert、查询
- `server.py`：`save_recommendation` + `get_recommendation_stats` 工具
- `refresh.py`：`review_recommendations` 逻辑挂入 `eod`
- **验收**：DSH 里说"推荐两只票"→ 落表；三天后问"上次推荐的怎么样了"→ 有数据

### 阶段二：进化（1 天）——让方法论"转起来"
- skill 文件结构 + 初始 v1 方法论（基于现有数据能力写一版朴素策略）
- `update_recommendation_skill` 工具（版本管理 + 依据校验）
- `get_recommendation_stats` 增加按 skill_version 切片
- **验收**：人为构造 10 条假推荐+绩效 → 复盘 → skill 升 v2 → 归因能区分 v1/v2

### 依赖与顺序

- **不依赖**板块热度表（可后补，推荐归因先按概念/风格切）
- **弱依赖**新闻数据（thesis 由你在对话中提供或模型联网，落表字段已预留）
- 建议先扩自选池：推荐过的票自动进日K跟踪（review 功能需要行情数据）

---

## 6. 边界与风险声明

- 推荐记录与绩效均为**研究复盘用途**，不构成投资建议；`thesis` 字段保留完整逻辑供事后检验
- 绩效计算基于前复权收盘价，不含滑点/手续费/可成交性（打板类推荐实际买不进的情形，可在 final_note 标注）
- 概念快照目前为部分成分（活跃 Top10），基于概念归因的推荐天然偏向活跃票——已知偏差，记录在案

---

**待你确认的三个决策点**：
1. 推荐是否限制在"涨停池/概念成分"这类短线池，还是全市场自由推荐？（影响初始 skill v1 写法）
2. 绩效评估周期默认 5 天 OK 吗（T+1/3/5 三个观察点）？
3. 阶段一先行还是两阶段一起做？
