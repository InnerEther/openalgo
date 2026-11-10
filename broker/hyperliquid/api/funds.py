"""The Hyperliquid wallet's perpetuals account in OpenAlgo's funds format.

Values are in USDC. ``availablecash`` is what Hyperliquid reports as
withdrawable, ``collateral`` the account value, ``utiliseddebits`` the margin in
use, and ``m2munrealized`` the open positions' unrealized P&L. Hyperliquid has
no session realized P&L figure, so ``m2mrealized`` is 0.
"""

from broker.hyperliquid.api import client
from utils.logging import get_logger

logger = get_logger(__name__)

DEFAULT_MARGIN_RESPONSE = {
    "availablecash": "0.00",
    "collateral": "0.00",
    "m2mrealized": "0.00",
    "m2munrealized": "0.00",
    "utiliseddebits": "0.00",
}


def _f(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def get_margin_data(auth_token):
    try:
        state = client.clearinghouse_state(auth_token)
    except client.HyperliquidError:
        logger.exception("Hyperliquid account unavailable")
        return dict(DEFAULT_MARGIN_RESPONSE)

    summary = state.get("marginSummary") or {}
    unrealized = sum(
        _f(((entry or {}).get("position") or {}).get("unrealizedPnl"))
        for entry in state.get("assetPositions", []) or []
    )
    return {
        "availablecash": f"{_f(state.get('withdrawable')):.2f}",
        "collateral": f"{_f(summary.get('accountValue')):.2f}",
        "m2mrealized": "0.00",
        "m2munrealized": f"{unrealized:.2f}",
        "utiliseddebits": f"{_f(summary.get('totalMarginUsed')):.2f}",
    }
