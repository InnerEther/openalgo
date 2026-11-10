"""Which Hyperliquid network a call goes to: mainnet or testnet.

One plugin serves two brokers. ``hyperliquid`` is mainnet. ``hyperliquid_testnet``
is Hyperliquid's test network, with its own markets, accounts, API wallets and
test funds. The ``broker/hyperliquid_testnet`` package holds no logic of its
own: each of its modules re-exports this plugin's module of the same name with
every function wrapped in :func:`on_testnet`, so the call and everything it does
(the info and exchange addresses, the WebSocket address, the order signature)
uses testnet.

The network travels in a ``ContextVar`` rather than in the environment, so it
follows the broker module that was called and never which broker's keys the
process happens to hold. That matters for the symbol list, which is downloaded
on a thread a login starts, before the new broker's keys are published. A
``ContextVar`` belongs to its green thread under eventlet and to its OS thread
otherwise, so two requests on different networks never see each other's.
Code that starts a thread of its own inside a testnet call must enter
:func:`testnet` again there; the streaming adapter is wrapped whole for that
reason (:func:`testnet_class`), so its WebSocket callbacks run on testnet too.
"""

import contextlib
import contextvars
import functools
import inspect

MAINNET_URL = "https://api.hyperliquid.xyz"
TESTNET_URL = "https://api.hyperliquid-testnet.xyz"

MAINNET_BROKER = "hyperliquid"
TESTNET_BROKER = "hyperliquid_testnet"

_testnet = contextvars.ContextVar("hyperliquid_testnet", default=False)


def is_testnet() -> bool:
    """Whether the current call runs for the Hyperliquid Testnet broker."""
    return _testnet.get()


def broker_name() -> str:
    """The OpenAlgo broker name of the network in use."""
    return TESTNET_BROKER if is_testnet() else MAINNET_BROKER


def base_url() -> str:
    """The REST origin of the network in use, without a trailing slash."""
    return TESTNET_URL if is_testnet() else MAINNET_URL


def ws_url() -> str:
    """The WebSocket address of the network in use."""
    return "wss://" + base_url().removeprefix("https://") + "/ws"


@contextlib.contextmanager
def testnet():
    """Run the enclosed code on testnet."""
    token = _testnet.set(True)
    try:
        yield
    finally:
        _testnet.reset(token)


def on_testnet(func):
    """``func``, run on testnet whenever it is called."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with testnet():
            return func(*args, **kwargs)

    return wrapper


def testnet_class(cls, name: str | None = None):
    """A subclass of ``cls`` whose own methods all run on testnet.

    Static and class methods are left as they are: none of them reaches
    Hyperliquid.
    """
    methods = {
        attr: on_testnet(value) for attr, value in vars(cls).items() if inspect.isfunction(value)
    }
    methods["__module__"] = cls.__module__
    return type(name or cls.__name__, (cls,), methods)


def testnet_namespace(module) -> dict:
    """The public names of a mainnet module, with its own functions run on testnet.

    Functions the module only imports are passed through unchanged, as are its
    classes: a class that reaches Hyperliquid is wrapped by the testnet module
    itself with :func:`testnet_class`, and the others (exceptions, the
    ``SymToken`` table) must stay the same class on both networks.
    """
    out = {}
    for attr, value in vars(module).items():
        if attr.startswith("_"):
            continue
        if inspect.isfunction(value) and value.__module__ == module.__name__:
            out[attr] = on_testnet(value)
        else:
            out[attr] = value
    return out
