# 股票工作台 PRD（自用版）

> 本文档由历史对话整理而成，反映讨论演进后的**最终结论**，而非过程中的中间方案。
> 形态：基于 `mcp-eastmoney` 二次改造的 Python MCP Server + SQLite 数据层，接入 DeepSeek Harness（dsh）作为 AI 投研工作台。

---

## 1. 产品定位与演进结论

**一句话定位**：个人自用的 A 股智能投研工作台 —— 把行情、K 线、行业/概念分类、板块热度封装成 MCP 工具，模型通过工具调用获取数据，用户通过对话/页面查看分析结果。

**核心场景（驱动整个设计的场景）**：

> "根据这一周的新闻，帮我推荐几个可以建仓的股票"

这个场景的完整链路是：**新闻出现 → 识别关联概念 → 查该概念成分股 → 查看这些成分股/概念当前热度 → 输出候选**。数据层的一切设计都围绕打通这条链路展开。

### 1.1 关键架构决策（讨论中推翻或确认的）

| 决策点 | 结论 | 原因 |
|---|---|---|
| 插件（TS）还是 MCP（Python） | **MCP 优先** | 数据层逻辑（抓取、落库、指标）全部用 Python 更顺手；DSH 通过 `dsh-mcp-client` 以 stdio 挂载，工具与 TS 插件注册的无区别。MCP 做不到的事（改宿主行为、加 UI）数据层不需要 |
| 数据源 | **Baostock + AkShare 双源分工** | 原版 mcp-eastmoney 直连东财 `push2delay` 接口被服务端限流（`Empty reply from server`），实测换源是最优解 |
| 是否落库 | **需要，但按需分阶段** | 落库的三个真实理由：① 历史序列（MA20、资金流趋势）实时接口给不了；② 降级兜底；③ 避免重复请求。概念快照更是"新闻→推荐"场景的刚需 |
| 是否定时任务 | **需要，且是分钟线存在的理由** | 日 K 随时可回补，但 **15 分钟线错过不可补**（Baostock 分钟线只有几个月回溯窗口），定时落库是唯一获取手段 |
| 万能爬虫客户端 | **不做** | 不同数据源的协议、字段、反爬策略没有共性，"万能"是陷阱。方案是 Baostock（稳定 API）+ AkShare（社区维护封装）分工 |
| 概念数据用谁 | **同花顺概念（经 Tushare/AkShare）优先，东财概念兜底** | 同花顺概念偏"投资逻辑叙事"，与新闻事件对应更稳；开盘啦偏短线情绪，噪音大 |

### 1.2 数据源分工（最终版）

| 数据 | 来源 | 接口 | 稳定性 |
|---|---|---|---|
| 股票基础信息（身份） | Baostock | `query_stock_basic()` | 高（独立 API，非爬虫） |
| 行业分类（证监会） | Baostock | `query_stock_industry()` | 高（每周一更新） |
| 日 K 线（含换手率 `turn`） | Baostock | `query_history_k_data_plus()` | 高 |
| 15 分钟 K 线 | Baostock | 同上，`frequency="15"` | 高（回溯窗口有限） |
| 概念板块列表/成分股 | AkShare | `stock_board_concept_name_ths()` / `stock_board_concept_cons_em()`（东财概念，THS 成分接口已被 AkShare 移除） | 中（爬虫封装，可能失败） |
| 实时行情快照 | AkShare | `stock_zh_a_spot_em()`（全市场快照筛选） | 中 |
| 板块热度（热榜） | 同花顺热榜接口（经 AkShare/Tushare） | — | 中 |

**统一返回契约**（所有客户端方法遵守）：

```json
{
  "data": { ... },
  "source": "baostock",
  "stale": false,
  "error": null,
  "fetched_at": "2026-09-21T10:30:00+08:00"
}
```

---

## 2. 模型面 vs 用户面

这是整个产品最重要的边界划分：**模型的可见范围由工具注册决定，用户的可见范围由 UI/对话输出决定，两者不重叠**。

| 维度 | 模型面（Model-Facing） | 用户面（User-Facing） |
|---|---|---|
| 载体 | MCP Tool | 对话回复 / Web UI |
| 触发 | 模型主动调用 | 用户提问 / 打开页面 |
| 数据粒度 | 原始数据、明细、可计算 | 聚合结果、图表、结论 |
| 返回格式 | 结构化 JSON，字段精简 | 可视化（表格/图表/卡片） |
| 是否暴露数据源 | 是（`source` 字段） | 否 |
| 错误处理 | 返回 `error` 字段供模型推理 | 展示"数据延迟"提示 |

