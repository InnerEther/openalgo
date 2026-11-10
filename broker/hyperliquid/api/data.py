"""Hyperliquid market data: quotes, depth and candles.

All of it comes from Hyperliquid's public info endpoint (see ``client.py``), so
the stored session token (the wallet address) is not needed here.

- Quotes read the asset contexts (one call answers every symbol, cached for two
  seconds) plus the order book for bid and ask. Hyperliquid's REST API has no
  last-trade price, so ``ltp`` is the mid price, and the mark price when the
  book is one-sided. The WebSocket feed carries real trades.
- Candles come from ``candleSnapshot``, which serves only the most recent 5000
  candles of each interval, and a request is paged forward from the start.
"""

import time
from datetime import date as _date
from datetime import datetime
from datetime import time as _time

import pandas as pd
import pytz

from broker.hyperliquid.api import client
from utils.logging import get_logger
from utils.thread_safe_cache import LockedTTLCache

logger = get_logger(__name__)

# OpenAlgo's date arguments are IST calendar dates on every broker (see the
# Delta Exchange plugin for why); the exchange itself runs 24x7 on UTC.
IST = pytz.timezone("Asia/Kolkata")

# The 24h high and low cost a candle request (weight 20 or more of the IP's
# 1200 a minute), so a quote box polling every second reuses them for a minute.
_DAY_RANGE_CACHE = LockedTTLCache(maxsize=256, ttl=60.0)

HISTORY_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume", "oi"]

#: Most candles Hyperliquid returns for one request.
_PAGE_LIMIT = 5000
_MAX_PAGES = 20


def _f(value, default=0.0):
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _coin(symbol: str, exchange: str) -> str:
    """The coin name Hyperliquid expects for an OpenAlgo symbol."""
    from database.token_db import get_br_symbol

    coin = get_br_symbol(symbol, exchange)
    if not coin:
        raise Exception(f"{symbol} is not in the Hyperliquid symbol list")
    return coin


def _levels(side, n=5):
    out = []
    for level in (side or [])[:n]:
        if isinstance(level, dict):
            out.append({"price": _f(level.get("px")), "quantity": _f(level.get("sz"))})
    while len(out) < n:
        out.append({"price": 0.0, "quantity": 0.0})
    return out


def _ltp(ctx: dict) -> float:
    return _f(ctx.get("midPx")) or _f(ctx.get("markPx"))


