# 架构（docs/architecture.md）

单机、本地运行，三个进程：

```
浏览器 ──HTTP(127.0.0.1:8710)──> Web 进程（FastAPI + 前端静态文件）
                                   │  只读查询 / 写入「指令表」「配置版本」「运行授权」
                                   ▼
                    data/sim.db   data/live.db   （SQLite WAL，两个独立文件）
                                   ▲
                                   │  唯一的交易执行者
后台工作进程（每个命名空间一个，文件锁 + 数据库租约互斥）
   └── TradingEngine.tick()  每 2 秒一次
         ├── 行情 Provider ── 演示数据 / 历史回放 / moomoo OpenD（只读）
         ├── AI Provider  ── Claude（结构化 JSON）/ 固定测试响应 / 关闭
         ├── 风控引擎（确定性程序）
         ├── 订单管理器 + 退出协调器
         └── 执行适配器 ── 本地模拟撮合器 / 伪券商（测试）/ moomoo（SIMULATE 或 REAL）
                                   │
                                   ▼
                        moomoo OpenD（您在本机登录）
```

- 网页关闭、会话过期不影响后台：后台只依赖数据库中的**运行授权**（签名、可撤销、有到期时间）。
- 前端从不直接连接 OpenD；Web 进程也从不下单。所有改变交易状态的操作写成「指令」，由工作进程执行。
- 模拟（sim）与实盘（live）分离：数据库文件、账户绑定、风控参数版本、执行适配器、启动配置全部独立。
  实盘工作进程只有在部署配置 `LIVE_TRADING_ENABLED=true` 时才会启动。

## 目录

| 目录 | 职责 |
| --- | --- |
| `backend/app/config.py` | 部署配置（只能由本机 `.env` 设置） |
| `backend/app/db/` | 实体（`models.py`）、定点金额类型（`types.py`）、会话与迁移（`session.py`） |
| `backend/alembic/` | 数据库迁移 |
| `backend/app/core/` | 枚举、交易日历、价格档位/手数/费用、审计与通知、运行授权签名、时钟 |
| `backend/app/market_data/` | 行情接口、演示数据生成、历史回放、moomoo 行情、技术特征、授权股票池筛选 |
| `backend/app/ai/` | 提案结构（Pydantic/JSON Schema）、版本化政策提示词、供应商适配器、输出校验、调用预算 |
| `backend/app/strategies/` | AI 自主决策策略（主功能）、均线基准策略（对照）、共用执行链 |
| `backend/app/risk/` | 风控参数版本、下单前校验与仓位计算、亏损/回撤/计划亏损锁定 |
| `backend/app/orders/` | 订单生命周期与对账（`manager.py`）、退出协调器防超卖（`coordinator.py`） |
| `backend/app/portfolio/` | 现金与持仓账本（只按成交记账） |
| `backend/app/broker/` | 执行适配器接口、moomoo 适配器、只读诊断、实盘闸门 |
| `backend/app/simulation/` | 本地模拟撮合器、伪券商（故障注入）、日 K 回测 |
| `backend/app/learning/` | 经验记录、日报/周度分析、候选改进评估、上线/回退、调度 |
| `backend/app/workers/` | 交易引擎（一次 tick 的完整流程）、工作进程入口 |
| `backend/app/api/` | Web 接口、会话/CSRF/Host 校验、聚合视图 |
| `frontend/` | React + TypeScript + Vite + Ant Design 中文界面 |
| `tests/` | 单元、集成、端到端验收测试 |
| `scripts/` | 一键启动/停止、演示数据生成、可复现端到端示例 |

## 一次 tick 的数据流（自动模式）

```
行情/时钟 ─> 撮合与推送处理 ─> 对账 ─> 公司行动 ─> 授权/范围/健康 ─> 净值与锁定
     ─> 保护性退出（止损/止盈/持有期限，不依赖 AI、不依赖授权）
     ─> 订单维护（到期撤单、退出改价）
     ─> 策略：AI 决策（后台线程，结构化 JSON → 校验）/ 基准规则
           ─> 决策记录（事实｜规则计算｜模型判断｜证据）
           ─> 风控校验 + 资金/名额预留（BEGIN IMMEDIATE 事务）─> 订单意图 CREATED
     ─> 提交（先持久化 SUBMITTING，再调用券商；超时 → UNKNOWN）
     ─> 推送/成交 ─> 账本 ─> 持仓保护
     ─> 日报 / 周度改进（去重键）
     ─> 回放时钟前进（演示/历史回放）
```

## 关键实体

AccountBinding、MarketCapability、Instrument、QuoteSnapshot、Watchlist、UniverseRule/UniverseSnapshot、
StrategyVersion、RiskPolicyVersion、FeeSchedule、FxRate、Portfolio、Authorization、Decision、AICallLog、NewsItem、
OrderIntent、BrokerOrder、Fill、OrderEvent、CashLedger、Position、PositionLedger、RiskReservation、DailyEquity、RiskLock、
AuditEvent、Notification、WorkerHeartbeat、Command、ConfigDraft、BacktestRun、ReviewRun、ExperienceRecord、
ImprovementCandidate、EvaluationRun、DeploymentHistory。

每个交易对象记录 mode、account_id、market、currency、strategy_version 与 created_at_utc；业务时间（回放时为回放时钟）另存 `as_of`。
金额/价格以 ×10⁶ 定点整数存储（`Money` 类型），Python 内全程 `Decimal`。
