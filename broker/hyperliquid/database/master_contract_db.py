"""Hyperliquid's instrument list, written to the symtoken table.

Two sources, both public:

- ``metaAndAssetCtxs``: the main perpetuals. ``BTC`` becomes ``BTCUSDFUT``
  (instrument type ``PERPFUT``), the same shape Delta Exchange perpetuals use,
  so a strategy written against one reads the other. The token is the asset
  index, which is also Hyperliquid's asset id for orders.
- ``spotMetaAndAssetCtxs``: spot pairs. ``PURR/USDC`` becomes ``PURRUSDC``
  (``SPOT``). Most pairs are named ``@<index>`` on Hyperliquid, so the symbol is
  built from the base and quote token names. The token is ``10000 + index``.

``brsymbol`` keeps the exact coin name Hyperliquid's candle, book and WebSocket
requests expect (``BTC``, ``PURR/USDC``, ``@107``). Builder-deployed perpetual
markets (HIP-3, such as ``xyz:AAPL``) are not listed yet.
"""

import math
import os
import re

import pandas as pd
from sqlalchemy import Column, Float, Index, Integer, Sequence, String
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker

from broker.hyperliquid.api.client import (
    SPOT_ASSET_OFFSET,
    perp_meta_and_contexts,
    spot_meta_and_contexts,
)
from database.engine_factory import create_db_engine
from extensions import socketio
from utils.logging import get_logger

logger = get_logger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_db_engine(DATABASE_URL)
db_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
Base = declarative_base()
Base.query = db_session.query_property()

EXCHANGE = "CRYPTO"
BREXCHANGE = "HYPERLIQUID"

# Hyperliquid prices carry at most 5 significant figures and at most
# 6 - szDecimals decimals on perpetuals (8 - szDecimals on spot).
_MAX_DECIMALS = {"PERPFUT": 6, "SPOT": 8}
_NON_ALNUM = re.compile(r"[^A-Z0-9]")


class SymToken(Base):
    __tablename__ = "symtoken"
    id = Column(Integer, Sequence("symtoken_id_seq"), primary_key=True)
    symbol = Column(String, nullable=False, index=True)
    brsymbol = Column(String, nullable=False, index=True)
    name = Column(String)
    exchange = Column(String, index=True)
    brexchange = Column(String, index=True)
    token = Column(String, index=True)
    expiry = Column(String)
    strike = Column(Float)
    lotsize = Column(Integer)
    instrumenttype = Column(String)
    tick_size = Column(Float)
    contract_value = Column(Float)

    __table_args__ = (Index("idx_symbol_exchange", "symbol", "exchange"),)


def init_db():
    logger.info("Initializing Master Contract DB")
    Base.metadata.create_all(bind=engine)


def delete_symtoken_table():
    logger.info("Deleting Symtoken Table")
    SymToken.query.delete()
    db_session.commit()


def copy_from_dataframe(df):
    """Insert the rows in chunks, committing each so a lock is never held long."""
    rows = df.to_dict(orient="records")
    chunk_size = 500
    try:
        for i in range(0, len(rows), chunk_size):
            db_session.bulk_insert_mappings(SymToken, rows[i : i + chunk_size])
            db_session.commit()
        logger.info(f"Inserted {len(rows)} Hyperliquid instruments")
    except Exception:
        logger.exception("Error inserting Hyperliquid instruments")
        db_session.rollback()
        raise


def tick_size_for(price, sz_decimals: int, instrument_type: str) -> float:
    """The price step at the current price.

    Hyperliquid has no fixed tick: a price may carry 5 significant figures, and
    no more decimals than the market allows. The step is whichever of the two
    is coarser, worked out from the current mid or mark price.
    """
    max_decimals = max(_MAX_DECIMALS.get(instrument_type, 6) - int(sz_decimals or 0), 0)
    floor_step = 10.0**-max_decimals
    try:
        px = float(price)
    except (TypeError, ValueError):
        px = 0.0
    if px <= 0:
        return floor_step
    sig_step = 10.0 ** (math.floor(math.log10(px)) - 4)
    step = max(sig_step, floor_step)
    # Integer prices are always valid, so the step never exceeds 1.
    return float(f"{min(step, 1.0):.10g}")


def _clean(name: str) -> str:
    return _NON_ALNUM.sub("", (name or "").upper())


