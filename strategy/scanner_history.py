"""Scanner history loader for Thinkorswim CSV exports."""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

import pandas as pd

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"(20\d{2}-\d{2}-\d{2})")
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9\.\-]{0,9}$")
_HEADER_TOKENS = {"SYMBOL", "TICKER", "NAME", "STOCK", "UNDERLYING"}


def _normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


@dataclass
class ScannerHistory:
    """Holds per-day scanner snapshots and serves a tradable universe by date."""

    scanner_names: List[str]
    snapshots: Dict[pd.Timestamp, Dict[str, Set[str]]] = field(default_factory=dict)

    @classmethod
    def from_directory(
        cls,
        root_dir: str,
        scanner_names: Iterable[str],
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> "ScannerHistory":
        scanner_list = [s.strip() for s in scanner_names if s and s.strip()]
        history = cls(scanner_names=scanner_list)
        if not scanner_list:
            logger.warning("Scanner history requested with no scanner names configured")
            return history

        root = Path(root_dir)
        if not root.exists():
            logger.warning(f"Scanner history directory not found: {root_dir}")
            return history

        start_ts = pd.Timestamp(start_date).normalize() if start_date else None
        end_ts = pd.Timestamp(end_date).normalize() if end_date else None

        csv_files = sorted(root.rglob("*.csv"))
        if not csv_files:
            logger.warning(f"No scanner CSV files found under {root_dir}")
            return history

        scanner_norm = {_normalize_name(name): name for name in scanner_list}

        for path in csv_files:
            snap_date = _extract_snapshot_date(path)
            if snap_date is None:
                continue

            if start_ts is not None and snap_date < start_ts:
                continue
            if end_ts is not None and snap_date > end_ts:
                continue

            scanner_name = _resolve_scanner_name(path, scanner_norm)
            if scanner_name is None:
                continue

            symbols = _load_symbols_from_csv(path)
            if not symbols:
                continue

            day_map = history.snapshots.setdefault(snap_date, {})
            bucket = day_map.setdefault(scanner_name, set())
            bucket.update(symbols)

        logger.info(
            "Loaded scanner history: %d days, %d unique symbols",
            len(history.snapshots),
            len(history.all_symbols()),
        )
        return history

    def has_data(self) -> bool:
        return bool(self.snapshots)

    def all_symbols(self) -> List[str]:
        symbols: Set[str] = set()
        for by_scanner in self.snapshots.values():
            for bucket in by_scanner.values():
                symbols.update(bucket)
        return sorted(symbols)

    def get_symbols_for_date(self, date: datetime, carry_forward_days: int = 0) -> List[str]:
        target = pd.Timestamp(date).normalize()
        if target in self.snapshots:
            return self._union_for_date(target)

        if carry_forward_days <= 0:
            return []

        available = sorted(d for d in self.snapshots if d <= target)
        if not available:
            return []

        latest = available[-1]
        age_days = int((target - latest).days)
        if age_days > carry_forward_days:
            return []
        return self._union_for_date(latest)

    def _union_for_date(self, date: pd.Timestamp) -> List[str]:
        by_scanner = self.snapshots.get(date, {})
        symbols: Set[str] = set()
        for scanner_name in self.scanner_names:
            symbols.update(by_scanner.get(scanner_name, set()))
        return sorted(symbols)


def _extract_snapshot_date(path: Path) -> Optional[pd.Timestamp]:
    candidates = [path.stem] + [part for part in path.parts]
    for token in candidates:
        match = _DATE_RE.search(token)
        if not match:
            continue
        try:
            return pd.Timestamp(match.group(1)).normalize()
        except Exception:
            continue
    return None


def _resolve_scanner_name(path: Path, scanner_norm: Dict[str, str]) -> Optional[str]:
    if not scanner_norm:
        return None

    haystack = _normalize_name(" ".join([path.stem] + list(path.parts)))
    for normalized, raw_name in scanner_norm.items():
        if normalized and normalized in haystack:
            return raw_name

    if len(scanner_norm) == 1:
        return next(iter(scanner_norm.values()))
    return None


def _load_symbols_from_csv(path: Path) -> Set[str]:
    symbols: Set[str] = set()
    try:
        with path.open("r", newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            for row in reader:
                if not row:
                    continue
                raw = row[0].strip().upper()
                if not raw or raw in _HEADER_TOKENS:
                    continue
                if _SYMBOL_RE.match(raw):
                    symbols.add(raw)
    except Exception as exc:
        logger.warning(f"Failed loading scanner CSV {path}: {exc}")
    return symbols
