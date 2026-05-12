# Schwab Momentum Bot

Fully automated momentum breakout trading bot for Charles Schwab. Windows desktop app with PySide6 GUI.

## Features

- **Intraday breakout entries** with volume pace confirmation
- **AI-authored rule-based strategies** (entry/exit expressions + indicators)
- **Momentum universe** from computed ThinkScript scanners (or scanner exports/watchlists/CSV fallback)
- **Volatility contraction filter** (VCP-style 2-of-3 gate)
- **Regime detection**: TREND / CHOP / RISK_OFF based on benchmark SMA10/SMA20
- **Risk-per-trade** editable live in the GUI (0.05% - 2.0%)
- **Kill switch** (Ctrl+Shift+K) — instant OFF, cancel all entries
- **Breakeven at +1R** — stop moves to entry price automatically
- **EOD exit** — closes positions below SMA10/SMA20 near market close
- **Risk-off liquidation** — sells all when benchmark declines K consecutive days
- **Manual override respected** — bot stops managing if you trade in Schwab directly
- **Shadow mode** — run signals without placing orders
- **Full SQLite logging** — every signal, order, fill, regime change recorded

## Quick Start

### 1. Install Dependencies

```bash
cd schwab-momentum-bot
pip install -r requirements.txt
```

### 2. Register Schwab API App

