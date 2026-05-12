"""Universe construction from Schwab watchlists or CSV fallback."""

import os
import glob
import logging
from datetime import datetime
from typing import List, Set

from broker.schwab_client import SchwabBrokerClient
from core.config import Config
from strategy.scanner_history import ScannerHistory

logger = logging.getLogger(__name__)


class UniverseBuilder:
    """Builds the tradeable universe from watchlists or CSV files."""

    def __init__(self, broker: SchwabBrokerClient, config: Config):
        self.broker = broker
        self.config = config
        self._cached_symbols: List[str] = []

    def get_universe(self) -> List[str]:
        """Get the current universe of symbols to consider.
        Default: union of Schwab watchlists. Fallback: CSV import.
        """
        source = (self.config.universe.source or "watchlists").strip().lower()
        if source == "scanner_files":
            symbols = self._load_from_scanner_files()
        elif source == "computed_scanners":
            symbols = self._load_candidate_universe()
        elif self.config.universe.use_csv_fallback:
            symbols = self._load_from_csv()
        else:
            symbols = self._load_from_watchlists()
            if not symbols:
                logger.warning("Watchlists returned empty, trying CSV fallback")
                symbols = self._load_from_csv()

        symbols = sorted(set(symbols))
        if symbols != self._cached_symbols:
            logger.info(f"Universe updated: {len(symbols)} symbols")
            self._cached_symbols = symbols
        return symbols

    def _load_from_scanner_files(self) -> List[str]:
        """Load latest scanner results from local Thinkorswim CSV exports."""
        cfg = self.config.universe
        history = ScannerHistory.from_directory(
            cfg.scanner_history_dir,
            cfg.scanner_names,
        )
        if not history.has_data():
            return []

        symbols = history.get_symbols_for_date(
            datetime.utcnow(),
            carry_forward_days=cfg.scanner_carry_forward_days,
        )
        if not symbols:
            logger.warning(
                "No scanner symbols found for current date (carry-forward=%sd)",
                cfg.scanner_carry_forward_days,
            )
        return symbols

    def _load_candidate_universe(self) -> List[str]:
        """Load candidate symbols, then scanners are applied inside the engine."""
        if self.config.universe.use_csv_fallback:
            symbols = self._load_from_csv()
            if symbols:
                return symbols

        symbols = self._load_from_watchlists()
        if not symbols:
            logger.warning("Computed scanners: watchlists empty, trying CSV fallback")
            symbols = self._load_from_csv()
        return symbols

    def _load_from_watchlists(self) -> List[str]:
        """Pull symbols from configured Schwab watchlists and union them."""
        all_symbols: Set[str] = set()
        for wl_name in self.config.universe.watchlist_names:
            try:
                symbols = self.broker.get_watchlist_symbols(wl_name)
                if symbols:
                    logger.info(f"Watchlist {wl_name}: {len(symbols)} symbols")
                    all_symbols.update(symbols)
                else:
                    logger.info(f"Watchlist {wl_name}: empty or not found")
            except Exception as e:
                logger.error(f"Error reading watchlist {wl_name}: {e}")
        return list(all_symbols)

    def _load_from_csv(self) -> List[str]:
        """Load symbols from the latest CSV file in the fallback directory."""
        csv_dir = self.config.universe.csv_fallback_dir
        if not os.path.exists(csv_dir):
            logger.warning(f"CSV fallback directory does not exist: {csv_dir}")
            return []

        csv_files = glob.glob(os.path.join(csv_dir, "*.csv"))
        if not csv_files:
            logger.warning(f"No CSV files found in {csv_dir}")
            return []

        # Use the most recently modified CSV
        latest_csv = max(csv_files, key=os.path.getmtime)
        logger.info(f"Loading universe from CSV: {latest_csv}")

        symbols = []
        try:
            with open(latest_csv, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    # Support both single-column and multi-column CSVs
                    parts = line.split(',')
                    symbol = parts[0].strip().upper()
                    # Skip header rows
                    if symbol in ('SYMBOL', 'TICKER', 'NAME'):
                        continue
                    if symbol and symbol.isalpha() and len(symbol) <= 10:
                        symbols.append(symbol)
        except Exception as e:
            logger.error(f"Error reading CSV {latest_csv}: {e}")

        logger.info(f"Loaded {len(symbols)} symbols from CSV")
        return symbols
