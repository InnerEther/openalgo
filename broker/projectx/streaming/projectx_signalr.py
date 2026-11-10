"""A minimal SignalR (JSON protocol) client for the ProjectX market hub.

ProjectX publishes live market data over an ASP.NET Core SignalR hub. The hub
accepts a direct WebSocket connection (no negotiate step) with the session
token in the ``access_token`` query parameter, so the protocol needed here is
small enough to speak over ``websocket-client`` without another dependency:

    every frame is JSON terminated by the record separator 0x1E
    client -> {"protocol": "json", "version": 1}       handshake
    server -> {}                                       handshake accepted
    type 1  invocation  {"type": 1, "target": ..., "arguments": [...]}
    type 6  ping        sent by both sides to keep the connection alive
    type 7  close       the server is ending the connection (may carry "error")

Hub methods used (from the ProjectX documentation and its Python SDK):
    SubscribeContractQuotes / UnsubscribeContractQuotes          (contractId)
    SubscribeContractTrades / UnsubscribeContractTrades          (contractId)
    SubscribeContractMarketDepth / UnsubscribeContractMarketDepth (contractId)
Events received: GatewayQuote, GatewayTrade, GatewayDepth, each with
``arguments = [contractId, payload]``.

The client runs on its own daemon thread with a reconnect loop and re-reads the
token before every connection. It lives in the websocket proxy, which runs
unpatched, so standard threading is correct here.
"""

import json
import threading
import time
from collections.abc import Callable
from urllib.parse import quote

import websocket

from utils.logging import get_logger

RECORD_SEPARATOR = "\x1e"

MSG_INVOCATION = 1
MSG_PING = 6
MSG_CLOSE = 7

#: How often this side sends a ping; SignalR servers drop a client silent for 30s.
PING_INTERVAL = 15.0
#: The hub pings every 15s, so this much silence means the connection is dead.
STALL_TIMEOUT = 60.0


def encode(message: dict) -> str:
    """One SignalR JSON frame."""
    return json.dumps(message, separators=(",", ":")) + RECORD_SEPARATOR


