/** 后端 JSON 结构（金额/价格均为十进制字符串）。 */

export type Dec = string;

export interface AuthState {
  password_set: boolean;
  logged_in: boolean;
}

export interface WorkerInfo {
  alive: boolean;
  heartbeat_at: string | null;
  age_s: number | null;
  status: { portfolios?: Record<string, PortfolioRuntime> } & Record<string, unknown>;
  pid?: number;
  started_at?: string;
}

export interface PortfolioRuntime {
  mode?: string;
  business_time?: string;
  is_replay?: boolean;
  data_source?: { name: string; label: string; connected: boolean; realtime: boolean };
  adapter?: { broker: string; trd_env: string; health: string };
  ai?: { label: string; is_fixture: boolean; pending_jobs: number };
  auth_ok?: boolean;
  auth_reason?: string;
  health_blocks?: string[];
  scope_size?: number;
  broker_total_assets?: Dec | null;
}

export interface SystemStatus {
  now_utc: string;
  now_myt: string;
  deployment: { live_trading_enabled: boolean; allow_client_side_stops_live: boolean };
  ai: { provider: string; label: string; model: string; is_fixture: boolean; key_present: boolean };
  workers: Record<string, WorkerInfo>;
  opend: { address: string; reachable: boolean };
}

export interface CurrencyView {
  currency: string;
  allocation: Dec;
  cash: Dec;
  reserved: Dec;
  available: Dec;
  market_value: Dec;
  equity: Dec;
  realized_pnl: Dec;
  unrealized_pnl: Dec;
  fees: Dec;
  max_drawdown_pct: Dec;
  day_pnl: Dec;
  total_pnl: Dec;
  missing_prices: string[];
}

export interface OverviewPosition {
  id: number;
  symbol: string;
  name: string;
  market: string;
  currency: string;
  qty: number;
  avg_cost: Dec;
  last: Dec | null;
  market_value: Dec | null;
  unrealized: Dec | null;
  unrealized_pct: Dec | null;
  stop_price: Dec | null;
  take_profit_price: Dec | null;
  max_hold_until: string | null;
  opened_at: string | null;
  strategy_version: string;
  reconcile_locked: boolean;
  quote_source: string | null;
  quote_ts: string | null;
}

export interface RiskLockRow {
  id: number;
  portfolio_id: number;
  kind: string;
  scope: string;
  reason: string;
  release_not_before: string | null;
  requires_manual_review: boolean;
  active: boolean;
  released_at: string | null;
  released_by: string;
  release_note: string;
  as_of: string;
  created_at_utc: string;
}

export interface AuditRow {
  id: number;
  category: string;
  action: string;
  actor: string;
  portfolio_id?: number | null;
  symbol: string | null;
  decision_id: string | null;
  intent_id: string | null;
  message: string;
  data?: unknown;
  as_of: string | null;
  created_at_utc: string;
}

export interface Overview {
  ns: string;
  deployment: { live_trading_enabled: boolean };
  worker: WorkerInfo;
  portfolio: null | {
    id: number;
    mode: string;
    mode_label: string;
    name: string;
    running: boolean;
    data_source: string;
    data_source_label: string;
    pause_new_entries: boolean;
    emergency_stop: boolean;
    business_time: string;
    business_time_myt: string;
    is_replay: boolean;
    runtime: PortfolioRuntime;
  };
  auth: null | {
    id: number;
    status: string;
    ok: boolean;
    reason: string;
    expires_at: string;
    summary_text: string;
    capital: Record<string, Dec>;
    max_planned_loss: Record<string, Dec>;
    scope: { type: string; universe_rule_id?: number | null; symbols?: string[] };
    overnight: boolean;
    strategy: string | null;
  };
  currencies: CurrencyView[];
  positions: OverviewPosition[];
  locks: RiskLockRow[];
  today: {
    day?: string;
    decisions?: Record<string, number>;
    orders?: Record<string, number>;
    executed?: { id: string; symbol: string; action: string; source: string; reason: string; as_of: string }[];
  };
  why_no_trade: { reason: string; count: number }[];
  allow_new_entries: { ok: boolean; reasons: string[] };
  notifications_unread: number;
  fx?: { pair: string; rate: Dec; source: string; as_of: string } | null;
  combined_myr?: { equity: Dec; note: string } | null;
  latest_daily_report?: { id: number; summary: string; period_start: string } | null;
  recent_events?: AuditRow[];
  account?: { broker_total_assets: Dec | null; note: string };
}

