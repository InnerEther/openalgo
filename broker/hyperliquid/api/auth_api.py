"""Signing in to Hyperliquid.

Hyperliquid has no login: market data is public, and an account is read by its
public wallet address. The address saved as the API key is the "token" OpenAlgo
stores, and the login checks it by reading that account once, so a mistyped
address is caught at login rather than as an empty positions page later.

Orders are signed with an agent (API wallet) key, saved as the API secret. It is
optional: without it the login still succeeds for charts and the account pages,
and an order is refused with a sentence saying how to add one. When it is
saved, the login checks that Hyperliquid has approved it for this wallet, so a
wrong key is caught here rather than at the first order.
"""

import os
import re

from broker.hyperliquid.api import signing
from broker.hyperliquid.api.client import HyperliquidError, clearinghouse_state, post_info
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

    agent_error = check_agent_key(address, os.getenv("BROKER_API_SECRET"))
    if agent_error:
        return None, agent_error

    logger.info("Hyperliquid login: wallet account read successfully")
    return address, None


def check_agent_key(address: str, secret: str | None) -> str | None:
    """None when no key is saved or the key may trade for ``address``, else why not."""
    if not (secret or "").strip():
        return None
    try:
        agent = signing.address_of(signing.private_key_from_hex(secret))
    except signing.InvalidKey:
        return (
            "The Hyperliquid agent key saved under Broker Configuration is not a valid "
            "private key. Copy the API wallet's private key from Hyperliquid and save "
            "it again."
        )
    if agent == address:
        logger.warning("Hyperliquid agent key is the wallet's own key; an API wallet is safer")
        return None
    try:
        role = post_info({"type": "userRole", "user": agent}) or {}
    except HyperliquidError:
        logger.exception("Hyperliquid agent check failed")
        return (
            "Hyperliquid could not be reached to check the agent key. Check this "
            "server's internet connection and try again in a minute."
        )
    master = str(((role.get("data") or {}).get("user")) or "").lower()
    if role.get("role") == "agent" and master == address:
        return None
    logger.error("Hyperliquid agent %s has role %s for %s", agent, role, master or "nobody")
    return (
        "The saved agent key is not approved for this Hyperliquid wallet. Approve the "
        "API wallet for this account on Hyperliquid's API page, or save the key of an "
        "API wallet that is approved for it."
    )