def decode(raw) -> list[dict]:
    """Every JSON message in a received WebSocket frame."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    messages = []
    for part in raw.split(RECORD_SEPARATOR):
        if not part.strip():
            continue
        try:
            message = json.loads(part)
        except ValueError:
            continue
        if isinstance(message, dict):
            messages.append(message)
    return messages


def hub_url(base: str, token: str) -> str:
    """The WebSocket address of a hub, with the token as ``access_token``."""
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}access_token={quote(token, safe='')}"


class SignalRHubClient:
    """Keeps one SignalR hub connection open and hands its invocations on.

    Args:
        hub_base: The hub's https address, such as ``https://rtc.topstepx.com/hubs/market``.
        token_provider: Returns the current session token, or None.
        on_invocation: Called as ``on_invocation(target, arguments)`` for each event.
        on_ready: Called after every successful handshake, so the owner can
            subscribe again after a reconnect.
        on_closed: Called whenever the connection ends.
    """

    def __init__(
        self,
        hub_base: str,
        token_provider: Callable[[], str | None],
        on_invocation: Callable[[str, list], None],
        on_ready: Callable[[], None] | None = None,
        on_closed: Callable[[], None] | None = None,
        name: str = "projectx_market_hub",
    ):
        self.hub_base = hub_base
        self.token_provider = token_provider
        self.on_invocation = on_invocation
        self.on_ready = on_ready
        self.on_closed = on_closed
        self.logger = get_logger(name)

        self._stop = threading.Event()
        self._ws: websocket.WebSocketApp | None = None
        self._ws_lock = threading.Lock()
        self._ready = False
        self._last_message = 0.0
        self._thread: threading.Thread | None = None
        self._keepalive: threading.Thread | None = None

    @property
    def ready(self) -> bool:
        return self._ready

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="projectx-hub", daemon=True)
        self._thread.start()
        if not (self._keepalive and self._keepalive.is_alive()):
            self._keepalive = threading.Thread(
                target=self._keep_alive, name="projectx-hub-ping", daemon=True
            )
            self._keepalive.start()

    def stop(self) -> None:
        self._stop.set()
        self._ready = False
        with self._ws_lock:
            ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                self.logger.debug("Closing the ProjectX hub connection raised", exc_info=True)

    def send(self, target: str, arguments: list) -> bool:
        """Invoke a hub method without waiting for a result. False if not connected."""
        if not self._ready:
            return False
        return self._send_raw(
            encode({"type": MSG_INVOCATION, "target": target, "arguments": arguments})
        )

    # -------------------------------------------------------------- internals

    def _send_raw(self, frame: str) -> bool:
        with self._ws_lock:
            ws = self._ws
        if ws is None:
            return False
        try:
            ws.send(frame)
            return True
        except Exception as exc:
            self.logger.warning(f"ProjectX hub send failed: {exc}")
            return False

    def _run(self) -> None:
        delay = 2.0
        while not self._stop.is_set():
            token = None
            try:
                token = self.token_provider()
            except Exception:
                self.logger.exception("Reading the ProjectX session token failed")
            if not token:
                self.logger.warning("No ProjectX session token; the live feed waits for a login")
                self._stop.wait(30.0)
                continue

            connected_at = time.monotonic()
            ws = websocket.WebSocketApp(
                hub_url(self.hub_base, token),
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            with self._ws_lock:
                self._ws = ws
            try:
                ws.run_forever(skip_utf8_validation=True)
            except Exception:
                self.logger.exception("ProjectX hub connection failed")
            finally:
                with self._ws_lock:
                    if self._ws is ws:
                        self._ws = None
                self._ready = False

            if self._stop.is_set():
                break
            # A connection that held for a while resets the backoff.
            if time.monotonic() - connected_at > 60:
                delay = 2.0
            self.logger.info(f"ProjectX hub disconnected; reconnecting in {delay:.0f}s")
            self._stop.wait(delay)
            delay = min(delay * 2, 60.0)

    def _keep_alive(self) -> None:
        while not self._stop.wait(PING_INTERVAL):
            if not self._ready:
                continue
            self._send_raw(encode({"type": MSG_PING}))
            if time.monotonic() - self._last_message > STALL_TIMEOUT:
                self.logger.warning("ProjectX hub went silent; reconnecting")
                with self._ws_lock:
                    ws = self._ws
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        self.logger.debug("Closing a stalled hub connection raised", exc_info=True)

    def _on_open(self, ws) -> None:
        self._last_message = time.monotonic()
        try:
            ws.send(encode({"protocol": "json", "version": 1}))
        except Exception as exc:
            self.logger.warning(f"ProjectX hub handshake could not be sent: {exc}")

    def _on_message(self, ws, raw) -> None:
        self._last_message = time.monotonic()
        for message in decode(raw):
            if not self._ready and "type" not in message:
                # The handshake answer: {} when accepted, {"error": ...} when not.
                if message.get("error"):
                    self.logger.error(f"ProjectX hub refused the handshake: {message.get('error')}")
                    ws.close()
                    return
                self._ready = True
                self.logger.info("ProjectX market hub connected")
                if self.on_ready:
                    try:
                        self.on_ready()
                    except Exception:
                        self.logger.exception("ProjectX hub on_ready callback failed")
                continue

            msg_type = message.get("type")
            if msg_type == MSG_INVOCATION:
                target = message.get("target") or ""
                arguments = message.get("arguments") or []
                try:
                    self.on_invocation(target, arguments)
                except Exception:
                    self.logger.exception(f"Handling ProjectX hub event {target} failed")
            elif msg_type == MSG_CLOSE:
                error = message.get("error")
                if error:
                    self.logger.error(f"ProjectX hub closed the connection: {error}")
                ws.close()
                return

    def _on_error(self, ws, error) -> None:
        self.logger.warning(f"ProjectX hub error: {error}")

    def _on_close(self, ws, status_code=None, reason=None) -> None:
        self._ready = False
        if self.on_closed:
            try:
                self.on_closed()
            except Exception:
                self.logger.exception("ProjectX hub on_closed callback failed")
