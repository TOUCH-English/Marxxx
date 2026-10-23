/** 界面上使用的中文标签（与后端 core/enums.py 保持一致）。 */

export type Ns = 'sim' | 'live';

export const NS_LABEL: Record<Ns, string> = { sim: '自动模拟', live: '真实自动交易' };

export const MODE_LABELS: Record<string, string> = {
  DEMO: '演示（固定数据·本地模拟）',
  LOCAL_PAPER: '本地纸上交易（本地模拟成交）',
  BROKER_PAPER: '券商模拟账户',
  LIVE_REVIEW: '实盘·逐笔确认',
  LIVE_AUTO: '实盘·自动交易',
};

export const ORDER_STATUS_LABELS: Record<string, string> = {
  CREATED: '已建立',
  RISK_REJECTED: '被风控拒绝',
  AWAITING_APPROVAL: '待确认',
  SUBMITTING: '提交中',
  ACKNOWLEDGED: '已提交（券商已接收）',
  PARTIALLY_FILLED: '部分成交',
  FILLED: '全部成交',
  CANCEL_PENDING: '撤单中',
  CANCELLED: '已撤销',
  REJECTED: '被拒绝',
  UNKNOWN: '状态未知（需核对）',
};

export const ORDER_STATUS_COLOR: Record<string, string> = {
  CREATED: 'default',
  RISK_REJECTED: 'volcano',
  AWAITING_APPROVAL: 'gold',
  SUBMITTING: 'processing',
  ACKNOWLEDGED: 'blue',
  PARTIALLY_FILLED: 'cyan',
  FILLED: 'green',
  CANCEL_PENDING: 'orange',
  CANCELLED: 'default',
  REJECTED: 'red',
  UNKNOWN: 'magenta',
};

export const OPEN_ORDER_STATUSES = ['CREATED', 'AWAITING_APPROVAL', 'SUBMITTING', 'ACKNOWLEDGED', 'PARTIALLY_FILLED', 'CANCEL_PENDING', 'UNKNOWN'];
export const CANCELLABLE_STATUSES = ['CREATED', 'SUBMITTING', 'ACKNOWLEDGED', 'PARTIALLY_FILLED'];

export const DECISION_OUTCOME_LABELS: Record<string, string> = {
  AUTO_EXECUTED: '已自动执行',
  WAITING_PRICE: '等待价格条件',
  RISK_BLOCKED: '被风控拦截',
  NO_ACTION: '无需交易',
  AWAITING_APPROVAL: '待确认',
  INVALID: '无效（未产生订单）',
  EXPIRED: '已过期（未产生订单）',
};

export const DECISION_OUTCOME_COLOR: Record<string, string> = {
  AUTO_EXECUTED: 'green',
  WAITING_PRICE: 'blue',
  RISK_BLOCKED: 'volcano',
  NO_ACTION: 'default',
  AWAITING_APPROVAL: 'gold',
  INVALID: 'red',
  EXPIRED: 'default',
};

export const ACTION_LABELS: Record<string, string> = {
  BUY_PROPOSAL: '买入',
  SELL_PROPOSAL: '卖出 / 减仓',
  HOLD: '继续持有',
  NO_TRADE: '不交易',
};

export const ACTION_COLOR: Record<string, string> = {
  BUY_PROPOSAL: 'red',
  SELL_PROPOSAL: 'green',
  HOLD: 'blue',
  NO_TRADE: 'default',
};

export const DECISION_SOURCE_LABELS: Record<string, string> = {
  AI_MODEL: '真实 AI 模型',
  AI_FIXTURE: '固定测试响应（非真实 AI）',
  PROTECTION: '程序保护规则',
  BASELINE_MA: '固定规则基准',
  USER: '用户操作',
};

export const SIDE_LABELS: Record<string, string> = { BUY: '买入', SELL: '卖出' };

export const INTENT_REASON_LABELS: Record<string, string> = {
  ENTRY: '开仓',
  EXIT_STOP: '止损退出',
  EXIT_TAKE_PROFIT: '止盈退出',
  EXIT_TIME: '持有期满退出',
  EXIT_AI: 'AI 决定卖出',
  EXIT_SIGNAL: '信号退出',
  MANUAL_LIQUIDATE: '手动清仓',
};

export const LOCK_KIND_LABELS: Record<string, string> = {
  DAILY_LOSS: '当日亏损暂停',
  DRAWDOWN: '最大回撤暂停',
  MAX_LOSS: '计划亏损停止线',
  PLANNED_LOSS: '计划亏损停止线',
  UNKNOWN_ORDER: '存在状态未知订单',
  RECONCILE: '对账差异',
  MANUAL: '人工锁定',
  HEALTH: '健康检查',
};

/** 不能在页面上人工解除的锁定类型及原因。 */
export const LOCK_NOT_RELEASABLE: Record<string, string> = {
  DAILY_LOSS: '当日亏损锁定只能在马来西亚时间日界后、健康检查通过时由系统自动解除',
  UNKNOWN_ORDER: '请先在「订单与持仓」核对所有状态未知的订单，系统会自动解除',
};

export const CANDIDATE_STATUS: Record<string, { label: string; color: string }> = {
  PROPOSED: { label: '已提出', color: 'default' },
  SIMULATING: { label: '隔离模拟中', color: 'processing' },
  INSUFFICIENT_EVIDENCE: { label: '证据不足', color: 'orange' },
  REJECTED: { label: '未通过', color: 'red' },
  READY_FOR_REVIEW: { label: '可供审核', color: 'gold' },
  APPROVED: { label: '已批准', color: 'cyan' },
  ACTIVE: { label: '运行中', color: 'green' },
  ROLLED_BACK: { label: '已回退', color: 'purple' },
};

