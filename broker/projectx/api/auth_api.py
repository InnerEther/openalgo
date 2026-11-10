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
            return None, (
                "ProjectX did not accept the username and API key. Check that the "
                "username is your ProjectX login name (not your email) and that the "
                "API key is current."
            )
        return None, str(exc)

    token = body.get("token")
    if not token:
        logger.error("ProjectX login succeeded without returning a token")
        return None, "ProjectX did not return a session. Try logging in again."

    logger.info("ProjectX authentication successful")
    return token, None
