"""ProjectX Gateway endpoints, request plumbing and rate pacing.

ProjectX Gateway is the API behind TopstepX and the other prop firms that run
on the ProjectX platform. One login token (a JWT valid for 24 hours) serves
every REST call and both realtime hubs.

The hosts default to TopstepX and are read from the environment at call time,
so a trader on another ProjectX firm points OpenAlgo at that firm by setting
``PROJECTX_API_URL`` and ``PROJECTX_RTC_URL`` in ``.env``.

Rate limits published by ProjectX:
    POST /api/History/retrieveBars   50 requests per 30 seconds
    every other endpoint             200 requests per 60 seconds
A request over the limit is answered with HTTP 429.
"""

import os
import threading
import time
from collections import deque

from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)

DEFAULT_API_URL = "https://api.topstepx.com"
DEFAULT_RTC_URL = "https://rtc.topstepx.com"

#: Exchange code OpenAlgo uses for every ProjectX contract. ProjectX lists CME
#: Group futures (CME, CBOT, NYMEX, COMEX) without naming the venue per
#: contract, so they share one code.
EXCHANGE = "CME"

#: The sentence a trader reads when ProjectX refuses the stored session.
SESSION_EXPIRED = "Your ProjectX session has ended. Log in to ProjectX again from Select Broker."


def api_url() -> str:
    """The REST origin, without a trailing slash."""
    return (os.getenv("PROJECTX_API_URL") or DEFAULT_API_URL).rstrip("/")


def rtc_url() -> str:
    """The realtime (SignalR) origin, without a trailing slash."""
    return (os.getenv("PROJECTX_RTC_URL") or DEFAULT_RTC_URL).rstrip("/")


def use_live_data() -> bool:
    """Whether to read ProjectX's live data subscription instead of its sim data.

    Most evaluation and funded accounts carry the sim subscription only, and
    ProjectX answers ``live=true`` requests from them with nothing, so this is
    off unless ``PROJECTX_LIVE_DATA`` is set to true.
    """
    return os.getenv("PROJECTX_LIVE_DATA", "false").strip().lower() in ("1", "true", "yes")


class _SlidingWindow:
    """Blocks a caller until a request fits inside a rolling window.

    Only request threads (green threads under eventlet, pool threads under
    gthread, plain threads on the dev server) use it; the streaming adapter
    never does, so a stdlib lock is correct in every mode. The sleep happens
    outside the lock.
    """

    def __init__(self, limit: int, window: float):
        self.limit = limit
        self.window = window
        self._stamps: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._stamps and now - self._stamps[0] >= self.window:
                    self._stamps.popleft()
                if len(self._stamps) < self.limit:
                    self._stamps.append(now)
                    return
                wait = self.window - (now - self._stamps[0]) + 0.01
            time.sleep(max(wait, 0.01))


# A margin under the published limits, so a burst never earns a 429.
_BARS_WINDOW = _SlidingWindow(limit=45, window=30.0)
_GENERAL_WINDOW = _SlidingWindow(limit=180, window=60.0)


class ProjectXError(Exception):
    """A ProjectX request that did not succeed, with a sentence a trader can read."""

    def __init__(self, message: str, status_code: int = 500):
        super().__init__(message)
        self.status_code = status_code


def post(path: str, payload: dict, token: str | None = None, timeout: float = 30.0) -> dict:
    """POST JSON to a Gateway endpoint and return the decoded body.

    Args:
        path: The endpoint path, such as ``/api/History/retrieveBars``.
        payload: The JSON body.
        token: The session JWT; omitted only for the login call.
        timeout: Seconds before the request is abandoned.

    Raises:
        ProjectXError: On a transport failure, a non-200 answer, or a body
            whose ``success`` is false.
    """
    (_BARS_WINDOW if path.endswith("/retrieveBars") else _GENERAL_WINDOW).acquire()

    headers = {"accept": "text/plain", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        response = get_httpx_client().post(
            f"{api_url()}{path}", json=payload, headers=headers, timeout=timeout
        )
    except Exception as exc:
        logger.exception(f"ProjectX request to {path} failed: {exc}")
        raise ProjectXError(
            "ProjectX could not be reached. Check your internet connection.", 502
        ) from exc

    if response.status_code == 401:
        raise ProjectXError(SESSION_EXPIRED, 401)
    if response.status_code == 429:
        raise ProjectXError(
            "ProjectX is limiting requests right now. Wait a few seconds and try again.", 429
        )
    if response.status_code != 200:
        logger.error(f"ProjectX {path} answered HTTP {response.status_code}: {response.text[:300]}")
        raise ProjectXError("ProjectX did not answer the request. Try again shortly.", 502)

    try:
        body = response.json()
    except ValueError as exc:
        logger.error(
            f"ProjectX {path} answered with a body that is not JSON: {response.text[:300]}"
        )
        raise ProjectXError(
            "ProjectX sent an answer OpenAlgo could not read. Try again shortly.", 502
        ) from exc

    if not isinstance(body, dict) or not body.get("success", False):
        error_code = body.get("errorCode") if isinstance(body, dict) else None
        error_message = body.get("errorMessage") if isinstance(body, dict) else None
        logger.error(f"ProjectX {path} refused the request: errorCode={error_code} {error_message}")
        raise ProjectXError(error_message or "ProjectX refused the request.", 400)

    return body
