"""Which Hyperliquid WebSocket subscriptions each OpenAlgo mode needs."""

#: OpenAlgo mode -> Hyperliquid subscription types.
#:
#: trades          last traded price and size
#: activeAssetCtx  24h volume, open interest, price 24h ago (and mid/mark until
#:                 the first trade arrives)
#: candle 1d       the current UTC day's open, high and low
#: bbo             best bid and ask with sizes
#: l2Book          the book, of which OpenAlgo shows the top 5 levels
MODE_FEEDS = {
    1: ("trades", "activeAssetCtx"),
    2: ("trades", "activeAssetCtx", "candle", "bbo"),
    3: ("trades", "activeAssetCtx", "candle", "bbo", "l2Book"),
}

MODE_NAMES = {1: "LTP", 2: "QUOTE", 3: "DEPTH"}

SUPPORTED_MODES = (1, 2, 3)
DEPTH_LEVELS = (1, 5)


def feeds_for(mode: int) -> tuple[str, ...]:
    return MODE_FEEDS.get(mode, MODE_FEEDS[1])


def subscription(feed: str, coin: str) -> dict:
    """The subscription body for one feed of one coin."""
    if feed == "candle":
        return {"type": "candle", "coin": coin, "interval": "1d"}
    return {"type": feed, "coin": coin}
