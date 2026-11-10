# Connecting Hyperliquid

Hyperliquid is a crypto exchange for perpetual futures and spot pairs. OpenAlgo
connects to it as the `hyperliquid` broker for charts, quotes, depth and the
live feed, your balance and positions, and orders.

## What you need

1. Your **wallet address**, starting with `0x`. It is public: on its own it
   only lets OpenAlgo read your balance, positions and orders.
2. To place orders, an **agent key**: the private key of an API wallet you
   approve on Hyperliquid's API page (More, then API). An API wallet can trade
   for your account but cannot withdraw. Without it, charts and the account
   pages work and orders are refused with a message.

## Connect

1. Open **Select Broker**, find **Hyperliquid** and choose **Configure**.
2. Enter the wallet address and, for orders, the agent key, then save.
3. Choose **Login with Hyperliquid**. OpenAlgo reads the account once, checks
   that the agent key is approved for that wallet, and downloads the symbol list.

Symbols are on the `CRYPTO` exchange: perpetuals as `BTCUSDFUT`, spot pairs as
`HYPEUSDC`. Hyperliquid serves only the latest 5000 candles of each interval.

## Hyperliquid Testnet

Hyperliquid runs a separate test network with test funds. OpenAlgo lists it as
its own broker, **Hyperliquid Testnet**, so the broker badge always says which
network you are on and each keeps its own saved keys. Accounts, API wallets,
markets and prices on testnet are separate from mainnet, and an order signed for
one network is refused by the other.

1. Open [app.hyperliquid-testnet.xyz](https://app.hyperliquid-testnet.xyz) and
   connect your wallet, then claim 1,000 test USDC from the faucet at
   [app.hyperliquid-testnet.xyz/drip](https://app.hyperliquid-testnet.xyz/drip).
   The faucet only pays an address that has made a deposit on Hyperliquid
   mainnet. If you sign in to Hyperliquid by email, the testnet site gives you a
   different address: export the mainnet wallet into a wallet extension and
   connect that instead.
2. On the testnet site's API page, create and approve an API wallet, and copy
   its private key. A mainnet API wallet does not work on testnet.
3. Open **Select Broker**, find **Hyperliquid Testnet** and choose
   **Configure**. Save the wallet address and the testnet agent key.
4. Choose **Login with Hyperliquid Testnet**. The testnet symbol list is
   downloaded at login.

Switching between Hyperliquid and Hyperliquid Testnet is a broker switch like
any other: it is confirmed on Select Broker and refused while Live automation
runs on the broker you are leaving.

If your `.env` sets `VALID_BROKERS`, add `hyperliquid_testnet` to it and
restart OpenAlgo, or the card does not appear.
