# moomoo 能力对照（docs/capabilities.md）

资料日期：2026-10-23。接入路线固定为 **moomoo OpenD + 官方 Python SDK（`moomoo-api`）**，未混用其他体系。

> **说明**：本项目在云端开发环境构建，该环境 **无法访问 openapi.moomoo.com**（DNS/代理拒绝），
> 也没有 OpenD 和您的 moomoo 账户。因此：
> - 「官方声称支持」一栏来自任务书第 2 节在 2026-10-23 对官方文档的核查结论，本次未能重新打开官方页面复核；
> - 「本机测试通过」只包括 **SDK 源码核对**（从 PyPI 安装 `moomoo-api==10.11.7108` 后直接阅读源码）和 **伪券商/本地模拟测试**；
> - 所有需要真实 OpenD、账户或行情权限的项目都标为「尚未验证」或「需要权限」。
> - 系统运行后，「连接与权限诊断」页面的只读诊断结果会写入数据库（`market_capability` 表），**实际验证结果优先于本表**；
>   一次失败不代表所有账户都不支持。

状态说明：**官方声称支持** ｜ **本机测试通过** ｜ **需要权限** ｜ **尚未验证** ｜ **不支持**

官方来源：
- API 总览 https://openapi.moomoo.com/moomoo-api-doc/en/intro/intro.html
- 权限及配额 https://openapi.moomoo.com/moomoo-api-doc/en/intro/authority.html
- 下单 https://openapi.moomoo.com/moomoo-api-doc/en/trade/place-order.html
- AI 接入 https://openapi.moomoo.com/moomoo-api-doc/en/intro/ai.html
- 下载 https://www.moomoo.com/download/OpenAPI
- SDK：PyPI `moomoo-api` 10.11.7108（GitHub MoomooOpen/py-moomoo-api）

## 市场与账户

| 能力 | 状态 | 依据 / 备注 |
| --- | --- | --- |
| Moomoo MY 交易马股股票/ETF | 官方声称支持；**尚未验证** | 总览列出（任务书核查）。需用您的账户只读诊断确认 `trdmarket_auth` 含 MY |
| Moomoo MY 交易美股股票/ETF | 官方声称支持；**尚未验证** | 同上，需确认含 US |
| 马股实时行情（API） | **不支持（按保守口径）/需要权限** | 官方总览与权限页表述不一致，权限页列为 Unsupported。系统不承诺马股实时行情：无可靠报价时禁止开仓；可用获授权的历史数据做「历史回放」 |
| 美股实时行情（API） | **需要权限；尚未验证** | API 行情权限可能与 App 不同。诊断页用 `get_market_snapshot` 只读检测 |
| 马股券商模拟交易 | **不支持** | 官方总览表。系统使用独立的本地模拟器（界面标为「本地模拟」，不称券商模拟） |
| 美股券商模拟交易（BROKER_PAPER） | **尚未验证** | 检测到 SIMULATE 账户并由您选择后才开放 |
| 现金与融资额度区分 | **尚未验证** | SDK `accinfo_query` 字段含 `my_cash`/`us_cash`/`*_avl_withdrawal_cash`（源码核对）。系统取两者较小值作「可用且可结算现金」，不使用 `power`/`net_cash_power`。实际返回值需账户验证；不可靠时禁用自动开仓 |
| 券商原生止损限价单（STOP_LIMIT） | **尚未验证**（按账户/市场分别验证） | SDK 有 `OrderType.STOP_LIMIT`。诊断用 `acctradinginfo_query` 只读探测，**不下单**。未验证时实盘自动交易保持禁用（除非您在部署配置接受客户端监控止损） |
| 频率限制 | **尚未验证（数值）** | 以官方「权限及配额」页为准（本环境无法访问）。系统下单频率极低（同一标的 AI 分析间隔 ≥15 分钟），不自动重发 |

## SDK 行为（本机源码核对，moomoo-api 10.11.7108）

| 事实 | 状态 | 源码位置 / 系统处理 |
| --- | --- | --- |
| `place_order(..., trd_env=TrdEnv.REAL, acc_id=0, ...)` 默认环境为 **REAL** | **本机测试通过** | `moomoo/trade/open_trade_context.py` 第 549–551 行。适配器每次显式传 `trd_env` |
| `acc_id=0` 时 SDK 按 `acc_index`（默认 0）取账户列表中的账户 | **本机测试通过** | 同文件 `_check_acc_id_and_acc_index`。适配器拒绝 `acc_id` 为空或 0，账户必须由您在页面选择 |
| `remark` 最长 64 字节（UTF-8） | **本机测试通过** | `place_order` 内校验。系统用 36 字符的意图 ID 作为 remark，仅作关联线索 |
| `price`/`qty` 在 SDK 内部转为 float | **本机测试通过** | 系统内部全程 Decimal，只在调用 SDK 的边界转换 |
| 存在 `SecurityFirm.FUTUMY`、`TrdMarket.MY`、`Market.MY`、`Currency.MYR` | **本机测试通过** | 枚举核对 |
| 订单/成交推送：`TradeOrderHandlerBase` / `TradeDealHandlerBase` | **本机测试通过（接口）** | 推送只放入线程安全队列，由工作进程单线程处理 |
| 订单回调可能先于下单函数返回 | 官方提示；**本机测试通过（伪券商）** | 按券商订单号或 remark 归并，顺序无关（`tests/integration/test_orders.py`） |
| OpenD 未运行时，`OpenQuoteContext`/`OpenSecTradeContext` 构造函数会**无限重试并阻塞** | **本机测试发现** | `common/open_context_base.py` `__init__` 中 `while True`。系统先做 TCP 探测；只读诊断在子进程中运行并设总超时 |
| `modify_order(ModifyOrderOp.CANCEL, ...)` 撤单 | **本机测试通过（接口）** | 撤单返回成功 ≠ 撤单完成，状态以券商推送/查询为准 |
| `order_fee_query` 可查询订单费用 | 官方 SDK 提供；**尚未验证** | 实盘成交先按费用表估算（标记「预估」），后续可接入核对 |

## 其他系统假设

| 项目 | 状态 | 备注 |
| --- | --- | --- |
| 交易日历 | **本机测试通过（固定样例）** | `exchange_calendars` 4.13.2（XKLS/XNYS）。马股午休 12:30–14:30、16:45 后不提交新委托由本系统补充。节假日需每年对照交易所公告核对；可在 `data/calendar_overrides.json` 增补 |
| 马股最小价格变动单位 | **尚未验证** | 按 Bursa 档位：<1 为 0.005；1–9.99 为 0.01；10–99.98 为 0.02；≥100 为 0.10 |
| 费用 | **测试值（非官方）** | `TEST_FEE_SCHEDULES`。实盘前必须在「设置 → 费用」按 moomoo 官网当前收费填写并勾选「已核对」，否则实盘闸门不通过 |
| 证券代码格式（如 `US.AAPL`、`MY.1155`） | **尚未验证（马股代码）** | 美股格式与 SDK 一致；马股代码请以诊断页 `get_market_snapshot` 返回为准 |
| AI 模型（Anthropic Claude） | **尚未验证（无密钥）** | 默认 `claude-opus-5-5`，结构化 JSON 输出 + 服务器端拒答回退。无密钥时使用「固定测试响应」 |
