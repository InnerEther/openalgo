"""Hyperliquid orders: signing, order building and the order calls, offline.

The signing vectors in ``hyperliquid_signing_vectors.json`` were produced by
Hyperliquid's own Python SDK, so a match proves the signatures are the ones
Hyperliquid verifies. No test here reaches the network: the HTTP client is
replaced, and a test that would send anything unexpected fails.
"""

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from coincurve import PublicKey

from broker.hyperliquid.api import auth_api, client, exchange, order_api, signing

VECTORS = json.loads(
    (Path(__file__).parent / "hyperliquid_signing_vectors.json").read_text(encoding="utf-8")
)["cases"]

AGENT_KEY = "0x" + "11" * 32
AGENT = signing.address_of(signing.private_key_from_hex(AGENT_KEY))
ACCOUNT = "0x" + "ab" * 20

ASSETS = {
    "BTC": {"asset": 0, "sz_decimals": 5, "is_spot": False},
    "ETH": {"asset": 1, "sz_decimals": 4, "is_spot": False},
    "@107": {"asset": 10107, "sz_decimals": 2, "is_spot": True, "base": "HYPE"},
}
SYMBOLS = {"BTCUSDFUT": "BTC", "ETHUSDFUT": "ETH", "HYPEUSDC": "@107"}
CONTEXTS = {"BTC": {"midPx": "74507.5"}, "ETH": {"midPx": "2111.25"}, "@107": {"midPx": "74.2755"}}


@pytest.mark.parametrize("case", VECTORS, ids=lambda c: c["action"]["type"])
def test_signatures_match_hyperliquids_sdk(case):
    key = signing.private_key_from_hex(case["key"])
    assert signing.address_of(key) == case["address"]
    assert (
        signing.sign_l1_action(key, case["action"], case["nonce"], case["mainnet"]) == case["sig"]
    )


@pytest.mark.parametrize("bad", ["", "0x12", "zz" * 32, "00" * 32, "0x" + "ff" * 32])
def test_a_bad_key_is_refused(bad):
    with pytest.raises(signing.InvalidKey):
        signing.private_key_from_hex(bad)


@pytest.mark.parametrize(
    "price, sz_decimals, is_spot, expected",
    [
        ("74507.53", 5, False, "74508"),
        ("123456.7", 5, False, "123457"),
        ("2111.237", 4, False, "2111.2"),
        ("0.0045137", 0, False, "0.004514"),
        ("0.00451378", 0, True, "0.0045138"),
        ("74.27551", 2, True, "74.276"),
    ],
)
def test_prices_follow_hyperliquids_rule(price, sz_decimals, is_spot, expected):
    assert order_api.wire(order_api.round_price(price, sz_decimals, is_spot)) == expected


def test_sizes_round_down_to_the_step():
    assert order_api.wire(order_api.round_size("0.123456789", 5)) == "0.12345"
    assert order_api.wire(order_api.round_size("3", 2)) == "3"
    with pytest.raises(order_api.OrderRefused, match="smallest size"):
        order_api.round_size("0.000001", 5)
    with pytest.raises(order_api.OrderRefused):
        order_api.round_size("-1", 5)


@pytest.fixture
def market(monkeypatch):
    import database.token_db as token_db

    monkeypatch.setattr(token_db, "get_br_symbol", lambda symbol, exchange: SYMBOLS.get(symbol))
    monkeypatch.setattr(client, "asset_info", lambda coin: ASSETS.get(coin))
    monkeypatch.setattr(client, "asset_contexts", lambda: CONTEXTS)


def _order(**fields):
    base = {"symbol": "BTCUSDFUT", "exchange": "CRYPTO", "action": "BUY", "quantity": "0.01"}
    base.update(fields)
    return order_api.build_order(base)