**规则**：模型面只给「原料 + 工具」，用户面只给「成品 + 结论」。AI 分析层是两者之间的转换器。

### 2.1 模型面工具清单（MCP 暴露）

| 工具名 | 输入 | 功能 | 数据来源 |
|---|---|---|---|
| `get_stock_quote` | `code` | 实时行情 | AkShare 实时快照 |
| `get_kline` | `code, period(daily/15min), limit` | 历史 K 线 | quotes_daily / quotes_15min（优先读库） |
| `search` | `keyword` | 股票搜索 | stock_basic |
| `refresh_daily` | `codes, days` | 拉取日 K + 15 分钟线落库 | Baostock |
| `get_concept_members` | `concept_name` | 概念成分股列表 | stock_concepts |
| `get_stock_concepts` | `symbol` | 个股所属概念（双向查询） | stock_concepts |
| `get_stock_industry` | `symbol` | 个股行业分类 | stock_industry |
| `get_hot_sectors` | `type, top_n` | 当前热度前 N 的概念/行业 | sector_hot_rank |
| `get_sector_hot_trend` | `sector_code, days` | 概念近期热度变化趋势 | sector_hot_rank（快照对比） |

模型面看不到的：原始 HTML、抓取日志、Provider 切换细节、SQLite 路径。

### 2.2 用户面展示（后续阶段）

- 个股页面显示"所属概念/行业"标签
- 首页显示"今日热度 TOP 10 概念"
- 复盘报告："AI 制药本周热度排名从 15 升至 6，催化事件是……"
- 数据状态条：数据延迟 / 来源标记（`stale` 字段）

---

## 3. 数据层设计（核心）

### 3.1 存储

| 存储 | 位置 | 存什么 | 生命周期 |
|---|---|---|---|
| SQLite 主库 | `~/.dsh-stock/stock.db`（env `STOCK_DB_PATH` 可覆盖） | 基础信息、行情、分类、热度快照 | 永久，增量更新 |
| 内存缓存 | 进程内 | 实时行情 TTL 5s（交易时段）/ 60s（非交易时段）；板块 TTL 5min | 短 |
| 原始响应归档 | `~/.dsh-stock/raw/` | 接口原始 JSON | 保留 30 天，调试/重新解析用 |
| 配置 | 本地 | 自选股列表、数据源优先级 | 永久，不入会话日志 |

### 3.2 核心表结构（最终四表 + 扩展表）

**用户的最终结论**：最基础的落库表为四张 —— 股票基础信息表、行业板块表、概念板块表、股票行情表，**都可以落库，只是更新方式不同**。

#### 表 1：`stock_basic` — 股票基础信息（主键表，先落）

```sql
CREATE TABLE stock_basic (
    code        TEXT PRIMARY KEY,   -- sh.600519 或 600519（需统一格式）
    name        TEXT,
    ipo_date    TEXT,
    out_date    TEXT,               -- 未退市为空
    type        INTEGER,            -- 1=股票 2=指数 4=可转债 5=ETF
    status      INTEGER,            -- 1=上市 0=退市
    updated_at  TEXT
);
```
- 来源：Baostock `query_stock_basic()`（约 5000 条）
- 更新：**变更驱动**，每周跑一次或手动触发
- 股本/市值不单独存：市值 = 收盘价 × 总股本，现算即可

#### 表 2：`stock_industry` — 行业分类

```sql
CREATE TABLE stock_industry (
    symbol           TEXT PRIMARY KEY,
    industry         TEXT,          -- 如"银行"
    classification   TEXT,          -- 证监会
    update_date      TEXT
);
```
- 来源：Baostock `query_stock_industry()`（每周一官方更新）
- 更新：**每周一次**

#### 表 3：`stock_concepts` — 概念板块映射（含快照）

```sql
CREATE TABLE concepts (              -- 概念信息表
    concept_code TEXT PRIMARY KEY,
    concept_name TEXT NOT NULL,
    concept_type TEXT,               -- 'concept' / 'industry'
    source       TEXT,               -- 'eastmoney' / 'ths'
    updated_at   TEXT
);

CREATE TABLE stock_concepts (        -- 概念↔股票映射（核心，带快照日期）
    concept_code  TEXT NOT NULL,
    concept_name  TEXT NOT NULL,     -- 冗余存名称，查询免 JOIN
    symbol        TEXT NOT NULL,
    name          TEXT,
    snapshot_date TEXT NOT NULL,     -- 快照日期，主键一部分
    PRIMARY KEY (concept_code, symbol, snapshot_date)
);
```
- 来源：AkShare 东财概念接口（同花顺成分接口已被 AkShare 移除）
- 更新：**每日快照**（成分股随事件/政策/市场认知变化，快照才能做"这周新进了哪些票"）
- 认知边界：概念标签是**平台的观点，不是事实**；同一只股票在不同平台、不同时期的标签可能不同。概念标签只作为**线索**，不作为**结论**

