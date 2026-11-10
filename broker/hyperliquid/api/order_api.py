"""Hyperliquid orders and account reads.

The order book, trade book, positions and holdings are read from Hyperliquid's
public info endpoint for the wallet address the trader logged in with (the
stored session token).

Orders are signed with the agent (API wallet) key saved as the API secret and
sent to ``/exchange`` (see ``exchange.py`` and ``signing.py``). OpenAlgo's
order fields map onto Hyperliquid's like this:

- MARKET: an immediate-or-cancel limit order 5% through the mid price, which
  is how Hyperliquid's own SDK sends a market order. What cannot fill at once
  is cancelled, never left resting.
- LIMIT: a good-till-cancelled limit order.
- SL and SL-M: trigger orders on the mark price, a limit or a market order
  once triggered. Whether the trigger sits above or below the current price
  decides Hyperliquid's stop-loss or take-profit trigger, so the order fires
  when the price reaches the trigger from where it is now.

Prices are rounded to Hyperliquid's rule (5 significant figures, and at most
6 decimals on perpetuals or 8 on spot, less the size decimals), and sizes down
to the market's size step. The product (MIS, NRML, CNC) has no meaning on
Hyperliquid and is ignored.
"""

from datetime import datetime
from decimal import ROUND_DOWN, Decimal, InvalidOperation

import pytz

from broker.hyperliquid.api import client
from broker.hyperliquid.api.exchange import ExchangeError, post_action, statuses
from utils.logging import get_logger
from utils.position_read import read_position_book, refuse_smart_order_on_read_failure
from utils.smart_order_guard import PositionBookCache, SymbolLocks

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")
EXCHANGE = "CRYPTO"

#: How far through the mid price a market order may fill, as Hyperliquid's SDK.
MARKET_SLIPPAGE = Decimal("0.05")


class _Response:
    """The response object the order services read ``status`` from."""

    def __init__(self, status: int):
        self.status = status


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


# -- order building -------------------------------------------------------------


class OrderRefused(Exception):
    """An order refused before anything was sent; the message is for a trader."""


def _decimal(value, what: str) -> Decimal:
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError):
        raise OrderRefused(f"The {what} '{value}' is not a number.") from None
    if not number.is_finite():
        raise OrderRefused(f"The {what} '{value}' is not a number.")
    return number


def wire(number: Decimal) -> str:
    """A number as Hyperliquid's wire format writes it: no exponent, no trailing zeros."""
    text = f"{number.normalize():f}"
    return "0" if text in ("-0", "") else text


def round_size(quantity, sz_decimals: int) -> Decimal:
    size = _decimal(quantity, "quantity")
    if size <= 0:
        raise OrderRefused("The quantity must be more than 0.")
    step = Decimal(1).scaleb(-sz_decimals)
    rounded = size.quantize(step, rounding=ROUND_DOWN)
    if rounded <= 0:
        raise OrderRefused(
            f"The quantity {quantity} is below this market's smallest size, {wire(step)}."
        )
    return rounded


def round_price(price, sz_decimals: int, is_spot: bool) -> Decimal:
    """Round a price to 5 significant figures and the market's decimal limit."""
    px = _decimal(price, "price")
    if px <= 0:
        raise OrderRefused("The price must be more than 0.")
    if px >= 100000:
        # Integer prices are always valid, and finer than 5 significant figures here.
        return px.quantize(Decimal(1))
    exponent = px.adjusted() - 4
    max_decimals = max((8 if is_spot else 6) - sz_decimals, 0)
    step = Decimal(1).scaleb(max(exponent, -max_decimals))
    return px.quantize(step)


def _market(symbol: str, exchange: str) -> tuple[str, dict]:
    from database.token_db import get_br_symbol

    coin = get_br_symbol(symbol, exchange)
    if not coin:
        raise OrderRefused(f"{symbol} is not in the Hyperliquid symbol list.")
    info = client.asset_info(coin)
    if not info:
        raise OrderRefused(f"{symbol} is not trading on Hyperliquid.")
    return coin, info


def _mid(coin: str) -> Decimal:
    ctx = client.asset_contexts().get(coin) or {}
    mid = _f(ctx.get("midPx")) or _f(ctx.get("markPx"))
    if not mid:
        raise OrderRefused("Hyperliquid has no current price for this market.")
    return Decimal(str(mid))


