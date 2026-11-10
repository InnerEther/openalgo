"""Book mappings for ProjectX. Orders are not connected, so every book is empty."""


def _as_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return data["data"]
    return []


def map_order_data(order_data):
    return _as_list(order_data)


def calculate_order_statistics(order_data):
    return {
        "total_buy_orders": 0,
        "total_sell_orders": 0,
        "total_completed_orders": 0,
        "total_open_orders": 0,
        "total_rejected_orders": 0,
    }


def transform_order_data(orders):
    return _as_list(orders)


def map_trade_data(trade_data):
    return _as_list(trade_data)


def transform_tradebook_data(tradebook_data):
    return _as_list(tradebook_data)


def map_position_data(position_data):
    return _as_list(position_data)


def transform_positions_data(positions_data):
    return _as_list(positions_data)


def map_portfolio_data(portfolio_data):
    return _as_list(portfolio_data)


def calculate_portfolio_statistics(holdings_data):
    return {
        "totalholdingvalue": 0,
        "totalinvvalue": 0,
        "totalprofitandloss": 0,
        "totalpnlpercentage": 0,
    }


def transform_holdings_data(holdings_data):
    return _as_list(holdings_data)
