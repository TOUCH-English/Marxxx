# 验收结果（docs/acceptance.md）

测试日期：2026-10-23（云端开发环境，Linux，Python 3.13.16）。
运行命令：`.venv/bin/python -m pytest`（项目根目录）。结果：**136 passed**（2026-10-23 夜，第 3 批 A 之后）。前端构建与浏览器冒烟测试见「前端」一节。

> 说明：本环境没有 moomoo OpenD、没有您的账户、没有 AI 密钥，也无法访问 openapi.moomoo.com。
> 因此所有「券商」相关测试使用 **本地模拟撮合器** 或 **伪券商（故障注入）**；凡需要真实账户/行情/模型的项目单独列为「未验证」，
> 不以 mock 测试冒充真实接通。

## 规格第 14 节验收项

| # | 验收项 | 结果 | 测试 |
| --- | --- | --- | --- |
| 1 | DEMO/PAPER 无法触发真实下单；SDK 显式指定环境与账户 | **通过**（离线）；真实 OpenD **未验证** | `tests/integration/test_isolation_security.py::test_sdk_calls_pass_explicit_env_and_account`、`::test_demo_and_paper_never_touch_real_broker`、`::test_real_adapter_rechecks_deployment_switch_every_order` |
| 2 | 只改前端模式或伪造请求不能启用实盘 | **通过** | `::test_frontend_or_forged_request_cannot_enable_live` |
| 3 | 两个并发订单不能超出现金、仓位或名额 | **通过** | `tests/integration/test_risk.py::test_concurrent_entries_cannot_exceed_cash_or_slots`、`::test_concurrent_entries_cannot_double_spend_cash` |
| 4 | 重复信号/点击/回调/多工作进程不重复下单或记账 | **通过** | `tests/integration/test_orders.py::test_duplicate_signal_and_double_click_do_not_duplicate_orders`、`::test_callback_before_return_and_fills_dedup`、`::test_second_worker_cannot_start` |
| 5 | 券商已接收但本机超时：不重发；重启进入对账 | **通过**（伪券商）；真实券商 **未验证** | `::test_timeout_after_accept_goes_unknown_and_is_never_resent`、`::test_crash_after_submit_marks_unknown_on_restart` |
| 6 | 回调先于下单返回、部分成交后撤单、撤单时继续成交 | **通过**（伪券商） | `::test_callback_before_return_and_fills_dedup`、`::test_partial_fill_then_cancel_and_fill_during_cancel`、`::test_cancel_rejected_keeps_broker_truth` |
| 7 | 范围外、融资、错误币种、过期行情、停牌、休市、非法手数被拒绝 | **通过** | `tests/integration/test_risk.py::test_rejections`、`::test_illegal_lot_rejected_by_simulator` |
| 8 | AI 无响应/JSON 错误/伪造证券/提示注入/过期提案不产生订单 | **通过** | `tests/integration/test_ai_safety.py`（13 种异常输出 + 注入 + 过期） |
| 9 | 日亏损与回撤锁定在重启和反弹后保持 | **通过** | `tests/integration/test_risk.py::test_daily_loss_and_drawdown_locks_persist` |
| 10 | 暂停开仓不停保护；紧急停止不卖出资产 | **通过** | `::test_pause_keeps_protection_and_emergency_stop_does_not_sell` |
| 11 | 手动改变持仓触发对账差异与暂停，不擅自修正 | **通过**（伪券商）；真实账户 **未验证** | `tests/integration/test_orders.py::test_manual_app_trade_triggers_reconcile_lock_without_auto_fix` |
| 12 | 止损与另一退出信号同时出现不超卖 | **通过** | `::test_stop_and_ai_exit_together_never_oversell` |
| 13 | 现金账本与持仓按成交和费用平衡；模拟与实盘数据不混合 | **通过** | `tests/integration/test_isolation_security.py::test_ledger_balances_and_namespaces_never_mix` |
| 14 | 美股夏令时、跨午夜、马股午休/假日固定样例 | **通过** | `tests/unit/test_calendar_money.py` |
| 15 | 回测无未来 K 线、无同一收盘价立即成交 | **通过** | `tests/unit/test_backtest.py` |
| 16 | 无密钥新环境能启动演示，按钮产生真实状态变化 | **通过**（API 层全按钮）；前端浏览器冒烟见下文 | `tests/e2e/test_api_flow.py` |
| 17 | API 未连接时不显示连接成功、不把测试数据标为实时 | **通过** | `::test_unconnected_api_never_reports_success_or_fake_realtime` |
| 18 | 秘密不进入浏览器/日志/版本库；未鉴权/跨站状态变更被拒绝 | **通过** | `::test_secrets_never_leak_and_cross_site_rejected` |
| 19 | 一次启动后 AI 自动买入与卖出，无逐笔确认；部分减仓与全部退出 | **通过**（本地纸上交易 + LIVE_AUTO 执行路径用伪券商） | `tests/e2e/test_auto_trading_e2e.py::test_ai_buys_then_reduces_then_exits_without_confirmation[sim/live]` |
| 20 | 无均线交叉时 AI 可交易而基准不交易 | **通过** | `::test_ai_and_baseline_are_independent` |
| 21 | 浏览器关闭/会话过期后台继续；到期/撤销阻止新风险并保留保护退出 | **通过** | `::test_background_continues_without_browser_and_revocation_blocks_new_risk`、`::test_authorization_expiry_stops_new_risk_but_keeps_protection` |
| 22 | AI 无法扩大股票池、放宽止损、提高额度、修改政策 | **通过** | `tests/integration/test_ai_safety.py::test_ai_cannot_expand_pool_relax_stop_or_raise_limits` |
| 23 | 日/周任务按时运行、重启去重、缺数据不伪造 | **通过** | `tests/integration/test_learning.py::test_daily_weekly_jobs_run_once_and_survive_restart` |
| 24 | 候选不读未来数据、不改运行政策、不扩大风险 | **通过** | `::test_candidate_uses_only_post_cutoff_past_data_and_never_auto_activates`、`::test_candidate_changes_are_whitelisted` |
| 25 | 失败与证据不足候选被保留，不自动 ACTIVE | **通过** | `::test_failed_and_insufficient_candidates_are_kept_and_ready_needs_user` |
| 26 | 未批准经验不改变提示词；批准切换不重复下单、不移除保护 | **通过** | `::test_unverified_experience_never_enters_prompt_and_sources_are_separated`、`::test_approved_switch_keeps_protection_no_duplicate_orders_and_rollback_keeps_locks` |
| 27 | 升级/暂停/回退有审计；回退不清锁定；模拟经验不混入真实业绩 | **通过** | 同上 + `learning/scheduler.py::_post_upgrade_monitor` |

