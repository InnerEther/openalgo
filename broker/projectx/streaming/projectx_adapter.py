"""OpenAlgo websocket adapter for the ProjectX market hub.

Each OpenAlgo subscription (symbol, exchange, mode) maps to a ProjectX
contract id from the master contract. One hub subscription per contract and
channel serves every OpenAlgo subscriber of it:

    mode 1 LTP    -> SubscribeContractQuotes
    mode 2 QUOTE  -> SubscribeContractQuotes + SubscribeContractTrades (last traded size)
    mode 3 DEPTH  -> the above + SubscribeContractMarketDepth

ProjectX sends quote updates with only the fields that changed, so the
adapter keeps the latest value of every field per contract and publishes the
merged state. Depth arrives as level updates (bid or ask price with its
resting size, a size of 0 removing the level, and a reset clearing the book);
the adapter keeps both sides and publishes the best five levels.
"""

import threading
import time
from typing import Any

from broker.projectx.api.baseurl import rtc_url
from broker.projectx.streaming.projectx_signalr import SignalRHubClient
from utils.logging import get_logger
from websocket_proxy.base_adapter import BaseBrokerWebSocketAdapter
from websocket_proxy.mapping import SymbolMapper

MODE_LTP, MODE_QUOTE, MODE_DEPTH = 1, 2, 3
MODE_NAMES = {MODE_LTP: "LTP", MODE_QUOTE: "QUOTE", MODE_DEPTH: "DEPTH"}

CHANNEL_QUOTES = "Quotes"
CHANNEL_TRADES = "Trades"
CHANNEL_DEPTH = "MarketDepth"

# ProjectX depth entry types.
DOM_ASK, DOM_BID, DOM_BEST_ASK, DOM_BEST_BID, DOM_TRADE, DOM_RESET = 1, 2, 3, 4, 5, 6
DOM_NEW_BEST_BID, DOM_NEW_BEST_ASK = 9, 10

DEPTH_LEVELS = 5


def channels_for_mode(mode: int) -> tuple[str, ...]:
    if mode == MODE_LTP:
        return (CHANNEL_QUOTES,)
    if mode == MODE_QUOTE:
        return (CHANNEL_QUOTES, CHANNEL_TRADES)
    return (CHANNEL_QUOTES, CHANNEL_TRADES, CHANNEL_DEPTH)


