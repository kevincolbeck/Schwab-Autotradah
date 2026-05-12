"""Live execution adapter for rule-based strategy specs."""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from backtest.rule_based_engine import (
    RuleBasedStrategySpec,
    _atr,
    _evaluate_custom_formula,
    _evaluate_rule_scalar,
    _evaluate_rule_series,
    _rsi,
    _sorted_indicators,
    _zscore,
    referenced_price_symbols,
    strategy_spec_uses_auto_us_universe,
    trade_symbols_from_strategy_spec,
)
from core.config import Config
from core.constants import EventType, TradeStatus
from data.market_data import MarketDataCache
from data.models import BotOrder, Event, Trade
from data.us_universe import USMarketUniverseProvider
from execution.order_manager import OrderManager

logger = logging.getLogger(__name__)

_PRICE_FIELDS = ("open", "high", "low", "close", "volume")


class LiveRuleBasedExecutor:
    """Runs rule-based strategy logic in live mode, bypassing momentum modules."""

    def __init__(self, config: Config, data_cache: MarketDataCache, order_mgr: OrderManager, db_session_factory):
        self.config = config
        self.data = data_cache
        self.order_mgr = order_mgr
        self.db_session_factory = db_session_factory
        self._spec: Optional[RuleBasedStrategySpec] = None
        self._spec_path: Optional[Path] = None
        self._spec_mtime: Optional[float] = None
        self._trade_symbols: List[str] = []
        self._warned_short = False
        self._warned_intraday_vwap = False

        # EOD signal evaluation state
        self._eod_overlay_quotes: Dict[str, dict] = {}
        self._eod_bars_refreshed_date: Optional[str] = None

        # Allocated capital / rolling balance tracking
        self._allocated_capital: float = getattr(
            config.strategy_runtime, "allocated_capital", 0.0
        )
        self._allocated_balance: float = self._allocated_capital
        self._balance_file = Path("allocated_balance.json")
        self._trades_closed_count: int = 0
        self._total_pnl: float = 0.0
        if self._allocated_capital > 0:
            self._load_balance()
            logger.info(
                "Allocated capital mode: $%.2f (current balance: $%.2f)",
                self._allocated_capital,
                self._allocated_balance,
            )

    def _load_balance(self):
        """Load rolling balance from file if it exists."""
        if not self._balance_file.exists():
            self._save_balance()
            return
        try:
            data = json.loads(self._balance_file.read_text(encoding="utf-8"))
            saved_initial = data.get("initial_capital", 0.0)
            if saved_initial != self._allocated_capital:
                logger.info(
                    "Allocated capital changed (%.2f -> %.2f), resetting balance.",
                    saved_initial,
                    self._allocated_capital,
                )
                self._save_balance()
                return
            self._allocated_balance = float(data.get("current_balance", self._allocated_capital))
            self._total_pnl = float(data.get("total_pnl", 0.0))
            self._trades_closed_count = int(data.get("trades_closed", 0))
            logger.info(
                "Loaded allocated balance: $%.2f (P&L: %+.2f from %d trades)",
                self._allocated_balance,
                self._total_pnl,
                self._trades_closed_count,
            )
        except Exception as exc:
            logger.warning("Failed to load balance file, using initial capital: %s", exc)
            self._allocated_balance = self._allocated_capital

    def _save_balance(self):
        """Persist rolling balance to file."""
        try:
            data = {
                "initial_capital": self._allocated_capital,
                "current_balance": round(self._allocated_balance, 2),
                "total_pnl": round(self._total_pnl, 2),
                "trades_closed": self._trades_closed_count,
                "last_updated": datetime.utcnow().isoformat(),
            }
            self._balance_file.write_text(
                json.dumps(data, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            logger.warning("Failed to save balance file: %s", exc)

    @staticmethod
    def _is_market_open() -> bool:
        """Check if the US stock market is currently in regular trading hours."""
        et_now = datetime.now(ZoneInfo("US/Eastern"))
        if et_now.weekday() >= 5:
            return False
        market_open = et_now.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = et_now.replace(hour=16, minute=0, second=0, microsecond=0)
        return market_open <= et_now < market_close

    def run_cycle(
        self,
        current_time: datetime,
        equity: float,
        shadow_mode: bool = False,
        allow_new_trades: bool = True,
        allow_exit_management: bool = True,
        eod_mode: bool = False,
        morning_mode: bool = False,
    ):
        """Run one live rule-based cycle.

        When *eod_mode* is True (3:50 PM ET trigger), real-time quotes are
        fetched and overlaid onto the daily bars so that indicators and signals
        use the current price as a synthetic close.

        When *morning_mode* is True (9:30 AM ET trigger), yesterday's actual
        close data is used to evaluate signals that formed overnight, and market
        orders are fired immediately at the open.
        """
        if not self._load_spec_if_needed():
            return
        if not self._spec:
            return

        active_symbols = self._get_active_trade_symbols()
        symbols = sorted(set(self._trade_symbols + active_symbols))
        if not symbols:
            return

        if self._spec.use_intraday_vwap_stop and not self._warned_intraday_vwap:
            logger.warning(
                "Live rule-based: intraday VWAP stop is not yet implemented for live execution; "
                "using stop_loss/exit_rule/time exits."
            )
            self._warned_intraday_vwap = True

        # --- Morning mode: invalidate cache so we fetch fresh daily bars
        #     (yesterday's confirmed close). No quote overlay — we are acting
        #     on the signal that formed at yesterday's close. ---
        if morning_mode:
            today_str = datetime.now(ZoneInfo("US/Eastern")).strftime("%Y-%m-%d")
            if self._eod_bars_refreshed_date != today_str:
                for sym in symbols:
                    self.data.invalidate(sym)
                ref_syms = list(referenced_price_symbols(self._spec)) if self._spec else []
                for sym in ref_syms:
                    self.data.invalidate(sym)
                self._eod_bars_refreshed_date = today_str
            logger.info("Morning open signal evaluation: checking yesterday's close signals for %d symbols", len(symbols))

        # --- EOD mode: overlay real-time quotes onto daily bars ---
        if eod_mode:
            today_str = datetime.now(ZoneInfo("US/Eastern")).strftime("%Y-%m-%d")
            # Invalidate daily bar cache once per day to get fresh history
            if self._eod_bars_refreshed_date != today_str:
                for sym in symbols:
                    self.data.invalidate(sym)
                ref_syms = list(referenced_price_symbols(self._spec)) if self._spec else []
                for sym in ref_syms:
                    self.data.invalidate(sym)
                self._eod_bars_refreshed_date = today_str

            # Fetch real-time quotes for all symbols (trade + reference)
            all_syms = sorted(set(
                symbols + list(referenced_price_symbols(self._spec) if self._spec else [])
            ))
            self._eod_overlay_quotes = self.data.get_quotes_batch(all_syms)
            logger.info(
                "EOD signal evaluation: overlaying real-time quotes for %d symbols",
                len(self._eod_overlay_quotes),
            )

        frames = self._prepare_symbol_frames(symbols)
        self._eod_overlay_quotes = {}  # Clear after frame construction

        if not frames:
            return

        if allow_exit_management:
            self._manage_open_positions(frames, current_time, shadow_mode)
        if allow_new_trades:
            if self._allocated_capital > 0:
                usable_equity = self._allocated_balance
            else:
                usable_equity = float(equity or 0.0)
            self._scan_entries(frames, current_time, usable_equity, shadow_mode)

    def _load_spec_if_needed(self) -> bool:
        spec_path_text = (
            getattr(self.config.strategy_runtime, "active_strategy_spec_path", "").strip()
        )
        if not spec_path_text:
            logger.error("Live rule-based strategy requires strategy_runtime.active_strategy_spec_path")
            return False

        path = Path(spec_path_text)
        if not path.exists():
            logger.error("Live rule-based strategy spec not found: %s", spec_path_text)
            return False

        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None

        if (
            self._spec is not None
            and self._spec_path == path
            and self._spec_mtime is not None
            and mtime == self._spec_mtime
        ):
            return True

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            spec = RuleBasedStrategySpec.from_dict(raw)
        except Exception as exc:
            logger.error("Failed to load live rule-based strategy spec: %s", exc)
            return False

        trade_symbols = self._resolve_trade_symbols(spec)
        if not trade_symbols:
            logger.error("Live rule-based strategy resolved zero trade symbols")
            return False

        self._spec = spec
        self._spec_path = path
        self._spec_mtime = mtime
        self._trade_symbols = trade_symbols
        logger.info(
            "Loaded live rule-based strategy '%s' with %d symbols from %s",
            spec.name,
            len(trade_symbols),
            str(path),
        )
        return True

    def reload_spec(self):
        """Force reload of strategy spec from disk (called by AI Strategist)."""
        self._spec_mtime = None  # Invalidate cache to force reload on next cycle
        logger.info("Strategy spec reload requested")

    def _resolve_trade_symbols(self, spec: RuleBasedStrategySpec) -> List[str]:
        explicit = trade_symbols_from_strategy_spec(spec)
        if explicit:
            return explicit

        if strategy_spec_uses_auto_us_universe(spec):
            symbols = USMarketUniverseProvider().get_symbols()
            if symbols:
                return symbols
        return []

    def _get_active_trade_symbols(self) -> List[str]:
        session: Session = self.db_session_factory()
        try:
            rows = session.query(Trade.symbol).filter(
                Trade.status.in_([
                    TradeStatus.OPEN.name,
                    TradeStatus.BREAKEVEN.name,
                    TradeStatus.PENDING_ENTRY.name,
                ]),
            ).all()
            return sorted({str(r[0]).upper() for r in rows if r and r[0]})
        finally:
            session.close()

    def _prepare_symbol_frames(self, symbols: List[str]) -> Dict[str, pd.DataFrame]:
        if not self._spec:
            return {}
        reference_frames = self._build_reference_frames()
        out: Dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            df = self._normalized_daily_bars(symbol)
            if df is None or df.empty:
                continue
            merged = df
            for ref_df in reference_frames:
                merged = merged.merge(ref_df, on="datetime", how="left")
            frame = self._build_indicator_frame(merged)
            try:
                frame["entry_signal_long"] = _evaluate_rule_series(self._spec.entry_rule_long, frame)
                if (self._spec.entry_rule_short or "").strip():
                    frame["entry_signal_short"] = _evaluate_rule_series(self._spec.entry_rule_short, frame)
                else:
                    frame["entry_signal_short"] = pd.Series(False, index=frame.index)
                out[symbol] = frame.set_index("datetime", drop=False)
            except Exception as exc:
                logger.warning("Live rule-based signal eval skipped for %s: %s", symbol, exc)
        return out

    def _build_reference_frames(self) -> List[pd.DataFrame]:
        if not self._spec:
            return []
        symbols = referenced_price_symbols(self._spec)
        out: List[pd.DataFrame] = []
        for symbol in symbols:
            df = self._normalized_daily_bars(symbol)
            if df is None or df.empty:
                continue
            ref = df[["datetime", *_PRICE_FIELDS]].copy()
            rename = {field: f"{symbol.lower()}_{field}" for field in _PRICE_FIELDS}
            out.append(ref.rename(columns=rename))
        return out

    def _normalized_daily_bars(self, symbol: str) -> Optional[pd.DataFrame]:
        df = self.data.get_daily_bars(symbol, min_bars=250)
        if df is None or df.empty or "datetime" not in df.columns:
            return None
        frame = df.copy()
        frame["datetime"] = pd.to_datetime(frame["datetime"]).dt.normalize()
        for field in _PRICE_FIELDS:
            if field not in frame.columns:
                frame[field] = np.nan
        frame = frame[["datetime", *_PRICE_FIELDS]].sort_values("datetime").reset_index(drop=True)

        # --- EOD overlay: replace/append today's bar with real-time quote ---
        quote = self._eod_overlay_quotes.get(symbol)
        if quote and quote.get("last", 0) > 0:
            today = pd.Timestamp(datetime.now(ZoneInfo("US/Eastern")).date())
            last_dt = frame["datetime"].iloc[-1]
            if last_dt >= today:
                # Today's bar already in history — update with real-time data
                idx = frame.index[-1]
                frame.at[idx, "close"] = quote["last"]
                if quote.get("high", 0) > 0:
                    frame.at[idx, "high"] = max(float(frame.at[idx, "high"]), float(quote["high"]))
                if quote.get("low", 0) > 0:
                    frame.at[idx, "low"] = min(float(frame.at[idx, "low"]), float(quote["low"]))
                if quote.get("volume", 0) > 0:
                    frame.at[idx, "volume"] = quote["volume"]
            else:
                # Today's bar missing — append synthetic bar from quote
                new_bar = pd.DataFrame([{
                    "datetime": today,
                    "open": quote.get("open", quote["last"]),
                    "high": quote.get("high", quote["last"]),
                    "low": quote.get("low", quote["last"]),
                    "close": quote["last"],
                    "volume": quote.get("volume", 0),
                }])
                frame = pd.concat([frame, new_bar], ignore_index=True)

        return frame

    def _build_indicator_frame(self, frame: pd.DataFrame) -> pd.DataFrame:
        out = frame.copy()
        if not self._spec:
            return out
        for ind in _sorted_indicators(self._spec.indicators):
            try:
                self._apply_indicator(out, ind)
            except Exception as exc:
                logger.warning("Live rule-based indicator skipped (%s): %s", ind, exc)
        return out

    def _apply_indicator(self, frame: pd.DataFrame, ind: dict):
        if not isinstance(ind, dict):
            return
        ind_type = str(ind.get("type", "")).strip().lower()
        if not ind_type:
            return
        name = str(ind.get("name", "")).strip().lower()

        # --- External symbol reference ---
        if ind_type == "external":
            symbol = str(ind.get("symbol", "")).strip().upper()
            ext_field = str(ind.get("field", "close")).strip().lower()
            if not symbol:
                raise ValueError("External indicator requires a 'symbol' field")
            ref_col = f"{symbol.lower()}_{ext_field}"
            if ref_col in frame.columns:
                if name:
                    frame[name] = frame[ref_col].copy()
            else:
                raise ValueError(
                    f"External reference column '{ref_col}' not found. "
                    f"Ensure symbol {symbol} is fetched as reference data."
                )
            return

        # --- Custom formula evaluation ---
        if ind_type == "custom":
            formula = str(ind.get("formula", "")).strip()
            if not formula:
                raise ValueError("Custom indicator requires a 'formula' field")
            if not name:
                raise ValueError("Custom indicator requires a 'name' field")
            frame[name] = _evaluate_custom_formula(formula, frame)
            return

        # --- Standard indicator types ---
        length = int(ind.get("length", 20))
        if length <= 0:
            raise ValueError("length must be >= 1")
        source = str(ind.get("source", "close")).strip().lower()
        if not name:
            name = f"{ind_type}_{length}"
        if source not in frame.columns:
            raise ValueError(f"unknown source column: {source}")

        series = frame[source]
        if ind_type == "sma":
            frame[name] = series.rolling(length, min_periods=length).mean()
        elif ind_type == "ema":
            frame[name] = series.ewm(span=length, adjust=False).mean()
        elif ind_type == "rsi":
            frame[name] = _rsi(series, length)
        elif ind_type == "zscore":
            frame[name] = _zscore(series, length)
        elif ind_type == "atr":
            frame[name] = _atr(frame, length)
        elif ind_type == "stddev":
            frame[name] = series.rolling(length, min_periods=length).std()
        elif ind_type in {"vwap", "vwap_proxy"}:
            frame[name] = (frame["high"] + frame["low"] + frame["close"]) / 3.0
        else:
            raise ValueError(f"unsupported indicator type: {ind_type}")

    def _manage_open_positions(
        self, frames: Dict[str, pd.DataFrame], current_time: datetime, shadow_mode: bool
    ):
        if not self._spec:
            return
        session: Session = self.db_session_factory()
        try:
            trades = session.query(Trade).filter(
                Trade.status.in_([TradeStatus.OPEN.name, TradeStatus.BREAKEVEN.name]),
                Trade.bot_managed == True,
            ).all()
            for trade in trades:
                symbol = trade.symbol
                frame = frames.get(symbol)
                if frame is None or frame.empty:
                    continue
                row = frame.iloc[-1]

                exit_price = None
                exit_reason = None

                stop_price = float(trade.current_stop_price or trade.initial_stop_price or 0.0)
                if self._spec.stop_loss_pct > 0 and stop_price > 0:
                    if float(row.get("low", np.nan)) <= stop_price:
                        exit_price = stop_price
                        exit_reason = "STOP_LOSS"

                if exit_price is None and self._spec.take_profit_pct > 0 and trade.entry_price:
                    tp_price = float(trade.entry_price) * (1 + self._spec.take_profit_pct / 100.0)
                    if float(row.get("high", np.nan)) >= tp_price:
                        exit_price = tp_price
                        exit_reason = "TAKE_PROFIT"

                if exit_price is None and self._spec.max_holding_days > 0:
                    entry_dt = trade.entry_time or trade.entry_signal_time or current_time
                    days_held = (pd.Timestamp(current_time) - pd.Timestamp(entry_dt)).days
                    if days_held >= self._spec.max_holding_days:
                        exit_price = float(row.get("close", 0.0))
                        exit_reason = "TIME_EXIT"

                if exit_price is None:
                    context = {
                        "entry_price": float(trade.entry_price or 0.0),
                        "initial_stop": float(trade.initial_stop_price or 0.0),
                        "current_stop": float(trade.current_stop_price or 0.0),
                        "stop_price": float(trade.current_stop_price or 0.0),
                        "r_value": float(trade.r_value or 0.0),
                        "shares": int(trade.shares or 0),
                        "days_held": (
                            (pd.Timestamp(current_time) - pd.Timestamp(trade.entry_time or trade.entry_signal_time or current_time)).days
                        ),
                    }
                    should_exit = _evaluate_rule_scalar(
                        self._spec.exit_rule,
                        row,
                        position_side="LONG",
                        context=context,
                    )
                    if should_exit:
                        exit_price = float(row.get("close", 0.0))
                        exit_reason = "RULE_EXIT"

                if exit_price and exit_price > 0 and exit_reason:
                    self._close_trade(session, trade, float(exit_price), exit_reason, shadow_mode)

            session.commit()
        except Exception as exc:
            logger.error("Live rule-based exit management error: %s", exc, exc_info=True)
            session.rollback()
        finally:
            session.close()

    def _scan_entries(
        self,
        frames: Dict[str, pd.DataFrame],
        current_time: datetime,
        equity: float,
        shadow_mode: bool,
    ):
        if not self._spec:
            return
        session: Session = self.db_session_factory()
        try:
            active_rows = session.query(Trade.symbol).filter(
                Trade.status.in_([
                    TradeStatus.OPEN.name,
                    TradeStatus.BREAKEVEN.name,
                    TradeStatus.PENDING_ENTRY.name,
                ])
            ).all()
            active_symbols = {str(r[0]).upper() for r in active_rows if r and r[0]}
            open_count = len(active_symbols)
            if open_count >= self._spec.max_positions:
                return

            live_equity = float(equity or 0.0)
            if live_equity <= 0:
                try:
                    acct = self.data.broker.get_account_info()
                    live_equity = float(acct.get("equity", 0.0))
                except Exception:
                    live_equity = 0.0
            if live_equity <= 0:
                logger.warning("Live rule-based: equity unavailable, skipping entries")
                return

            # Collect and rank candidates (matches backtest ranking logic)
            candidates = []
            for symbol in self._trade_symbols:
                if symbol in active_symbols:
                    logger.debug("SKIP %s: already in active trade", symbol)
                    continue

                frame = frames.get(symbol)
                if frame is None or frame.empty:
                    logger.debug("SKIP %s: no price data", symbol)
                    continue
                row = frame.iloc[-1]

                # Log key indicator values for every symbol so we can see what's blocking
                indicator_summary = " | ".join(
                    f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}"
                    for k, v in row.items()
                    if k not in ("datetime", "open", "high", "low", "close", "volume")
                    and not str(k).endswith("_close") and not str(k).startswith("entry_signal")
                    and isinstance(v, (int, float)) and not isinstance(v, bool)
                )
                long_signal = bool(row.get("entry_signal_long", False))
                short_signal = bool(row.get("entry_signal_short", False))

                if not long_signal:
                    logger.info("NO SIGNAL %s | close=%.2f | %s", symbol, float(row.get("close", 0)), indicator_summary)
                else:
                    logger.info("SIGNAL    %s | close=%.2f | %s", symbol, float(row.get("close", 0)), indicator_summary)

                if long_signal and short_signal:
                    continue
                if short_signal and not self._warned_short:
                    logger.warning(
                        "Live rule-based: short entries are not supported by current live order stack; "
                        "short signals will be ignored."
                    )
                    self._warned_short = True
                if not long_signal:
                    continue

                if self._spec.ranking_field:
                    rank_val = float(row.get(self._spec.ranking_field, 0))
                else:
                    vol = float(row.get("volume", 0))
                    vol_avg = float(row.get("vol_sma_20", 1))
                    rank_val = vol / max(vol_avg, 1.0)
                candidates.append((rank_val, symbol, row))

            # Sort by ranking value descending (highest momentum first)
            candidates.sort(key=lambda x: x[0], reverse=True)

            for _, symbol, row in candidates:
                if len(active_symbols) >= self._spec.max_positions:
                    break

                entry_price = float(row.get(self._spec.entry_price_field, row.get("close", 0.0)))
                if entry_price <= 0:
                    continue
                if self._spec.stop_loss_pct > 0:
                    stop_price = entry_price * (1 - self._spec.stop_loss_pct / 100.0)
                else:
                    stop_price = entry_price * 0.99
                r_value = max(abs(entry_price - stop_price), entry_price * 0.005)
                shares = self._shares_for_spec(live_equity, entry_price, stop_price, r_value)
                if shares <= 0:
                    continue

                trade = Trade(
                    symbol=symbol,
                    status=TradeStatus.PENDING_ENTRY.name,
                    entry_signal_time=current_time,
                    entry_price=entry_price,
                    shares=shares,
                    entry_reason="RULE_LONG",
                    initial_stop_price=stop_price,
                    current_stop_price=stop_price,
                    r_value=r_value,
                    regime_at_entry="CUSTOM",
                    size_multiplier=1.0,
                )
                session.add(trade)
                session.flush()

                if self._is_market_open():
                    order_id = self.order_mgr.place_market_entry_order(
                        trade,
                        shares,
                        session,
                        shadow_mode=shadow_mode,
                    )
                else:
                    # Market closed — place extended-hours limit order
                    limit_price = round(entry_price * 1.003, 2)  # 0.3% above last
                    order_id = self.order_mgr.place_limit_entry_order_extended(
                        trade,
                        shares,
                        limit_price,
                        session,
                        shadow_mode=shadow_mode,
                    )
                    logger.info(
                        "Market closed — using extended-hours limit buy for %s at $%.2f",
                        symbol,
                        limit_price,
                    )
                if order_id:
                    trade.entry_order_id = order_id
                    self._log_event(
                        session,
                        EventType.ENTRY_TRIGGERED,
                        symbol,
                        trade.id,
                        (
                            f"Rule entry triggered: qty={shares} "
                            f"entry_ref={entry_price:.2f} stop={stop_price:.2f}"
                        ),
                    )
                    active_symbols.add(symbol)
                else:
                    trade.status = TradeStatus.CANCELLED.name

            session.commit()
        except Exception as exc:
            logger.error("Live rule-based entry scan error: %s", exc, exc_info=True)
            session.rollback()
        finally:
            session.close()

    def _shares_for_spec(
        self, equity: float, entry_price: float, stop_price: float, r_value: float
    ) -> int:
        if not self._spec:
            return 0
        if self._spec.position_size_mode == "risk_pct":
            risk_dollars = equity * (self._spec.risk_per_trade_pct / 100.0)
            return max(0, math.floor(risk_dollars / max(r_value, 1e-9)))
        notional = equity * (self._spec.position_size_pct / 100.0)
        return max(0, math.floor(notional / max(entry_price, 1e-9)))

    def _close_trade(
        self,
        session: Session,
        trade: Trade,
        exit_price: float,
        reason: str,
        shadow_mode: bool,
    ):
        if shadow_mode:
            logger.info("[SHADOW] Would close %s at %.2f (%s)", trade.symbol, exit_price, reason)
            order_id = "SHADOW_ORDER"
        else:
            order_id = self.data.broker.place_market_sell(trade.symbol, int(trade.shares or 0))

        if order_id:
            bot_order = BotOrder(
                trade_id=trade.id,
                symbol=trade.symbol,
                broker_order_id=order_id,
                side="SELL",
                order_type="MARKET",
                status="WORKING",
                requested_qty=int(trade.shares or 0),
                placed_at=datetime.utcnow(),
                purpose=reason,
            )
            session.add(bot_order)
            self._cancel_stop_orders(session, trade)

        trade.status = TradeStatus.CLOSED_RULE.name
        trade.exit_time = datetime.utcnow()
        trade.exit_price = exit_price
        trade.exit_reason = reason
        if trade.entry_price and trade.shares:
            trade.pnl_dollars = (exit_price - trade.entry_price) * trade.shares
            if trade.r_value:
                trade.pnl_r_multiple = (exit_price - trade.entry_price) / trade.r_value

            # Update allocated balance tracker
            if self._allocated_capital > 0 and trade.pnl_dollars is not None:
                self._allocated_balance += float(trade.pnl_dollars)
                self._total_pnl += float(trade.pnl_dollars)
                self._trades_closed_count += 1
                self._save_balance()
                logger.info(
                    "Allocated balance updated: $%.2f (%+.2f on %s, total P&L: %+.2f)",
                    self._allocated_balance,
                    float(trade.pnl_dollars),
                    trade.symbol,
                    self._total_pnl,
                )

        self._log_event(
            session,
            EventType.EOD_EXIT,
            trade.symbol,
            trade.id,
            f"Rule exit: reason={reason} price={exit_price:.2f}",
        )

    def _cancel_stop_orders(self, session: Session, trade: Trade):
        stop_orders = session.query(BotOrder).filter(
            BotOrder.trade_id == trade.id,
            BotOrder.purpose == "STOP_LOSS",
            BotOrder.status.in_(["WORKING", "PENDING"]),
        ).all()
        for so in stop_orders:
            if so.broker_order_id and so.broker_order_id not in ("SHADOW_STOP", "SHADOW_ORDER"):
                self.data.broker.cancel_order(so.broker_order_id)
            so.status = "CANCELLED"
            so.cancelled_at = datetime.utcnow()

    def _log_event(
        self,
        session: Session,
        event_type: EventType,
        symbol: str,
        trade_id: Optional[int],
        message: str,
    ):
        session.add(
            Event(
                event_type=event_type.name,
                symbol=symbol,
                trade_id=trade_id,
                message=message,
            )
        )
