"""Market data fetching and caching layer."""

import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd
import numpy as np

from broker.schwab_client import SchwabBrokerClient
from core.config import Config

logger = logging.getLogger(__name__)


class MarketDataCache:
    """In-memory cache for price history and quotes with staleness tracking."""

    def __init__(self, broker: SchwabBrokerClient, config: Config):
        self.broker = broker
        self.config = config
        self._daily_bars: Dict[str, pd.DataFrame] = {}
        self._daily_bars_fetched: Dict[str, float] = {}
        self._quotes: Dict[str, dict] = {}
        self._quotes_fetched: float = 0.0
        self._daily_cache_ttl = 3600  # 1 hour for daily bars
        self._quote_cache_ttl = 5  # 5 seconds for quotes

    def get_daily_bars(self, symbol: str, min_bars: int = 200) -> Optional[pd.DataFrame]:
        """Get daily OHLCV bars for a symbol. Fetches from broker if not cached or stale."""
        now = time.time()
        if symbol in self._daily_bars:
            age = now - self._daily_bars_fetched.get(symbol, 0)
            if age < self._daily_cache_ttl and len(self._daily_bars[symbol]) >= min_bars:
                return self._daily_bars[symbol]

        try:
            candles = self.broker.get_price_history(symbol)
            if not candles:
                logger.warning(f"No price history for {symbol}")
                return None

            df = pd.DataFrame(candles)
            df['datetime'] = pd.to_datetime(df['datetime'], unit='ms')
            df = df.sort_values('datetime').reset_index(drop=True)
            df.columns = ['datetime', 'open', 'high', 'low', 'close', 'volume']

            self._daily_bars[symbol] = df
            self._daily_bars_fetched[symbol] = now
            return df
        except Exception as e:
            logger.error(f"Error fetching daily bars for {symbol}: {e}")
            return self._daily_bars.get(symbol)

    def get_quotes_batch(self, symbols: List[str]) -> Dict[str, dict]:
        """Get real-time quotes for multiple symbols. Uses cache if fresh."""
        now = time.time()
        if now - self._quotes_fetched < self._quote_cache_ttl:
            cached = {s: self._quotes[s] for s in symbols if s in self._quotes}
            if len(cached) == len(symbols):
                return cached

        # Batch fetch in chunks of 500 (Schwab limit)
        all_quotes = {}
        for i in range(0, len(symbols), 500):
            chunk = symbols[i:i+500]
            try:
                quotes = self.broker.get_quotes(chunk)
                all_quotes.update(quotes)
            except Exception as e:
                logger.error(f"Error fetching quotes batch: {e}")

        self._quotes.update(all_quotes)
        self._quotes_fetched = now
        return {s: all_quotes[s] for s in symbols if s in all_quotes}

    def get_quote(self, symbol: str) -> Optional[dict]:
        """Get a single real-time quote."""
        result = self.get_quotes_batch([symbol])
        return result.get(symbol)

    def is_quote_stale(self, symbol: str) -> bool:
        """Check if a quote is older than the configured staleness threshold."""
        if symbol not in self._quotes:
            return True
        fetched = self._quotes_fetched
        return (time.time() - fetched) > self.config.safety.stale_data_seconds

    def invalidate(self, symbol: Optional[str] = None):
        """Invalidate cache for a symbol or all symbols."""
        if symbol:
            self._daily_bars.pop(symbol, None)
            self._daily_bars_fetched.pop(symbol, None)
            self._quotes.pop(symbol, None)
        else:
            self._daily_bars.clear()
            self._daily_bars_fetched.clear()
            self._quotes.clear()
            self._quotes_fetched = 0.0

    def prefetch_daily_bars(self, symbols: List[str]):
        """Pre-fetch daily bars for all symbols (rate-limited)."""
        for symbol in symbols:
            if symbol not in self._daily_bars:
                self.get_daily_bars(symbol)