export interface Proposal {
  decision_id?: string;
  symbol?: string;
  action?: string;
  as_of_utc?: string;
  expires_at_utc?: string;
  evidence_ids?: string[];
  rationale?: string;
  risk_flags?: string[];
  entry_condition?: { type?: string; price?: Dec | null } | null;
  exit_condition?: { stop_loss_price?: Dec | null; take_profit_price?: Dec | null; max_holding_days?: number | null } | null;
  target_position_pct?: Dec | null;
  max_buy_price?: Dec | null;
  sell_fraction?: Dec | null;
  min_sell_price?: Dec | null;
  strategy_version?: string;
  [k: string]: unknown;
}

export interface Check {
  name: string;
  ok: boolean;
  detail: string;
}

export interface RuleCalc {
  ok?: boolean;
  checks?: Check[];
  sizing?: Record<string, string>;
  qty?: number;
  limit_price?: Dec;
  est_fee_buy?: Dec;
  est_fee_sell?: Dec;
  [k: string]: unknown;
}

export interface EvidenceItem {
  id: string;
  type: string;
  symbol?: string;
  desc?: string;
  title?: string;
  url?: string;
  source?: string;
  published_at?: string;
  fetched_at?: string;
  [k: string]: unknown;
}

export interface Decision {
  id: string;
  symbol: string;
  market: string;
  currency: string;
  action: string;
  source: string;
  as_of: string;
  expires_at: string | null;
  outcome: string;
  outcome_reason: string;
  strategy_version: string;
  mode: string;
  data_source: string;
  created_at_utc: string;
  ai_call_id: number | null;
  outcome_label: string;
  rationale: string;
  proposal: Proposal | null;
}

export interface DecisionFull extends Decision {
  facts: Record<string, unknown> | null;
  rule_calc: RuleCalc | null;
  model_judgment: { rationale?: string; risk_flags?: string[]; provider_label?: string; note?: string; [k: string]: unknown } | null;
  evidence: EvidenceItem[] | null;
  intents: Intent[];
  ai_call?: null | {
    id: number;
    provider: string;
    model: string;
    prompt_version: string;
    status: string;
    error: string;
    latency_ms: number;
    input_tokens: number;
    output_tokens: number;
    input_hash: string;
    created_at_utc: string;
  };
}

export interface AICall {
  id: number;
  provider: string;
  model: string;
  prompt_version: string;
  purpose: string;
  input_hash: string;
  input_snapshot: unknown;
  output_text: string;
  status: string;
  error: string;
  latency_ms: number;
  input_tokens: number;
  output_tokens: number;
  as_of: string | null;
  created_at_utc: string;
}

export interface Intent {
  id: string;
  symbol: string;
  market: string;
  currency: string;
  side: string;
  reason: string;
  qty: number;
  limit_price: Dec | null;
  status: string;
  status_detail: string;
  filled_qty: number;
  avg_fill_price: Dec | null;
  fees: Dec;
  est_fees: Dec | null;
  broker_order_id: string | null;
  decision_id: string | null;
  as_of: string | null;
  created_at_utc: string;
  updated_at_utc: string;
  expires_at: string | null;
  needs_manual_review: boolean;
  mode: string;
  strategy_version: string;
  stop_price: Dec | null;
  take_profit_price: Dec | null;
  ref_price: Dec | null;
  status_label: string;
}

export interface OrderEvent {
  id: number;
  intent_id: string;
  event: string;
  from_status: string | null;
  to_status: string | null;
  detail: string;
  payload: unknown;
  created_at_utc: string;
}

export interface FillRow {
  id: number;
  intent_id: string;
  portfolio_id: number;
  mode: string;
  broker: string;
  broker_fill_id: string;
  symbol: string;
  side: string;
  qty: number;
  price: Dec;
  fee: Dec;
  fee_estimated: boolean;
  currency: string;
  filled_at: string;
  created_at_utc: string;
}