#### 表 4：`quotes_daily` / `quotes_15min` — 行情

```sql
CREATE TABLE quotes_daily (
    symbol TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, amount REAL,
    change_pct REAL,        -- 涨跌幅 %
    turnover_rate REAL,     -- 换手率 %（Baostock turn 字段自带）
    source TEXT, updated_at TEXT,
    PRIMARY KEY (symbol, trade_date)
);

CREATE TABLE quotes_15min (          -- 15 分钟线，增量落，错过不可补
    symbol TEXT NOT NULL,
    datetime TEXT NOT NULL,          -- 2026-09-21 14:15:00
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, amount REAL,
    PRIMARY KEY (symbol, datetime)
);
```
- 来源：Baostock（日 K 可回补多年历史；15 分钟线仅最近几个月）
- 更新：**每日收盘后增量**（日 K 首次初始化时一次性回补）
- K 线粒度结论：只落 **日 K + 15 分钟**。5 分钟/1 分钟数据量爆炸且信息量近零，不落

#### 扩展表（后续阶段）

- `sector_hot_rank`：每日板块热度快照（板块代码、热度值、排名、涨跌幅、日期）— 存快照看趋势（"连续三天排名上升"比单日数据有意义）
- `news`：新闻表（news_id 哈希、symbol、title、summary、url、publish_at）
- `watchlist`：自选股（symbol、added_at、note、group）
- `fetch_log`：抓取审计（api、provider、status、latency_ms、error），支撑降级链可追溯与补跑判断

### 3.3 数据流向

```
定时任务/手动触发
    ↓
Baostock / AkShare 拉取 → 原始归档(raw/) → 解析 → SQLite 入库
                                              ↓
                              指标计算（MA/MACD/KDJ/RSI/BOLL，读库计算写回）
                                              ↓
              ┌───────────────────────────────┴─────────────────────┐
              ↓                                                       ↓
    模型面：MCP 工具查库/查缓存                            用户面：分析结论/页面渲染
```

### 3.4 定时任务设计

按更新频率分任务，不做一个大而全的脚本：

| 任务 | Cron | 内容 |
|---|---|---|
| 每日收盘后 | `0 16 * * 1-5` | `quotes_daily` + `quotes_15min` + `stock_concepts` 快照 + `sector_hot_rank` 快照 |
| 每周 | 周一 | `stock_basic` + `stock_industry` |
| 按需手动 | — | 新股上市/退市/概念大调整时补跑 |

实现：dsh 社区定时插件（`chicheng-cron` / `@weibaohui/dsh-tasks`）触发 Python 脚本。
**注意**：DSH 定时任务在宿主进程关闭期间**不会补跑**。脚本需自查 `fetch_log` 最后记录日期，过期先补跑。

### 3.5 落库实现要点

- `INSERT OR REPLACE` 保证幂等，重复运行不产生脏数据
- 单只股票失败不阻断整体，记录在返回结果 `{code: "failed: ..."}` 中
- AkShare / Baostock 均为同步库，必须用 `asyncio.to_thread` 包装，避免阻塞 MCP 事件循环
- Baostock 不是线程安全的；每次调用独立 login/logout 循环
- Baostock `rs.get_data()` 在 pandas 2.0+ 崩溃，需手动 `rs.next()` + `get_row_data()` 收集
- Baostock 返回全是字符串，落库前做类型转换；代码格式 `sh.600519` 与纯 6 位码需统一

---

## 4. 技术架构与接入方式

```
DSH（MCP Client，stdio 子进程自动拉起并管理生命周期）
      │
      ▼
mcp-eastmoney（二次改造后的 Python MCP Server）
      ├── baostock_client.py   ← 基础信息 / 行业 / K 线
      ├── akshare 层           ← 实时快照 / 概念板块 / 热榜
      ├── db.py                ← SQLite 落库（init_db / upsert_*）
      └── refresh 脚本         ← 定时任务入口
      │
      ▼
~/.dsh-stock/stock.db
```

