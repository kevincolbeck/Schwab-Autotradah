"""
Schwab WebSocket streaming client.

Subscribes to:
  LEVELONE_EQUITIES   — real-time quotes (bid, ask, last, volume)
  NYSE_BOOK           — L2 order book depth for NYSE-listed tickers
  NASDAQ_BOOK         — L2 order book depth for NASDAQ-listed tickers
  TIMESALE_EQUITY     — every trade tick (for footprint candle computation)
  LEVELONE_OPTIONS    — live option contract quotes + greeks
  TIMESALE_OPTIONS    — option trade ticks (for sweep detection)
"""

import asyncio
import json
import logging
import time
from collections import defaultdict
from typing import Callable, Optional
import websockets

from backend.data.schwab_auth import get_valid_token, get_streamer_info
from backend.config import ALL_TICKERS, MAG7, ETFS

logger = logging.getLogger(__name__)

# ── Field maps ────────────────────────────────────────────────────────────────

L1_EQUITY_FIELDS = {
    0: "symbol", 1: "bid", 2: "ask", 3: "last", 4: "bid_size",
    5: "ask_size", 8: "total_volume", 9: "last_size",
    12: "high", 13: "low", 15: "close", 28: "vwap",
}

TIMESALE_FIELDS = {
    0: "symbol", 1: "trade_time", 2: "last_price", 3: "last_size", 4: "last_sequence",
}

L1_OPTIONS_FIELDS = {
    0: "symbol", 2: "bid", 3: "ask", 4: "last",
    10: "open_interest", 17: "underlying_price",
    19: "delta", 20: "gamma", 21: "theta", 22: "vega",
    24: "implied_volatility", 27: "volume",
}

TIMESALE_OPTIONS_FIELDS = {
    0: "symbol", 1: "trade_time", 2: "last_price", 3: "last_size",
}

NYSE_LISTED   = {"AAPL", "MSFT", "GOOGL", "META", "AMZN", "TSLA", "SPY", "QQQ"}
NASDAQ_LISTED = {"NVDA"}