补充测试：事件触发的额外 AI 复核（限频）、基准策略「AI 可否决」只能否决、AI 目标仓位上限跟随已确认风控参数（`tests/integration/test_strategies_extra.py`）；
Anthropic 适配器响应处理（模拟客户端，`tests/unit/test_anthropic_provider.py`）；
只读诊断子进程输出解析（结果 JSON 前后夹有 SDK 日志）、按市场/币种检查现金字段（假 SDK 跑完整诊断流程，`tests/unit/test_diagnostics.py`）；
实盘闸门按市场要求现金字段通过（`test_isolation_security.py::test_cash_fields_capability_is_per_market_and_gate_keeps_strict_cash_threshold`）；
实时行情的快照合并与限速、证券类型向券商核实、演示证券不进入实时模式（`tests/unit/test_moomoo_quotes.py`，用假行情上下文，未接真实 OpenD）。

## 前端

- 构建：`cd frontend && npm run build`（`tsc -b && vite build`，TypeScript 严格模式）→ **通过，0 错误**。
- 浏览器冒烟（Playwright + Chromium）：先用一键启动脚本 `python3 scripts/launcher.py start` 在全新数据目录启动网页和后台工作进程，再运行
  `PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers npm run e2e` → **1 passed（26 秒）**。覆盖：设置密码 → 退出 → 登录 →
  启动向导开始演示 → 等到 AI 自动决策/持仓出现 → 逐页访问全部页面 → 只读诊断 → 证券图表 → 决策详情 → 运行回测 →
  点「暂停新开仓」并通过 API 确认状态改变 → 恢复 → 切换到实盘页确认「真实交易在部署配置中关闭」横幅；**全程无控制台错误**。
- 再次输入密码（428）流程：拦截请求模拟一次 428 → 弹窗 → 错误密码提示「密码错误」→ 正确密码后原请求重试成功。
- 手机宽度（390px）无横向滚动。
- 一键启动/停止脚本：在 Linux 上实测 `launcher.py start / status / stop` 正常；Windows（`start.bat`）、macOS 未实测。

## macOS 实机验证（2026-10-23，由在用户 Mac 上协助的 Codex 执行并回报）

- 环境：arm64 Mac，Python 3.13.14。
- 安装包：提交 `6e35c5c` 的 ZIP，SHA-256 `3f79140a5996cc563f9b474873a02d968dc5f18de988c640a1f193ec3baf60e5`，与发布值一致；已核对包含释放租约、约 35 秒租约重试、启动存活检查三处修复。
- 升级方式：在原安装路径更新源码，保留本机 `.env`、网页密码和数据库。
- 结果：`start → stop → 无额外等待立即 start` **通过**。新的模拟后台进程成功运行，数据库心跳距当前 0.8 秒。
- 配置状态：AI 密钥已配置（`key_configured=true`，未显示内容）、`LIVE_TRADING_ENABLED=false`、`AI_IN_REPLAY=false`。
- 未做的事：没有发送真实 AI 请求，没有任何交易。

