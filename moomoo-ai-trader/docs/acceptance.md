# 验收结果（docs/acceptance.md）

测试日期：2026-10-23（云端开发环境，Linux，Python 3.13.16）。
运行命令：`.venv/bin/python -m pytest`（项目根目录）。结果：**66 passed**（约 60 秒）。前端构建与浏览器冒烟测试见「前端」一节。

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

补充测试：事件触发的额外 AI 复核（限频）、基准策略「AI 可否决」只能否决（`tests/integration/test_strategies_extra.py`）；
Anthropic 适配器响应处理（模拟客户端，`tests/unit/test_anthropic_provider.py`）。

## 前端

- 构建：`cd frontend && npm run build`（`tsc -b && vite build`，TypeScript 严格模式）→ **通过，0 错误**。
- 浏览器冒烟（Playwright + Chromium）：先用一键启动脚本 `python3 scripts/launcher.py start` 在全新数据目录启动网页和后台工作进程，再运行
  `PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers npm run e2e` → **1 passed（26 秒）**。覆盖：设置密码 → 退出 → 登录 →
  启动向导开始演示 → 等到 AI 自动决策/持仓出现 → 逐页访问全部页面 → 只读诊断 → 证券图表 → 决策详情 → 运行回测 →
  点「暂停新开仓」并通过 API 确认状态改变 → 恢复 → 切换到实盘页确认「真实交易在部署配置中关闭」横幅；**全程无控制台错误**。
- 再次输入密码（428）流程：拦截请求模拟一次 428 → 弹窗 → 错误密码提示「密码错误」→ 正确密码后原请求重试成功。
- 手机宽度（390px）无横向滚动。
- 一键启动/停止脚本：在 Linux 上实测 `launcher.py start / status / stop` 正常；Windows（`start.bat`）、macOS 未实测。

## 可复现示例

`python scripts/demo_e2e.py --ticks 900` → `data/examples/demo_run/`（已提交）：
固定测试响应 + 虚构演示数据，确定性可复现。里程碑：第 2 步自动买入 US.DMOA，第 112 步 AI 自动减仓，
第 203 步持有期限退出，第 373 步自动止损；全程无逐笔确认；每日中文日报与周度分析在 `reports.txt`；
另附同一数据上的基准均线回测（`backtest_baseline_*`，标注「样本量过小，结果不具统计意义」）。
真实模型版本（`--ai anthropic`）需要密钥，**未运行、未验证**。

## 未验证 / 限制（需要您的环境）

| 项目 | 原因 | 如何验证 |
| --- | --- | --- |
| OpenD 连接、账户列表、行情权限、现金字段、订单类型探测 | 本环境无 OpenD/账户 | 「连接与权限诊断」页运行只读诊断 |
| 券商模拟账户（BROKER_PAPER）下单 | 同上 | 诊断通过并选择 SIMULATE 账户后用向导启动 |
| 真实下单、撤单、成交推送、对账（LIVE_REVIEW / LIVE_AUTO） | 交付时实盘关闭；仅伪券商故障注入验证 | 完成 README 第三节 C 的全部条件后，小额启用 |
| 马股实时行情 | 官方资料不一致，权限页列为不支持 | 诊断页 `MY.xxxx` 快照检测；无行情时用历史回放 |
| 真实 AI 模型表现与结构化输出兼容性 | 无密钥 | `.env` 填密钥后运行演示或 `scripts/demo_e2e.py --ai anthropic` |
| 费用、马股价格档位、节假日数据 | 测试值/第三方日历 | 按官方资料核对并在设置中更新 |
| 实盘股票池的全市场筛选 | 首版从已核对的自选/证券表中按规则筛选；未接入 `get_stock_filter` | — |
| 真实模型的候选改进自动评估 | 需前向影子模拟（首版未自动实现） | 见 `docs/learning-loop.md` |
| 邮件/推送告警 | 未配置（首版只有站内通知） | 无人值守实盘前需另行接入并验证 |
| Windows/macOS 一键脚本实机运行 | 本环境为 Linux | 在您的电脑运行 `scripts/start.bat` / `start.sh` |