def test_order_types_map_onto_hyperliquid(market):
    buy = _order(pricetype="MARKET")
    assert buy == {
        "a": 0,
        "b": True,
        "p": "78233",
        "s": "0.01",
        "r": False,
        "t": {"limit": {"tif": "Ioc"}},
    }
    sell = _order(pricetype="MARKET", action="SELL")
    assert sell["p"] == "70782" and sell["b"] is False

    limit = _order(pricetype="LIMIT", price="70000.4")
    assert limit["t"] == {"limit": {"tif": "Gtc"}} and limit["p"] == "70000"

    # A buy stop above the market is a stop-loss; a sell trigger above it waits
    # for the price to rise (take-profit), as on any trading screen.
    stop = _order(pricetype="SL-M", trigger_price="76000")
    assert stop["t"] == {"trigger": {"isMarket": True, "triggerPx": "76000", "tpsl": "sl"}}
    assert stop["p"] == "79800"
    take = _order(pricetype="SL", action="SELL", trigger_price="80000", price="79900")
    assert take["t"] == {"trigger": {"isMarket": False, "triggerPx": "80000", "tpsl": "tp"}}
    assert take["p"] == "79900"
    sell_stop = _order(pricetype="SL", action="SELL", trigger_price="70000", price="69900")
    assert sell_stop["t"]["trigger"]["tpsl"] == "sl"

    spot = order_api.build_order(
        {
            "symbol": "HYPEUSDC",
            "exchange": "CRYPTO",
            "action": "BUY",
            "quantity": "1.239",
            "pricetype": "LIMIT",
            "price": "74.2",
        }
    )
    assert spot["a"] == 10107 and spot["s"] == "1.23"

    with pytest.raises(order_api.OrderRefused):
        _order(symbol="NOPE", pricetype="MARKET")
    with pytest.raises(order_api.OrderRefused):
        _order(pricetype="CO")


