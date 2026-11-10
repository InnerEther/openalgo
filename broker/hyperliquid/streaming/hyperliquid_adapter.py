"""OpenAlgo WebSocket adapter for Hyperliquid's public market-data feed.

One upstream connection carries every symbol. Each OpenAlgo mode maps to a set
of Hyperliquid subscriptions (see ``hyperliquid_mapping``); the adapter keeps
the latest value of every field per coin, merges each update into it and
publishes the whole to every OpenAlgo subscriber of that coin, so a depth
subscriber also carries the last trade, and a quote subscriber the book top.

Field sources:
  ltp, ltq                trades (the last trade in each frame)
  volume, oi, prev_close  activeAssetCtx / activeSpotAssetCtx
  open, high, low         candle 1d (the current UTC day)
  bid/ask price and qty   bbo
  depth                   l2Book, top 5 levels per side

The broker factory loads this module by name
(``broker.hyperliquid.streaming.hyperliquid_adapter``) and the class
``HyperliquidWebSocketAdapter``.
"""

import threading
import time
from typing import Any

from broker.hyperliquid.api import client
from broker.hyperliquid.streaming import hyperliquid_mapping as mapping
from broker.hyperliquid.streaming.hyperliquid_websocket import HyperliquidWebSocket
from database.token_db import get_br_symbol
from utils.logging import get_logger
from websocket_proxy.base_adapter import BaseBrokerWebSocketAdapter
from websocket_proxy.mapping import SymbolMapper


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _levels(side, n=5):
    out = []
    for level in (side or [])[:n]:
        if isinstance(level, dict):
            out.append({"price": _f(level.get("px")), "quantity": _f(level.get("sz"))})
    while len(out) < n:
        out.append({"price": 0.0, "quantity": 0.0})
    return out