def build_order(data: dict, reduce_only: bool = False, mid: Decimal | None = None) -> dict:
    """An OpenAlgo order as one Hyperliquid order wire.

    Args:
        data: The OpenAlgo order (symbol, exchange, action, quantity,
            pricetype, price, trigger_price).
        reduce_only: Only reduce an open position (closing orders).
        mid: The current mid price, when already known.
    """
    coin, info = _market(data.get("symbol", ""), data.get("exchange", ""))
    sz_decimals, is_spot = info["sz_decimals"], info["is_spot"]
    action = str(data.get("action", "")).upper()
    if action not in ("BUY", "SELL"):
        raise OrderRefused("The action must be BUY or SELL.")
    is_buy = action == "BUY"
    size = round_size(data.get("quantity"), sz_decimals)
    pricetype = str(data.get("pricetype") or "MARKET").upper()

    def through(reference: Decimal) -> Decimal:
        factor = 1 + MARKET_SLIPPAGE if is_buy else 1 - MARKET_SLIPPAGE
        return round_price(reference * factor, sz_decimals, is_spot)

    if pricetype == "MARKET":
        price = through(mid if mid is not None else _mid(coin))
        order_type = {"limit": {"tif": "Ioc"}}
    elif pricetype == "LIMIT":
        price = round_price(data.get("price"), sz_decimals, is_spot)
        order_type = {"limit": {"tif": "Gtc"}}
    elif pricetype in ("SL", "SL-M"):
        trigger = round_price(data.get("trigger_price"), sz_decimals, is_spot)
        current = mid if mid is not None else _mid(coin)
        # A buy stop above the market and a sell stop below it are stop-losses;
        # the other two wait for the price to come back to them (take-profit).
        above = trigger >= current
        tpsl = "sl" if above == is_buy else "tp"
        is_market = pricetype == "SL-M" or _f(data.get("price")) <= 0
        price = (
            through(trigger) if is_market else round_price(data.get("price"), sz_decimals, is_spot)
        )
        order_type = {"trigger": {"isMarket": is_market, "triggerPx": wire(trigger), "tpsl": tpsl}}
    else:
        raise OrderRefused(f"The price type {pricetype} is not supported on Hyperliquid.")

    return {
        "a": info["asset"],
        "b": is_buy,
        "p": wire(price),
        "s": wire(size),
        "r": reduce_only,
        "t": order_type,
    }


def _status_oid(status) -> str | None:
    if isinstance(status, dict):
        for value in status.values():
            if isinstance(value, dict) and value.get("oid") is not None:
                return str(value["oid"])
    return None


def _status_error(status) -> str | None:
    if isinstance(status, dict) and status.get("error"):
        from broker.hyperliquid.api.exchange import trader_message

        return trader_message(status["error"])
    return None


# -- order actions ----------------------------------------------------------------


def place_order_api(data, auth):
    try:
        order = build_order(data)
        response = post_action({"type": "order", "orders": [order], "grouping": "na"})
    except (OrderRefused, ExchangeError) as exc:
        return _Response(400), _error(str(exc)), None
    except client.HyperliquidError:
        logger.exception("Hyperliquid market data needed for the order was unavailable")
        return (
            _Response(400),
            _error("Hyperliquid could not be reached, so the order was not sent."),
            None,
        )

    found = statuses(response)
    first = found[0] if found else None
    error = _status_error(first)
    if error:
        return _Response(400), _error(error), None
    oid = _status_oid(first)
    if not oid:
        logger.error("Hyperliquid order answer without an order id: %s", response)
        return (
            _Response(500),
            _error("Hyperliquid did not confirm the order. Check the order book."),
            None,
        )
    logger.info("Hyperliquid order placed: %s", oid)
    return _Response(200), {"status": "success", "orderid": oid}, oid


def _open_order(oid: str, auth: str) -> dict | None:
    for order in client.open_orders(auth):
        if isinstance(order, dict) and str(order.get("oid")) == str(oid):
            return order
    return None


def modify_order(data, auth):
    orderid = str(data.get("orderid", ""))
    try:
        oid = int(orderid)
    except ValueError:
        return _error(f"'{orderid}' is not a Hyperliquid order id."), 400
    try:
        order = build_order(data)
        response = post_action({"type": "batchModify", "modifies": [{"oid": oid, "order": order}]})
    except (OrderRefused, ExchangeError) as exc:
        return _error(str(exc)), 400
    except client.HyperliquidError:
        logger.exception("Hyperliquid market data needed for the modify was unavailable")
        return _error("Hyperliquid could not be reached, so the order was not changed."), 400
    found = statuses(response)
    first = found[0] if found else None
    error = _status_error(first)
    if error:
        return _error(error), 400
    return {"status": "success", "orderid": _status_oid(first) or orderid}, 200


def _cancel(orders: list[dict]) -> list:
    cancels = []
    for order in orders:
        info = client.asset_info(order.get("coin", ""))
        if not info:
            raise OrderRefused(f"{order.get('coin')} is not trading on Hyperliquid.")
        cancels.append({"a": info["asset"], "o": int(order["oid"])})
    return statuses(post_action({"type": "cancel", "cancels": cancels}))


def cancel_order(orderid, auth):
    try:
        order = _open_order(str(orderid), auth)
        if order is None:
            return _error(f"Order {orderid} is not open on Hyperliquid."), 404
        found = _cancel([order])
    except (OrderRefused, ExchangeError) as exc:
        return _error(str(exc)), 400
    except client.HyperliquidError:
        logger.exception("Hyperliquid cancel lookup failed")
        return _error("Hyperliquid could not be reached, so the order was not cancelled."), 400
    error = _status_error(found[0] if found else None)
    if error:
        return _error(error), 400
    return {"status": "success", "orderid": str(orderid)}, 200