class BrokerData:
    """Hyperliquid market data for the common OpenAlgo data contract."""

    #: OpenAlgo interval -> Hyperliquid candle interval.
    TIMEFRAME_MAP = {
        "1m": "1m",
        "3m": "3m",
        "5m": "5m",
        "15m": "15m",
        "30m": "30m",
        "1h": "1h",
        "2h": "2h",
        "4h": "4h",
        "D": "1d",
        "W": "1w",
        "M": "1M",
    }

    _INTERVAL_MS = {
        "1m": 60_000,
        "3m": 180_000,
        "5m": 300_000,
        "15m": 900_000,
        "30m": 1_800_000,
        "1h": 3_600_000,
        "2h": 7_200_000,
        "4h": 14_400_000,
        "1d": 86_400_000,
        "1w": 604_800_000,
        "1M": 2_592_000_000,
    }

    def __init__(self, auth_token: str):
        self.auth_token = auth_token
        self.timeframe_map = self.TIMEFRAME_MAP

    # -- quotes ---------------------------------------------------------------

    def _quote_from(self, coin: str, ctx: dict, book: dict | None) -> dict:
        bids, asks = [], []
        if book and isinstance(book.get("levels"), list) and len(book["levels"]) == 2:
            bids, asks = book["levels"]
        prev = _f(ctx.get("prevDayPx"))
        return {
            "ltp": _ltp(ctx),
            "open": prev,
            "high": 0.0,
            "low": 0.0,
            "volume": _f(ctx.get("dayBaseVlm")),
            "prev_close": prev,
            "oi": _f(ctx.get("openInterest")),
            "bid": _f(bids[0].get("px")) if bids else 0.0,
            "ask": _f(asks[0].get("px")) if asks else 0.0,
        }

    def _day_range(self, coin: str) -> tuple[float, float]:
        """High and low of the last 24 hours, from hourly candles."""
        key = (client.base_url(), coin)
        cached = _DAY_RANGE_CACHE.get(key)
        if cached is not None:
            return cached
        now_ms = int(time.time() * 1000)
        rows = client.candles(coin, "1h", now_ms - 86_400_000, now_ms)
        highs = [_f(c.get("h")) for c in rows if isinstance(c, dict)]
        lows = [_f(c.get("l")) for c in rows if isinstance(c, dict)]
        result = (max(highs) if highs else 0.0, min(lows) if lows else 0.0)
        _DAY_RANGE_CACHE[key] = result
        return result

    def get_quotes(self, symbol: str, exchange: str) -> dict:
        try:
            coin = _coin(symbol, exchange)
            ctx = client.asset_contexts().get(coin)
            if ctx is None:
                raise Exception(f"Hyperliquid has no live market for {symbol}")
            quote = self._quote_from(coin, ctx, client.l2_book(coin))
            try:
                quote["high"], quote["low"] = self._day_range(coin)
            except client.HyperliquidError:
                logger.warning("Hyperliquid 24h range unavailable for %s", coin)
            return quote
        except Exception as e:
            logger.error(f"[Hyperliquid] get_quotes error for {symbol}: {e}")
            raise Exception(f"Error fetching quotes for {symbol}: {e}") from e

    def get_multiquotes(self, symbols: list) -> list:
        """Quotes for many symbols from one asset-context call.

        Bid and ask need a book request per coin, which would spend the IP
        budget on a large watchlist, so they are left at 0 here; the live feed
        carries them.
        """
        from database.token_db import get_br_symbol

        contexts = client.asset_contexts()
        results = []
        for item in symbols:
            symbol, exchange = item.get("symbol"), item.get("exchange")
            coin = get_br_symbol(symbol, exchange)
            ctx = contexts.get(coin) if coin else None
            if ctx is None:
                results.append(
                    {
                        "symbol": symbol,
                        "exchange": exchange,
                        "error": "Symbol not found on Hyperliquid",
                    }
                )
                continue
            results.append(
                {"symbol": symbol, "exchange": exchange, "data": self._quote_from(coin, ctx, None)}
            )
        return results

    # -- depth ----------------------------------------------------------------

    def get_depth(self, symbol: str, exchange: str) -> dict:
        try:
            coin = _coin(symbol, exchange)
            ctx = client.asset_contexts().get(coin) or {}
            book = client.l2_book(coin)
            levels = book.get("levels") if isinstance(book, dict) else None
            bids_raw, asks_raw = (
                levels if isinstance(levels, list) and len(levels) == 2 else ([], [])
            )
            bids, asks = _levels(bids_raw), _levels(asks_raw)
            prev = _f(ctx.get("prevDayPx"))
            return {
                "bids": bids,
                "asks": asks,
                "ltp": _ltp(ctx),
                "ltq": 0,
                "volume": _f(ctx.get("dayBaseVlm")),
                "open": prev,
                "high": 0.0,
                "low": 0.0,
                "prev_close": prev,
                "oi": _f(ctx.get("openInterest")),
                "totalbuyqty": sum(level["quantity"] for level in bids),
                "totalsellqty": sum(level["quantity"] for level in asks),
            }
        except Exception as e:
            logger.error(f"[Hyperliquid] get_depth error for {symbol}: {e}")
            raise Exception(f"Error fetching market depth for {symbol}: {e}") from e

    # -- history --------------------------------------------------------------

    @staticmethod
    def _to_ms(value, end_of_day: bool) -> int:
        """An IST calendar date as epoch milliseconds (start or end of that day)."""
        if isinstance(value, str):
            day = datetime.strptime(value, "%Y-%m-%d").date()
        elif isinstance(value, datetime):
            day = value.date()
        elif isinstance(value, _date):
            day = value
        else:
            raise ValueError(f"Unsupported date {value!r}")
        moment = datetime.combine(day, _time(23, 59, 59) if end_of_day else _time.min)
        return int(IST.localize(moment).timestamp() * 1000)

    def get_history(
        self, symbol: str, exchange: str, interval: str, start_date, end_date
    ) -> pd.DataFrame:
        try:
            if interval not in self.TIMEFRAME_MAP:
                raise Exception(
                    f"Unsupported interval '{interval}'. Supported: {', '.join(self.TIMEFRAME_MAP)}"
                )
            hl_interval = self.TIMEFRAME_MAP[interval]
            coin = _coin(symbol, exchange)

            now_ms = int(time.time() * 1000)
            start_ms = self._to_ms(start_date, end_of_day=False)
            end_ms = min(self._to_ms(end_date, end_of_day=True), now_ms)

            # Only the latest 5000 candles exist; asking from further back
            # returns the same window, so start no earlier than that.
            step = self._INTERVAL_MS[hl_interval]
            start_ms = max(start_ms, now_ms - step * _PAGE_LIMIT)

            rows: list[dict] = []
            cursor = start_ms
            for _ in range(_MAX_PAGES):
                if cursor > end_ms:
                    break
                page = client.candles(coin, hl_interval, cursor, end_ms)
                if not page:
                    break
                for c in page:
                    if not isinstance(c, dict):
                        continue
                    rows.append(
                        {
                            "timestamp": int(c.get("t", 0)) // 1000,
                            "open": _f(c.get("o")),
                            "high": _f(c.get("h")),
                            "low": _f(c.get("l")),
                            "close": _f(c.get("c")),
                            "volume": _f(c.get("v")),
                            "oi": 0,
                        }
                    )
                last_open = max(int(c.get("t", 0)) for c in page if isinstance(c, dict))
                next_cursor = last_open + step
                if next_cursor <= cursor:
                    break
                cursor = next_cursor

            if not rows:
                logger.warning(f"[Hyperliquid] No candles for {coin} @ {hl_interval}")
                return pd.DataFrame(columns=HISTORY_COLUMNS)

            df = pd.DataFrame(rows, columns=HISTORY_COLUMNS)
            df = (
                df.sort_values("timestamp")
                .drop_duplicates(subset=["timestamp"], keep="last")
                .reset_index(drop=True)
            )
            return df
        except Exception as e:
            logger.error(f"[Hyperliquid] get_history error for {symbol}: {e}")
            raise Exception(f"Error fetching historical data for {symbol}: {e}") from e

    def get_intervals(self) -> list:
        return list(self.TIMEFRAME_MAP.keys())