def perp_rows(universe: list, contexts: list) -> list[dict]:
    rows = []
    for index, asset in enumerate(universe):
        if not isinstance(asset, dict) or asset.get("isDelisted"):
            continue
        coin = asset.get("name") or ""
        base = _clean(coin)
        if not base:
            continue
        ctx = contexts[index] if index < len(contexts) and isinstance(contexts[index], dict) else {}
        sz_decimals = int(asset.get("szDecimals") or 0)
        rows.append(
            {
                "symbol": f"{base}USDFUT",
                "brsymbol": coin,
                "name": base,
                "exchange": EXCHANGE,
                "brexchange": BREXCHANGE,
                "token": str(index),
                "expiry": "",
                "strike": 0.0,
                "lotsize": 10.0**-sz_decimals,
                "instrumenttype": "PERPFUT",
                "tick_size": tick_size_for(
                    ctx.get("midPx") or ctx.get("markPx"), sz_decimals, "PERPFUT"
                ),
                "contract_value": 1.0,
            }
        )
    return rows


def spot_rows(spot_meta: dict, contexts: list) -> list[dict]:
    tokens = {t.get("index"): t for t in spot_meta.get("tokens", []) if isinstance(t, dict)}
    ctx_by_coin = {c.get("coin"): c for c in contexts if isinstance(c, dict)}
    rows = []
    for pair in spot_meta.get("universe", []):
        if not isinstance(pair, dict):
            continue
        pair_tokens = pair.get("tokens") or []
        if len(pair_tokens) != 2:
            continue
        base_token, quote_token = tokens.get(pair_tokens[0]), tokens.get(pair_tokens[1])
        if not base_token or not quote_token:
            continue
        base, quote = _clean(base_token.get("name")), _clean(quote_token.get("name"))
        coin = pair.get("name") or ""
        if not base or not quote or not coin:
            continue
        sz_decimals = int(base_token.get("szDecimals") or 0)
        ctx = ctx_by_coin.get(coin, {})
        rows.append(
            {
                "symbol": f"{base}{quote}",
                "brsymbol": coin,
                "name": base,
                "exchange": EXCHANGE,
                "brexchange": BREXCHANGE,
                "token": str(SPOT_ASSET_OFFSET + int(pair.get("index", 0))),
                "expiry": "",
                "strike": 0.0,
                "lotsize": 10.0**-sz_decimals,
                "instrumenttype": "SPOT",
                "tick_size": tick_size_for(
                    ctx.get("midPx") or ctx.get("markPx"), sz_decimals, "SPOT"
                ),
                "contract_value": 1.0,
                # Canonical pairs win a name clash with a later look-alike token.
                "_canonical": bool(pair.get("isCanonical")),
            }
        )
    return rows


def build_dataframe(universe, perp_contexts, spot_meta, spot_contexts) -> pd.DataFrame:
    rows = perp_rows(universe, perp_contexts)
    spot = spot_rows(spot_meta, spot_contexts) if spot_meta else []
    if spot:
        spot_df = pd.DataFrame(spot).sort_values("_canonical", ascending=False, kind="stable")
        spot_df = spot_df.drop_duplicates(subset=["symbol"], keep="first").drop(
            columns="_canonical"
        )
        rows.extend(spot_df.to_dict(orient="records"))
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.drop_duplicates(subset=["symbol"], keep="first").drop_duplicates(
            subset=["token"], keep="first"
        )
    return df


def master_contract_download():
    """Download Hyperliquid's perpetuals and spot pairs and replace symtoken."""
    logger.info("Downloading Master Contract from Hyperliquid")
    try:
        universe, perp_contexts = perp_meta_and_contexts()
        try:
            spot_meta, spot_contexts = spot_meta_and_contexts()
        except Exception:
            logger.exception("Hyperliquid spot list unavailable; loading perpetuals only")
            spot_meta, spot_contexts = {}, []

        df = build_dataframe(universe, perp_contexts, spot_meta, spot_contexts)
        if df.empty:
            return socketio.emit(
                "master_contract_download",
                {"status": "error", "message": "Hyperliquid returned no instruments."},
            )

        delete_symtoken_table()
        copy_from_dataframe(df)
        return socketio.emit(
            "master_contract_download",
            {
                "status": "success",
                "message": f"Successfully Downloaded {len(df)} Hyperliquid Instruments",
            },
        )
    except Exception as e:
        logger.exception(f"Error during Hyperliquid master contract download: {e}")
        return socketio.emit(
            "master_contract_download",
            {
                "status": "error",
                "message": "The Hyperliquid symbol list could not be downloaded. "
                "Log in again in a minute to retry.",
            },
        )


def search_symbols(symbol, exchange):
    return SymToken.query.filter(
        SymToken.symbol.like(f"%{symbol}%"), SymToken.exchange == exchange
    ).all()