export interface IntentFull extends Intent {
  risk_check: RuleCalc | null;
  events: OrderEvent[];
  fills: FillRow[];
}

export interface PositionRow {
  id: number;
  portfolio_id: number;
  mode: string;
  symbol: string;
  market: string;
  currency: string;
  qty: number;
  avg_cost: Dec;
  realized_pnl: Dec;
  fees_paid: Dec;
  status: string;
  opened_at: string | null;
  closed_at: string | null;
  entry_decision_id: string | null;
  strategy_version: string;
  stop_price: Dec | null;
  take_profit_price: Dec | null;
  max_hold_until: string | null;
  protection_version: string;
  entry_ref_price: Dec | null;
  max_qty: number;
  created_at_utc: string;
}

export interface Trace {
  decision?: DecisionFull | null;
  intents: IntentFull[];
  audit: AuditRow[];
  positions: (PositionRow | null)[];
}

export interface NotificationRow {
  id: number;
  level: string;
  title: string;
  body: string;
  read: boolean;
  created_at_utc: string;
}

export interface DiagItem {
  key: string;
  label: string;
  status: string;
  detail: string;
  hint?: string[];
  market?: string;
  capability?: string;
  [k: string]: unknown;
}

export interface DiagAccount {
  acc_id: string;
  acc_id_masked: string;
  trd_env: string;
  acc_type: string;
  security_firm: string;
  markets: string[];
  status: string;
  filter_market: string;
}

export interface DiagResult {
  opend?: string;
  security_firm?: string;
  sdk_version?: string;
  ok: boolean;
  items: DiagItem[];
  accounts: DiagAccount[];
  checked_at?: string;
  [k: string]: unknown;
}

export interface Binding {
  id: number;
  broker: string;
  trd_env: string;
  acc_id_masked: string;
  markets: string[];
  confirmed_by_user: boolean;
  verified_at: string | null;
  active: boolean;
  acc_type: string;
}

export interface Capability {
  id: number;
  market: string;
  item: string;
  status: string;
  detail: string;
  source: string;
  checked_at: string;
}

export interface ConnectionStatus {
  last: DiagResult | null;
  capabilities: {
    sim: Capability[];
    live: Capability[];
    sim_bindings: Binding[];
    live_bindings: Binding[];
  };
  login_steps: string[];
}

export interface GateCheck {
  name: string;
  ok: boolean;
  detail: string;
}

export interface LiveGate {
  passed: boolean;
  checks: GateCheck[];
  note: string;
}

export interface RiskVersion {
  id: number;
  version: number;
  params: Record<string, string | number | boolean>;
  confirmed_by_user: boolean;
  confirmed_at: string | null;
  is_test_seed: boolean;
  note: string;
  created_at_utc: string;
}

export interface RiskInfo {
  active: RiskVersion | null;
  versions: RiskVersion[];
  labels: Record<string, string>;
  hard_limits: Record<string, string | number>;
  seed: Record<string, string | number | boolean>;
  locks: RiskLockRow[];
  note: string;
}

export interface DiffRow {
  key: string;
  label: string;
  before: unknown;
  after: unknown;
  loosens: boolean;
}

export type PoolRules = {
  markets?: string[];
  sec_types?: string[];
  exclude_leveraged?: boolean;
  min_price?: Record<string, string>;
  max_price?: Record<string, string>;
  min_avg_turnover_20d?: Record<string, string>;
  exclude_symbols?: string[];
  max_members?: number;
  [k: string]: unknown;
};

export interface UniverseRule {
  id: number;
  version: number;
  rules: PoolRules;
  confirmed_by_user: boolean;
  confirmed_at: string | null;
  created_at_utc: string;
  note: string;
}

export interface CurrentRunScope {
  portfolio_id: number;
  mode: string;
  authorization_id: number;
  scope: { type: 'POOL' | 'WHITELIST'; universe_rule_id?: number; symbols?: string[] };
  expires_at: string;
}

