"""The Hyperliquid plugin's translations, checked offline on payloads shaped as
Hyperliquid sends them (captured from the live API on 2026-11-10)."""

import pandas as pd
import pytest

from broker.hyperliquid.api import data as hl_data
from broker.hyperliquid.api import order_api
from broker.hyperliquid.database import master_contract_db as mc
from broker.hyperliquid.streaming.hyperliquid_adapter import HyperliquidWebSocketAdapter as Adapter

UNIVERSE = [
    {"szDecimals": 5, "name": "BTC", "maxLeverage": 40},
    {"szDecimals": 4, "name": "ETH", "maxLeverage": 25},
    {"szDecimals": 0, "name": "OLD", "maxLeverage": 3, "isDelisted": True},
    {"szDecimals": 0, "name": "kPEPE", "maxLeverage": 10},
]
PERP_CTX = [
    {"markPx": "74499.0", "midPx": "74507.5", "prevDayPx": "74303.0", "openInterest": "41991.0"},
    {"markPx": "2111.2", "midPx": "2111.25", "prevDayPx": "2100.0", "openInterest": "1.0"},
    {"markPx": "1.0", "midPx": None, "prevDayPx": "1.0"},
    {"markPx": "0.004512", "midPx": "0.004513", "prevDayPx": "0.0045"},
]
SPOT_META = {
    "tokens": [
        {"name": "USDC", "szDecimals": 8, "index": 0},
        {"name": "PURR", "szDecimals": 0, "index": 1},
        {"name": "HYPE", "szDecimals": 2, "index": 150},
    ],
    "universe": [
        {"tokens": [1, 0], "name": "PURR/USDC", "index": 0, "isCanonical": True},
        {"tokens": [150, 0], "name": "@107", "index": 107, "isCanonical": False},
    ],
}
SPOT_CTX = [
    {"coin": "PURR/USDC", "midPx": "0.092535", "markPx": "0.09263", "prevDayPx": "0.0907"},
    {"coin": "@107", "midPx": "74.2755", "markPx": "74.27", "prevDayPx": "73.358"},
]


def test_symbols_follow_openalgo_crypto_naming():
    df = mc.build_dataframe(UNIVERSE, PERP_CTX, SPOT_META, SPOT_CTX).set_index("symbol")
    assert list(df.index) == ["BTCUSDFUT", "ETHUSDFUT", "KPEPEUSDFUT", "PURRUSDC", "HYPEUSDC"]
    # brsymbol keeps the exact coin name Hyperliquid's requests take.
    assert df.loc["KPEPEUSDFUT", "brsymbol"] == "kPEPE"
    assert df.loc["HYPEUSDC", "brsymbol"] == "@107"
    assert df.loc["HYPEUSDC", "token"] == "10107"
    assert df.loc["ETHUSDFUT", "token"] == "1"
    assert set(df["exchange"]) == {"CRYPTO"}
    assert df.loc["BTCUSDFUT", "lotsize"] == pytest.approx(0.00001)
    assert df.loc["BTCUSDFUT", "instrumenttype"] == "PERPFUT"
    assert df.loc["PURRUSDC", "instrumenttype"] == "SPOT"


@pytest.mark.parametrize(
    "price, sz_decimals, kind, step",
    [
        ("74507.5", 5, "PERPFUT", 1.0),  # 5 significant figures
        ("2111.25", 4, "PERPFUT", 0.1),
        ("0.004513", 0, "PERPFUT", 0.000001),  # capped by 6 - szDecimals decimals
        ("0.092535", 0, "SPOT", 0.000001),
        (None, 2, "PERPFUT", 0.0001),
    ],
)
def test_tick_size(price, sz_decimals, kind, step):
    assert mc.tick_size_for(price, sz_decimals, kind) == pytest.approx(step)


