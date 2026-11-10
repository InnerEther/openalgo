"""ProjectX market data: candles, quotes and depth.

ProjectX Gateway has one market-data REST endpoint, ``POST
/api/History/retrieveBars``. It has no quote or order-book endpoint; live
prices arrive only over the SignalR market hub (see ``streaming/``). So the
quote and depth calls here are built from bars:

    the current daily bar (``includePartialBar``) gives open, high, low,
    volume and the last traded price (its close), and the daily bar before it
    gives the previous close.

Bid, ask and order-book levels are not available over REST and come back as
zero; the live feed carries best bid and ask.
"""

from datetime import UTC, date, datetime, time, timedelta, timezone

import pandas as pd
import pytz

from broker.projectx.api.baseurl import ProjectXError, post, use_live_data
from database.token_db import get_token
from utils.logging import get_logger
from utils.thread_safe_cache import LockedTTLCache

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")
CHICAGO = pytz.timezone("America/Chicago")

#: ProjectX aggregation units.
UNIT_SECOND, UNIT_MINUTE, UNIT_HOUR, UNIT_DAY, UNIT_WEEK, UNIT_MONTH = 1, 2, 3, 4, 5, 6

#: The most bars ProjectX returns for one request.
MAX_BARS = 20000

#: Pages fetched for one history request at most (20 x 20,000 bars).
MAX_PAGES = 20

_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume", "oi"]

# Quotes are rebuilt from bars and bars are limited to 50 requests per 30
# seconds, so a quote is reused for a moment rather than costing a request
# every time a page polls it. Live prices come from the streaming feed.
_QUOTE_CACHE = LockedTTLCache(maxsize=512, ttl=2.0)


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


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def trading_date(moment: pd.Timestamp) -> date:
    """The CME trading day a session bar belongs to.

    A CME Globex session opens at 17:00 Chicago time and belongs to the next
    calendar day, so adding seven hours to the Chicago time lands every
    session open on its trading day, whether ProjectX stamps a daily bar at
    the open (22:00 or 23:00 UTC the day before) or at midnight UTC.
    """
    return (moment.tz_convert(CHICAGO) + pd.Timedelta(hours=7)).date()