export interface UniverseInfo {
  rules: UniverseRule[];
  labels: Record<string, string>;
  default_rules: PoolRules;
  auto_us_rules?: PoolRules;
  current_run?: CurrentRunScope | null;
  latest_snapshot: null | { id: number; members?: unknown; [k: string]: unknown };
  whitelist: string[];
  note: string;
}

export interface UniverseMember {
  symbol: string;
  name: string;
  market: string;
  sec_type: string;
  last_close: Dec | null;
  avg_turnover_20d: Dec | null;
  reasons?: string[];
  pool_rank?: number;
  market_cap_usd?: Dec | null;
  change_20d_pct?: number | null;
  change_60d_pct?: number | null;
  rank_score?: number | null;
}

export interface UniversePreview {
  as_of: string;
  members: UniverseMember[];
  excluded: UniverseMember[];
  data_source: string;
  disclaimer: string;
  stats?: { matched_total?: number; fetched?: number; verified?: number; members?: number; excluded?: number; method?: string; source?: string };
}

export interface InstrumentRow {
  symbol: string;
  name: string;
  market: string;
  sec_type: string;
  currency: string;
  lot_size: number;
  is_demo: boolean;
  is_leveraged: boolean;
  spread_bps?: number;
}

export interface QuoteSnap {
  id: number;
  symbol: string;
  last: Dec | null;
  bid: Dec | null;
  ask: Dec | null;
  volume: number | null;
  source: string;
  is_realtime: boolean;
  suspended: boolean;
  source_ts: string | null;
  recv_ts: string | null;
}

export interface WatchItem {
  id: number;
  symbol: string;
  in_whitelist: boolean;
  note: string;
  added_at: string;
  quote: QuoteSnap | null;
}

export interface Bar {
  day: string;
  open: Dec;
  high: Dec;
  low: Dec;
  close: Dec;
  volume: number;
  adj_close: Dec;
}

export interface InstrumentDetail {
  symbol: string;
  instrument: InstrumentRow | null;
  quote: null | {
    last: Dec | null;
    bid: Dec | null;
    ask: Dec | null;
    volume: number | null;
    prev_close: Dec | null;
    source: string;
    is_realtime: boolean;
    source_ts: string | null;
    recv_ts: string | null;
    suspended: boolean;
    note: string;
    delay_seconds: number | null;
    spread_bps: number | null;
  };
  bars: Bar[];
  features: Record<string, unknown>;
  data_source: string;
  error: string;
  last_snapshot: QuoteSnap | null;
  as_of: string;
  disclaimer: string;
}

export interface StrategyVersion {
  id: number;
  key: string;
  version: string;
  params: Record<string, unknown>;
  prompt_version: string;
  status: string;
  parent_id: number | null;
  approved_by_user: boolean;
  approved_at: string | null;
  note: string;
  created_at_utc: string;
}

export interface Deployment {
  id: number;
  strategy_key: string;
  from_version_id: number | null;
  to_version_id: number;
  action: string;
  candidate_id: number | null;
  actor: string;
  note: string;
  created_at_utc: string;
}

export interface ConfigDraft {
  id: number;
  target: string;
  request_text: string;
  before: unknown;
  after: unknown;
  diff: DiffRow[];
  loosens_risk: boolean;
  status: string;
  created_by: string;
  created_at_utc: string;
  decided_at: string | null;
}

export interface FeeItem {
  name: string;
  side?: string;
  rate?: string;
  per_share?: string;
  min?: string;
  max?: string;
  max_rate_of_value?: string;
  fixed?: string;
  rounding?: string;
  block_fee?: string;
  block_value?: string;
  block_currency?: string;
  fx_rate?: string;
  block_max?: string;
  [k: string]: unknown;
}

export interface FeeSchedule {
  id: number;
  market: string;
  params: { label?: string; items?: FeeItem[] };
  source: string;
  as_of: string;
  is_test: boolean;
  active: boolean;
  created_at_utc: string;
}

export interface FeesInfo {
  items: FeeSchedule[];
  ai_pricing: { input_per_mtok_usd: string; output_per_mtok_usd: string; source: string; model: string };
  cost_items: string[];
}

