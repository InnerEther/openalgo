"""Hyperliquid Testnet: ``broker.hyperliquid.api.funds``, run on testnet."""

import broker.hyperliquid.api.funds as _mainnet
from broker.hyperliquid.network import testnet_namespace

globals().update(testnet_namespace(_mainnet))
