"""Hyperliquid Testnet: ``broker.hyperliquid.api.order_api``, run on testnet."""

import broker.hyperliquid.api.order_api as _mainnet
from broker.hyperliquid.network import testnet_namespace

globals().update(testnet_namespace(_mainnet))
