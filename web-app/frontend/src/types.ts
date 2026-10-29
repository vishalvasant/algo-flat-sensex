export type DecisionFlowStageStatus = "ok" | "warn" | "block" | "pending" | "idle";

export interface DecisionFlowStage {
  id: string;
  label: string;
  status: DecisionFlowStageStatus;
  detail: string;
  value?: string | number | null;
}

export interface DecisionFlowSnapshot {
  stages: DecisionFlowStage[];
  cash_index_spot?: number | null;
  fut_spot?: number | null;
  basis_points?: number | null;
  basis_limit_points?: number;
  basis_ok?: boolean;
  fut_vs_vwap?: string | null;
  structure_5m?: string | null;
  bias_5m?: string | null;
  levels_anchor?: string | null;
  vwap_source?: string | null;
  momentum_source?: string | null;
  momentum_aligned?: boolean;
  no_trade_code?: string | null;
  vix_level?: number | null;
  vix_regime?: string | null;
  sector_breadth_pct?: number | null;
  advance_pct?: number | null;
  ce_strength?: number | null;
  pe_strength?: number | null;
  ce_pe_verdict?: string | null;
  early_bird_active?: boolean;
  reversal_risk_elevated?: boolean;
  data_quality?: string | null;
  ml_shadow?: Record<string, unknown> | null;
}

export interface MarketIntelligenceSnapshot {
  enabled?: boolean;
  version?: string;
  vix?: Record<string, unknown>;
  breadth?: Record<string, unknown>;
  sectors?: Record<string, unknown>;
  ce_pe?: Record<string, unknown>;
  premium?: Record<string, unknown>;
  global?: Record<string, unknown>;
  early_bird?: Record<string, unknown>;
  reversal?: Record<string, unknown>;
  divergence?: Record<string, unknown>;
  data_quality?: Record<string, unknown>;
  fut?: Record<string, unknown>;
  spot?: Record<string, unknown>;
}

export interface LevelMapSummary {
  supports?: number[];
  resistances?: number[];
  nearest_support?: number | null;
  nearest_resistance?: number | null;
  distance_to_support_pts?: number | null;
  distance_to_resistance_pts?: number | null;
  at_support?: boolean;
  at_resistance?: boolean;
  confluence_support?: number;
  confluence_resistance?: number;
  level_labels?: Record<string, number>;
  rejection_setup?: string | null;
  rejection_level?: string | null;
  rejection_signals?: string[];
  break_confirmed?: Record<string, boolean>;
}

export interface MarketSummary {
  underlying: string;
  active_underlying?: string;
  commodities?: CommoditySnapshot[];
  spot_ltp: number | null;
  session_vwap: number | null;
  spot_vs_vwap: string | null;
  atm_strike: number | null;
  bias_5m: string;
  market_session: string;
  market_open: boolean;
  strategy: string;
  regime?: string | null;
  confidence?: number | null;
  bullish_confidence?: number | null;
  bearish_confidence?: number | null;
  router_confidence?: number | null;
  bias_confidence_mismatch?: boolean;
  mtf_confidence_gap?: number;
  trading_mode: string;
  ist_time?: string;
  today_pnl: number;
  today_realized_pnl?: number;
  broker_day_pnl?: number;
  broker_realized_pnl?: number;
  broker_unrealized_pnl?: number;
  algo_today_pnl?: number;
  trade_count: number;
  starting_capital?: number;
  available_capital?: number;
  deployed_capital?: number;
  used_margin?: number;
  has_open_position?: boolean;
  auto_trading_active?: boolean;
  auto_trade_enabled?: boolean;
  scan_interval_seconds?: number;
  candidate_count?: number;
  rejection_count?: number;
  consecutive_losses?: number;
  max_daily_loss?: number;
  max_deployed_pct_of_equity?: number;
  kill_switch?: boolean;
  entries_blocked?: boolean;
  block_reason?: string | null;
  feed_mode?: string;
  ws_open?: boolean;
  ws_last_quote_ts?: string | null;
  ws_quote_age_sec?: number | null;
  last_quote_ts?: string | null;
  quote_age_sec?: number | null;
  feed_detail?: string;
  expiry_symbol?: string | null;
  is_expiry_day?: boolean;
  instrument_count?: number;
  recent_rejections?: Array<{ tsym: string; reasons: string[] }>;
  open_position?: {
    tsym: string;
    quantity: number;
    entry_price: number;
    current_ltp?: number;
    unrealized_pnl?: number;
    side?: string;
    setup_type?: string;
    premium_deployed?: number;
  };
  open_positions?: Array<{
    tsym: string;
    side: string;
    quantity: number;
    entry_price: number;
    current_ltp: number;
    unrealized_pnl: number;
    premium_deployed: number;
    setup_type: string;
  }>;
  open_position_count?: number;
  unrealized_pnl?: number;
  equity?: number;
  risk_dashboard?: {
    realized_pnl: number;
    unrealized_pnl: number;
    total_pnl: number;
    loss_budget_pct: number;
    margin_deploy_pct: number;
    risk_pct: number;
    risk_driver: "none" | "margin" | "loss_budget";
    has_open_position: boolean;
    entry_status: string;
  };
  unread_notifications?: number;
  levels?: LevelMapSummary | null;
  cash_index_spot?: number | null;
  fut_spot?: number | null;
  basis_points?: number | null;
  fut_vs_vwap?: string | null;
  decision_flow?: DecisionFlowSnapshot | null;
  market_intelligence?: MarketIntelligenceSnapshot | null;
  ml_shadow?: Record<string, unknown> | null;
  spot_fut?: Record<string, unknown> | null;
  market_ticks?: Record<string, unknown> | null;
}