class SchwabStreamClient:
    def __init__(self):
        self._ws = None
        self._streamer_info: dict = {}
        self._request_id = 0
        self._running = False

        # Published state — read by strategy modules
        self.quotes: dict[str, dict] = defaultdict(dict)
        self.option_quotes: dict[str, dict] = defaultdict(dict)

        # Tick callbacks — registered by strategy modules
        self._equity_tick_callbacks: list[Callable] = []
        self._option_tick_callbacks: list[Callable] = []
        self._l2_callbacks: list[Callable] = []
        self._quote_callbacks: list[Callable] = []

    # ── Public registration ───────────────────────────────────────────────────

    def on_equity_tick(self, fn: Callable) -> None:
        """Register callback for each equity trade tick: fn(symbol, price, size, ts)"""
        self._equity_tick_callbacks.append(fn)

    def on_option_tick(self, fn: Callable) -> None:
        """Register callback for each option trade tick: fn(symbol, price, size, ts)"""
        self._option_tick_callbacks.append(fn)

    def on_l2_update(self, fn: Callable) -> None:
        """Register callback for L2 book updates: fn(symbol, bids, asks)"""
        self._l2_callbacks.append(fn)

    def on_quote_update(self, fn: Callable) -> None:
        """Register callback for L1 quote updates: fn(symbol, quote_dict)"""
        self._quote_callbacks.append(fn)

    def get_quote(self, symbol: str) -> dict:
        return self.quotes.get(symbol, {})

    def get_option_quote(self, symbol: str) -> dict:
        return self.option_quotes.get(symbol, {})

    # ── Connection lifecycle ──────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        while self._running:
            try:
                await self._connect_and_run()
            except Exception as e:
                logger.error(f"Stream disconnected: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()

    async def _connect_and_run(self) -> None:
        self._streamer_info = await get_streamer_info()
        token = await get_valid_token()

        stream_url = self._streamer_info.get("streamerSocketUrl", "wss://streamer-api.schwab.com/ws")
        customer_id = self._streamer_info.get("schwabClientCustomerId", "")
        correl_id = self._streamer_info.get("schwabClientCorrelId", "")

        async with websockets.connect(stream_url, ping_interval=20, ping_timeout=10) as ws:
            self._ws = ws
            logger.info("Schwab WebSocket connected")

            await self._login(ws, token, customer_id, correl_id)
            await self._subscribe_all(ws, customer_id, correl_id)

            async for raw in ws:
                await self._handle_message(json.loads(raw))

    # ── Protocol messages ─────────────────────────────────────────────────────

    def _next_id(self) -> str:
        self._request_id += 1
        return str(self._request_id)

    def _make_request(self, service: str, command: str, params: dict,
                      customer_id: str, correl_id: str) -> dict:
        return {
            "requests": [{
                "service": service,
                "command": command,
                "requestid": self._next_id(),
                "SchwabClientCustomerId": customer_id,
                "SchwabClientCorrelId": correl_id,
                "parameters": params,
            }]
        }

    async def _login(self, ws, token: str, customer_id: str, correl_id: str) -> None:
        msg = {
            "requests": [{
                "service": "ADMIN",
                "command": "LOGIN",
                "requestid": "0",
                "SchwabClientCustomerId": customer_id,
                "SchwabClientCorrelId": correl_id,
                "parameters": {
                    "Authorization": token,
                    "SchwabClientChannel": "IO /3",
                    "SchwabClientFunctionId": "APIAPP",
                },
            }]
        }
        await ws.send(json.dumps(msg))
        resp = json.loads(await ws.recv())
        code = resp.get("response", [{}])[0].get("content", {}).get("code", -1)
        if code != 0:
            raise RuntimeError(f"Schwab stream login failed: {resp}")
        logger.info("Schwab stream login successful")

    async def _subscribe_all(self, ws, customer_id: str, correl_id: str) -> None:
        ticker_str = ",".join(ALL_TICKERS)
        nyse_str   = ",".join(t for t in ALL_TICKERS if t in NYSE_LISTED)
        nasdaq_str = ",".join(t for t in ALL_TICKERS if t in NASDAQ_LISTED)

        subs = [
            # L1 quotes
            self._make_request("LEVELONE_EQUITIES", "SUBS",
                {"keys": ticker_str, "fields": ",".join(str(k) for k in L1_EQUITY_FIELDS)},
                customer_id, correl_id),
            # Trade ticks
            self._make_request("TIMESALE_EQUITY", "SUBS",
                {"keys": ticker_str, "fields": "0,1,2,3,4"},
                customer_id, correl_id),
            # L2 — NYSE
            self._make_request("NYSE_BOOK", "SUBS",
                {"keys": nyse_str, "fields": "0,1,2"},
                customer_id, correl_id),
        ]
        if nasdaq_str:
            subs.append(self._make_request("NASDAQ_BOOK", "SUBS",
                {"keys": nasdaq_str, "fields": "0,1,2"},
                customer_id, correl_id))

        for sub in subs:
            await ws.send(json.dumps(sub))
            await asyncio.sleep(0.1)

        logger.info(f"Subscribed to streams for: {ticker_str}")

    async def subscribe_options(self, option_symbols: list[str],
                                customer_id: str, correl_id: str) -> None:
        """Dynamically subscribe to specific option contracts (called when a signal fires)."""
        if not option_symbols or not self._ws:
            return
        sym_str = ",".join(option_symbols)
        l1_sub = self._make_request("LEVELONE_OPTIONS", "ADD",
            {"keys": sym_str, "fields": ",".join(str(k) for k in L1_OPTIONS_FIELDS)},
            customer_id, correl_id)
        ts_sub = self._make_request("TIMESALE_OPTIONS", "ADD",
            {"keys": sym_str, "fields": "0,1,2,3"},
            customer_id, correl_id)
        await self._ws.send(json.dumps(l1_sub))
        await asyncio.sleep(0.05)
        await self._ws.send(json.dumps(ts_sub))

    # ── Message dispatch ──────────────────────────────────────────────────────

    async def _handle_message(self, msg: dict) -> None:
        for data_block in msg.get("data", []):
            service = data_block.get("service", "")
            content = data_block.get("content", [])

            if service == "LEVELONE_EQUITIES":
                self._handle_l1_equity(content)
            elif service in ("TIMESALE_EQUITY",):
                await self._handle_equity_tick(content)
            elif service in ("NYSE_BOOK", "NASDAQ_BOOK"):
                await self._handle_l2(content)
            elif service == "LEVELONE_OPTIONS":
                self._handle_l1_options(content)
            elif service == "TIMESALE_OPTIONS":
                await self._handle_option_tick(content)

    def _handle_l1_equity(self, content: list) -> None:
        for item in content:
            symbol = item.get("key", item.get("0", ""))
            if not symbol:
                continue
            q = {L1_EQUITY_FIELDS[k]: item[str(k)]
                 for k in L1_EQUITY_FIELDS if str(k) in item}
            q["symbol"] = symbol
            self.quotes[symbol].update(q)
            for cb in self._quote_callbacks:
                asyncio.create_task(cb(symbol, self.quotes[symbol]))

    async def _handle_equity_tick(self, content: list) -> None:
        ts = time.time()
        for item in content:
            symbol = item.get("key", item.get("0", ""))
            price  = float(item.get("2", 0))
            size   = int(item.get("3", 0))
            if not symbol or price <= 0:
                continue
            for cb in self._equity_tick_callbacks:
                asyncio.create_task(cb(symbol, price, size, ts))

    async def _handle_l2(self, content: list) -> None:
        for item in content:
            symbol = item.get("key", item.get("0", ""))
            bids   = item.get("1", [])   # list of [price, size, count]
            asks   = item.get("2", [])
            if not symbol:
                continue
            for cb in self._l2_callbacks:
                asyncio.create_task(cb(symbol, bids, asks))

    def _handle_l1_options(self, content: list) -> None:
        for item in content:
            symbol = item.get("key", item.get("0", ""))
            if not symbol:
                continue
            q = {L1_OPTIONS_FIELDS[k]: item[str(k)]
                 for k in L1_OPTIONS_FIELDS if str(k) in item}
            q["symbol"] = symbol
            self.option_quotes[symbol].update(q)

    async def _handle_option_tick(self, content: list) -> None:
        ts = time.time()
        for item in content:
            symbol = item.get("key", item.get("0", ""))
            price  = float(item.get("2", 0))
            size   = int(item.get("3", 0))
            if not symbol or price <= 0:
                continue
            for cb in self._option_tick_callbacks:
                asyncio.create_task(cb(symbol, price, size, ts))


# Singleton
stream_client = SchwabStreamClient()
