"""Calls to Hyperliquid's public info endpoint.

Everything Hyperliquid serves for market data and for reading an account comes
from one address, ``POST https://api.hyperliquid.xyz/info``, with a JSON body
whose ``type`` names the request (``metaAndAssetCtxs``, ``l2Book``,
``candleSnapshot``, ``clearinghouseState`` and so on). None of it needs a key:
reading an account only needs its public wallet address.

Hyperliquid limits these requests by IP with a weight budget (1200 per minute;
``l2Book`` and ``allMids`` weigh 2, most others 20). The asset contexts, which
answer every quote, are cached for a moment so a burst of quote requests costs
one call instead of one per symbol.
"""

import time

import httpx

from utils.httpx_client import get_httpx_client
from utils.logging import get_logger
from utils.thread_safe_cache import LockedTTLCache

logger = get_logger(__name__)

BASE_URL = "https://api.hyperliquid.xyz"
INFO_URL = f"{BASE_URL}/info"
WS_URL = "wss://api.hyperliquid.xyz/ws"

#: Spot assets are numbered from 10000 in Hyperliquid's asset ids.
SPOT_ASSET_OFFSET = 10000

_TRANSIENT_ERRORS = (
    httpx.ReadError,
    httpx.WriteError,
    httpx.ConnectError,
    httpx.PoolTimeout,
    httpx.ReadTimeout,
    httpx.TimeoutException,
)
_MAX_ATTEMPTS = 3

# Asset contexts change every block, but a quote two seconds old is fine for a
# quote box, and it saves the IP budget for candles.
_CONTEXT_CACHE = LockedTTLCache(maxsize=4, ttl=2.0)

# Asset ids and size decimals change only when Hyperliquid lists a market.
_ASSET_CACHE = LockedTTLCache(maxsize=2, ttl=600.0)


class HyperliquidError(Exception):
    """A request Hyperliquid refused or could not answer."""


def post_info(payload: dict, timeout: float = 15.0):
    """POST one request to the info endpoint and return the decoded JSON.

    Retries a dropped connection and a rate-limit answer (HTTP 429) a couple of
    times with a short backoff before giving up.

    Raises:
        HyperliquidError: When Hyperliquid answers with an error or not at all.
    """
    client = get_httpx_client()
    delay = 0.5
    last_error = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = client.post(INFO_URL, json=payload, timeout=timeout)
        except _TRANSIENT_ERRORS as exc:
            last_error = exc
            logger.warning(
                "Hyperliquid %s request failed (attempt %s): %s",
                payload.get("type"),
                attempt,
                exc,
            )
        else:
            if response.status_code == 200:
                return response.json()
            if response.status_code == 429:
                last_error = HyperliquidError("rate limited")
                logger.warning("Hyperliquid rate limit hit on %s", payload.get("type"))
            else:
                raise HyperliquidError(
                    f"Hyperliquid answered {response.status_code} to {payload.get('type')}: "
                    f"{response.text[:200]}"
                )
        if attempt < _MAX_ATTEMPTS:
            time.sleep(delay)
            delay *= 2
    raise HyperliquidError(f"Hyperliquid did not answer {payload.get('type')}: {last_error}")


def perp_meta_and_contexts() -> tuple[list, list]:
    """The main perpetuals universe and its live asset contexts, index-aligned."""
    data = post_info({"type": "metaAndAssetCtxs"})
    meta, contexts = data[0], data[1]
    return meta.get("universe", []), contexts


def spot_meta_and_contexts() -> tuple[dict, list]:
    """The spot universe and tokens, and the live spot asset contexts."""
    data = post_info({"type": "spotMetaAndAssetCtxs"})
    return data[0], data[1]


def asset_contexts() -> dict[str, dict]:
    """Every listed coin's live context, keyed by the coin name Hyperliquid uses.

    Perpetuals are keyed by their name (``BTC``), spot pairs by their pair name
    (``PURR/USDC`` or ``@107``). Each context carries ``markPx``, ``midPx``,
    ``prevDayPx``, ``dayBaseVlm`` and ``dayNtlVlm``; perpetuals also carry
    ``openInterest``, ``funding`` and ``oraclePx``.
    """
    cached = _CONTEXT_CACHE.get("all")
    if cached is not None:
        return cached

    out: dict[str, dict] = {}
    universe, contexts = perp_meta_and_contexts()
    for asset, ctx in zip(universe, contexts, strict=False):
        if isinstance(ctx, dict):
            out[asset.get("name", "")] = ctx
    try:
        _spot_meta, spot_contexts = spot_meta_and_contexts()
        for ctx in spot_contexts:
            if isinstance(ctx, dict) and ctx.get("coin"):
                out[ctx["coin"]] = ctx
    except HyperliquidError:
        # Perpetual quotes are still good without the spot half.
        logger.exception("Could not load Hyperliquid spot contexts")

    _CONTEXT_CACHE["all"] = out
    return out


def _load_assets() -> dict[str, dict]:
    out: dict[str, dict] = {}
    universe, _contexts = perp_meta_and_contexts()
    for index, asset in enumerate(universe):
        if isinstance(asset, dict) and asset.get("name"):
            out[asset["name"]] = {
                "asset": index,
                "sz_decimals": int(asset.get("szDecimals") or 0),
                "is_spot": False,
            }
    spot_meta, _spot_contexts = spot_meta_and_contexts()
    tokens = {t.get("index"): t for t in spot_meta.get("tokens", []) if isinstance(t, dict)}
    for pair in spot_meta.get("universe", []):
        pair_tokens = (pair or {}).get("tokens") or []
        base = tokens.get(pair_tokens[0]) if pair_tokens else None
        if base and pair.get("name"):
            out[pair["name"]] = {
                "asset": SPOT_ASSET_OFFSET + int(pair.get("index", 0)),
                "sz_decimals": int(base.get("szDecimals") or 0),
                "is_spot": True,
                "base": base.get("name", ""),
            }
    return out


def asset_info(coin: str) -> dict | None:
    """Order details of one coin: ``asset`` id, ``sz_decimals`` and ``is_spot``.

    Reloaded once when a coin is missing, so a market listed since the last
    load is found.
    """
    assets = _ASSET_CACHE.get("all")
    if assets is None or coin not in assets:
        assets = _load_assets()
        _ASSET_CACHE["all"] = assets
    return assets.get(coin)


def l2_book(coin: str) -> dict:
    """The order book of one coin: ``levels`` is ``[bids, asks]``, best first."""
    return post_info({"type": "l2Book", "coin": coin}) or {}


def candles(coin: str, interval: str, start_ms: int, end_ms: int) -> list:
    """Candles for one coin, oldest first, between two millisecond times."""
    data = post_info(
        {
            "type": "candleSnapshot",
            "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": end_ms},
        },
        timeout=30.0,
    )
    return data if isinstance(data, list) else []


def clearinghouse_state(user: str) -> dict:
    """A wallet's perpetuals account: margin summary and open positions."""
    return post_info({"type": "clearinghouseState", "user": user}) or {}


def spot_clearinghouse_state(user: str) -> dict:
    """A wallet's spot balances."""
    return post_info({"type": "spotClearinghouseState", "user": user}) or {}


def open_orders(user: str) -> list:
    """A wallet's resting orders, with trigger and order-type detail."""
    data = post_info({"type": "frontendOpenOrders", "user": user})
    return data if isinstance(data, list) else []


def user_fills(user: str) -> list:
    """A wallet's most recent fills (up to 2000), newest first."""
    data = post_info({"type": "userFills", "user": user})
    return data if isinstance(data, list) else []
