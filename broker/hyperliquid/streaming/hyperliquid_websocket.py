"""Low-level client for Hyperliquid's public WebSocket.

Address: wss://api.hyperliquid.xyz/ws, or the testnet's when HYPERLIQUID_TESTNET
is true (``client.ws_url``).

Protocol: JSON frames, no authentication for market data.

  subscribe:    {"method": "subscribe",   "subscription": {"type": "trades", "coin": "BTC"}}
  unsubscribe:  {"method": "unsubscribe", "subscription": {...the same...}}
  heartbeat:    {"method": "ping"}  ->  {"channel": "pong"}

Hyperliquid closes a connection that has sent nothing for 60 seconds, so a ping
frame goes out every 30 seconds. Every message arrives as
``{"channel": <name>, "data": <payload>}``; ``subscriptionResponse`` and
``pong`` are acknowledgements and are not passed on.

This runs in the websocket proxy process, which is never monkey-patched, so the
plain ``threading`` primitives here are real.
"""

import json
import ssl
import threading
import time

import websocket

from broker.hyperliquid.api.client import ws_url
from utils.logging import get_logger

logger = get_logger("hyperliquid_websocket")


def subscription_key(subscription: dict) -> str:
    """A stable key for one subscription dict."""
    return json.dumps(subscription, sort_keys=True, separators=(",", ":"))


class HyperliquidWebSocket:
    HEARTBEAT_INTERVAL = 30

    def __init__(
        self,
        on_message=None,
        on_open=None,
        on_close=None,
        on_error=None,
        url: str | None = None,
        max_retry_attempt: int = 10,
        retry_delay: int = 2,
        retry_multiplier: int = 2,
    ):
        self.url = url or ws_url()
        self.on_message = on_message or (lambda msg: None)
        self.on_open = on_open or (lambda: None)
        self.on_close = on_close or (lambda: None)
        self.on_error = on_error or (lambda err: None)
        self.max_retry_attempt = max_retry_attempt
        self.retry_delay = retry_delay
        self.retry_multiplier = retry_multiplier

        self.wsapp: websocket.WebSocketApp | None = None
        self._lock = threading.Lock()
        self._connected = False
        self._connect_succeeded = False
        self._stop = threading.Event()
        self._heartbeat: threading.Thread | None = None
        # Every active subscription, kept across reconnects so they are
        # replayed, and filled before the first connect so nothing is lost.
        self._active: dict[str, dict] = {}

    @property
    def is_connected(self) -> bool:
        return self._connected

    # -- subscriptions -------------------------------------------------------

    def _send_locked(self, frame: dict) -> None:
        """Send a frame if the socket is up. Caller holds ``_lock``."""
        if not self._connected or not self.wsapp:
            return
        try:
            self.wsapp.send(json.dumps(frame))
        except Exception as exc:
            logger.error("Hyperliquid WS send failed: %s", exc)

    def subscribe(self, subscription: dict) -> None:
        key = subscription_key(subscription)
        with self._lock:
            if key in self._active:
                return
            self._active[key] = subscription
            self._send_locked({"method": "subscribe", "subscription": subscription})

    def unsubscribe(self, subscription: dict) -> None:
        key = subscription_key(subscription)
        with self._lock:
            if self._active.pop(key, None) is None:
                return
            self._send_locked({"method": "unsubscribe", "subscription": subscription})

    def forget_subscriptions(self) -> None:
        with self._lock:
            self._active.clear()

    # -- connection ----------------------------------------------------------

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.HEARTBEAT_INTERVAL):
            with self._lock:
                self._send_locked({"method": "ping"})

    def connect(self) -> None:
        """Run the connection with reconnects (blocking: call it on a thread)."""
        self._stop.clear()
        if self._heartbeat is None or not self._heartbeat.is_alive():
            self._heartbeat = threading.Thread(
                target=self._heartbeat_loop, name="hyperliquid-ws-heartbeat", daemon=True
            )
            self._heartbeat.start()

        attempts = 0
        delay = self.retry_delay
        while not self._stop.is_set() and attempts <= self.max_retry_attempt:
            try:
                logger.info("Hyperliquid WS connecting (attempt %s)", attempts + 1)
                self.wsapp = websocket.WebSocketApp(
                    self.url,
                    on_open=self._ws_on_open,
                    on_message=self._ws_on_message,
                    on_error=self._ws_on_error,
                    on_close=self._ws_on_close,
                )
                self.wsapp.run_forever(sslopt={"cert_reqs": ssl.CERT_REQUIRED})
            except Exception as exc:
                logger.error("Hyperliquid WS connect error: %s", exc)
            if self._stop.is_set():
                break
            # A connection that came up healthy restores the retry budget, so
            # a feed that drops once a day keeps coming back.
            if self._connect_succeeded:
                self._connect_succeeded = False
                attempts = 0
                delay = self.retry_delay
            attempts += 1
            logger.warning("Hyperliquid WS disconnected; retrying in %ss", delay)
            if self._stop.wait(delay):
                break
            delay = min(delay * self.retry_multiplier, 60)

        if attempts > self.max_retry_attempt:
            logger.error("Hyperliquid WS gave up after %s attempts", attempts)

    def close_connection(self) -> None:
        self._stop.set()
        if self.wsapp:
            try:
                self.wsapp.close()
            except Exception:
                logger.debug("Hyperliquid WS close raised", exc_info=True)

    # -- callbacks -------------------------------------------------------------

    def _ws_on_open(self, wsapp) -> None:
        logger.info("Hyperliquid WS connected")
        with self._lock:
            self._connected = True
            self._connect_succeeded = True
            for subscription in self._active.values():
                self._send_locked({"method": "subscribe", "subscription": subscription})
        self.on_open()

    def _ws_on_message(self, wsapp, raw) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            logger.debug("Hyperliquid WS non-JSON frame: %s", str(raw)[:120])
            return
        channel = msg.get("channel")
        if channel in ("pong", "subscriptionResponse"):
            return
        if channel == "error":
            logger.error("Hyperliquid WS error frame: %s", msg.get("data"))
            return
        self.on_message(msg)

    def _ws_on_error(self, wsapp, error) -> None:
        logger.error("Hyperliquid WS error: %s", error)
        self.on_error(error)

    def _ws_on_close(self, wsapp, *args) -> None:
        with self._lock:
            self._connected = False
        logger.info("Hyperliquid WS closed")
        self.on_close()