class BrokerData:
    """ProjectX market data for one stored session token."""

    #: OpenAlgo interval -> (ProjectX unit, unitNumber).
    TIMEFRAME_MAP = {
        "1s": (UNIT_SECOND, 1),
        "5s": (UNIT_SECOND, 5),
        "10s": (UNIT_SECOND, 10),
        "15s": (UNIT_SECOND, 15),
        "30s": (UNIT_SECOND, 30),
        "45s": (UNIT_SECOND, 45),
        "1m": (UNIT_MINUTE, 1),
        "2m": (UNIT_MINUTE, 2),
        "3m": (UNIT_MINUTE, 3),
        "5m": (UNIT_MINUTE, 5),
        "10m": (UNIT_MINUTE, 10),
        "15m": (UNIT_MINUTE, 15),
        "20m": (UNIT_MINUTE, 20),
        "30m": (UNIT_MINUTE, 30),
        "1h": (UNIT_HOUR, 1),
        "2h": (UNIT_HOUR, 2),
        "3h": (UNIT_HOUR, 3),
        "4h": (UNIT_HOUR, 4),
        "D": (UNIT_DAY, 1),
        "W": (UNIT_WEEK, 1),
        "M": (UNIT_MONTH, 1),
    }

    def __init__(self, auth_token):
        self.auth_token = auth_token
        self.timeframe_map = self.TIMEFRAME_MAP

    # ------------------------------------------------------------------ helpers

    def _contract_id(self, symbol: str, exchange: str) -> str:
        contract_id = get_token(symbol, exchange)
        if not contract_id:
            raise ProjectXError(
                f"{symbol} is not in the ProjectX contract list. Download the contract "
                "list again, or pick the contract from symbol search.",
                404,
            )
        return contract_id

    def _retrieve_bars(
        self,
        contract_id: str,
        unit: int,
        unit_number: int,
        start: datetime,
        end: datetime,
        limit: int = MAX_BARS,
        include_partial: bool = True,
    ) -> list[dict]:
        """One retrieveBars call. ProjectX returns the newest bars first."""
        body = post(
            "/api/History/retrieveBars",
            {
                "contractId": contract_id,
                "live": use_live_data(),
                "startTime": _iso(start),
                "endTime": _iso(end),
                "unit": unit,
                "unitNumber": unit_number,
                "limit": limit,
                "includePartialBar": include_partial,
            },
            token=self.auth_token,
        )
        bars = body.get("bars") or []
        return bars if isinstance(bars, list) else []

    # ------------------------------------------------------------------ history

    def get_history(self, symbol, exchange, interval, start_date, end_date) -> pd.DataFrame:
        """Candles for ``symbol`` between two dates, as OpenAlgo's 7-column frame.

        The dates are read as IST calendar days (00:00 IST to 23:59:59 IST), the
        convention every OpenAlgo caller builds them with. The end is capped at
        the present, and the bar still forming is included so the chart ends on
        it.

        Timestamps are epoch seconds. Intraday bars keep ProjectX's own UTC
        time. Daily, weekly and monthly bars are stamped at 00:00 UTC of their
        CME trading day, the same convention OpenAlgo's other brokers follow for
        daily candles.
        """
        if interval not in self.TIMEFRAME_MAP:
            supported = ", ".join(self.TIMEFRAME_MAP)
            raise ProjectXError(
                f"ProjectX does not offer the {interval} interval. Choose one of {supported}.", 400
            )
        unit, unit_number = self.TIMEFRAME_MAP[interval]
        contract_id = self._contract_id(symbol, exchange)

        start = IST.localize(datetime.combine(_to_date(start_date), time.min))
        end = IST.localize(datetime.combine(_to_date(end_date), time(23, 59, 59)))
        now = datetime.now(UTC)
        if end > now:
            end = now
        if end <= start:
            return pd.DataFrame(columns=_COLUMNS)

        bars: list[dict] = []
        page_end = end
        for _ in range(MAX_PAGES):
            page = self._retrieve_bars(contract_id, unit, unit_number, start, page_end)
            if not page:
                break
            bars.extend(page)
            if len(page) < MAX_BARS:
                break
            earliest = min(pd.Timestamp(bar.get("t")) for bar in page if bar.get("t"))
            if earliest <= pd.Timestamp(start) or earliest >= pd.Timestamp(page_end):
                break
            page_end = earliest.to_pydatetime()
        else:
            logger.warning(
                f"ProjectX history for {symbol} {interval} stopped at {MAX_PAGES} pages; "
                "the earliest part of the range was not fetched"
            )

        return self._bars_to_frame(bars, unit)

    @staticmethod
    def _bars_to_frame(bars: list[dict], unit: int) -> pd.DataFrame:
        rows = [
            {
                "timestamp": bar.get("t"),
                "open": _f(bar.get("o")),
                "high": _f(bar.get("h")),
                "low": _f(bar.get("l")),
                "close": _f(bar.get("c")),
                "volume": _i(bar.get("v")),
                "oi": 0,
            }
            for bar in bars
            if isinstance(bar, dict) and bar.get("t")
        ]
        if not rows:
            return pd.DataFrame(columns=_COLUMNS)

        df = pd.DataFrame(rows, columns=_COLUMNS)
        stamps = pd.to_datetime(df["timestamp"], utc=True, format="ISO8601")
        if unit >= UNIT_DAY:
            days = [trading_date(stamp) for stamp in stamps]
            stamps = pd.to_datetime(pd.Series(days), utc=True)
        df["timestamp"] = stamps.astype("int64") // 10**9
        return (
            df.sort_values("timestamp")
            .drop_duplicates(subset=["timestamp"], keep="last")
            .reset_index(drop=True)
        )

    # ------------------------------------------------------------------ quotes

    def _daily_snapshot(self, symbol: str, exchange: str) -> dict:
        key = (symbol, exchange)
        cached = _QUOTE_CACHE.get(key)
        if cached is not None:
            return cached

        contract_id = self._contract_id(symbol, exchange)
        now = datetime.now(UTC)
        bars = self._retrieve_bars(contract_id, UNIT_DAY, 1, now - timedelta(days=10), now, limit=2)
        frame = self._bars_to_frame(bars, UNIT_DAY)
        if frame.empty:
            snapshot = {
                "ltp": 0.0,
                "open": 0.0,
                "high": 0.0,
                "low": 0.0,
                "volume": 0,
                "prev_close": 0.0,
            }
        else:
            today = frame.iloc[-1]
            prev_close = float(frame.iloc[-2]["close"]) if len(frame) > 1 else 0.0
            snapshot = {
                "ltp": float(today["close"]),
                "open": float(today["open"]),
                "high": float(today["high"]),
                "low": float(today["low"]),
                "volume": int(today["volume"]),
                "prev_close": prev_close,
            }
        _QUOTE_CACHE[key] = snapshot
        return snapshot

    def get_quotes(self, symbol: str, exchange: str) -> dict:
        """Last price and today's range, from the current daily bar."""
        snapshot = self._daily_snapshot(symbol, exchange)
        return {
            "ltp": snapshot["ltp"],
            "open": snapshot["open"],
            "high": snapshot["high"],
            "low": snapshot["low"],
            "volume": snapshot["volume"],
            "prev_close": snapshot["prev_close"],
            "oi": 0,
            "bid": 0.0,
            "ask": 0.0,
        }

    def get_depth(self, symbol: str, exchange: str) -> dict:
        """The quote with empty book levels; ProjectX publishes no book over REST."""
        snapshot = self._daily_snapshot(symbol, exchange)
        empty_side = [{"price": 0.0, "quantity": 0} for _ in range(5)]
        return {
            "bids": [dict(level) for level in empty_side],
            "asks": [dict(level) for level in empty_side],
            "ltp": snapshot["ltp"],
            "ltq": 0,
            "volume": snapshot["volume"],
            "open": snapshot["open"],
            "high": snapshot["high"],
            "low": snapshot["low"],
            "prev_close": snapshot["prev_close"],
            "oi": 0,
            "totalbuyqty": 0,
            "totalsellqty": 0,
        }

    def get_intervals(self) -> list:
        return list(self.TIMEFRAME_MAP.keys())
