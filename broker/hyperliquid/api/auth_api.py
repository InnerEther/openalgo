"""Signing in to Hyperliquid.

Hyperliquid has no login: market data is public, and an account is read by its
public wallet address. The address saved as the API key is the "token" OpenAlgo
stores, and the login checks it by reading that account once, so a mistyped
address is caught at login rather than as an empty positions page later.

Placing orders needs a wallet or agent private key to sign each action. That is
not part of this plugin yet, so no secret is asked for or read.
"""

import os
import re

from broker.hyperliquid.api.client import HyperliquidError, clearinghouse_state
from utils.logging import get_logger

logger = get_logger(__name__)

_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")


def normalize_address(value: str | None) -> str | None:
    """A wallet address in lower case, or None when it is not one."""
    value = (value or "").strip()
    return value.lower() if _ADDRESS.fullmatch(value) else None


def authenticate_broker(code=None):
    """Check the saved wallet address and return it as the session token.

    Args:
        code: Unused; kept for the shared callback signature.

    Returns:
        (address, None) on success, (None, message) on failure.
    """
    address = normalize_address(os.getenv("BROKER_API_KEY"))
    if not address:
        return None, (
            "The Hyperliquid wallet address saved under Broker Configuration is not a "
            "valid address. It starts with 0x and has 40 more letters and digits."
        )

    try:
        state = clearinghouse_state(address)
    except HyperliquidError:
        logger.exception("Hyperliquid login check failed")
        return None, (
            "Hyperliquid could not be reached. Check this server's internet connection "
            "and try again in a minute."
        )

    if not isinstance(state, dict) or "marginSummary" not in state:
        logger.error("Unexpected Hyperliquid account answer: %s", str(state)[:200])
        return None, "Hyperliquid did not return an account for that wallet address."

    logger.info("Hyperliquid login: wallet account read successfully")
    return address, None
