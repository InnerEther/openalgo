"""Hyperliquid Testnet: ``broker.hyperliquid.database.master_contract_db``, run on testnet."""

import broker.hyperliquid.database.master_contract_db as _mainnet
from broker.hyperliquid.network import testnet_namespace

globals().update(testnet_namespace(_mainnet))
