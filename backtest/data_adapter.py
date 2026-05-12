"""Adapter that mimics MarketDataCache for backtesting.

Serves a rolling window of historical data up to the current simulation date,
making existing strategy modules work without modification.
"""

from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd


class BacktestDataCache:
    """Drop-in replacement for data.market_data.MarketDataCache.

    Stores the full historical DataFrame per symbol and exposes
    get_daily_bars(symbol) that returns data up to (and including)
    the current simulation date.
    """

    def __init__(
        self,
        full_data: Dict[str, pd.DataFrame],
        intraday_data: Optional[Dict[str, pd.DataFrame]] = None,
    ):
        self._full_data = full_data
        self._intraday_data = intraday_data or {}
        self._current_date: Optional[pd.Timestamp] = None
        self._date_indices: Dict[str, pd.DatetimeIndex] = {}
        self._daily_slice_cache: Dict[str, pd.DataFrame] = {}
        for sym, df in full_data.items():
            self._date_indices[sym] = pd.DatetimeIndex(df["datetime"])
        for sym, df in self._intraday_data.items():
            if "datetime" in df.columns:
                self._intraday_data[sym] = df.sort_values("datetime").reset_index(drop=True)

    def set_current_date(self, date: datetime):
        """Advance the simulation clock. Called by the backtest engine each day."""
        next_date = pd.Timestamp(date).normalize()
        if self._current_date != next_date:
            self._current_date = next_date
            self._daily_slice_cache = {}

    def get_daily_bars(self, symbol: str, min_bars: int = 200) -> Optional[pd.DataFrame]:
        """Return daily bars up to and including current_date."""
        if symbol not in self._full_data or self._current_date is None:
            return None

        subset = self._daily_slice_cache.get(symbol)
        if subset is None:
            df = self._full_data[symbol]
            idx = self._date_indices[symbol]
            end_pos = idx.searchsorted(self._current_date, side="right")
            subset = df.iloc[:end_pos]
            self._daily_slice_cache[symbol] = subset

        if len(subset) < min_bars:
            return None

        return subset

    def get_full_bars(self, symbol: str) -> Optional[pd.DataFrame]:
        return self._full_data.get(symbol)

    def get_intraday_bars(
        self, symbol: str, session_date: Optional[datetime] = None
    ) -> Optional[pd.DataFrame]:
        df = self._intraday_data.get(symbol)
        if df is None or df.empty or "datetime" not in df.columns:
            return None

        if session_date is None:
            if self._current_date is None:
                return df
            session_date = self._current_date

        day = pd.Timestamp(session_date).normalize()
        next_day = day + pd.Timedelta(days=1)
        mask = (df["datetime"] >= day) & (df["datetime"] < next_day)
        out = df.loc[mask]
        if out.empty:
            return None
        return out.reset_index(drop=True)

    def get_quote(self, symbol: str) -> Optional[dict]:
        """Synthesize a quote dict from today's daily bar."""
        df = self.get_daily_bars(symbol, min_bars=1)
        if df is None or df.empty:
            return None
        today = df.iloc[-1]
        avg_vol = df["volume"].tail(20).mean() if len(df) >= 20 else df["volume"].mean()
        return {
            "last": today["close"],
            "bid": today["close"],
            "ask": today["close"],
            "volume": today["volume"],
            "avg_volume": avg_vol,
            "asset_type": "EQUITY",
            "exchange": "NYSE",
        }

    def get_quotes_batch(self, symbols: List[str]) -> Dict[str, dict]:
        result = {}
        for s in symbols:
            q = self.get_quote(s)
            if q is not None:
                result[s] = q
        return result

    def is_quote_stale(self, symbol: str) -> bool:
        return False

    def prefetch_daily_bars(self, symbols: List[str]):
        pass

    def invalidate(self, symbol: Optional[str] = None):
        pass

    @property
    def available_symbols(self) -> List[str]:
        return list(self._full_data.keys())

    def get_trading_dates(
        self, start: datetime, end: datetime, reference_symbol: Optional[str] = None
    ) -> List[datetime]:
        """Return sorted trading dates, preferring a reference symbol when available."""
        if reference_symbol and reference_symbol in self._full_data:
            df = self._full_data[reference_symbol]
            dates = df["datetime"]
            mask = (dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))
            return sorted(dates[mask].dt.to_pydatetime().tolist())

        # Fallback: use the symbol with the most bars to minimize missing dates.
        best_df = None
        for sym, df in self._full_data.items():
            if best_df is None or len(df) > len(best_df):
                best_df = df
        if best_df is not None:
            dates = best_df["datetime"]
            mask = (dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))
            return sorted(dates[mask].dt.to_pydatetime().tolist())
        return []