export const EXPERIENCE_KIND_LABELS: Record<string, string> = {
  CLOSED_TRADE: '已平仓交易',
  RISK_REJECTED: '被风控拒绝',
  NO_TRADE: '主动不交易',
  EXECUTION_FAILURE: '执行故障',
};

export const DIAG_STATUS: Record<string, { label: string; color: string }> = {
  PASS: { label: '通过', color: 'green' },
  FAIL: { label: '失败', color: 'red' },
  NEEDS_PERMISSION: { label: '需要权限', color: 'orange' },
  UNSUPPORTED: { label: '不支持', color: 'volcano' },
  INFO: { label: '信息', color: 'blue' },
  SKIPPED: { label: '已跳过', color: 'default' },
};

export const CAPABILITY_STATUS: Record<string, { label: string; color: string }> = {
  LOCAL_PASSED: { label: '本机检测通过', color: 'green' },
  NEEDS_PERMISSION: { label: '需要权限', color: 'orange' },
  UNSUPPORTED: { label: '不支持', color: 'volcano' },
  UNVERIFIED: { label: '未验证', color: 'default' },
};

export const DATA_SOURCE_LABELS: Record<string, string> = {
  DEMO_FIXTURE: '演示固定数据（虚构证券，本地模拟成交）',
  HISTORICAL_REPLAY: '历史回放（非实时，本地模拟成交）',
  MOOMOO_REALTIME: 'moomoo OpenD 实时行情',
};

export const AUDIT_CATEGORIES = ['CONTROL', 'ORDER', 'DECISION', 'AI', 'RISK', 'CONFIG', 'CONNECTION', 'UNIVERSE', 'REVIEW', 'STRATEGY', 'LEDGER', 'RECONCILE', 'SYSTEM', 'AUTH'];

export const AUDIT_CATEGORY_LABELS: Record<string, string> = {
  CONTROL: '控制操作',
  ORDER: '订单',
  DECISION: '决策',
  RISK: '风控',
  CONFIG: '配置',
  CONNECTION: '连接',
  UNIVERSE: '股票池',
  REVIEW: '复盘',
  STRATEGY: '策略版本',
  LEDGER: '账本',
  AI: 'AI 调用',
  SYSTEM: '系统',
  RECONCILE: '对账',
  AUTH: '授权',
};

export const MARKET_LABELS: Record<string, string> = { MY: '马股', US: '美股' };
export const SEC_TYPE_LABELS: Record<string, string> = { STOCK: '股票', ETF: 'ETF' };
export const TRD_ENV_LABELS: Record<string, string> = { SIMULATE: '券商模拟', REAL: '真实账户', LOCAL: '本地模拟' };

export const FEATURE_LABELS: Record<string, string> = {
  bars_available: '可用日K数量',
  ret_5d: '5 日收益',
  ret_20d: '20 日收益',
  ret_60d: '60 日收益',
  vol_20d: '20 日波动率',
  sma_10: '10 日均线',
  sma_20: '20 日均线',
  sma_60: '60 日均线',
  atr_14: '14 日 ATR（平均真实波幅）',
  atr_pct: 'ATR 占价格比例',
  avg_turnover_20d: '20 日平均成交额',
  dist_from_252d_high: '距 52 周高点',
  last: '最新价',
  chg_today: '今日涨跌',
  spread_bps: '买卖价差（基点）',
};

/** 风控参数中以比例（0.10 = 10%）保存的键。 */
export function isRatioParam(key: string): boolean {
  return key.endsWith('_pct');
}

export const RISK_PARAM_HELP: Record<string, string> = {
  max_position_pct: '一只股票最多占用策略资金的比例，防止过度集中。',
  max_total_exposure_pct: '所有持仓合计最多占用的比例，其余保持现金。',
  risk_per_trade_pct: '每笔交易如果触发止损，计划最多亏损策略资金的比例。',
  max_positions: '同时最多持有几只股票。',
  max_daily_new_orders: '每天最多开几笔新仓。',
  daily_loss_pct: '当天亏损达到此比例后，暂停新开仓直到下一交易日。',
  max_drawdown_pct: '从净值高点回落达到此比例后，暂停新开仓，需要人工复核。',
  max_spread_bps: '买卖价差过大时不开仓（1 基点 = 0.01%）。',
  default_max_hold_days: '默认最长持有天数，到期由程序自动卖出。',
};

const FEATURE_RATIO_KEYS = new Set(['ret_5d', 'ret_20d', 'ret_60d', 'vol_20d', 'atr_pct', 'dist_from_252d_high', 'chg_today']);

/** 技术特征的显示：收益/波动类显示为百分比，成交额加千分位。 */
export function fmtFeature(key: string, v: unknown): string {
  if (v === null || v === undefined || v === '') return '数据不足';
  if (FEATURE_RATIO_KEYS.has(key) && (typeof v === 'number' || typeof v === 'string')) {
    const n = Number(v);
    if (Number.isFinite(n)) return `${(n * 100).toFixed(2)}%`;
  }
  if (key === 'avg_turnover_20d' && (typeof v === 'number' || typeof v === 'string')) {
    const n = Number(v);
    if (Number.isFinite(n)) return Math.round(n).toLocaleString('en-US');
  }
  return String(v);
}
