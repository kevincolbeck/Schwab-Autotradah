"""Automatic US equity universe provider (no user CSV input required)."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Set

import pandas as pd

logger = logging.getLogger(__name__)

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
CACHE_FILE = "auto_universe_cache.json"

_SYMBOL_RE = re.compile(r"^[A-Z]{1,5}(?:-[A-Z])?$")
_EXCLUDE_NAME_TOKENS = {
    "WARRANT",
    "RIGHT",
    "UNIT",
    "PREFERRED",
    "PREF",
    "DEPOSITARY",
    "NOTE",
    "BOND",
    "ETF",
    "ETN",
    "TRUST",
    "FUND",
}


class USMarketUniverseProvider:
    """Builds a broad US stock candidate universe from public exchange symbol files."""

    def __init__(self, cache_path: str = CACHE_FILE):
        self.cache_path = Path(cache_path)

    def get_symbols(self, force_refresh: bool = False) -> List[str]:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if not force_refresh:
            cached = self._read_cache()
            if cached and cached.get("as_of") == today and cached.get("symbols"):
                return sorted(set(cached["symbols"]))

        symbols = self._download_symbols()
        if symbols:
            self._write_cache(today, symbols)
            return symbols

        cached = self._read_cache()
        if cached and cached.get("symbols"):
            logger.warning(
                "Falling back to cached universe with %d symbols from %s",
                len(cached["symbols"]),
                cached.get("as_of", "unknown_date"),
            )
            return sorted(set(cached["symbols"]))
        return []

    def _download_symbols(self) -> List[str]:
        symbols: Set[str] = set()
        try:
            nasdaq = pd.read_csv(NASDAQ_LISTED_URL, sep="|")
            symbols.update(self._parse_nasdaq_listed(nasdaq))
        except Exception as exc:
            logger.warning(f"Failed to load nasdaqlisted symbols: {exc}")

        try:
            other = pd.read_csv(OTHER_LISTED_URL, sep="|")
            symbols.update(self._parse_other_listed(other))
        except Exception as exc:
            logger.warning(f"Failed to load otherlisted symbols: {exc}")

        out = sorted(symbols)
        logger.info("Auto US universe loaded %d candidate symbols", len(out))
        return out

    def _parse_nasdaq_listed(self, df: pd.DataFrame) -> Set[str]:
        out: Set[str] = set()
        for _, row in df.iterrows():
            symbol = str(row.get("Symbol", "")).strip().upper()
            name = str(row.get("Security Name", "")).strip().upper()
            test_issue = str(row.get("Test Issue", "N")).strip().upper()
            if test_issue != "N":
                continue
            normalized = _normalize_symbol(symbol)
            if not normalized or not _is_stock_like_name(name):
                continue
            out.add(normalized)
        return out

    def _parse_other_listed(self, df: pd.DataFrame) -> Set[str]:
        out: Set[str] = set()
        for _, row in df.iterrows():
            symbol = str(row.get("ACT Symbol", "")).strip().upper()
            name = str(row.get("Security Name", "")).strip().upper()
            test_issue = str(row.get("Test Issue", "N")).strip().upper()
            if test_issue != "N":
                continue
            normalized = _normalize_symbol(symbol)
            if not normalized or not _is_stock_like_name(name):
                continue
            out.add(normalized)
        return out

    def _read_cache(self) -> Optional[dict]:
        if not self.cache_path.exists():
            return None
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _write_cache(self, as_of: str, symbols: List[str]):
        payload = {"as_of": as_of, "symbols": sorted(set(symbols))}
        try:
            self.cache_path.write_text(json.dumps(payload), encoding="utf-8")
        except Exception as exc:
            logger.warning(f"Failed to write auto universe cache: {exc}")


def _normalize_symbol(raw: str) -> Optional[str]:
    symbol = (raw or "").strip().upper().replace("/", "-").replace(".", "-")
    if not symbol:
        return None
    if not _SYMBOL_RE.match(symbol):
        return None
    if symbol.endswith("W"):
        return None
    return symbol


def _is_stock_like_name(name: str) -> bool:
    if not name or name == "NAN":
        return False
    for token in _EXCLUDE_NAME_TOKENS:
        if token in name:
            return False
    return True