export interface WizardPreview {
  summary: string;
  problems: string[];
  fee_impact: { currency: string; max_trade: Dec; round_trip_fee?: Dec; fee_pct?: Dec; note: string }[];
  risk_params: Record<string, string | number | boolean> | null;
  pool_rule: UniverseRule | null;
  pool_rule_confirmed: boolean;
  gate: GateCheck[] | null;
  warning: string;
}

export interface BacktestMetrics {
  assumptions?: string[];
  per_currency?: Record<
    string,
    {
      initial: Dec;
      final_equity: Dec;
      net_return_pct: Dec;
      max_drawdown_pct: Dec;
      benchmark_buy_hold_equal_weight_pct: Dec;
      benchmark_cash_pct: Dec;
      round_trips?: number;
      net_pnl?: Dec;
      fees?: Dec;
      payoff_ratio?: Dec | null;
      profit_factor?: Dec | null;
    }
  >;
  round_trips?: number;
  buys?: number;
  win_rate_pct?: Dec;
  avg_holding_days?: Dec;
  fees_by_currency?: Record<string, Dec>;
  fee_share_of_gross_pct?: Record<string, Dec> | null;
  sample_days?: number;
  open_positions_at_end?: number;
  sample_warning?: string;
  label?: string;
  data_source?: string;
  leakage_note?: string;
  [k: string]: unknown;
}

export interface BacktestSummary {
  id: number;
  kind: string;
  strategy_key: string;
  params: Record<string, unknown>;
  result: BacktestMetrics;
  created_at_utc: string;
}

export interface BacktestFull extends BacktestSummary {
  trades: Record<string, unknown>[];
  equity_curve: Record<string, string>[];
  status: string;
}

export interface ReviewRun {
  id: number;
  kind: string;
  dedupe_key: string;
  portfolio_id: number;
  period_start: string;
  period_end: string;
  status: string;
  summary: string;
  stats: Record<string, unknown>;
  data_source: string;
  created_at_utc: string;
}

export interface Candidate {
  id: number;
  review_run_id: number;
  dedupe_key: string;
  base_strategy_version_id: number;
  candidate_strategy_version_id: number | null;
  hypothesis: string;
  changes: { path?: string; from?: unknown; to?: unknown; [k: string]: unknown };
  evidence: Record<string, unknown>;
  expected_benefit: string;
  possible_cost: string;
  data_cutoff: string;
  eval_plan: Record<string, unknown>;
  status: string;
  status_reason: string;
  proposed_by: string;
  created_at_utc: string;
  updated_at_utc: string;
}

export interface LearningInfo {
  data_source: string | null;
  mode: string | null;
  experience_counts: Record<string, number>;
  trade_classes: Record<string, number>;
  active_version: StrategyVersion | null;
  candidates: Candidate[];
  evaluations: { id: number; candidate_id: number; window_start: string; window_end: string; verdict: string; notes: string; created_at_utc: string }[];
  deployments: Deployment[];
  weekly: ReviewRun[];
  tried_total: number;
  failed_total: number;
  note: string;
}

export interface SettingsInfo {
  ai: {
    provider: string;
    model: string;
    key_present: boolean;
    key_hint: string;
    timeout_s: number;
    max_retries: number;
    monthly_call_limit: number;
    monthly_token_limit: number;
    monthly_budget_usd: string;
    price_input_per_mtok: string;
    price_output_per_mtok: string;
    price_source: string;
    min_interval_minutes: number;
  };
  deployment: {
    live_trading_enabled: boolean;
    allow_client_side_stops_live: boolean;
    app: string;
    opend: string;
    security_firm: string;
  };
  data_dir: string;
  how_to_change: string;
  backup: string;
  notifications: string;
}

export interface AIUsage {
  calls: number;
  input_tokens: number;
  output_tokens: number;
  estimated_cost_usd: string;
  call_limit: number;
  token_limit: number;
  budget_usd: string;
  price_source: string;
  [k: string]: unknown;
}

export interface CommandRow {
  id: number;
  type: string;
  payload: unknown;
  status: string;
  result: string;
  created_by: string;
  created_at_utc: string;
  processed_at: string | null;
}

export interface Items<T> {
  items: T[];
}