export type Severity = "info" | "warning" | "critical";

export interface FlattradeSession {
  user_id: string;
  source: string;
  expires_at: string;
  valid: boolean;
}

export interface FlattradeCredentialsStatus {
  source: string;
  provider?: string;
  user_id?: string | null;
  api_key_masked: string | null;
  api_secret_set: boolean;
  password_set?: boolean;
  totp_secret_set?: boolean;
  access_token_set?: boolean;
  analytic_token_set?: boolean;
  token_expires_at?: string | null;
  redirect_url: string;
  has_api_credentials: boolean;
  has_auto_login: boolean;
  has_access_token?: boolean;
}

export interface EngineHealth {
  status: string;
  trading_mode: string;
  db_ok: boolean;
  broker_connected: boolean;
  market_open?: boolean;
  market_session?: string;
  broker_session: FlattradeSession | null;
  broker_credentials?: {
    provider?: string;
    configured: boolean;
    has_auto_login?: boolean;
    has_access_token?: boolean;
  };
  flattrade_session: FlattradeSession | null;
  flattrade_credentials?: {
    provider?: string;
    configured: boolean;
    has_auto_login: boolean;
    has_access_token?: boolean;
  };
  spot_ltp: string | null;
  instrument_count: number;
  last_quote_ts: string | null;
  kill_switch?: boolean;
  ts: string;
  error?: string;
}

export interface Notification {
  id: string;
  ts: string;
  type: string;
  severity: Severity;
  title: string;
  message: string;
  read: boolean;
}

export interface Trade {
  id: string;
  tsym?: string;
  side?: string;
  instrument_token?: string;
  entry_ts: string;
  exit_ts: string;
  entry_price?: number;
  exit_price?: number;
  quantity?: number;
  lot_size?: number;
  lots?: number;
  pnl: string | number;
  pnl_pct?: number | null;
  mfe?: number | null;
  mae?: number | null;
  exit_reason: string;
  setup_type: string;
  hold_seconds?: number | null;
  mode: string;
}

export interface TradesReportSummary {
  trades: number;
  wins: number;
  losses: number;
  win_rate_pct: number;
  total_pnl: number;
  avg_pnl: number;
  best_trade: number;
  worst_trade: number;
  gross_profit: number;
  gross_loss: number;
}

export interface TradesReport {
  from_date: string | null;
  to_date: string | null;
  generated_at: string;
  summary: TradesReportSummary;
  by_exit_reason: Record<string, { count: number; pnl: number }>;
  by_setup: Record<string, { count: number; pnl: number }>;
  by_day: Record<string, { count: number; pnl: number }>;
  trades: Trade[];
}

