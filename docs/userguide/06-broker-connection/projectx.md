# Connecting ProjectX (TopstepX)

ProjectX is the trading platform behind TopstepX and several other futures prop
firms. OpenAlgo connects to it as the `projectx` broker and uses it for
**charting**: the contract list, candles at every interval from 1 second to
1 month, quotes and the live price feed on `/trading` and `/websocket/test`.

Orders are **not** connected yet. Placing, changing or cancelling an order with
ProjectX selected is refused with a message, and the order, trade and position
books stay empty. Trade on your firm's own platform.

## What you need

1. Your **ProjectX username**: the name you log in to TopstepX (or your firm's
   platform) with, not your email address.
2. A **ProjectX API key**, generated on your firm's ProjectX dashboard. API
   access is a paid add-on on most firms.

## Connect

1. Open **Select Broker**, find **ProjectX** and choose **Configure**.
2. Enter the username and the API key, then save.
3. Choose **Login with ProjectX**. There is nothing to type: OpenAlgo signs in
   with the saved keys and downloads the contract list.

If you keep keys in `.env` instead, set `BROKER_API_KEY` to the username,
`BROKER_API_SECRET` to the API key and
`REDIRECT_URL = 'http://127.0.0.1:5000/projectx/callback'`. Make sure
`projectx` is in `VALID_BROKERS`. To show only ProjectX on Select Broker, set
`VALID_BROKERS = 'projectx'`.

## Optional `.env` settings

| Setting | Default | When to change it |
| --- | --- | --- |
| `PROJECTX_API_URL` | `https://api.topstepx.com` | Your firm runs ProjectX on its own address |
| `PROJECTX_RTC_URL` | `https://rtc.topstepx.com` | The live feed address of that firm |
| `PROJECTX_LIVE_DATA` | `false` | Your account has a live (exchange) data subscription. Evaluation and most funded accounts use sim data, which is the default |
| `PROJECTX_ACCOUNT_ID` | first active account that can trade | Funds should show a different account |

## Symbols

Every ProjectX contract is on the exchange **`CME`** (CME Group: CME, CBOT,
NYMEX and COMEX contracts alike), and its symbol is the contract name ProjectX
uses, for example `ESZ5`, `MNQZ5` or `GCG6`. ProjectX lists the current
contract of each product, so the symbols change when a contract rolls. Search
by the product (`ES`, `NQ`, `CL`) in the chart's symbol search to find it.

## Good to know

- **Sessions last one day.** ProjectX sessions expire after 24 hours, and
  OpenAlgo ends broker sessions at its daily rollover (3:00 AM IST by default,
  `SESSION_EXPIRY_TIME`). Log in again from Select Broker each day.
- **Quotes come from candles.** ProjectX has no quote endpoint, so the quote
  box reads the current daily candle: last price, open, high, low, volume and
  the previous close. Bid and ask appear on the live feed only.
- **Depth.** The live feed builds a five-level book from ProjectX's market
  depth stream. The one-off depth request has no book and shows the quote.
- **Request limits.** ProjectX allows 50 candle requests every 30 seconds.
  OpenAlgo paces itself under that, so a burst of chart loads can take a few
  seconds longer rather than fail.
- **Market hours.** The CME exchange is not in OpenAlgo's market calendar, so
  features that check whether a market is open (sandbox auto square-off,
  scheduled strategies) do not know CME hours.
