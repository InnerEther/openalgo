"""ProjectX login: username plus API key in, session JWT out.

The trader saves two values in Broker Configuration (or ``.env``):
    BROKER_API_KEY     their ProjectX username (the platform login, not the email)
    BROKER_API_SECRET  the API key generated on the firm's ProjectX dashboard

Both are read when the login runs, never at import, because the process may
hold another broker's keys until this login commits.
"""

import os

from broker.projectx.api.baseurl import ProjectXError, post
from utils.logging import get_logger

logger = get_logger(__name__)

#: ProjectX's loginKey error codes, as its documentation names them, and the
#: sentence a trader reads for each. A refused login still answers HTTP 200.
LOGIN_REFUSALS = {
    3: (
        "ProjectX did not accept this username and API key together. Use your "
        "ProjectX platform login name (not your email or account number), generate "
        "a fresh API key in the platform's API settings, save both under Broker "
        "Configuration and log in again."
    ),
    7: (
        "ProjectX is waiting for you to accept its agreements. Log in to your "
        "ProjectX trading platform, complete the agreements, then log in here again."
    ),
    9: (
        "Your ProjectX user has no active API subscription, so ProjectX will not "
        "issue a session. Add the API subscription with your firm, then log in again."
    ),
    10: (
        "Your firm has turned off API key logins on ProjectX. Contact your firm to "
        "enable API access."
    ),
}
LOGIN_REFUSED = (
    "ProjectX refused the login. Check your username and API key under Broker "
    "Configuration and log in again."
)


def authenticate_broker(code=None):
    """Exchange the saved username and API key for a ProjectX session token.

    Args:
        code: Unused; ProjectX has no redirect. Kept for the shared callback.

    Returns:
        tuple: ``(token, None)`` on success, ``(None, message)`` on failure.
    """
    username = (os.getenv("BROKER_API_KEY") or "").strip()
    api_key = (os.getenv("BROKER_API_SECRET") or "").strip()

    if not username or not api_key:
        return None, (
            "Your ProjectX username and API key are not saved. "
            "Add them under Broker Configuration and log in again."
        )

    try:
        body = post("/api/Auth/loginKey", {"userName": username, "apiKey": api_key})
    except ProjectXError as exc:
        if exc.status_code == 400:
            return None, LOGIN_REFUSALS.get(exc.error_code, LOGIN_REFUSED)
        return None, str(exc)

    token = body.get("token")
    if not token:
        logger.error("ProjectX login succeeded without returning a token")
        return None, "ProjectX did not return a session. Try logging in again."

    logger.info("ProjectX authentication successful")
    return token, None
