"""Hyperliquid Testnet: ``broker.hyperliquid.api.auth_api``, run on testnet."""

import broker.hyperliquid.api.auth_api as _mainnet
from broker.hyperliquid.network import testnet_namespace

globals().update(testnet_namespace(_mainnet))
