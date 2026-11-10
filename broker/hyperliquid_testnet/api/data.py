"""Hyperliquid Testnet: ``broker.hyperliquid.api.data``, run on testnet."""

import broker.hyperliquid.api.data as _mainnet
from broker.hyperliquid.network import testnet_class, testnet_namespace

globals().update(testnet_namespace(_mainnet))
BrokerData = testnet_class(_mainnet.BrokerData)