def test_feed_frames_become_openalgo_fields():
    trades = {
        "channel": "trades",
        "data": [
            {"coin": "BTC", "side": "B", "px": "74500.0", "sz": "0.01", "time": 1},
            {"coin": "BTC", "side": "A", "px": "74505.0", "sz": "0.0021", "time": 2},
        ],
    }
    assert Adapter.normalise(trades) == [
        ("BTC", {"ltp": 74505.0, "ltq": 0.0021, "last_trade_time": 2, "_traded": True})
    ]
    bbo = {
        "channel": "bbo",
        "data": {"coin": "BTC", "time": 1, "bbo": [{"px": "74505.0", "sz": "2.0", "n": 3}, None]},
    }
    ((_, fields),) = Adapter.normalise(bbo)
    assert fields == {"bid_price": 74505.0, "bid_qty": 2.0, "ask_price": 0.0, "ask_qty": 0.0}
    book = {
        "channel": "l2Book",
        "data": {
            "coin": "BTC",
            "levels": [[{"px": "1", "sz": "2", "n": 1}], [{"px": "3", "sz": "4", "n": 1}]],
        },
    }
    ((_, fields),) = Adapter.normalise(book)
    assert fields["depth"]["buy"][0] == {"price": 1.0, "quantity": 2.0}
    assert len(fields["depth"]["sell"]) == 5
    assert fields["totalsellqty"] == 4.0
    spot_ctx = {"channel": "activeSpotAssetCtx", "data": {"coin": "@107", "ctx": SPOT_CTX[1]}}
    ((coin, fields),) = Adapter.normalise(spot_ctx)
    assert coin == "@107" and "oi" not in fields and fields["prev_close"] == 73.358
    assert Adapter.normalise({"channel": "unknown", "data": {}}) == []


def test_mid_price_stands_in_until_the_first_trade():
    adapter = Adapter.__new__(Adapter)
    import threading

    adapter._lock = threading.Lock()
    adapter.last_values = {}
    adapter.subscriptions = {
        "BTCUSDFUT_CRYPTO_1": {
            "symbol": "BTCUSDFUT",
            "exchange": "CRYPTO",
            "coin": "BTC",
            "mode": 1,
        }
    }
    published = []
    adapter.publish_market_data = lambda topic, payload: published.append((topic, payload))

    adapter._publish("BTC", {"_mid": 100.0, "volume": 5.0})
    assert published[-1][0] == "CRYPTO_BTCUSDFUT_LTP"
    assert published[-1][1]["ltp"] == 100.0
    adapter._publish("BTC", {"ltp": 101.0, "ltq": 1.0, "_traded": True})
    adapter._publish("BTC", {"_mid": 99.0})
    assert published[-1][1]["ltp"] == 101.0
    assert "_traded" not in published[-1][1]


def test_history_pages_forward_and_returns_seconds(monkeypatch):
    now_ms = 1_794_300_000_000
    monkeypatch.setattr(hl_data.time, "time", lambda: now_ms / 1000)
    monkeypatch.setattr(hl_data, "_coin", lambda symbol, exchange: "BTC")
    calls = []

    def fake_candles(coin, interval, start, end):
        calls.append((start, end))
        if len(calls) > 2:
            return []
        return [
            {"t": start + i * 3_600_000, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "0.25"}
            for i in range(3)
        ]

    monkeypatch.setattr(hl_data.client, "candles", fake_candles)
    df = hl_data.BrokerData("x").get_history(
        "BTCUSDFUT", "CRYPTO", "1h", "2026-11-09", "2026-11-10"
    )
    assert list(df.columns) == hl_data.HISTORY_COLUMNS
    assert len(df) == 6 and df["timestamp"].is_monotonic_increasing
    assert calls[1][0] == calls[0][0] + 3 * 3_600_000
    assert df["volume"].iloc[0] == 0.25
    assert isinstance(df, pd.DataFrame)


def test_orders_are_refused_before_reaching_hyperliquid(monkeypatch):
    monkeypatch.setattr(order_api.client, "post_info", lambda *a, **k: pytest.fail("network"))
    response, body, orderid = order_api.place_order_api({"symbol": "BTCUSDFUT"}, "0xabc")
    assert response.status == 400 and orderid is None
    assert "not available" in body["message"]
    assert order_api.cancel_order("1", "0xabc")[1] == 400
    assert order_api.modify_order({}, "0xabc")[1] == 400
    assert order_api.close_all_positions("k", "0xabc")[1] == 400