class HyperliquidWebSocketAdapter(BaseBrokerWebSocketAdapter):
    def __init__(self):
        super().__init__()
        self.logger = get_logger("hyperliquid_websocket_adapter")
        self.broker_name = client.broker_name()
        self.user_id = None
        self.ws_client: HyperliquidWebSocket | None = None
        self._lock = threading.Lock()
        self.last_values: dict[str, dict] = {}  # coin -> merged fields
        self._connect_thread: threading.Thread | None = None

    # -- BaseBrokerWebSocketAdapter ------------------------------------------

    def initialize(self, broker_name: str, user_id: str, auth_data: dict | None = None) -> None:
        # Market data is public: nothing to authenticate.
        self.broker_name = broker_name
        self.user_id = user_id
        self.ws_client = HyperliquidWebSocket(
            on_message=self._on_message,
            on_open=self._on_open,
            on_close=self._on_close,
        )
        self.running = True

    def connect(self) -> None:
        if not self.ws_client:
            self.logger.error("Call initialize() before connect()")
            return
        if self._connect_thread and self._connect_thread.is_alive():
            return
        self._connect_thread = threading.Thread(
            target=self.ws_client.connect, name="hyperliquid-ws", daemon=True
        )
        self._connect_thread.start()

    def disconnect(self) -> None:
        self.running = False
        if self.ws_client:
            self.ws_client.forget_subscriptions()
            self.ws_client.close_connection()
        self.connected = False
        self.cleanup_zmq()

    def subscribe(
        self, symbol: str, exchange: str, mode: int = 2, depth_level: int = 5
    ) -> dict[str, Any]:
        if mode not in mapping.SUPPORTED_MODES:
            return self._create_error_response(
                "INVALID_MODE", f"Mode {mode} is not supported. Use 1, 2 or 3."
            )
        if not SymbolMapper.get_token_from_symbol(symbol, exchange):
            return self._create_error_response(
                "SYMBOL_NOT_FOUND", f"{symbol} not found for exchange {exchange}"
            )
        coin = get_br_symbol(symbol, exchange)
        if not coin:
            return self._create_error_response(
                "SYMBOL_NOT_FOUND", f"{symbol} not found for exchange {exchange}"
            )

        feeds = mapping.feeds_for(mode)
        with self._lock:
            self.subscriptions[f"{symbol}_{exchange}_{mode}"] = {
                "symbol": symbol,
                "exchange": exchange,
                "coin": coin,
                "mode": mode,
                "feeds": feeds,
                "depth_level": depth_level,
            }
        if self.ws_client:
            for feed in feeds:
                self.ws_client.subscribe(mapping.subscription(feed, coin))
        self.logger.info("Subscribed %s.%s mode=%s", symbol, exchange, mode)
        return self._create_success_response(
            f"Subscription requested for {symbol}.{exchange}",
            symbol=symbol,
            exchange=exchange,
            mode=mode,
        )

    def unsubscribe(self, symbol: str, exchange: str, mode: int = 2) -> dict[str, Any]:
        with self._lock:
            stored = self.subscriptions.pop(f"{symbol}_{exchange}_{mode}", None)
            if not stored:
                return self._create_success_response(
                    f"Not subscribed to {symbol}.{exchange}", symbol=symbol, exchange=exchange
                )
            coin = stored["coin"]
            still_needed = {
                feed
                for sub in self.subscriptions.values()
                if sub["coin"] == coin
                for feed in sub["feeds"]
            }
            stale = [feed for feed in stored["feeds"] if feed not in still_needed]
            if not still_needed:
                self.last_values.pop(coin, None)

        # The connection stays up with no symbols: the pool reuses this adapter.
        if self.ws_client:
            for feed in stale:
                self.ws_client.unsubscribe(mapping.subscription(feed, coin))
        return self._create_success_response(
            f"Unsubscribed from {symbol}.{exchange}", symbol=symbol, exchange=exchange, mode=mode
        )

    # -- callbacks -------------------------------------------------------------

    def _on_open(self) -> None:
        self.connected = True

    def _on_close(self) -> None:
        self.connected = False

    def _on_message(self, msg: dict) -> None:
        try:
            for coin, fields in self.normalise(msg):
                self._publish(coin, fields)
        except Exception:
            self.logger.exception("Hyperliquid message handling failed")

    # -- normalisation ---------------------------------------------------------

    @staticmethod
    def normalise(msg: dict) -> list[tuple[str, dict]]:
        """Turn one Hyperliquid frame into ``(coin, fields)`` updates."""
        channel = msg.get("channel")
        data = msg.get("data")

        if channel == "trades" and isinstance(data, list) and data:
            last_by_coin: dict[str, dict] = {}
            for trade in data:
                if isinstance(trade, dict) and trade.get("coin"):
                    last_by_coin[trade["coin"]] = trade
            return [
                (
                    coin,
                    {
                        "ltp": _f(t.get("px")),
                        "ltq": _f(t.get("sz")),
                        "last_trade_time": int(t.get("time", 0) or 0),
                        "_traded": True,
                    },
                )
                for coin, t in last_by_coin.items()
            ]

        if channel in ("activeAssetCtx", "activeSpotAssetCtx") and isinstance(data, dict):
            ctx = data.get("ctx") or {}
            fields = {
                "volume": _f(ctx.get("dayBaseVlm")),
                "prev_close": _f(ctx.get("prevDayPx")),
                "close": _f(ctx.get("prevDayPx")),
                "_mid": _f(ctx.get("midPx")) or _f(ctx.get("markPx")),
            }
            if "openInterest" in ctx:
                fields["oi"] = _f(ctx.get("openInterest"))
            return [(data.get("coin", ""), fields)]

        if channel == "candle" and isinstance(data, dict):
            return [
                (
                    data.get("s", ""),
                    {
                        "open": _f(data.get("o")),
                        "high": _f(data.get("h")),
                        "low": _f(data.get("l")),
                    },
                )
            ]

        if channel == "bbo" and isinstance(data, dict):
            bbo = data.get("bbo") or [None, None]
            bid = bbo[0] if len(bbo) > 0 and isinstance(bbo[0], dict) else {}
            ask = bbo[1] if len(bbo) > 1 and isinstance(bbo[1], dict) else {}
            return [
                (
                    data.get("coin", ""),
                    {
                        "bid_price": _f(bid.get("px")),
                        "bid_qty": _f(bid.get("sz")),
                        "ask_price": _f(ask.get("px")),
                        "ask_qty": _f(ask.get("sz")),
                    },
                )
            ]

        if channel == "l2Book" and isinstance(data, dict):
            levels = data.get("levels") or [[], []]
            bids = _levels(levels[0] if len(levels) > 0 else [])
            asks = _levels(levels[1] if len(levels) > 1 else [])
            return [
                (
                    data.get("coin", ""),
                    {
                        "depth": {"buy": bids, "sell": asks},
                        "totalbuyqty": sum(level["quantity"] for level in bids),
                        "totalsellqty": sum(level["quantity"] for level in asks),
                    },
                )
            ]

        return []

    def _publish(self, coin: str, fields: dict) -> None:
        if not coin:
            return
        with self._lock:
            subscribers = [dict(s) for s in self.subscriptions.values() if s["coin"] == coin]
            # A frame can still arrive just after the last unsubscribe; caching
            # it would leave an entry nothing ever removes.
            if not subscribers:
                return
            cached = self.last_values.setdefault(coin, {})
            mid = fields.pop("_mid", None)
            cached.update(fields)
            # Until the first trade arrives, the mid price stands in for LTP so
            # a quiet market does not show 0.
            if mid and not cached.get("_traded"):
                cached["ltp"] = mid
            merged = {k: v for k, v in cached.items() if not k.startswith("_")}

        ts = int(time.time() * 1000)
        for sub in subscribers:
            mode = sub["mode"]
            data = dict(merged)
            data.update(
                {
                    "symbol": sub["symbol"],
                    "exchange": sub["exchange"],
                    "mode": mode,
                    "timestamp": ts,
                }
            )
            topic = f"{sub['exchange']}_{sub['symbol']}_{mapping.MODE_NAMES[mode]}"
            self.publish_market_data(topic, data)
