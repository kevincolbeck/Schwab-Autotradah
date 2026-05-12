"""Schwab API client wrapper for trading operations."""

import json
import logging
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any

from schwab.client import Client as SchwabClientClass

from core.config import Config

logger = logging.getLogger(__name__)


class SchwabBrokerClient:
    """Wrapper around schwab-py client for all broker operations."""

    def __init__(self, client: SchwabClientClass, config: Config):
        self.client = client
        self.config = config
        self._account_hash: Optional[str] = None
        self._last_request_time = 0.0
        self._min_request_interval = 60.0 / 120  # 120 req/min

    def _rate_limit(self):
        """Enforce rate limiting between API calls."""
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < self._min_request_interval:
            time.sleep(self._min_request_interval - elapsed)
        self._last_request_time = time.time()

    def _ensure_account_hash(self):
        """Resolve account hash if not set."""
        if self._account_hash:
            return
        if self.config.schwab.account_hash:
            self._account_hash = self.config.schwab.account_hash
            return
        self._rate_limit()
        resp = self.client.get_account_numbers()
        if resp.status_code != 200:
            raise ConnectionError(f"Failed to get account numbers: {resp.status_code}")
        accounts = resp.json()
        if not accounts:
            raise ValueError("No accounts found")
        self._account_hash = accounts[0]['hashValue']
        logger.info(f"Using account hash: {self._account_hash[:8]}...")

    # --- Account Info ---

    def get_account_info(self) -> Dict[str, Any]:
        """Get account balances (equity, buying power, etc.)."""
        self._ensure_account_hash()
        self._rate_limit()
        resp = self.client.get_account(
            self._account_hash,
            fields=[SchwabClientClass.Account.Fields.POSITIONS]
        )
        if resp.status_code != 200:
            raise ConnectionError(f"Failed to get account info: {resp.status_code}")
        data = resp.json()
        balances = data.get('securitiesAccount', {}).get('currentBalances', {})
        return {
            'equity': balances.get('liquidationValue', 0.0),
            'buying_power': balances.get('buyingPower', 0.0),
            'cash': balances.get('cashBalance', 0.0),
            'account_type': data.get('securitiesAccount', {}).get('type', ''),
            'raw': data,
        }

    def get_positions(self) -> List[Dict[str, Any]]:
        """Get current positions from broker."""
        self._ensure_account_hash()
        self._rate_limit()
        resp = self.client.get_account(
            self._account_hash,
            fields=[SchwabClientClass.Account.Fields.POSITIONS]
        )
        if resp.status_code != 200:
            raise ConnectionError(f"Failed to get positions: {resp.status_code}")
        data = resp.json()
        positions_raw = (
            data.get('securitiesAccount', {}).get('positions', [])
        )
        positions = []
        for pos in positions_raw:
            instrument = pos.get('instrument', {})
            positions.append({
                'symbol': instrument.get('symbol', ''),
                'asset_type': instrument.get('assetType', ''),
                'qty': pos.get('longQuantity', 0) - pos.get('shortQuantity', 0),
                'avg_price': pos.get('averagePrice', 0.0),
                'market_value': pos.get('marketValue', 0.0),
                'current_day_pnl': pos.get('currentDayProfitLoss', 0.0),
                'current_day_pnl_pct': pos.get('currentDayProfitLossPercentage', 0.0),
            })
        return positions

    def get_orders(self, status: str = None, max_results: int = 50) -> List[Dict[str, Any]]:
        """Get open/recent orders from broker."""
        self._ensure_account_hash()
        self._rate_limit()
        from_time = datetime.utcnow() - timedelta(days=1)
        to_time = datetime.utcnow() + timedelta(days=1)
        resp = self.client.get_orders_for_account(
            self._account_hash,
            from_entered_datetime=from_time,
            to_entered_datetime=to_time,
            max_results=max_results,
            status=status,
        )
        if resp.status_code != 200:
            logger.warning(f"Failed to get orders: {resp.status_code}")
            return []
        return resp.json() or []

    # --- Order Placement ---

    def place_stop_limit_buy(
        self, symbol: str, qty: int, stop_price: float, limit_price: float
    ) -> Optional[str]:
        """Place a stop-limit buy order. Returns broker order ID or None."""
        self._ensure_account_hash()
        self._rate_limit()
        order_spec = {
            "orderType": "STOP_LIMIT",
            "session": "NORMAL",
            "duration": "DAY",
            "stopPrice": str(round(stop_price, 2)),
            "price": str(round(limit_price, 2)),
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [{
                "instruction": "BUY",
                "quantity": qty,
                "instrument": {
                    "symbol": symbol,
                    "assetType": "EQUITY"
                }
            }]
        }
        resp = self.client.place_order(self._account_hash, order_spec)
        if resp.status_code in (200, 201):
            order_id = _extract_order_id(resp)
            logger.info(f"Stop-limit buy placed: {symbol} qty={qty} stop={stop_price} limit={limit_price} id={order_id}")
            return order_id
        logger.error(f"Failed to place stop-limit buy for {symbol}: {resp.status_code} {resp.text}")
        return None

    def place_market_buy(self, symbol: str, qty: int) -> Optional[str]:
        """Place a market buy order. Returns broker order ID or None."""
        self._ensure_account_hash()
        self._rate_limit()
        order_spec = {
            "orderType": "MARKET",
            "session": "NORMAL",
            "duration": "DAY",
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [{
                "instruction": "BUY",
                "quantity": qty,
                "instrument": {
                    "symbol": symbol,
                    "assetType": "EQUITY"
                }
            }]
        }
        resp = self.client.place_order(self._account_hash, order_spec)
        if resp.status_code in (200, 201):
            order_id = _extract_order_id(resp)
            logger.info(f"Market buy placed: {symbol} qty={qty} id={order_id}")
            return order_id
        logger.error(f"Failed to place market buy for {symbol}: {resp.status_code} {resp.text}")
        return None

    def place_sell_stop(
        self, symbol: str, qty: int, stop_price: float
    ) -> Optional[str]:
        """Place a sell stop (protective stop loss). Returns broker order ID or None."""
        self._ensure_account_hash()
        self._rate_limit()
        order_spec = {
            "orderType": "STOP",
            "session": "NORMAL",
            "duration": "GTC",
            "stopPrice": str(round(stop_price, 2)),
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [{
                "instruction": "SELL",
                "quantity": qty,
                "instrument": {
                    "symbol": symbol,
                    "assetType": "EQUITY"
                }
            }]
        }
        resp = self.client.place_order(self._account_hash, order_spec)
        if resp.status_code in (200, 201):
            order_id = _extract_order_id(resp)
            logger.info(f"Sell stop placed: {symbol} qty={qty} stop={stop_price} id={order_id}")
            return order_id
        logger.error(f"Failed to place sell stop for {symbol}: {resp.status_code} {resp.text}")
        return None

    def place_limit_buy_extended(
        self, symbol: str, qty: int, limit_price: float
    ) -> Optional[str]:
        """Place a limit buy order valid for all sessions (including after-hours).

        Uses session='SEAMLESS' so the order can fill in pre-market, regular,
        or after-hours sessions.
        """
        self._ensure_account_hash()
        self._rate_limit()
        order_spec = {
            "orderType": "LIMIT",
            "session": "SEAMLESS",
            "duration": "DAY",
            "price": str(round(limit_price, 2)),
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [{
                "instruction": "BUY",
                "quantity": qty,
                "instrument": {
                    "symbol": symbol,
                    "assetType": "EQUITY"
                }
            }]
        }
        resp = self.client.place_order(self._account_hash, order_spec)
        if resp.status_code in (200, 201):
            order_id = _extract_order_id(resp)
            logger.info(f"Extended-hours limit buy placed: {symbol} qty={qty} limit={limit_price} id={order_id}")
            return order_id
        logger.error(f"Failed to place extended-hours limit buy for {symbol}: {resp.status_code} {resp.text}")
        return None

    def place_market_sell(self, symbol: str, qty: int) -> Optional[str]:
        """Place a market sell order. Returns broker order ID or None."""
        self._ensure_account_hash()
        self._rate_limit()
        order_spec = {
            "orderType": "MARKET",
            "session": "NORMAL",
            "duration": "DAY",
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [{
                "instruction": "SELL",
                "quantity": qty,
                "instrument": {
                    "symbol": symbol,
                    "assetType": "EQUITY"
                }
            }]
        }
        resp = self.client.place_order(self._account_hash, order_spec)
        if resp.status_code in (200, 201):
            order_id = _extract_order_id(resp)
            logger.info(f"Market sell placed: {symbol} qty={qty} id={order_id}")
            return order_id
        logger.error(f"Failed to place market sell for {symbol}: {resp.status_code} {resp.text}")
        return None

    def place_moc_sell(self, symbol: str, qty: int) -> Optional[str]:
        """Place a market-on-close sell order. Falls back to market if MOC unavailable."""
        self._ensure_account_hash()
        self._rate_limit()
        order_spec = {
            "orderType": "MARKET",
            "session": "NORMAL",
            "duration": "DAY",
            "orderStrategyType": "SINGLE",
            "specialInstruction": "MARKET_ON_CLOSE",
            "orderLegCollection": [{
                "instruction": "SELL",
                "quantity": qty,
                "instrument": {
                    "symbol": symbol,
                    "assetType": "EQUITY"
                }
            }]
        }
        resp = self.client.place_order(self._account_hash, order_spec)
        if resp.status_code in (200, 201):
            order_id = _extract_order_id(resp)
            logger.info(f"MOC sell placed: {symbol} qty={qty} id={order_id}")
            return order_id
        # Fallback to regular market sell
        logger.warning(f"MOC sell failed for {symbol}, falling back to market sell")
        return self.place_market_sell(symbol, qty)

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order by broker order ID."""
        self._ensure_account_hash()
        self._rate_limit()
        resp = self.client.cancel_order(order_id, self._account_hash)
        if resp.status_code in (200, 201):
            logger.info(f"Order cancelled: {order_id}")
            return True
        logger.warning(f"Failed to cancel order {order_id}: {resp.status_code}")
        return False

    def replace_stop_order(
        self, order_id: str, symbol: str, qty: int, new_stop_price: float
    ) -> Optional[str]:
        """Replace (modify) a stop order with a new stop price."""
        self._ensure_account_hash()
        self._rate_limit()
        order_spec = {
            "orderType": "STOP",
            "session": "NORMAL",
            "duration": "GTC",
            "stopPrice": str(round(new_stop_price, 2)),
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [{
                "instruction": "SELL",
                "quantity": qty,
                "instrument": {
                    "symbol": symbol,
                    "assetType": "EQUITY"
                }
            }]
        }
        resp = self.client.replace_order(self._account_hash, order_id, order_spec)
        if resp.status_code in (200, 201):
            new_order_id = _extract_order_id(resp)
            logger.info(f"Stop order replaced: {order_id} -> {new_order_id} new_stop={new_stop_price}")
            return new_order_id
        logger.error(f"Failed to replace stop order {order_id}: {resp.status_code}")
        return None

    # --- Market Data ---

    def get_quotes(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """Get real-time quotes for a list of symbols."""
        if not symbols:
            return {}
        self._rate_limit()
        resp = self.client.get_quotes(symbols)
        if resp.status_code != 200:
            logger.warning(f"Failed to get quotes: {resp.status_code}")
            return {}
        raw = resp.json()
        quotes = {}
        for sym, data in raw.items():
            quote = data.get('quote', {})
            quotes[sym] = {
                'last': quote.get('lastPrice', 0.0),
                'bid': quote.get('bidPrice', 0.0),
                'ask': quote.get('askPrice', 0.0),
                'high': quote.get('highPrice', 0.0),
                'low': quote.get('lowPrice', 0.0),
                'open': quote.get('openPrice', 0.0),
                'close': quote.get('closePrice', 0.0),
                'volume': quote.get('totalVolume', 0),
                'avg_volume_10d': quote.get('averageVolume10Day', 0),
                'avg_volume': quote.get('averageVolume', 0),
                'exchange': quote.get('exchange', ''),
                'asset_type': data.get('assetMainType', ''),
                'quote_time': quote.get('quoteTime', 0),
            }
        return quotes

    def get_price_history(
        self,
        symbol: str,
        period_type: str = "month",
        period: int = 6,
        frequency_type: str = "daily",
        frequency: int = 1,
    ) -> List[Dict[str, Any]]:
        """Get historical price bars for a symbol."""
        self._rate_limit()

        try:
            # Pull the library's default daily range to ensure we have enough
            # bars for SMA-based filters/regime calculations.
            resp = self.client.get_price_history_every_day(symbol)
        except Exception as e:
            logger.warning(f"get_price_history_every_day failed for {symbol}: {e}")
            return []
        if resp.status_code != 200:
            logger.warning(f"Failed to get price history for {symbol}: {resp.status_code}")
            return []
        data = resp.json()
        candles = data.get('candles', [])
        return [{
            'datetime': c.get('datetime'),
            'open': c.get('open'),
            'high': c.get('high'),
            'low': c.get('low'),
            'close': c.get('close'),
            'volume': c.get('volume'),
        } for c in candles]

    # --- Watchlists ---

    def get_watchlist_symbols(self, watchlist_name: str) -> List[str]:
        """Get symbols from a named watchlist."""
        self._ensure_account_hash()
        self._rate_limit()
        fetch_fn = None
        if hasattr(self.client, "get_watchlists_for_single_account"):
            fetch_fn = self.client.get_watchlists_for_single_account
        elif hasattr(self.client, "get_watchlists_for_account"):
            fetch_fn = self.client.get_watchlists_for_account
        else:
            logger.warning(
                "Installed schwab client library does not expose watchlist APIs. "
                "Use scanner file universe or CSV fallback."
            )
            return []

        try:
            resp = fetch_fn(self._account_hash)
            if resp.status_code != 200:
                logger.warning(f"Failed to get watchlists: {resp.status_code}")
                return []
            watchlists = resp.json()
            for wl in watchlists:
                if wl.get('name', '').upper() == watchlist_name.upper():
                    items = wl.get('watchlistItems', [])
                    return [
                        item.get('instrument', {}).get('symbol', '')
                        for item in items
                        if item.get('instrument', {}).get('symbol')
                    ]
            logger.info(f"Watchlist '{watchlist_name}' not found")
            return []
        except Exception as e:
            logger.error(f"Error fetching watchlist {watchlist_name}: {e}")
            return []


def _extract_order_id(resp) -> Optional[str]:
    """Extract order ID from the Location header of a place_order response."""
    location = resp.headers.get('Location', '')
    if location:
        # Location header format: .../orders/{orderId}
        parts = location.rstrip('/').split('/')
        return parts[-1] if parts else None
    return None
