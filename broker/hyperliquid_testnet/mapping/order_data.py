"""Hyperliquid Testnet: ``broker.hyperliquid.mapping.order_data``, run on testnet."""

import broker.hyperliquid.mapping.order_data as _mainnet
from broker.hyperliquid.network import testnet_namespace

globals().update(testnet_namespace(_mainnet))