### OpenD 只读接入（同日，Codex 在用户 Mac 上执行并回报）

- 安装包：官网下载页的 `moomoo_OpenD_10.11.7108_Mac.tar.gz`，长度与服务器一致。
  先前 `codesign` 失败是受限工具环境造成的；用正常系统权限重验：`codesign` valid on disk、
  Gatekeeper `accepted`（`source=Notarized Developer ID`，Shenzhen Futu，团队 ID 6HE4ZULJ45）、`stapler validate` 成功。正常安装，没有绕过任何安全设置。
- 用户在 OpenD 官方界面自行登录（密码未经过本系统或聊天）。
- 只读诊断结果：OpenD 连接、行情服务器登录、交易服务器登录 **通过**；`US.AAPL` 快照 **可读**；`MY.1155` **NEEDS_PERMISSION**（无马股行情权限）。
- 发现的问题：诊断页报 `JSONDecodeError: Extra data`。原因是只解析子进程标准输出的最后一行，而 SDK 关闭连接的日志可能打印在结果 JSON 之后。
  修复：从后往前逐行找「是对象且 `items` 为列表」的 JSON；找不到时照旧显示「诊断进程失败」。Mac 上先做了同样的最小修复并通过实测，源码分支已同步并补回归测试。
- 没有做的事：没有绑定真实账户、没有解锁交易、没有下单、没有发送真实 AI 请求。

### 美股券商模拟账户只读验证（同日，Codex 在用户 Mac 上执行并回报）

- 用户在诊断页选择了美股券商模拟账户（SIMULATE）运行只读诊断；**没有确认绑定、没有解锁交易、没有下单**。
- `US.AAPL` 行情 **PASS**；美股交易权限 **PASS**；限价单只读探测 **PASS**。
- 止损限价单 **UNSUPPORTED**（原文 "Paper trading does not support Stop Limit order"）。这只代表模拟环境；
  本系统的止损由工作进程盯盘后发限价卖单，不依赖原生止损单。真实账户上线前须在真实账户重新探测。
- 现金字段原判 **FAIL**。补充只读检查（只报告字段是否有值）：`us_cash`、`us_avl_withdrawal_cash` 有值，
  `my_cash`、`my_avl_withdrawal_cash` 缺失。原因是诊断要求马币和美元两种货币都有值，把与美股账户无关的马币缺失也判为失败。
- 修复：诊断只检查账户所在市场的结算币种（美股看 USD），失败时列出缺失字段名，结果按市场保存（`cash_fields_US`）；
  实盘闸门改为按绑定账户的每个市场分别要求通过，旧版不分市场的记录不再被采信。**下单风控不变**：
  实盘仍按交易币种要求「现金」与「可提现现金」都有值、取二者较小者，从不使用购买力。
  修复后需在 Mac 上重新运行一次只读诊断，确认 `US 现金与融资区分（USD）` 为 PASS。

### 修复版 `0bc7b1a` 在 Mac 上的复验（同日，Codex 执行并回报）

- 完整 ZIP 与 `SHA256SUMS.txt` 一致；覆盖源码时保留了 `.env`、`data/` 和网页密码；没有改动真实交易开关。
- 本机 `pytest tests/unit/test_diagnostics.py` → **6 passed**。
- 通过真实 OpenD 复查用户指定的美股券商模拟账户：美股行情 **PASS**、`cash_fields_US` **PASS**、不再出现 `cash_fields_MY`、
  限价单只读探测 **PASS**、原生止损限价单 **UNSUPPORTED**（预期）。
- 网页端：重新登录 → 重新获取账户列表 → 对同一 SIMULATE 账户 + `US.AAPL` 运行诊断，页面显示
  「US 现金与融资区分（USD）通过」「US 限价单通过」「US 止损限价单不支持」，诊断完成。
- 没有绑定账户、没有解锁交易、没有下单、没有调用付费 AI。

### 行情修复版 `6015058` 与免费模型检查（同日，Codex 在 Mac 上执行并回报）

- `6015058` 已安装，校验值一致；`test_moomoo_quotes.py` 与 `test_risk.py` 共 **12 passed**。
- 真实 OpenD：`US.AAPL` 类型核实为 STOCK；连续 10 次读取报价只发出 **1 次**快照请求；历史日 K 线 **251 根**（AI 至少需要 30 根）。
- 经用户明确批准的免费模型检查：`models.retrieve` 返回 `claude-opus-5-5`；`count_tokens` 成功，测试输入（系统提示 + 空数据）**1012 tokens**。
- 没有调用付费生成，没有启动交易，实盘关闭。