1. Go to [developer.schwab.com](https://developer.schwab.com)
2. Create a new app
3. Set callback URL to `https://127.0.0.1:8182/callback`
4. Link your brokerage account
5. Copy your Client ID and Client Secret

### 3. Run Setup Wizard

```bash
python setup_wizard.py
```

Or edit `config.yaml` directly with your credentials.

### 4. Launch

```bash
python main.py
```

The app starts in **Shadow Mode** by default (no real orders). Uncheck "Shadow Mode" in the GUI to go live.

### 5. Build Windows EXE (Optional)

```bash
build.bat
```

Output: `dist/SchwabMomentumBot.exe`

## Configuration

All thresholds are in `config.yaml`. Key settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `sizing.risk_per_trade_pct` | 0.50% | Risk per trade (also editable in GUI) |
| `sizing.max_positions_trend` | 8 | Max concurrent positions in TREND |
| `sizing.max_positions_chop` | 4 | Max concurrent positions in CHOP |
| `regime.decline_consecutive_days` | 3 | Days of SMA10+SMA20 decline to trigger RISK_OFF |
| `regime.rise_consecutive_days` | 2 | Days of recovery to exit RISK_OFF |
| `regime.cooldown_days` | 3 | Cooldown between regime changes |
| `breakout.lookback` | 40 | Days for breakout level (highest high) |
| `breakout.min_rel_vol_pace` | 1.5 | Minimum relative volume pace |
| `eod_exit.eod_window_minutes` | 5 | Minutes before close to check EOD exit |
| `safety.daily_loss_lock_r` | 3.0 | Lock out new entries after -3R daily |
| `app.shadow_mode` | true | Start in shadow mode |

## Universe Setup

### Option A: Computed Scanners (Default)

Scanner logic is translated from your ThinkScript (qullamaggie 1M/3M/6M + test scanner)
and evaluated from daily OHLCV data.

Set in `config.yaml`:
- `universe.source: computed_scanners`
- `universe.scanner_names` list
- scanner thresholds in `universe.*` (ADR, dollar volume, return %, and test scanner params)

Live trading computed scanners run over a candidate universe loaded from watchlists
(or CSV fallback if enabled).

Backtester supports a no-upload mode:
- Turn on `Auto US market universe` in the Backtester UI.
- It automatically pulls US listings, downloads historical OHLCV, and computes scanner
  membership day-by-day for the selected backtest range.
- Optional `Auto limit` lets you cap symbols for faster test runs.
- Current implementation uses exchange symbol files for currently listed issues.
  For delisted-symbol complete historical universes, integrate a point-in-time provider.

### Option B: Thinkorswim Scanner CSV Snapshots

Set in `config.yaml`:
- `universe.source: scanner_files`
- `universe.scanner_names` list
- `universe.scanner_history_dir` folder path

Expected file patterns (recursive under `scanner_history_dir`):
- `scanner_name_YYYY-MM-DD.csv`
- `YYYY-MM-DD_scanner_name.csv`
- `YYYY-MM-DD/scanner_name.csv`

Backtests in scanner-history mode evaluate entries only on symbols that were present
in that day's scanner snapshot (with optional carry-forward days for missing snapshots).

## Long-Range Multi-Timeframe Import

You can pre-import long-range data into `backtest_data.db` (no manual CSV uploads):

```bash
python import_market_data.py --symbols ALL_US --start-date 2006-01-01 --end-date 2026-03-02 --timeframes 1d 1wk 1mo 5m
```

Useful flags:
- `--source auto|yfinance|polygon`
- `--polygon-api-key YOUR_KEY`
- `--max-symbols 1000` (recommended for first full import dry-runs)
- `--force-refresh`

Notes:
- Daily/weekly/monthly imports work with yfinance by default.
- Long-range intraday imports switch to Polygon automatically when:
  - `data.backtest_data_source` is `auto`, and
  - `data.polygon_api_key` (or `--polygon-api-key`) is set, and
  - requested intraday range exceeds typical yfinance retention windows.
- If Polygon is not configured, intraday imports are limited to recent windows (typically <=60 days).
- Imported bars are cached locally and reused by reruns.

## AI Strategy Rewrites (Backtester)

The Backtester chat can now propose full strategy specs, not only parameter tweaks.

When you apply an AI strategy proposal, the app will:
- save a working strategy spec JSON
- run a full backtest using that spec
- let you apply both config changes and strategy spec to live config

Rule-based strategy spec fields:
- `name`, `description`
- `symbols` (use `["ALL_US"]` to auto-expand to US universe in backtests)
- `indicators` (supports `sma`, `ema`, `rsi`, `zscore`, `atr`, `stddev`)
- `entry_rule_long`, `entry_rule_short`, `exit_rule` (expression over OHLCV + indicator columns)
  `exit_rule` can use `position_side` (`'LONG'`/`'SHORT'`).
- `entry_price_field` (`open`/`high`/`low`/`close`)
- `backtest_timeframe` (`1d` or minute intervals: `1m|2m|5m|15m|30m|60m|90m`)
- intraday controls:
  `use_intraday_vwap_stop` (bool), `intraday_interval` (`1m|2m|5m|15m|30m|60m|90m`)
- sizing:
  `position_size_mode: notional_pct|risk_pct`
  `position_size_pct` and/or `risk_per_trade_pct`
- `max_positions`
- optional `stop_loss_pct`, `take_profit_pct`, `max_holding_days`

Expression tips:
- You can reference other symbols as `<symbol>_<field>` (example: `qqq_open`, `spy_close`).
- `vwap_proxy` indicator is available as `(high + low + close) / 3` on daily bars.
- When `use_intraday_vwap_stop` is true, backtests pull intraday bars and exit when intraday close crosses VWAP against your position side.
- When `backtest_timeframe` is an intraday interval, entry/exit logic runs bar-by-bar on intraday candles (true intraday loop).

Notes:
- Rule-based mode is available in both backtesting and live engine routing.
- "Apply to Live Config" persists the strategy spec, sets live/backtest strategy selectors,
  and takes effect on next bot start.
- Live rule-based currently supports long entries. Short-entry rules are ignored.
- Live intraday VWAP-stop parity is not yet implemented; live mode uses stop/take-profit/time/rule exits.
- Every backtest/rerun now writes:
  - a line in `backtest_runs.jsonl` with `run_id`, result summary, and strategy metadata
  - an immutable replay snapshot at `backtest_runs/<run_id>.json` (full strategy + backtest-relevant config)
  - archived rule-based strategy specs at `strategy_specs/archived_runs/<run_id>.json`

Exact replay/apply workflow:
- Find the target run ID in `backtest_runs.jsonl`.
- Re-apply that run snapshot exactly:
  - `python apply_backtest_snapshot.py --run-id <RUN_ID>`
- This updates `config.yaml` backtest-relevant parameters and points
  `strategy_runtime.active_strategy_spec_path` to the frozen strategy spec for that run.

### Option C: Schwab Watchlists

Create these watchlists in your Schwab account:
- `LEADERS_1M` — 1-month momentum leaders
- `LEADERS_3M` — 3-month momentum leaders
- `LEADERS_6M` — 6-month momentum leaders

The bot unions all three and deduplicates.

### Option D: CSV Fallback

1. Set `universe.use_csv_fallback: true` in config.yaml
2. Create a `universe_csv/` folder next to the executable
3. Drop a CSV file with one ticker per line (or first column)
4. Bot reads the most recently modified CSV daily

## Strategy Flow

```
Universe (computed scanners / scanner snapshots / watchlists / CSV)
  → Eligibility (price/volume/spread)
  → Trend Quality (SMA50/200)
  → Volatility Contraction (2-of-3 gate)
  → Entry Signal (breakout or pullback)
  → Position Sizing (risk-based)
  → Order Placement (stop-limit buy)
  → Trade Management (BE at +1R, EOD exit)
  → Reconciliation (broker is truth)
```

## GUI Controls

- **Risk %** spinner: Changes risk for the next trade immediately
- **Kill Switch**: Cancels all pending entries, blocks new trades. Toggle "Manage exits while OFF" to keep stop/EOD management active
- **Start/Stop Engine**: Control the trading loop
- **Shadow Mode**: Signals only, no real orders
- **Re-enable bot management**: Per-symbol button if you manually override

## Safety Features

- Stop-limit entries only (no blind market orders)
- Daily loss lock at configurable R-multiple
- Stale data detection (no entries if quotes > 60s old)
- Spread check before every entry
- Max position % of equity (default 20%)
- Max total open risk (default 6R)
- Encrypted token storage for Schwab credentials

## Project Structure

```
schwab-momentum-bot/
├── main.py              # App entry point
├── setup_wizard.py      # First-run credential setup
├── config.yaml          # All configurable thresholds
├── build.bat            # PyInstaller build script
├── core/                # Engine, config, scheduler
├── broker/              # Schwab API, auth, reconciliation
├── strategy/            # Universe, filters, regime, signals, sizing
├── execution/           # Orders, trade management, kill switch
├── data/                # Market data cache, DB models
├── gui/                 # PySide6 dashboard, controls, log viewer
└── tests/               # Unit tests
```

## Testing

```bash
pytest tests/ -v
```

## License

Private use only. Not financial advice. Trade at your own risk.
