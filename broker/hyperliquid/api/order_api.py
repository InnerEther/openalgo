"""Hyperliquid account reads, and order calls that are not supported yet.

The order book, trade book, positions and holdings are read from Hyperliquid's
public info endpoint for the wallet address the trader logged in with (the
stored session token), so these pages show the real account.

Placing, changing or cancelling an order needs every action signed with the
wallet's key or an agent key. This plugin does not sign anything yet, so every
order call is refused with a sentence the trader can act on, before anything
reaches Hyperliquid.
"""

from datetime import datetime

import pytz

from broker.hyperliquid.api import client
from utils.logging import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")
EXCHANGE = "CRYPTO"

ORDERS_NOT_SUPPORTED = (
    "Hyperliquid is connected for charts and market data only. Placing, changing "
    "and cancelling orders on Hyperliquid is not available in OpenAlgo yet; use "
    "the Hyperliquid app for orders, or Analyze mode to test a strategy."
)


class _Refused:
    """The response object the order services read ``status`` from."""

    status = 400


def _oa_symbol(coin: str) -> str:
    from database.token_db import get_oa_symbol

    return get_oa_symbol(coin, EXCHANGE) or coin


def _ist(ms) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, IST).strftime("%d-%b-%Y %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return ""


def _f(value, default=0.0):
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _error(message: str) -> dict:
    return {"status": "error", "message": message}


# -- books ---------------------------------------------------------------------


_ORDER_TYPES = {
    "limit": "LIMIT",
    "market": "MARKET",
    "stop limit": "SL",
    "take profit limit": "SL",
    "stop market": "SL-M",
    "take profit market": "SL-M",
}


def get_order_book(auth):
    """Resting orders of the wallet, in the shape ``map_order_data`` expects."""
    try:
        orders = client.open_orders(auth)
    except client.HyperliquidError:
        logger.exception("Hyperliquid open orders unavailable")
        return _error("Hyperliquid could not be reached for your orders. Try again shortly.")
    out = []
    for o in orders:
        if not isinstance(o, dict):
            continue
        out.append(
            {
                "tradingSymbol": _oa_symbol(o.get("coin", "")),
                "exchangeSegment": EXCHANGE,
                "transactionType": "BUY" if o.get("side") == "B" else "SELL",
                "quantity": _f(o.get("origSz") or o.get("sz")),
                "price": _f(o.get("limitPx")),
                "triggerPrice": _f(o.get("triggerPx")),
                "orderType": _ORDER_TYPES.get(str(o.get("orderType", "")).lower(), "LIMIT"),
                "productType": "NRML",
                "orderId": str(o.get("oid", "")),
                "orderStatus": "open",
                "updateTime": _ist(o.get("timestamp")),
            }
        )
    return out


def get_trade_book(auth):
    """The wallet's recent fills (Hyperliquid returns up to 2000)."""
    try:
        fills = client.user_fills(auth)
    except client.HyperliquidError:
        logger.exception("Hyperliquid fills unavailable")
        return _error("Hyperliquid could not be reached for your trades. Try again shortly.")
    out = []
    for f in fills:
        if not isinstance(f, dict):
            continue
        out.append(
            {
                "tradingSymbol": _oa_symbol(f.get("coin", "")),
                "exchangeSegment": EXCHANGE,
                "productType": "NRML",
                "transactionType": "BUY" if f.get("side") == "B" else "SELL",
                "quantity": _f(f.get("sz")),
                "price": _f(f.get("px")),
                "orderId": str(f.get("oid", "")),
                "updateTime": _ist(f.get("time")),
            }
        )
    return out


def get_positions(auth, strict=False):
    """Open perpetual positions of the wallet."""
    try:
        state = client.clearinghouse_state(auth)
    except client.HyperliquidError:
        logger.exception("Hyperliquid positions unavailable")
        if strict:
            raise
        return _error("Hyperliquid could not be reached for your positions. Try again shortly.")
    try:
        contexts = client.asset_contexts()
    except client.HyperliquidError:
        contexts = {}
    out = []
    for entry in state.get("assetPositions", []) or []:
        pos = (entry or {}).get("position") or {}
        coin = pos.get("coin", "")
        if not coin:
            continue
        ctx = contexts.get(coin, {})
        out.append(
            {
                "tradingSymbol": _oa_symbol(coin),
                "exchangeSegment": EXCHANGE,
                "productType": "NRML",
                "netQty": _f(pos.get("szi")),
                "avgPrice": _f(pos.get("entryPx")),
                "lastTradedPrice": _f(ctx.get("midPx")) or _f(ctx.get("markPx")),
                "pnlAbsolute": _f(pos.get("unrealizedPnl")),
            }
        )
    return out


def get_holdings(auth):
    """Spot token balances of the wallet, other than USDC, valued in USDC."""
    try:
        state = client.spot_clearinghouse_state(auth)
    except client.HyperliquidError:
        logger.exception("Hyperliquid spot balances unavailable")
        return _error("Hyperliquid could not be reached for your balances. Try again shortly.")
    try:
        contexts = client.asset_contexts()
    except client.HyperliquidError:
        contexts = {}

    from database.token_db import get_br_symbol

    out = []
    for bal in state.get("balances", []) or []:
        coin = (bal or {}).get("coin", "")
        qty = _f(bal.get("total"))
        if not coin or coin == "USDC" or qty == 0:
            continue
        symbol = f"{coin.upper()}USDC"
        pair = get_br_symbol(symbol, EXCHANGE)
        price = 0.0
        if pair:
            ctx = contexts.get(pair, {})
            price = _f(ctx.get("midPx")) or _f(ctx.get("markPx"))
        cost = _f(bal.get("entryNtl"))
        value = qty * price
        pnl = value - cost if price and cost else 0.0
        out.append(
            {
                "tradingSymbol": symbol if pair else coin,
                "exchangeSegment": EXCHANGE,
                "totalQty": qty,
                "avgCostPrice": cost / qty if qty else 0.0,
                "lastTradedPrice": price,
                "pnlAbsolute": round(pnl, 2),
                "pnlPercent": round(pnl / cost * 100, 2) if cost and price else 0.0,
            }
        )
    return out


def get_open_position(tradingsymbol, exchange, product, auth):
    """Net quantity of one perpetual, as a string (used by smart orders)."""
    positions = get_positions(auth)
    if isinstance(positions, dict):
        return "0"
    for p in positions:
        if p["tradingSymbol"] == tradingsymbol and p["exchangeSegment"] == exchange:
            return str(p["netQty"])
    return "0"


# -- order actions: not supported yet -----------------------------------------


def place_order_api(data, auth):
    logger.info("Refused a Hyperliquid order: signing is not implemented")
    return _Refused(), _error(ORDERS_NOT_SUPPORTED), None


def place_smartorder_api(data, auth):
    return _Refused(), _error(ORDERS_NOT_SUPPORTED), None


def modify_order(data, auth):
    return _error(ORDERS_NOT_SUPPORTED), 400


def cancel_order(orderid, auth):
    return _error(ORDERS_NOT_SUPPORTED), 400


def cancel_all_orders_api(data, auth):
    return [], []


def close_all_positions(current_api_key, auth):
    return _error(ORDERS_NOT_SUPPORTED), 400
