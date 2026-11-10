"""Signing Hyperliquid exchange actions.

Every order, modify and cancel sent to Hyperliquid's ``/exchange`` endpoint
carries an EIP-712 signature over a "phantom agent", exactly as Hyperliquid's
own Python SDK builds it (``hyperliquid/utils/signing.py``, ``sign_l1_action``):

1. The action is msgpack-encoded, followed by the nonce (8 bytes, big endian)
   and a vault marker byte (0x00: no vault). Its keccak-256 is the
   ``connectionId``.
2. The phantom agent ``{source: "a", connectionId}`` ("a" on mainnet) is hashed
   as the EIP-712 struct ``Agent(string source,bytes32 connectionId)`` in the
   domain ``Exchange``, version ``1``, chain id 1337, verifying contract zero.
3. The digest is signed with secp256k1 (deterministic, low-s), and sent as
   ``{"r", "s", "v"}`` with ``v`` 27 or 28.

The key that signs is the trader's agent (API wallet) key, approved for their
account on Hyperliquid's API page; it can trade but cannot withdraw. Keccak
comes from pycryptodome and the signature from libsecp256k1 (coincurve).
``test/test_hyperliquid_signing.py`` checks this module against signatures the
SDK produced for the same actions.
"""

import msgpack
from coincurve import PrivateKey
from Crypto.Hash import keccak

_ZERO_ADDRESS = bytes(20)


def keccak256(data: bytes) -> bytes:
    return keccak.new(digest_bits=256, data=data).digest()


_DOMAIN_TYPE_HASH = keccak256(
    b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)
_AGENT_TYPE_HASH = keccak256(b"Agent(string source,bytes32 connectionId)")
_DOMAIN_SEPARATOR = keccak256(
    _DOMAIN_TYPE_HASH
    + keccak256(b"Exchange")
    + keccak256(b"1")
    + (1337).to_bytes(32, "big")
    + _ZERO_ADDRESS.rjust(32, b"\x00")
)


class InvalidKey(ValueError):
    """A value that is not a 32-byte secp256k1 private key."""


def private_key_from_hex(value: str | None) -> PrivateKey:
    raw = (value or "").strip()
    if raw.startswith(("0x", "0X")):
        raw = raw[2:]
    try:
        secret = bytes.fromhex(raw)
    except ValueError as exc:
        raise InvalidKey("not hex") from exc
    if len(secret) != 32:
        raise InvalidKey("not 32 bytes")
    try:
        return PrivateKey(secret)
    except Exception as exc:
        raise InvalidKey("out of range") from exc


def address_of(key: PrivateKey) -> str:
    """The lower-case 0x address of a private key."""
    public = key.public_key.format(compressed=False)[1:]
    return "0x" + keccak256(public)[-20:].hex()


def action_hash(action: dict, nonce: int, vault_address: str | None = None) -> bytes:
    data = msgpack.packb(action) + nonce.to_bytes(8, "big")
    if vault_address is None:
        data += b"\x00"
    else:
        data += b"\x01" + bytes.fromhex(
            vault_address[2:] if vault_address.startswith("0x") else vault_address
        )
    return keccak256(data)


def l1_digest(action: dict, nonce: int, is_mainnet: bool = True) -> bytes:
    """The EIP-712 digest a key signs for one exchange action."""
    connection_id = action_hash(action, nonce)
    source = b"a" if is_mainnet else b"b"
    struct_hash = keccak256(_AGENT_TYPE_HASH + keccak256(source) + connection_id)
    return keccak256(b"\x19\x01" + _DOMAIN_SEPARATOR + struct_hash)


def sign_l1_action(key: PrivateKey, action: dict, nonce: int, is_mainnet: bool = True) -> dict:
    """Sign an exchange action and return the signature as Hyperliquid takes it."""
    signature = key.sign_recoverable(l1_digest(action, nonce, is_mainnet), hasher=None)
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:64], "big")
    return {"r": hex(r), "s": hex(s), "v": 27 + signature[64]}