def _f(value, default=0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _i(value, default=0) -> int:
    try:
        return int(float(value)) if value is not None else default
    except (TypeError, ValueError):
        return default


def _entries(payload) -> list[dict]:
    """A hub payload as a list of dicts (ProjectX sends one or a list)."""
    if isinstance(payload, dict):
        inner = payload.get("data")
        if isinstance(inner, (list, dict)):
            return _entries(inner)
        return [payload]
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    return []


class ContractState:
    """The latest known market state of one contract."""

    def __init__(self):
        self.quote: dict[str, Any] = {}
        self.last_quantity = 0
        self.bids: dict[float, int] = {}
        self.asks: dict[float, int] = {}

    def apply_quote(self, payload) -> None:
        for entry in _entries(payload):
            for key, value in entry.items():
                if value is not None:
                    self.quote[key] = value

    def apply_trades(self, payload) -> None:
        trades = _entries(payload)
        if trades:
            last = trades[-1]
            self.last_quantity = _i(last.get("volume", last.get("size")))
            price = last.get("price")
            if price is not None:
                self.quote["lastPrice"] = price

    def apply_depth(self, payload) -> None:
        for entry in _entries(payload):
            kind = _i(entry.get("type"))
            if kind == DOM_RESET:
                self.bids.clear()
                self.asks.clear()
                continue
            price = entry.get("price")
            if price is None:
                continue
            price = _f(price)
            size = _i(entry.get("volume", entry.get("size")))
            if kind in (DOM_BID, DOM_BEST_BID, DOM_NEW_BEST_BID):
                book = self.bids
            elif kind in (DOM_ASK, DOM_BEST_ASK, DOM_NEW_BEST_ASK):
                book = self.asks
            else:
                continue
            if size > 0:
                book[price] = size
            else:
                book.pop(price, None)

    def snapshot(self, mode: int) -> dict[str, Any]:
        quote = self.quote
        ltp = _f(quote.get("lastPrice"))
        data: dict[str, Any] = {"ltp": ltp, "ltt": int(time.time() * 1000)}
        if mode == MODE_LTP:
            return data

        change = _f(quote.get("change"))
        data.update(
            {
                "open": _f(quote.get("open")),
                "high": _f(quote.get("high")),
                "low": _f(quote.get("low")),
                # ProjectX reports the change from the previous settlement, so
                # the previous close is the last price less that change.
                "close": round(ltp - change, 10) if ltp else 0.0,
                "volume": _i(quote.get("volume")),
                "last_quantity": self.last_quantity,
                "average_price": 0.0,
                "total_buy_quantity": sum(self.bids.values()),
                "total_sell_quantity": sum(self.asks.values()),
                "change": change,
                "change_percent": _f(quote.get("changePercent")),
                "bid": _f(quote.get("bestBid")),
                "ask": _f(quote.get("bestAsk")),
                "oi": 0,
            }
        )
        if mode == MODE_DEPTH:
            buy = sorted(self.bids.items(), key=lambda kv: kv[0], reverse=True)[:DEPTH_LEVELS]
            sell = sorted(self.asks.items(), key=lambda kv: kv[0])[:DEPTH_LEVELS]
            if not buy and quote.get("bestBid") is not None:
                buy = [(_f(quote.get("bestBid")), 0)]
            if not sell and quote.get("bestAsk") is not None:
                sell = [(_f(quote.get("bestAsk")), 0)]
            pad = [{"price": 0.0, "quantity": 0, "orders": 0}]
            data["depth"] = {
                "buy": [{"price": p, "quantity": q, "orders": 0} for p, q in buy]
                + pad * (DEPTH_LEVELS - len(buy)),
                "sell": [{"price": p, "quantity": q, "orders": 0} for p, q in sell]
                + pad * (DEPTH_LEVELS - len(sell)),
            }
        return data


class ProjectxWebSocketAdapter(BaseBrokerWebSocketAdapter):
    """ProjectX market hub -> OpenAlgo's ZeroMQ bus."""

    def __init__(self):
        super().__init__()
        self.logger = get_logger("projectx_websocket_adapter")
        self.broker_name = "projectx"
        self.user_id = None
        self._auth_override: str | None = None
        self.hub: SignalRHubClient | None = None
        self._lock = threading.Lock()
        # corr_id -> {symbol, exchange, mode, contract}
        self.subscriptions: dict[str, dict[str, Any]] = {}
        self._states: dict[str, ContractState] = {}

    # ------------------------------------------------------------ interface

    def initialize(self, broker_name, user_id, auth_data=None):
        self.broker_name = broker_name or "projectx"
        self.user_id = user_id
        if auth_data:
            self._auth_override = auth_data.get("auth_token") or auth_data.get("access_token")
        self.hub = SignalRHubClient(
            hub_base=f"{rtc_url()}/hubs/market",
            token_provider=self._token,
            on_invocation=self._on_event,
            on_ready=self._on_ready,
            on_closed=self._on_closed,
        )
        return self._create_success_response("ProjectX adapter initialized")

    def connect(self):
        if not self.hub:
            return self._create_error_response(
                "NOT_INITIALIZED", "Call initialize() before connect()"
            )
        self.hub.start()
        return self._create_success_response("Connecting to the ProjectX market hub")

    def disconnect(self):
        if self.hub:
            self.hub.stop()
        self.connected = False
        with self._lock:
            self.subscriptions.clear()
            self._states.clear()
        self.cleanup_zmq()
        return self._create_success_response("Disconnected from the ProjectX market hub")

    def subscribe(self, symbol, exchange, mode=MODE_QUOTE, depth_level=5):
        if mode not in MODE_NAMES:
            return self._create_error_response("INVALID_MODE", f"Mode {mode} is not supported")
        token_info = SymbolMapper.get_token_from_symbol(symbol, exchange)
        if not token_info:
            return self._create_error_response(
                "SYMBOL_NOT_FOUND", f"{symbol} not found for exchange {exchange}"
            )
        contract = str(token_info["token"])
        corr_id = f"{symbol}_{exchange}_{mode}"

        with self._lock:
            before = self._channels_for(contract)
            self.subscriptions[corr_id] = {
                "symbol": symbol,
                "exchange": exchange,
                "mode": mode,
                "contract": contract,
            }
            self._states.setdefault(contract, ContractState())
            added = [c for c in self._channels_for(contract) if c not in before]

        for channel in added:
            self._invoke(f"SubscribeContract{channel}", contract)

        return self._create_success_response(
            f"Subscribed to {symbol}.{exchange}",
            symbol=symbol,
            exchange=exchange,
            mode=mode,
            actual_depth=DEPTH_LEVELS if mode == MODE_DEPTH else None,
        )

    def unsubscribe(self, symbol, exchange, mode=MODE_QUOTE):
        corr_id = f"{symbol}_{exchange}_{mode}"
        with self._lock:
            stored = self.subscriptions.pop(corr_id, None)
            if not stored:
                return self._create_success_response(f"{symbol}.{exchange} was not subscribed")
            contract = stored["contract"]
            remaining = self._channels_for(contract)
            removed = [c for c in channels_for_mode(mode) if c not in remaining]
            if not remaining:
                self._states.pop(contract, None)

        for channel in removed:
            self._invoke(f"UnsubscribeContract{channel}", contract)

        return self._create_success_response(
            f"Unsubscribed from {symbol}.{exchange}", symbol=symbol, exchange=exchange, mode=mode
        )

    def unsubscribe_all(self):
        with self._lock:
            wanted = {contract: self._channels_for(contract) for contract in self._states}
            self.subscriptions.clear()
            self._states.clear()
        for contract, channels in wanted.items():
            for channel in sorted(channels):
                self._invoke(f"UnsubscribeContract{channel}", contract)
        return self._create_success_response("Unsubscribed from every ProjectX contract")

    # ------------------------------------------------------------ internals

    def _token(self) -> str | None:
        if self._auth_override:
            return self._auth_override
        return self.get_fresh_auth_token(self.user_id)

    def _channels_for(self, contract: str) -> set[str]:
        """Channels some subscription needs for ``contract``. Caller holds the lock."""
        channels: set[str] = set()
        for sub in self.subscriptions.values():
            if sub["contract"] == contract:
                channels.update(channels_for_mode(sub["mode"]))
        return channels

    def _invoke(self, target: str, contract: str) -> None:
        # Sent only while connected; after a (re)connect _on_ready replays
        # every subscription, so nothing is lost while the hub is down.
        if self.hub and self.hub.ready:
            self.hub.send(target, [contract])

    def _on_ready(self) -> None:
        self.connected = True
        with self._lock:
            wanted = {contract: self._channels_for(contract) for contract in self._states}
        for contract, channels in wanted.items():
            for channel in sorted(channels):
                self.hub.send(f"SubscribeContract{channel}", [contract])
        if wanted:
            self.logger.info(f"Resubscribed {len(wanted)} ProjectX contracts")

    def _on_closed(self) -> None:
        self.connected = False

    def _on_event(self, target: str, arguments: list) -> None:
        if len(arguments) < 2:
            return
        contract, payload = str(arguments[0]), arguments[1]
        with self._lock:
            state = self._states.get(contract)
            if state is None:
                return
            if target == "GatewayQuote":
                state.apply_quote(payload)
            elif target == "GatewayTrade":
                state.apply_trades(payload)
            elif target == "GatewayDepth":
                state.apply_depth(payload)
            else:
                return
            subscribers = [
                dict(s) for s in self.subscriptions.values() if s["contract"] == contract
            ]
            outgoing = [(sub, state.snapshot(sub["mode"])) for sub in subscribers]

        if target == "GatewayDepth":
            outgoing = [(sub, data) for sub, data in outgoing if sub["mode"] == MODE_DEPTH]

        now_ms = int(time.time() * 1000)
        for sub, data in outgoing:
            if not data.get("ltp") and sub["mode"] != MODE_DEPTH:
                continue
            data.update(
                {
                    "symbol": sub["symbol"],
                    "exchange": sub["exchange"],
                    "mode": sub["mode"],
                    "timestamp": now_ms,
                }
            )
            topic = f"{sub['exchange']}_{sub['symbol']}_{MODE_NAMES[sub['mode']]}"
            self.publish_market_data(topic, data)