### `40da2ad` 安装与 LOCAL_PAPER 准备（同日，Codex 在 Mac 上执行并回报）

- `40da2ad` 已安装，月度 AI 预算改为 10 美元，配置与数据保留。完整测试 81 项通过。
  1 项（`test_unconnected_api_never_reports_success_or_fake_realtime`）因本机 OpenD 正在 11111 端口运行，违反了测试「未连接」的假设；
  改用测试专用端口 11112 重跑通过。已修复：测试改为自动选用一个没有程序监听的端口（云端用占用 11111 的方式复现并确认修复）。
- 汇率：BNM 2026-10-23，1 USD = 4.0840 MYR。虚拟资金 2,448 USD，可承受亏损 122 USD，单只上限（25%）612 USD。
- 五只候选实测都是 STOCK 且一股价格低于 612 USD：AAPL 348.22、MSFT 521.08、AMZN 250.38、JPM 336.54、XOM 178.23。
- moomoo 官方收费页（topic9_136）可读：佣金 0.03% 向上取整到美分；平台费 0.99 USD/单；结算费 0.003 USD/股，上限为成交额的 1%；
  SEC 费 0.0000206（仅卖出，最低 0.01）；TAF 0；CAT 0.000003 USD/股；印花税每 1,000 马币或不足收 1 马币（普通股上限 1,000 马币），折算为美元。
  原费用计算不支持向上取整、印花税分档和马币折算，已补上（`tests/unit/test_fees.py`），费用页也增加了对应输入项。
- 模拟页在启动纸上交易前仍使用演示数据，自选页会拒绝真实股票。已改为：OpenD 可连接时，用 moomoo 只读核实名称和类型后保存。
- 没有交易，没有付费生成，风控草稿未确认。

## 可复现示例

`python scripts/demo_e2e.py --ticks 900` → `data/examples/demo_run/`（已提交）：
固定测试响应 + 虚构演示数据，确定性可复现。里程碑：第 2 步自动买入 US.DMOA，第 112 步 AI 自动减仓，
第 203 步持有期限退出，第 373 步自动止损；全程无逐笔确认；每日中文日报与周度分析在 `reports.txt`；
另附同一数据上的基准均线回测（`backtest_baseline_*`，标注「样本量过小，结果不具统计意义」）。
真实模型版本（`--ai anthropic`）需要密钥，**未运行、未验证**。

## 未验证 / 限制（需要您的环境）

| 项目 | 原因 | 如何验证 |
| --- | --- | --- |
| 真实账户（REAL）的现金字段、原生止损、交易权限 | 只验证了美股券商模拟账户；模拟环境的结果不代表真实账户 | 启用实盘前在诊断页选择真实账户运行只读诊断 |
| 券商模拟账户下单、撤单、成交推送、对账（BROKER_PAPER） | 模拟账户已只读验证，但尚未确认绑定、尚未运行 | 用户批准后短期运行 BROKER_PAPER |
| 券商模拟账户（BROKER_PAPER）下单 | 同上 | 诊断通过并选择 SIMULATE 账户后用向导启动 |
| 真实下单、撤单、成交推送、对账（LIVE_REVIEW / LIVE_AUTO） | 交付时实盘关闭；仅伪券商故障注入验证 | 完成 README 第三节 C 的全部条件后，小额启用 |
| 马股实时行情 | 用户 Mac 实测 `MY.1155` 为 NEEDS_PERMISSION（当前账户无马股行情权限） | 开通权限后在诊断页重测；否则马股只用历史回放 |
| 真实 AI 模型表现与结构化输出兼容性 | 模型可用性与 token 计数已免费验证；尚未发出付费生成请求 | `.env` 填密钥后运行演示或 `scripts/demo_e2e.py --ai anthropic` |
| 费用、马股价格档位、节假日数据 | 测试值/第三方日历 | 按官方资料核对并在设置中更新 |
| 实盘股票池的全市场筛选 | 首版从已核对的自选/证券表中按规则筛选；未接入 `get_stock_filter` | — |
| 真实模型的候选改进自动评估 | 需前向影子模拟（首版未自动实现） | 见 `docs/learning-loop.md` |
| 邮件/推送告警 | 未配置（首版只有站内通知） | 无人值守实盘前需另行接入并验证 |
| Windows 一键脚本实机运行 | 未在 Windows 上测试（macOS 已实测，见上文） | 在 Windows 电脑运行 `scripts/start.bat` |
