"""ProjectX account balance as OpenAlgo funds.

``POST /api/Account/search`` lists the trader's accounts with a balance each.
OpenAlgo shows one account: the one named by ``PROJECTX_ACCOUNT_ID`` in
``.env`` when set, otherwise the first active account that can trade.
"""

import os

from broker.projectx.api.baseurl import post
from utils.logging import get_logger

logger = get_logger(__name__)


def select_account(token: str) -> dict | None:
    """The ProjectX account OpenAlgo reports on, or None when there is none."""
    body = post("/api/Account/search", {"onlyActiveAccounts": True}, token=token)
    accounts = [a for a in (body.get("accounts") or []) if isinstance(a, dict)]
    wanted = (os.getenv("PROJECTX_ACCOUNT_ID") or "").strip()
    if wanted:
        for account in accounts:
            if str(account.get("id")) == wanted:
                return account
        logger.warning(
            "PROJECTX_ACCOUNT_ID does not name an active ProjectX account; using the first one"
        )
    tradable = [a for a in accounts if a.get("canTrade")]
    return (tradable or accounts or [None])[0]


def get_margin_data(auth_token):
    """Funds in OpenAlgo's shape, from the selected account's balance."""
    account = select_account(auth_token)
    try:
        balance = float((account or {}).get("balance") or 0)
    except (TypeError, ValueError):
        balance = 0.0
    return {
        "availablecash": f"{balance:.2f}",
        "collateral": "0.00",
        "m2mrealized": "0.00",
        "m2munrealized": "0.00",
        "utiliseddebits": "0.00",
    }
