# ruff: noqa: F811  (the broker-keys fixtures are imported by name and used as parameters)
"""The ProjectX (TopstepX) plugin: login, contract list, candles, quotes and the live feed.

Nothing here reaches ProjectX. The HTTP client and the hub socket are replaced
at the point where a request would leave, and the tests assert what was sent:
the endpoint, the bearer token and the body.
"""

import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

# The broker-keys fixtures, so a real Select Broker login can be driven.
from test_broker_keys import (  # noqa: F401
    USER,
    _app,
    _nothing_runs,
    _signed_in,
    completed,
    environment,
    stores,
)

import database.broker_keys_db as keys_db
import websocket_proxy  # noqa: F401  (imports the adapters in the order the proxy does)
from broker.projectx.api import auth_api, baseurl
from broker.projectx.api import data as px_data
from broker.projectx.database import master_contract_db as px_master
from broker.projectx.streaming import projectx_adapter as px_adapter
from broker.projectx.streaming import projectx_signalr as px_signalr


class _Response:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code
        self.text = json.dumps(body)

    def json(self):
        return self._body


class _Transport:
    """Stands in for the shared httpx client and records every request."""

    def __init__(self, answer):
        self.answer = answer
        self.sent = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.sent.append(
            SimpleNamespace(url=url, json=json, headers=headers or {}, timeout=timeout)
        )
        result = self.answer(url, json)
        return result if isinstance(result, _Response) else _Response(result)


