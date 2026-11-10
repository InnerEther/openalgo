"""Orders for ProjectX are not connected yet.

This plugin serves charting: contracts, candles, quotes and the live feed.
Placing, changing and cancelling orders through ProjectX is refused with a
sentence the trader can act on, and the order, trade and position books read
as empty, so the pages that show them still load.
"""

from utils.logging import get_logger

logger = get_logger(__name__)

ORDERS_NOT_CONNECTED = (
    "Orders through ProjectX are not connected in OpenAlgo yet. "
    "Place this order on your ProjectX trading platform."
)


class _Refused:
    """Stands in for an HTTP response: a status other than 200 is a refusal."""

    status = 400


def _refusal():
    return {"status": "error", "message": ORDERS_NOT_CONNECTED}


def get_order_book(auth):
    return []


def get_trade_book(auth):
    return []


def get_positions(auth):
    return []


def get_holdings(auth):
    return []


def get_open_position(tradingsymbol, exchange, product, auth):
    return "0"


def place_order_api(data, auth):
    logger.info("ProjectX order refused: orders are not connected")
    return _Refused(), _refusal(), None


def place_smartorder_api(data, auth):
    logger.info("ProjectX smart order refused: orders are not connected")
    return _Refused(), _refusal(), None


def modify_order(data, auth):
    return _refusal(), 400


def cancel_order(orderid, auth):
    return _refusal(), 400


def cancel_all_orders_api(data, auth):
    return [], []


def close_all_positions(current_api_key, auth):
    return _refusal(), 400