DSH 挂载配置（profile patch）：

```yaml
- id: stock-data-mcp
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: stock-data
    transport: stdio
    command: uv
    args: ['run', 'mcp-eastmoney']
    failOnStartupError: true   # 开发阶段设 true，启动失败显式报错
    env:
      STOCK_DB_PATH: '~/.dsh-stock/stock.db'
```

要点：
- 工具命名空间：模型看到 `mcp__stock-data__get_stock_quote` 等
- 修改配置后**必须新建空白 Session**，旧 Session 不加载新 preset
- MCP 工具注册是异步的，新会话第一轮 prompt 可能看不到 `mcp__*` 工具，第二轮即可用（已知现象）
- 依赖锁定：`mcp>=1.29.0,<2`（MCP SDK 2.0 移除了低阶 `Server.list_tools()` 装饰器 API，会导致原版启动崩溃）

---

## 5. 实施路线图

### 阶段一：跑通基础链路（当前进行中）

- [x] 锁定 mcp 1.x，原版 server 在 MCP Inspector 中跑通
- [x] 定位东财 `push2delay` 限流问题（TLS 握手成功但应用层断连，浏览器可、脚本不可）→ 决策：换源
- [ ] 落库 `stock_basic` + `stock_industry`（Baostock 两接口，`baostock_client.py`，代码已给出）
- [ ] 落库 `quotes_daily`（核心行情链路）
- [ ] DSH 挂载端到端验证："查一下 600519 的行情" → 模型自动调用工具

### 阶段二：K 线全量 + 定时

- [ ] `quotes_15min` 增量落库
- [ ] 首次历史日 K 一次性回补
- [ ] 配置 16:00 / 每周一两组定时任务 + 补跑自查逻辑

### 阶段三：概念与热度（"新闻→推荐"场景就绪）

- [ ] `concepts` + `stock_concepts` 每日快照
- [ ] `sector_hot_rank` 每日快照
- [ ] 暴露 `get_concept_members` / `get_stock_concepts` / `get_hot_sectors` / `get_sector_hot_trend` 工具
- [ ] 端到端验证："这周新闻 → 关联概念 → 成分股 → 热度筛选 → 候选列表"

### 阶段四：分析与展示

- [ ] 技术指标计算（MA/MACD/KDJ/RSI/BOLL，读 `quotes_daily` 计算写回 `indicators` 表，不重复抓取）
- [ ] AI 分析层：评分卡、风险提示、每日复盘（模型面→用户面转换器）
- [ ] 用户面页面（自选股卡片、板块热力、复盘报告）

### 落库顺序原则

**稳定的地基先打**：`stock_basic` → `stock_industry` → `quotes_daily`/`quotes_15min`（全走 Baostock，稳定）→ `stock_concepts` 最后（依赖 AkShare 爬虫接口，可能失败）。

---

## 6. 边界与合规

- **个人自用**。"推荐建仓"输出仅限自己参考；若开放给他人，涉及证券投资咨询业务资质问题，输出必须限定为"基于公开新闻和概念分类的客观关联展示"
- 不做自动交易、不接券商接口、不承诺收益
- 所有 AI 分析输出强制携带免责声明，不带买卖建议
- 所有数据源调用强制 try/except，单点故障不扩散；失败降级最终读本地缓存并标记 `stale: true`
- 凭证（Tushare Token 等）仅存本地 `.env`，不入会话日志

---

## 7. 已知风险与经验教训（来自实施过程）

| 问题 | 结论/规避方式 |
|---|---|
| MCP SDK 2.x 破坏性变更 | 依赖锁 `mcp>=1.29.0,<2` |
| 东财 `push2delay` 限流（Empty reply from server） | 换 Baostock/AkShare，不再直连东财实时接口 |
| Clash Verge 系统代理干扰 httpx | `AsyncClient(trust_env=False)` 或 Clash 加东财直连规则 |
| AkShare 同步阻塞 | `asyncio.to_thread` 包装 |
| Baostock 线程不安全 / pandas 2.0 不兼容 / 全字符串返回 | 独立 login 循环、手动收集行数据、落库前类型转换 |
| 同花顺概念成分 AkShare 接口被移除 | 用东财概念接口 `stock_board_concept_cons_em` 替代，或 Tushare `ths_member`（需 Token） |
| DSH 定时任务错过不补跑 | 脚本自查 `fetch_log` 日期决定补拉 |
| Baostock 分钟线回溯窗口短 | 15 分钟线必须每日定时落，错过不可补 |