def cancel_all_orders_api(data, auth):
    try:
        orders = [o for o in client.open_orders(auth) if isinstance(o, dict)]
    except client.HyperliquidError:
        logger.exception("Hyperliquid open orders unavailable for cancel all")
        return [], []
    if not orders:
        return [], []
    ids = [str(o.get("oid")) for o in orders]
    try:
        found = _cancel(orders)
    except (OrderRefused, ExchangeError):
        logger.exception("Hyperliquid cancel all failed")
        return [], ids
    cancelled, failed = [], []
    for oid, status in zip(ids, found + [None] * (len(ids) - len(found)), strict=False):
        (cancelled if status == "success" else failed).append(oid)
    return cancelled, failed


def close_all_positions(current_api_key, auth):
    """Close every perpetual position with reduce-only market orders, in one action."""
    positions = get_positions(auth)
    if isinstance(positions, dict):
        return positions, 500
    open_positions = [p for p in positions if p.get("netQty")]
    if not open_positions:
        return {"message": "No Open Positions Found"}, 200
    try:
        contexts = client.asset_contexts()
        orders, symbols = [], []
        for p in open_positions:
            coin, _info = _market(p["tradingSymbol"], EXCHANGE)
            ctx = contexts.get(coin) or {}
            mid = _f(ctx.get("midPx")) or _f(ctx.get("markPx"))
            orders.append(
                build_order(
                    {
                        "symbol": p["tradingSymbol"],
                        "exchange": EXCHANGE,
                        "action": "SELL" if p["netQty"] > 0 else "BUY",
                        "quantity": abs(p["netQty"]),
                        "pricetype": "MARKET",
                    },
                    reduce_only=True,
                    mid=Decimal(str(mid)) if mid else None,
                )
            )
            symbols.append(p["tradingSymbol"])
        found = statuses(post_action({"type": "order", "orders": orders, "grouping": "na"}))
    except (OrderRefused, ExchangeError) as exc:
        return _error(str(exc)), 400
    except client.HyperliquidError:
        logger.exception("Hyperliquid close all failed")
        return _error("Hyperliquid could not be reached, so no position was closed."), 400

    failed = [
        f"{symbol}: {_status_error(status)}"
        for symbol, status in zip(
            symbols, found + [None] * (len(symbols) - len(found)), strict=False
        )
        if _status_error(status) or not _status_oid(status)
    ]
    if failed:
        return _error("Some positions were not closed. " + "; ".join(failed)), 400
    return {"status": "success", "message": "All Open Positions SquaredOff"}, 200


# -- smart orders -----------------------------------------------------------------

_symbol_locks = SymbolLocks(name="hyperliquid smart orders")
_position_cache = PositionBookCache()


def _read_net(symbol: str, exchange: str, auth: str) -> float:
    """The net position of one symbol for a smart order; raises when unreadable."""
    coin, info = _market(symbol, exchange)
    if info["is_spot"]:
        state = read_position_book(
            "hyperliquid",
            lambda: client.spot_clearinghouse_state(auth),
            lambda r: isinstance(r, dict) and isinstance(r.get("balances"), list),
        )
        for bal in state["balances"]:
            if isinstance(bal, dict) and bal.get("coin") == info.get("base"):
                return _f(bal.get("total"))
        return 0.0
    positions = _position_cache.get(
        auth,
        lambda: read_position_book(
            "hyperliquid", lambda: get_positions(auth, strict=True), lambda r: isinstance(r, list)
        ),
    )
    for p in positions:
        if p.get("tradingSymbol") == symbol:
            return float(p.get("netQty") or 0)
    return 0.0


def get_open_position(tradingsymbol, exchange, product, auth):
    """Net quantity of one symbol as a string: the position, or the spot balance."""
    try:
        return str(_read_net(tradingsymbol, exchange, auth))
    except OrderRefused:
        return "0"


@refuse_smart_order_on_read_failure
def place_smartorder_api(data, auth):
    """Take the position to ``position_size``, placing only the difference."""
    symbol, exchange = data.get("symbol"), data.get("exchange")
    with _symbol_locks.hold(symbol, exchange, data.get("product")) as acquired:
        if not acquired:
            return SymbolLocks.busy(symbol)
        try:
            target = float(_decimal(data.get("position_size", "0"), "position size"))
            current = _read_net(symbol, exchange, auth)
        except OrderRefused as exc:
            return None, _error(str(exc)), None

        if target == current:
            message = (
                "No OpenPosition Found. Not placing Exit order."
                if float(_f(data.get("quantity"))) == 0
                else "No action needed. Position size matches current position"
            )
            return None, {"status": "success", "message": message}, None

        order = dict(data)
        order["action"] = "BUY" if target > current else "SELL"
        order["quantity"] = str(abs(Decimal(str(target)) - Decimal(str(current))))
        result = place_order_api(order, auth)
        _position_cache.invalidate(auth)
        return result