class FakeHTTP:
    """Stands in for the shared httpx client and records what was sent."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.sent = []
        self.urls = []

    def post(self, url, json=None, timeout=None):
        assert url.endswith("/exchange"), url
        assert timeout is not None
        self.urls.append(url)
        self.sent.append(json)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(200, json=answer, request=httpx.Request("POST", url))


@pytest.fixture
def http(monkeypatch, market):
    def install(*answers, key=AGENT_KEY):
        fake = FakeHTTP(answers)
        monkeypatch.setattr(exchange, "get_httpx_client", lambda: fake)
        monkeypatch.setenv("BROKER_API_SECRET", key)
        monkeypatch.delenv("HYPERLIQUID_TESTNET", raising=False)
        return fake

    return install


def _signer(payload, is_mainnet=True) -> str:
    digest = signing.l1_digest(payload["action"], payload["nonce"], is_mainnet)
    sig = payload["signature"]
    raw = (
        int(sig["r"], 16).to_bytes(32, "big")
        + int(sig["s"], 16).to_bytes(32, "big")
        + bytes([sig["v"] - 27])
    )
    public = PublicKey.from_signature_and_message(raw, digest, hasher=None)
    return "0x" + signing.keccak256(public.format(compressed=False)[1:])[-20:].hex()


def test_an_order_is_signed_by_the_agent_and_sent_once(http):
    fake = http(
        {
            "status": "ok",
            "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 77}}]}},
        }
    )
    response, body, oid = order_api.place_order_api(
        {
            "symbol": "BTCUSDFUT",
            "exchange": "CRYPTO",
            "action": "BUY",
            "quantity": "0.01",
            "pricetype": "LIMIT",
            "price": "70000",
        },
        ACCOUNT,
    )
    assert (response.status, body, oid) == (200, {"status": "success", "orderid": "77"}, "77")
    (payload,) = fake.sent
    assert payload["action"]["type"] == "order" and payload["vaultAddress"] is None
    assert _signer(payload) == AGENT
    assert fake.urls == ["https://api.hyperliquid.xyz/exchange"]


def test_the_testnet_switch_moves_every_address(monkeypatch):
    monkeypatch.delenv("HYPERLIQUID_TESTNET", raising=False)
    assert not client.is_testnet()
    assert client.base_url() == "https://api.hyperliquid.xyz"
    assert client.ws_url() == "wss://api.hyperliquid.xyz/ws"
    for value in ("true", "TRUE", "1", "yes"):
        monkeypatch.setenv("HYPERLIQUID_TESTNET", value)
        assert client.is_testnet()
    assert client.base_url() == "https://api.hyperliquid-testnet.xyz"
    assert client.ws_url() == "wss://api.hyperliquid-testnet.xyz/ws"
    monkeypatch.setenv("HYPERLIQUID_TESTNET", "false")
    assert not client.is_testnet()


def test_on_testnet_info_requests_go_to_testnet(monkeypatch):
    monkeypatch.setenv("HYPERLIQUID_TESTNET", "true")
    urls = []

    class Info:
        def post(self, url, json=None, timeout=None):
            urls.append(url)
            return httpx.Response(200, json={}, request=httpx.Request("POST", url))

    monkeypatch.setattr(client, "get_httpx_client", lambda: Info())
    client.post_info({"type": "meta"})
    assert urls == ["https://api.hyperliquid-testnet.xyz/info"]


def test_on_testnet_an_order_is_signed_for_testnet_and_sent_there(http, monkeypatch):
    fake = http(
        {
            "status": "ok",
            "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 5}}]}},
        }
    )
    monkeypatch.setenv("HYPERLIQUID_TESTNET", "true")
    response, _body, oid = order_api.place_order_api(
        {
            "symbol": "BTCUSDFUT",
            "exchange": "CRYPTO",
            "action": "BUY",
            "quantity": "0.01",
            "pricetype": "LIMIT",
            "price": "70000",
        },
        ACCOUNT,
    )
    assert (response.status, oid) == (200, "5")
    assert fake.urls == ["https://api.hyperliquid-testnet.xyz/exchange"]
    (payload,) = fake.sent
    # Signed for testnet: the same signature read as a mainnet one names
    # someone else, which is why each network refuses the other's orders.
    assert _signer(payload, is_mainnet=False) == AGENT
    assert _signer(payload, is_mainnet=True) != AGENT


def test_hyperliquids_refusal_reaches_the_trader(http):
    http(
        {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {"statuses": [{"error": "Order must have minimum value of $10. asset=0"}]},
            },
        }
    )
    response, body, oid = order_api.place_order_api(
        {
            "symbol": "BTCUSDFUT",
            "exchange": "CRYPTO",
            "action": "BUY",
            "quantity": "0.0001",
            "pricetype": "MARKET",
        },
        ACCOUNT,
    )
    assert response.status == 400 and oid is None
    assert body["message"] == "Order must have minimum value of $10."


def test_an_unknown_agent_gets_a_sentence_to_act_on(http):
    http({"status": "err", "response": "User or API Wallet 0x1234 does not exist."})
    _, body, _ = order_api.place_order_api(
        {
            "symbol": "BTCUSDFUT",
            "exchange": "CRYPTO",
            "action": "BUY",
            "quantity": "0.01",
            "pricetype": "MARKET",
        },
        ACCOUNT,
    )
    assert body["message"] == exchange.UNKNOWN_AGENT


def test_a_timeout_is_not_retried(http):
    fake = http(httpx.ReadTimeout("slow"))
    response, body, oid = order_api.place_order_api(
        {
            "symbol": "BTCUSDFUT",
            "exchange": "CRYPTO",
            "action": "BUY",
            "quantity": "0.01",
            "pricetype": "MARKET",
        },
        ACCOUNT,
    )
    assert len(fake.sent) == 1 and oid is None
    assert body["message"] == exchange.UNKNOWN_OUTCOME


def test_without_an_agent_key_nothing_is_sent(http):
    fake = http(key="")
    response, body, oid = order_api.place_order_api(
        {
            "symbol": "BTCUSDFUT",
            "exchange": "CRYPTO",
            "action": "BUY",
            "quantity": "0.01",
            "pricetype": "MARKET",
        },
        ACCOUNT,
    )
    assert response.status == 400 and body["message"] == exchange.NO_AGENT_KEY
    assert fake.sent == []


def test_nonces_never_repeat():
    nonces = [exchange.next_nonce() for _ in range(500)]
    assert nonces == sorted(set(nonces))


def test_cancel_finds_the_market_of_the_order(http, monkeypatch):
    monkeypatch.setattr(client, "open_orders", lambda user: [{"coin": "ETH", "oid": 42}])
    fake = http({"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"]}}})
    assert order_api.cancel_order("42", ACCOUNT) == ({"status": "success", "orderid": "42"}, 200)
    assert fake.sent[0]["action"] == {"type": "cancel", "cancels": [{"a": 1, "o": 42}]}
    assert order_api.cancel_order("43", ACCOUNT)[1] == 404


def test_cancel_all_reports_each_order(http, monkeypatch):
    monkeypatch.setattr(
        client, "open_orders", lambda user: [{"coin": "ETH", "oid": 1}, {"coin": "BTC", "oid": 2}]
    )
    http(
        {
            "status": "ok",
            "response": {
                "type": "cancel",
                "data": {"statuses": ["success", {"error": "Order was never placed"}]},
            },
        }
    )
    assert order_api.cancel_all_orders_api({}, ACCOUNT) == (["1"], ["2"])


def test_close_all_sends_reduce_only_market_orders(http, monkeypatch):
    monkeypatch.setattr(
        order_api,
        "get_positions",
        lambda auth, strict=False: [
            {"tradingSymbol": "BTCUSDFUT", "netQty": -0.5},
            {"tradingSymbol": "ETHUSDFUT", "netQty": 2.0},
        ],
    )
    fake = http(
        {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {
                    "statuses": [
                        {"filled": {"oid": 1, "totalSz": "0.5", "avgPx": "1"}},
                        {"filled": {"oid": 2, "totalSz": "2", "avgPx": "1"}},
                    ]
                },
            },
        }
    )
    assert order_api.close_all_positions("k", ACCOUNT)[1] == 200
    orders = fake.sent[0]["action"]["orders"]
    assert [(o["a"], o["b"], o["s"], o["r"]) for o in orders] == [
        (0, True, "0.5", True),
        (1, False, "2", True),
    ]


def test_smart_order_places_only_the_difference(http, monkeypatch):
    monkeypatch.setattr(
        order_api,
        "get_positions",
        lambda auth, strict=False: [{"tradingSymbol": "BTCUSDFUT", "netQty": 0.03}],
    )
    order_api._position_cache.invalidate(ACCOUNT)
    fake = http(
        {
            "status": "ok",
            "response": {"type": "order", "data": {"statuses": [{"filled": {"oid": 9}}]}},
        }
    )
    data = {
        "symbol": "BTCUSDFUT",
        "exchange": "CRYPTO",
        "product": "NRML",
        "action": "BUY",
        "quantity": "0.01",
        "pricetype": "MARKET",
        "position_size": "0.01",
    }
    _, body, oid = order_api.place_smartorder_api(data, ACCOUNT)
    assert oid == "9"
    sent = fake.sent[0]["action"]["orders"][0]
    assert sent["b"] is False and sent["s"] == "0.02"


def test_smart_order_refuses_when_the_position_is_unknown(http, monkeypatch):
    def unreadable(auth, strict=False):
        raise client.HyperliquidError("down")

    monkeypatch.setattr(order_api, "get_positions", unreadable)
    order_api._position_cache.invalidate(ACCOUNT)
    fake = http()
    data = {
        "symbol": "BTCUSDFUT",
        "exchange": "CRYPTO",
        "product": "NRML",
        "action": "SELL",
        "quantity": "0",
        "pricetype": "MARKET",
        "position_size": "0",
    }
    _, body, oid = order_api.place_smartorder_api(data, ACCOUNT)
    assert oid is None and body["status"] == "error"
    assert fake.sent == []


def test_spot_smart_order_reads_the_token_balance(http, monkeypatch):
    monkeypatch.setattr(
        client,
        "spot_clearinghouse_state",
        lambda user: {"balances": [{"coin": "HYPE", "total": "3.5"}]},
    )
    assert order_api.get_open_position("HYPEUSDC", "CRYPTO", "CNC", ACCOUNT) == "3.5"


def test_the_login_checks_the_agent_key(monkeypatch):
    assert auth_api.check_agent_key(ACCOUNT, "") is None
    assert "not a valid private key" in auth_api.check_agent_key(ACCOUNT, "0x12")
    assert auth_api.check_agent_key(AGENT, AGENT_KEY) is None  # the wallet's own key

    monkeypatch.setattr(
        auth_api, "post_info", lambda body: {"role": "agent", "data": {"user": ACCOUNT}}
    )
    assert auth_api.check_agent_key(ACCOUNT, AGENT_KEY) is None
    monkeypatch.setattr(
        auth_api, "post_info", lambda body: {"role": "agent", "data": {"user": "0x" + "cd" * 20}}
    )
    assert "not approved" in auth_api.check_agent_key(ACCOUNT, AGENT_KEY)
    monkeypatch.setattr(auth_api, "post_info", lambda body: {"role": "missing"})
    monkeypatch.delenv("HYPERLIQUID_TESTNET", raising=False)
    message = auth_api.check_agent_key(ACCOUNT, AGENT_KEY)
    assert "not approved" in message and "testnet" not in message
    monkeypatch.setenv("HYPERLIQUID_TESTNET", "true")
    assert "app.hyperliquid-testnet.xyz" in auth_api.check_agent_key(ACCOUNT, AGENT_KEY)


def test_decimal_wire_has_no_exponent():
    assert order_api.wire(Decimal("1E+5")) == "100000"
    assert order_api.wire(Decimal("0.000010")) == "0.00001"
    assert order_api.wire(Decimal("-0")) == "0"