@pytest.fixture
def transport(monkeypatch):
    holder = SimpleNamespace(client=None)

    def install(answer):
        holder.client = _Transport(answer)
        monkeypatch.setattr(baseurl, "get_httpx_client", lambda: holder.client)
        return holder.client

    # Rate pacing is real time; tests do not wait on it.
    monkeypatch.setattr(baseurl._BARS_WINDOW, "acquire", lambda: None)
    monkeypatch.setattr(baseurl._GENERAL_WINDOW, "acquire", lambda: None)
    px_data._QUOTE_CACHE.clear()
    for var in (
        "PROJECTX_API_URL",
        "PROJECTX_RTC_URL",
        "PROJECTX_LIVE_DATA",
        "PROJECTX_ACCOUNT_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    return install


# --------------------------------------------------------------------------- login


def test_login_sends_the_saved_username_and_key(transport, monkeypatch):
    monkeypatch.setenv("BROKER_API_KEY", "jane.trader")
    monkeypatch.setenv("BROKER_API_SECRET", "k7Qm2vX9")
    sent = transport(
        lambda url, body: {"token": "jwt-1", "success": True, "errorCode": 0, "errorMessage": None}
    )

    token, error = auth_api.authenticate_broker("projectx")

    assert (token, error) == ("jwt-1", None)
    assert sent.sent[0].url == "https://api.topstepx.com/api/Auth/loginKey"
    assert sent.sent[0].json == {"userName": "jane.trader", "apiKey": "k7Qm2vX9"}
    assert "Authorization" not in sent.sent[0].headers


def test_login_goes_to_the_firm_named_in_env(transport, monkeypatch):
    monkeypatch.setenv("BROKER_API_KEY", "u")
    monkeypatch.setenv("BROKER_API_SECRET", "k")
    monkeypatch.setenv("PROJECTX_API_URL", "https://api.otherfirm.example/")
    sent = transport(lambda url, body: {"token": "t", "success": True})

    auth_api.authenticate_broker()

    assert sent.sent[0].url == "https://api.otherfirm.example/api/Auth/loginKey"


def test_a_refused_login_reads_as_a_sentence(transport, monkeypatch):
    monkeypatch.setenv("BROKER_API_KEY", "u")
    monkeypatch.setenv("BROKER_API_SECRET", "wrong")
    transport(
        lambda url, body: {"token": None, "success": False, "errorCode": 3, "errorMessage": None}
    )

    token, error = auth_api.authenticate_broker()

    assert token is None
    assert "username and API key" in error
    assert "errorCode" not in error


@pytest.mark.parametrize(
    "code, words",
    [
        (3, "fresh API key"),
        (7, "agreements"),
        (9, "API subscription"),
        (10, "turned off"),
        (99, "refused"),
    ],
)
def test_each_login_refusal_names_its_cause(transport, monkeypatch, code, words):
    monkeypatch.setenv("BROKER_API_KEY", "u")
    monkeypatch.setenv("BROKER_API_SECRET", "k")
    transport(
        lambda url, body: {"token": None, "success": False, "errorCode": code, "errorMessage": None}
    )

    token, error = auth_api.authenticate_broker()

    assert token is None and words in error


def test_login_without_saved_keys_sends_nothing(transport, monkeypatch):
    monkeypatch.delenv("BROKER_API_KEY", raising=False)
    monkeypatch.delenv("BROKER_API_SECRET", raising=False)
    sent = transport(lambda url, body: pytest.fail("nothing may be sent"))

    token, error = auth_api.authenticate_broker()

    assert token is None and "Broker Configuration" in error
    assert sent.sent == []


def test_select_broker_login_runs_with_the_saved_keys_and_completes_once(
    stores, environment, completed, monkeypatch
):
    monkeypatch.setenv("VALID_BROKERS", "zerodha,projectx")
    keys_db.save_broker_keys("projectx", {"api_key": "jane.trader", "api_secret": "px-key"}, None)
    seen = {}

    def projectx_auth(code):
        seen["user"] = os.getenv("BROKER_API_KEY")
        seen["key"] = os.getenv("BROKER_API_SECRET")
        return "jwt-2", None

    app = _app()
    app.broker_auth_functions = {"projectx_auth": projectx_auth}
    client = _signed_in(app)

    linked = client.get("/projectx/callback")
    url = client.post("/auth/broker/login", json={"broker": "projectx"}).get_json()["url"]
    first = client.get(url)
    replay = client.get(url)

    assert (
        linked.headers["Location"].split("&broker=")[0].endswith("/broker?error=login_not_started")
    )
    assert url == "/projectx/callback"
    assert first.get_data(as_text=True) == "logged in"
    assert seen == {"user": "jane.trader", "key": "px-key"}
    assert completed == [("jwt-2", USER, "projectx")]
    assert (
        replay.headers["Location"].split("&broker=")[0].endswith("/broker?error=login_not_started")
    )


def test_projectx_is_listed_everywhere_a_broker_must_be():
    from utils.broker_keys import BROKER_NAMES
    from utils.broker_login import CALLBACK_START_BROKERS
    from utils.broker_setup_guide import GUIDES
    from utils.constants import VALID_EXCHANGES

    assert BROKER_NAMES["projectx"] == "ProjectX"
    assert "projectx" in CALLBACK_START_BROKERS
    assert GUIDES["projectx"]["sign_in"] == "saved_keys"
    assert "CME" in VALID_EXCHANGES
    for path in (".sample.env", "start.sh"):
        with open(path, encoding="utf-8") as handle:
            line = next(row for row in handle if "VALID_BROKERS" in row)
        assert "projectx" in line.split("'")[1].replace("}", "").split(","), path


# ------------------------------------------------------------------ contract list


def test_contract_names_split_into_root_and_month():
    assert px_master.split_contract_name("ESZ5")[0] == "ES"
    assert px_master.split_contract_name("ESZ5")[1].startswith("DEC-")
    assert px_master.split_contract_name("MNQH26") == ("MNQ", "MAR-26")
    assert px_master.split_contract_name("M2KU5")[0] == "M2K"
    assert px_master.split_contract_name("6EZ5")[0] == "6E"
    assert px_master.split_contract_name("ODDNAME") == ("ODDNAME", "")


def test_contracts_become_symtoken_rows():
    contracts = [
        {
            "id": "CON.F.US.EP.Z25",
            "name": "ESZ5",
            "description": "E-mini S&P 500",
            "tickSize": 0.25,
            "tickValue": 12.5,
            "activeContract": True,
            "symbolId": "F.US.EP",
        },
        {"id": "CON.F.US.EP.Z25", "name": "ESZ5"},  # a duplicate is dropped
        {"id": "CON.F.US.MNQ.Z25", "name": "MNQZ5", "tickSize": 0.25},
        {"id": None, "name": "BAD"},
    ]

    df = px_master.contracts_to_dataframe(contracts)

    assert list(df["symbol"]) == ["ESZ5", "MNQZ5"]
    row = df.iloc[0].to_dict()
    assert row["token"] == "CON.F.US.EP.Z25"
    assert row["name"] == "ES"
    assert row["exchange"] == "CME" and row["brexchange"] == "CME"
    assert row["instrumenttype"] == "FUT"
    assert row["tick_size"] == 0.25 and row["lotsize"] == 1


def test_the_contract_list_falls_back_to_sim_data(transport, monkeypatch):
    monkeypatch.setenv("PROJECTX_LIVE_DATA", "true")
    sent = transport(
        lambda url, body: {
            "contracts": [] if body["live"] else [{"id": "CON.F.US.EP.Z25", "name": "ESZ5"}],
            "success": True,
        }
    )

    contracts = px_master.fetch_contracts("jwt")

    assert [c["name"] for c in contracts] == ["ESZ5"]
    assert [r.json["live"] for r in sent.sent] == [True, False]
    assert all(r.headers["Authorization"] == "Bearer jwt" for r in sent.sent)


# ------------------------------------------------------------------------ candles


def _bar(ts, price, volume=10):
    return {"t": ts, "o": price, "h": price + 1, "l": price - 1, "c": price + 0.5, "v": volume}


def test_history_sends_the_right_request_and_returns_openalgo_columns(transport, monkeypatch):
    monkeypatch.setattr(px_data, "get_token", lambda symbol, exchange: "CON.F.US.EP.Z25")
    sent = transport(
        lambda url, body: {
            "bars": [
                _bar("2025-11-03T15:31:00+00:00", 6800),
                _bar("2025-11-03T15:30:00+00:00", 6799),
            ],
            "success": True,
        }
    )

    df = px_data.BrokerData("jwt").get_history("ESZ5", "CME", "5m", "2025-11-03", "2025-11-03")

    request = sent.sent[0]
    assert request.url.endswith("/api/History/retrieveBars")
    assert request.headers["Authorization"] == "Bearer jwt"
    assert request.json["contractId"] == "CON.F.US.EP.Z25"
    assert (request.json["unit"], request.json["unitNumber"]) == (px_data.UNIT_MINUTE, 5)
    assert request.json["live"] is False
    assert request.json["includePartialBar"] is True
    # IST calendar day: 00:00 IST is 18:30 UTC the day before.
    assert request.json["startTime"] == "2025-11-02T18:30:00Z"
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume", "oi"]
    # Newest-first bars come back oldest first, as epoch seconds of their UTC time.
    expected = int(datetime(2025, 11, 3, 15, 30, tzinfo=UTC).timestamp())
    assert list(df["timestamp"]) == [expected, expected + 60]


def test_daily_bars_land_on_their_cme_trading_day():
    session_open = [
        _bar("2025-11-02T23:00:00+00:00", 1),  # Sunday 17:00 CST open -> Monday 3 Nov
        _bar("2025-11-04T00:00:00+00:00", 2),  # midnight UTC stamp -> Tuesday 4 Nov
    ]

    df = px_data.BrokerData._bars_to_frame(session_open, px_data.UNIT_DAY)

    days = [datetime.fromtimestamp(ts, tz=UTC) for ts in df["timestamp"]]
    assert days == [datetime(2025, 11, 3, tzinfo=UTC), datetime(2025, 11, 4, tzinfo=UTC)]


def test_history_pages_backwards_when_a_page_is_full(transport, monkeypatch):
    monkeypatch.setattr(px_data, "get_token", lambda symbol, exchange: "CON.F.US.EP.Z25")
    monkeypatch.setattr(px_data, "MAX_BARS", 2)
    pages = [
        [_bar("2025-11-03T15:33:00Z", 4), _bar("2025-11-03T15:32:00Z", 3)],
        [_bar("2025-11-03T15:32:00Z", 3), _bar("2025-11-03T15:31:00Z", 2)],
        [_bar("2025-11-03T15:30:00Z", 1)],
    ]
    sent = transport(lambda url, body: {"bars": pages.pop(0), "success": True})

    df = px_data.BrokerData("jwt").get_history("ESZ5", "CME", "1m", "2025-11-03", "2025-11-03")

    assert len(sent.sent) == 3
    assert sent.sent[1].json["endTime"] == "2025-11-03T15:32:00Z"
    assert len(df) == 4 and df["timestamp"].is_monotonic_increasing


def test_an_unknown_contract_is_refused_before_a_request(transport, monkeypatch):
    monkeypatch.setattr(px_data, "get_token", lambda symbol, exchange: None)
    sent = transport(lambda url, body: pytest.fail("nothing may be sent"))

    with pytest.raises(baseurl.ProjectXError, match="contract list"):
        px_data.BrokerData("jwt").get_history("ZZZ", "CME", "1m", "2025-11-03", "2025-11-03")
    assert sent.sent == []


def test_an_expired_session_reads_as_a_sentence(transport, monkeypatch):
    monkeypatch.setattr(px_data, "get_token", lambda symbol, exchange: "CON.F.US.EP.Z25")
    transport(lambda url, body: _Response({}, status_code=401))

    with pytest.raises(baseurl.ProjectXError) as raised:
        px_data.BrokerData("jwt").get_history("ESZ5", "CME", "1m", "2025-11-03", "2025-11-03")
    assert "Log in to ProjectX again" in str(raised.value)
    assert "401" not in str(raised.value)


def test_a_quote_comes_from_the_current_daily_bar(transport, monkeypatch):
    monkeypatch.setattr(px_data, "get_token", lambda symbol, exchange: "CON.F.US.EP.Z25")
    today = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:00:00Z")
    yesterday = (datetime.now(UTC) - timedelta(days=1, hours=1)).strftime("%Y-%m-%dT%H:00:00Z")
    sent = transport(
        lambda url, body: {
            "bars": [
                {"t": today, "o": 6800, "h": 6810, "l": 6790, "c": 6805.25, "v": 1200},
                {"t": yesterday, "o": 6700, "h": 6801, "l": 6690, "c": 6799.5, "v": 9000},
            ],
            "success": True,
        }
    )
    handler = px_data.BrokerData("jwt")

    quote = handler.get_quotes("ESZ5", "CME")
    again = handler.get_quotes("ESZ5", "CME")

    assert quote["ltp"] == 6805.25 and quote["prev_close"] == 6799.5
    assert (quote["open"], quote["high"], quote["low"], quote["volume"]) == (6800, 6810, 6790, 1200)
    assert again == quote
    assert len(sent.sent) == 1  # the second read was served from the short cache
    assert sent.sent[0].json["unit"] == px_data.UNIT_DAY and sent.sent[0].json["limit"] == 2


def test_funds_report_the_chosen_account(transport, monkeypatch):
    from broker.projectx.api import funds

    monkeypatch.setenv("PROJECTX_ACCOUNT_ID", "22")
    transport(
        lambda url, body: {
            "accounts": [
                {"id": 11, "balance": 50000, "canTrade": True},
                {"id": 22, "balance": 148250.5},
            ],
            "success": True,
        }
    )

    assert funds.get_margin_data("jwt")["availablecash"] == "148250.50"


def test_orders_are_refused_with_a_sentence():
    from broker.projectx.api import order_api

    res, body, orderid = order_api.place_order_api({"symbol": "ESZ5"}, "jwt")

    assert res.status != 200 and orderid is None
    assert "ProjectX trading platform" in body["message"]


# ---------------------------------------------------------------------- live feed


def test_signalr_frames_round_trip():
    frame = px_signalr.encode({"type": 1, "target": "SubscribeContractQuotes", "arguments": ["C"]})
    raw = "{}" + px_signalr.RECORD_SEPARATOR + frame + '{"type":6}' + px_signalr.RECORD_SEPARATOR

    assert frame.endswith("\x1e")
    assert px_signalr.decode(raw) == [
        {},
        {"type": 1, "target": "SubscribeContractQuotes", "arguments": ["C"]},
        {"type": 6},
    ]
    assert px_signalr.hub_url("https://rtc.topstepx.com/hubs/market", "a b") == (
        "wss://rtc.topstepx.com/hubs/market?access_token=a%20b"
    )


class _Hub:
    ready = True

    def __init__(self):
        self.sent = []

    def send(self, target, arguments):
        self.sent.append((target, arguments[0]))
        return True


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setattr(
        px_adapter.SymbolMapper,
        "get_token_from_symbol",
        staticmethod(lambda symbol, exchange: {"token": f"CON.{symbol}", "brexchange": "CME"}),
    )
    instance = px_adapter.ProjectxWebSocketAdapter.__new__(px_adapter.ProjectxWebSocketAdapter)
    # The ZeroMQ publisher is the only thing the base class adds; record instead.
    instance.published = []
    instance.publish_market_data = lambda topic, data: instance.published.append((topic, data))
    instance.logger = px_adapter.get_logger("test")
    instance.broker_name = "projectx"
    instance.user_id = "trader"
    instance._auth_override = "jwt"
    instance._lock = px_adapter.threading.Lock()
    instance.subscriptions = {}
    instance._states = {}
    instance.connected = True
    instance.hub = _Hub()
    return instance


def test_one_hub_subscription_serves_every_mode_of_a_contract(adapter):
    adapter.subscribe("ESZ5", "CME", 1)
    adapter.subscribe("ESZ5", "CME", 2)
    adapter.subscribe("ESZ5", "CME", 3)
    adapter.unsubscribe("ESZ5", "CME", 3)
    adapter.unsubscribe("ESZ5", "CME", 2)
    adapter.unsubscribe("ESZ5", "CME", 1)

    assert adapter.hub.sent == [
        ("SubscribeContractQuotes", "CON.ESZ5"),
        ("SubscribeContractTrades", "CON.ESZ5"),
        ("SubscribeContractMarketDepth", "CON.ESZ5"),
        ("UnsubscribeContractMarketDepth", "CON.ESZ5"),
        ("UnsubscribeContractTrades", "CON.ESZ5"),
        ("UnsubscribeContractQuotes", "CON.ESZ5"),
    ]
    assert adapter._states == {}


def test_partial_quotes_merge_and_publish_on_openalgo_topics(adapter):
    adapter.subscribe("ESZ5", "CME", 1)
    adapter.subscribe("ESZ5", "CME", 2)

    adapter._on_event(
        "GatewayQuote",
        [
            "CON.ESZ5",
            {
                "lastPrice": 6805.25,
                "open": 6800,
                "high": 6810,
                "low": 6790,
                "volume": 1200,
                "change": 5.75,
                "bestBid": 6805,
                "bestAsk": 6805.5,
            },
        ],
    )
    adapter.published.clear()
    adapter._on_event("GatewayQuote", ["CON.ESZ5", {"lastPrice": 6806.0, "volume": 1201}])
    adapter._on_event("GatewayTrade", ["CON.ESZ5", [{"price": 6806.25, "volume": 3, "type": 0}]])

    topics = [topic for topic, _ in adapter.published]
    assert topics[:2] == ["CME_ESZ5_LTP", "CME_ESZ5_QUOTE"]
    quote = adapter.published[-1][1]
    assert quote["ltp"] == 6806.25 and quote["last_quantity"] == 3
    assert quote["open"] == 6800 and quote["volume"] == 1201  # kept from the first update
    assert quote["symbol"] == "ESZ5" and quote["exchange"] == "CME" and quote["mode"] == 2


def test_depth_levels_build_a_five_level_book(adapter):
    adapter.subscribe("ESZ5", "CME", 3)
    adapter._on_event("GatewayQuote", ["CON.ESZ5", {"lastPrice": 6805.25}])
    adapter._on_event(
        "GatewayDepth",
        [
            "CON.ESZ5",
            [
                {"type": 2, "price": 6805.0, "volume": 10},
                {"type": 2, "price": 6804.75, "volume": 7},
                {"type": 1, "price": 6805.5, "volume": 4},
                {"type": 1, "price": 6805.75, "volume": 0},
            ],
        ],
    )
    adapter._on_event("GatewayDepth", ["CON.ESZ5", [{"type": 2, "price": 6804.75, "volume": 0}]])

    topic, data = adapter.published[-1]
    assert topic == "CME_ESZ5_DEPTH"
    assert data["depth"]["buy"][0] == {"price": 6805.0, "quantity": 10, "orders": 0}
    assert data["depth"]["buy"][1]["quantity"] == 0  # removed level, padded
    assert data["depth"]["sell"][0]["price"] == 6805.5
    assert len(data["depth"]["buy"]) == len(data["depth"]["sell"]) == 5

    adapter._on_event("GatewayDepth", ["CON.ESZ5", [{"type": 6}]])
    assert adapter._states["CON.ESZ5"].bids == {}


def test_a_reconnect_replays_every_subscription(adapter):
    adapter.hub.ready = False
    adapter.subscribe("ESZ5", "CME", 2)
    assert adapter.hub.sent == []  # nothing goes out while the hub is down

    adapter.hub.ready = True
    adapter._on_ready()

    assert sorted(adapter.hub.sent) == [
        ("SubscribeContractQuotes", "CON.ESZ5"),
        ("SubscribeContractTrades", "CON.ESZ5"),
    ]


def test_events_for_unsubscribed_contracts_are_ignored(adapter):
    adapter._on_event("GatewayQuote", ["CON.NQZ5", {"lastPrice": 1}])
    assert adapter.published == []


def test_the_hub_handshake_then_events_reach_the_adapter():
    received, ready = [], []
    client = px_signalr.SignalRHubClient(
        "https://rtc.example/hubs/market",
        lambda: "t",
        lambda target, args: received.append((target, args)),
        on_ready=lambda: ready.append(True),
    )

    class _WS:
        closed = False

        def close(self):
            self.closed = True

    ws = _WS()
    client._on_message(ws, "{}\x1e")
    client._on_message(
        ws, '{"type":1,"target":"GatewayQuote","arguments":["C",{"lastPrice":1}]}\x1e'
    )
    client._on_message(ws, '{"type":7,"error":"Unauthorized"}\x1e')

    assert ready == [True]
    assert received == [("GatewayQuote", ["C", {"lastPrice": 1}])]
    assert ws.closed


def test_a_refused_handshake_closes_without_subscribing():
    ready = []
    client = px_signalr.SignalRHubClient(
        "https://x/hubs/market", lambda: "t", lambda *a: None, on_ready=lambda: ready.append(True)
    )
    ws = SimpleNamespace(closed=False, close=lambda: setattr(ws, "closed", True))

    client._on_message(ws, '{"error":"Requested protocol is not available"}\x1e')

    assert ready == [] and ws.closed and not client.ready


def test_dataframe_columns_are_stable_when_empty():
    df = px_data.BrokerData._bars_to_frame([], px_data.UNIT_MINUTE)
    assert isinstance(df, pd.DataFrame) and list(df.columns) == px_data._COLUMNS
