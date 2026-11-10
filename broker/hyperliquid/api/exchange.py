"""Sending signed actions to Hyperliquid's ``/exchange`` endpoint.

The account traded is the wallet address the trader logged in with (the stored
session token). The key that signs is the agent (API wallet) key saved as the
API secret, read from the environment at each call, as every broker plugin
reads its keys. An agent key approved for an account trades for it without a
vault address.

A POST here is never retried: a timeout does not say whether Hyperliquid took
the order, and a retry could place it twice. Each action gets a nonce from the
millisecond clock, made strictly increasing in this process, because
Hyperliquid rejects a nonce a signer has already used.
"""

import os
import re
import threading
import time

import httpx

from broker.hyperliquid.api import signing
from broker.hyperliquid.api.client import BASE_URL
from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)

EXCHANGE_URL = f"{BASE_URL}/exchange"

NO_AGENT_KEY = (
    "No Hyperliquid agent key is saved, so OpenAlgo cannot place orders. Create an API "
    "wallet on Hyperliquid's API page, then save its private key as the Agent Key "
    "under Broker Configuration and log in again."
)
BAD_AGENT_KEY = (
    "The Hyperliquid agent key saved under Broker Configuration is not a valid private "
    "key. Copy the API wallet's private key from Hyperliquid again and save it."
)
UNKNOWN_AGENT = (
    "Hyperliquid does not recognise the saved agent key for this account. The API "
    "wallet may have expired or been removed: approve a new one on Hyperliquid's API "
    "page and save its private key under Broker Configuration."
)
NOT_SENT = "Hyperliquid could not be reached, so the order was not sent. Try again."
UNKNOWN_OUTCOME = (
    "Hyperliquid did not answer in time, so it is not known whether the order was "
    "placed. Check the order book before trying again."
)

_nonce_lock = threading.Lock()
_last_nonce = 0


class ExchangeError(Exception):
    """An action Hyperliquid refused, or one that could not be sent.

    The message is written for a trader.
    """


def next_nonce() -> int:
    global _last_nonce
    with _nonce_lock:
        nonce = max(int(time.time() * 1000), _last_nonce + 1)
        _last_nonce = nonce
        return nonce


def signing_key():
    """The agent key from the environment, or ExchangeError."""
    value = os.getenv("BROKER_API_SECRET", "").strip()
    if not value:
        raise ExchangeError(NO_AGENT_KEY)
    try:
        return signing.private_key_from_hex(value)
    except signing.InvalidKey as exc:
        raise ExchangeError(BAD_AGENT_KEY) from exc


_ASSET_SUFFIX = re.compile(r"\s*asset=\d+\s*$")


def trader_message(error: str) -> str:
    """Hyperliquid's own refusal, without the internal asset number."""
    text = _ASSET_SUFFIX.sub("", str(error or "")).strip()
    if "does not exist" in text and ("API Wallet" in text or "User" in text):
        return UNKNOWN_AGENT
    return text or "Hyperliquid refused the order."


def post_action(action: dict) -> dict:
    """Sign an action, send it once, and return Hyperliquid's ``response`` body.

    Raises:
        ExchangeError: When the key is missing, the action could not be sent,
            or Hyperliquid answered with an error.
    """
    key = signing_key()
    nonce = next_nonce()
    payload = {
        "action": action,
        "nonce": nonce,
        "signature": signing.sign_l1_action(key, action, nonce),
        "vaultAddress": None,
        "expiresAfter": None,
    }
    client = get_httpx_client()
    try:
        response = client.post(EXCHANGE_URL, json=payload, timeout=15.0)
    except Exception as exc:
        logger.exception("Hyperliquid %s request failed", action.get("type"))
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            raise ExchangeError(NOT_SENT) from exc
        raise ExchangeError(UNKNOWN_OUTCOME) from exc

    try:
        body = response.json()
    except ValueError:
        body = None
    if response.status_code != 200 or not isinstance(body, dict):
        logger.error(
            "Hyperliquid %s answered %s: %s",
            action.get("type"),
            response.status_code,
            response.text[:300],
        )
        text = body.get("response") if isinstance(body, dict) else response.text
        raise ExchangeError(trader_message(text if isinstance(text, str) else ""))
    if body.get("status") != "ok":
        logger.error("Hyperliquid refused %s: %s", action.get("type"), body)
        raise ExchangeError(trader_message(body.get("response")))
    return body.get("response") or {}


def statuses(response: dict) -> list:
    """The per-item statuses of an order, modify or cancel response."""
    data = (response or {}).get("data") or {}
    found = data.get("statuses")
    return found if isinstance(found, list) else []
