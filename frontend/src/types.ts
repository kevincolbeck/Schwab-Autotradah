// Matches the _build_payload() shape from backend/api/websocket.py

export interface TickerState {
  price: number
  bid: number
  ask: number
  // GEX / key levels
  gex_call_wall: number | null
  gex_put_wall: number | null
  gex_gamma_flip: number | null
  prev_day_high: number | null
  prev_day_low: number | null
  opening_range_high: number | null
  opening_range_low: number | null
  round_number_above: number | null
  round_number_below: number | null
  // Opening play (pre-market)
  opening_play: OpeningPlay | null
  // Footprint
  footprint_delta_1m: number | null
  footprint_delta_5m: number | null
  footprint_absorption: boolean | null
  footprint_absorption_side: string | null  // "BULLISH" | "BEARISH"
  // L2
  l2_imbalance: number | null
  l2_bid_wall_price: number | null
  l2_bid_wall_size: number | null
  l2_ask_wall_price: number | null
  l2_ask_wall_size: number | null
  // Filters
  iv_rank: number | null
  rvol: number | null
  vwap: number | null
  // Options flow
  sweep_detected: boolean
  sweep_direction: string | null
  large_print_detected: boolean
  large_print_direction: string | null
  // Last signal evaluation
  last_signal: LastSignal | null
}

export interface OpeningPlay {
  direction: string | null       // "LONG" | "SHORT" | null
  conviction: number
  gap_pct: number | null
  pm_momentum_pct: number | null
  l2_direction: string | null
  gex_blocking: boolean
  queued: boolean
  fired: boolean
}

export interface LastSignal {
  ts: string
  direction: string | null
  conviction_score: number | null
  gate_passed: boolean
  gated_by: string | null
  signal_fired: boolean
}

export interface OpenPosition {
  trade_id: number
  ticker: string
  direction: string
  option_symbol: string
  option_strike: number
  option_type: string
  option_expiry: string
  entry_price: number
  contracts: number
  premium_paid: number
  stop_price: number
  target_2r: number
  target_3r: number
  current_mid: number | null
  unrealized_pnl: number | null
  unrealized_pct: number | null
}

export interface PnlPoint {
  ts: string
  cumulative_pnl: number
}

export interface WsPayload {
  type: string
  ts: string
  bot_status: 'RUNNING' | 'PAUSED' | 'STOPPED'
  market_open: boolean
  time_window: string  // "MORNING" | "AFTERNOON" | "OUTSIDE" | "PRE_OPEN_WATCH"
  vix: number | null
  vix_regime: string | null
  circuit_breaker: boolean
  circuit_breaker_reason: string | null
  paper_balance: number
  paper_day_start: number
  daily_pnl: number
  total_trades: number
  winning_trades: number
  premarket_done: boolean
  open_positions: OpenPosition[]
  pnl_curve: PnlPoint[]
  tickers: Record<string, TickerState>
}

// REST API types

export interface Trade {
  id: number
  ticker: string
  direction: string
  option_symbol: string
  option_strike: number
  option_type: string
  option_expiry: string
  entry_ts: string
  exit_ts: string | null
  entry_price: number   // option price at entry
  exit_price: number | null
  contracts: number
  premium_paid: number
  exit_reason: string | null
  gross_pnl: number | null
  net_pnl: number | null
  conviction_score: number | null
  iv_rank: number | null
  vix: number | null
  time_window: string | null
}

export interface TickerStat {
  trades: number
  wins: number
  win_rate: number
  total_pnl: number
  avg_pnl: number
  signal_accuracy_15m: number
  signal_accuracy_30m: number
  signal_accuracy_1h: number
}
