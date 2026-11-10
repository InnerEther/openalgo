"""Hyperliquid Testnet: the Hyperliquid feed adapter, run on testnet.

Every method runs on testnet, the constructor included, so the WebSocket client
it builds connects to the testnet address and keeps it across reconnects.
"""

from broker.hyperliquid.network import testnet_class
from broker.hyperliquid.streaming.hyperliquid_adapter import HyperliquidWebSocketAdapter

# websocket_proxy.broker_factory loads "<Broker>WebSocketAdapter" with the
# broker name capitalized, hence the underscore.
Hyperliquid_testnetWebSocketAdapter = testnet_class(  # noqa: N816
    HyperliquidWebSocketAdapter, "Hyperliquid_testnetWebSocketAdapter"
)
