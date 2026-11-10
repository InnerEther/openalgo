"""Hyperliquid account rows in OpenAlgo's common shapes.

``api/order_api.py`` already builds each row with the field names below, so the
``map_*`` steps only drop anything that is not a row.
"""

from utils.logging import get_logger

logger = get_logger(__name__)


def _rows(data):
    if isinstance(data, dict):
        data = [data] if "status" not in data else []
    return [row for row in (data or []) if isinstance(row, dict)]


# -- orders ----------------------------------------------------------------------


def map_order_data(order_data):
    return _rows(order_data)


def calculate_order_statistics(order_data):
    stats = {
        "total_buy_orders": 0,
        "total_sell_orders": 0,
        "total_completed_orders": 0,
        "total_open_orders": 0,
        "total_rejected_orders": 0,
    }
    for order in _rows(order_data):
        if order.get("transactionType") == "BUY":
            stats["total_buy_orders"] += 1
        elif order.get("transactionType") == "SELL":
            stats["total_sell_orders"] += 1
        status = order.get("orderStatus")
        if status == "open":
            stats["total_open_orders"] += 1
        elif status == "complete":
            stats["total_completed_orders"] += 1
        elif status == "rejected":
            stats["total_rejected_orders"] += 1
    return stats


def transform_order_data(orders):
    return [
        {
            "symbol": o.get("tradingSymbol", ""),
            "exchange": o.get("exchangeSegment", ""),
            "action": o.get("transactionType", ""),
            "quantity": o.get("quantity", 0),
            "price": o.get("price", 0.0),
            "trigger_price": o.get("triggerPrice", 0.0),
            "pricetype": o.get("orderType", ""),
            "product": o.get("productType", ""),
            "orderid": o.get("orderId", ""),
            "order_status": o.get("orderStatus", ""),
            "timestamp": o.get("updateTime", ""),
        }
        for o in _rows(orders)
    ]


# -- trades ----------------------------------------------------------------------


def map_trade_data(trade_data):
    return _rows(trade_data)


def transform_tradebook_data(tradebook_data):
    out = []
    for t in _rows(tradebook_data):
        qty, price = float(t.get("quantity", 0) or 0), float(t.get("price", 0) or 0)
        out.append(
            {
                "symbol": t.get("tradingSymbol", ""),
                "exchange": t.get("exchangeSegment", ""),
                "product": t.get("productType", ""),
                "action": t.get("transactionType", ""),
                "quantity": qty,
                "average_price": price,
                "trade_value": qty * price,
                "orderid": t.get("orderId", ""),
                "timestamp": t.get("updateTime", ""),
            }
        )
    return out


# -- positions -------------------------------------------------------------------


def map_position_data(position_data):
    return _rows(position_data)


def transform_positions_data(positions_data):
    return [
        {
            "symbol": p.get("tradingSymbol", ""),
            "exchange": p.get("exchangeSegment", ""),
            "product": p.get("productType", ""),
            "quantity": p.get("netQty", 0),
            "average_price": float(p.get("avgPrice", 0.0) or 0.0),
            "ltp": float(p.get("lastTradedPrice", 0.0) or 0.0),
            "pnl": float(p.get("pnlAbsolute", 0.0) or 0.0),
        }
        for p in _rows(positions_data)
    ]


# -- holdings --------------------------------------------------------------------


def map_portfolio_data(portfolio_data):
    return _rows(portfolio_data)


def calculate_portfolio_statistics(holdings_data):
    value = invested = 0.0
    for h in _rows(holdings_data):
        qty = float(h.get("totalQty", 0) or 0)
        value += qty * float(h.get("lastTradedPrice", 0) or 0)
        invested += qty * float(h.get("avgCostPrice", 0) or 0)
    pnl = value - invested
    return {
        "totalholdingvalue": round(value, 2),
        "totalinvvalue": round(invested, 2),
        "totalprofitandloss": round(pnl, 2),
        "totalpnlpercentage": round(pnl / invested * 100, 2) if invested else 0.0,
    }


def transform_holdings_data(holdings_data):
    return [
        {
            "symbol": h.get("tradingSymbol", ""),
            "exchange": h.get("exchangeSegment", ""),
            "quantity": h.get("totalQty", 0),
            "product": "CNC",
            "pnl": h.get("pnlAbsolute", 0.0),
            "pnlpercent": h.get("pnlPercent", 0.0),
        }
        for h in _rows(holdings_data)
    ]