export interface WatchlistItem {
  token: string;
  tsym: string;
  strike: number;
  option_type: string;
  is_atm: boolean;
  tradable: boolean;
  exchange?: string;
  lot_size?: number;
  ltp: number | null;
  bid: number | null;
  ask: number | null;
  volume: number | null;
  oi: number | null;
  iv?: number | null;
  delta?: number | null;
  gamma?: number | null;
  theta?: number | null;
  vega?: number | null;
  greeks_source?: string;
  last_update_ts: string | null;
}

export interface CommoditySnapshot {
  underlying: string;
  display_name?: string;
  spot_ltp: number | null;
  /** Futures LTP for option-chain ATM when spot_ltp is NSE cash index. */
  trading_spot_ltp?: number | null;
  session_open?: number | null;
  /** Previous session close — broker day-change baseline. */
  prev_close?: number | null;
  change?: number | null;
  change_pct?: number | null;
  atm_strike: number | null;
  expiry_symbol?: string | null;
  expiry_label?: string | null;
  fut_tsym?: string | null;
  card_type?: "tradeable" | "display" | "fut";
  instrument_count?: number;
  strike_band_points?: number;
  strike_step?: number;
  atm_strike_steps?: number;
  strike_count?: number;
  items?: WatchlistItem[];
}

export interface Watchlist {
  underlying: string;
  active_underlying?: string;
  commodities?: CommoditySnapshot[];
  atm_strike_steps?: number;
  spot_ltp: number | null;
  atm_strike: number | null;
  expiry_symbol?: string | null;
  instrument_count: number;
  strike_count?: number;
  strike_band_points?: number;
  strike_step?: number;
  last_quote_ts: string | null;
  feed_mode?: string;
  ws_open?: boolean;
  ws_last_quote_ts?: string | null;
  ws_quote_age_sec?: number | null;
  quote_age_sec?: number | null;
  market_open?: boolean;
  greeks_source?: string;
  items: WatchlistItem[];
  open_positions?: WatchlistOpenPosition[];
  bias_5m?: string;
  bullish_confidence?: number | null;
  bearish_confidence?: number | null;
  router_confidence?: number | null;
  bias_confidence_mismatch?: boolean;
  mtf_confidence_gap?: number;
}

export interface WatchlistOpenPosition {
  position_id?: string;
  instrument_token?: string;
  tsym: string;
  side?: string;
  quantity: number;
  lot_size?: number;
  lots?: number;
  entry_price: number;
  entry_price_source?: string;
  order_id?: string;
  entry_ts?: string;
  current_ltp?: number;
  unrealized_pnl?: number;
  premium_deployed?: number;
  setup_type?: string;
  stop_loss?: number | null;
  target_price?: number | null;
  managed?: boolean;
  source?: string;
  broker_entry_price?: number;
  mfe_points?: number;
  mfe_inr?: number;
  mfe_pct?: number;
  mae_points?: number;
  pnl_points?: number;
  trail_floor?: number | null;
  last_tick_ts?: string | null;
}

export interface PositionStreamPayload {
  ts?: string;
  feed_mode?: string;
  ws_open?: boolean;
  open_position_count?: number;
  open_positions: WatchlistOpenPosition[];
}

export interface ClosedBlotterTrade {
  id: string;
  tsym: string;
  side?: string;
  entry_ts: string;
  exit_ts: string;
  entry_price: number;
  exit_price: number;
  quantity: number;
  lot_size: number;
  lots: number;
  pnl: number;
  exit_reason: string;
  setup_type?: string;
  hold_seconds?: number;
}

export interface DecisionLogEvent {
  id: string;
  ts: string;
  event_type: string;
  severity: string;
  message: string;
  metadata: Record<string, unknown>;
}

export interface TradeBlotter {
  open_positions: WatchlistOpenPosition[];
  closed_trades: ClosedBlotterTrade[];
  today_pnl?: number;
}

export interface OhlcBar {
  ts: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number | null;
}

export interface ChartCandlesResponse {
  underlying: string;
  interval: string;
  price_source?: string;
  instrument_token?: string | null;
  fut_tsym?: string | null;
  bars: OhlcBar[];
}

export interface AuthUser {
  username: string;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  username: string;
  expires_at: string;
}
