"""ProjectX contract list -> OpenAlgo SymToken table.

ProjectX lists the contracts a trader can chart and trade with
``POST /api/Contract/available``. Each entry looks like::

    {"id": "CON.F.US.EP.Z25", "name": "ESZ5", "description": "E-mini S&P 500: December 2025",
     "tickSize": 0.25, "tickValue": 12.5, "activeContract": true, "symbolId": "F.US.EP"}

Mapping onto SymToken:

    symbol / brsymbol  name ("ESZ5"), what the TopstepX platform shows
    name               the product root ("ES"), the name minus its month code and year
    exchange           "CME" (CME Group: CME, CBOT, NYMEX and COMEX contracts alike)
    brexchange         "CME"
    token              id ("CON.F.US.EP.Z25"), what every ProjectX call takes
    expiry             contract month as MMM-YY ("DEC-25"); ProjectX does not publish the day
    lotsize            1 (one contract)
    tick_size          tickSize
    instrumenttype     "FUT"
"""

import os
import re
from datetime import UTC, datetime, timezone

import pandas as pd
from sqlalchemy import Column, Float, Index, Integer, Sequence, String
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker

from broker.projectx.api.baseurl import EXCHANGE, ProjectXError, post, use_live_data
from database.auth_db import Auth, get_auth_token
from database.engine_factory import create_db_engine
from extensions import socketio
from utils.logging import get_logger

logger = get_logger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")

engine = create_db_engine(DATABASE_URL)
db_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
Base = declarative_base()
Base.query = db_session.query_property()

_MONTH_CODES = {
    "F": "JAN",
    "G": "FEB",
    "H": "MAR",
    "J": "APR",
    "K": "MAY",
    "M": "JUN",
    "N": "JUL",
    "Q": "AUG",
    "U": "SEP",
    "V": "OCT",
    "X": "NOV",
    "Z": "DEC",
}
_CONTRACT_NAME = re.compile(r"^(?P<root>[A-Z0-9]+?)(?P<month>[FGHJKMNQUVXZ])(?P<year>\d{1,2})$")


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

    __table_args__ = (Index("idx_symbol_exchange", "symbol", "exchange"),)


def init_db():
    logger.info("Initializing Master Contract DB")
    Base.metadata.create_all(bind=engine)


def delete_symtoken_table():
    logger.info("Deleting Symtoken Table")
    SymToken.query.delete()
    db_session.commit()


def copy_from_dataframe(df):
    """Insert the rows of ``df`` into an emptied symtoken table."""
    records = df.to_dict(orient="records")
    try:
        if records:
            db_session.bulk_insert_mappings(SymToken, records)
            db_session.commit()
        logger.info(f"Inserted {len(records)} ProjectX contracts")
    except Exception as exc:
        logger.exception(f"Error inserting ProjectX contracts: {exc}")
        db_session.rollback()
        raise


def _stored_token():
    """The stored ProjectX session token (OpenAlgo is single user, so one row)."""
    login_username = os.getenv("LOGIN_USERNAME")
    if login_username:
        token = get_auth_token(login_username)
        if token:
            return token
    auth_obj = Auth.query.filter_by(broker="projectx", is_revoked=False).first()
    if auth_obj:
        return get_auth_token(auth_obj.name)
    return None


def split_contract_name(name: str) -> tuple[str, str]:
    """Split a ProjectX contract name into its product root and MMM-YY expiry.

    ``"ESZ5"`` gives ``("ES", "DEC-25")`` and ``"MNQH26"`` gives ``("MNQ", "MAR-26")``.
    A single-digit year is read as the nearest year ending in that digit that
    is not more than a year in the past, which is how futures exchanges reuse
    them. A name that does not parse gives ``(name, "")``.
    """
    match = _CONTRACT_NAME.match(name or "")
    if not match:
        return name, ""
    root = match.group("root")
    month = _MONTH_CODES[match.group("month")]
    year_digits = match.group("year")
    if len(year_digits) == 2:
        year = int(year_digits)
    else:
        this_year = datetime.now(UTC).year
        decade = this_year - this_year % 10
        candidate = decade + int(year_digits)
        if candidate < this_year - 1:
            candidate += 10
        year = candidate % 100
    return root, f"{month}-{year:02d}"


def contracts_to_dataframe(contracts: list[dict]) -> pd.DataFrame:
    """Turn the ProjectX contract list into SymToken rows."""
    rows = []
    seen = set()
    for contract in contracts:
        contract_id = contract.get("id")
        name = (contract.get("name") or "").strip().upper()
        if not contract_id or not name or contract_id in seen:
            continue
        seen.add(contract_id)
        root, expiry = split_contract_name(name)
        try:
            tick_size = float(contract.get("tickSize") or 0)
        except (TypeError, ValueError):
            tick_size = 0.0
        rows.append(
            {
                "symbol": name,
                "brsymbol": name,
                "name": root,
                "exchange": EXCHANGE,
                "brexchange": EXCHANGE,
                "token": str(contract_id),
                "expiry": expiry,
                "strike": 0.0,
                "lotsize": 1,
                "instrumenttype": "FUT",
                "tick_size": tick_size,
            }
        )
    return pd.DataFrame(
        rows,
        columns=[
            "symbol",
            "brsymbol",
            "name",
            "exchange",
            "brexchange",
            "token",
            "expiry",
            "strike",
            "lotsize",
            "instrumenttype",
            "tick_size",
        ],
    )


def fetch_contracts(token: str) -> list[dict]:
    """Every contract ProjectX lists for this account's data subscription."""
    body = post("/api/Contract/available", {"live": use_live_data()}, token=token)
    contracts = body.get("contracts") or []
    if not contracts and use_live_data():
        # The live subscription lists nothing for an account that only has
        # sim data; the sim list is the one such an account can chart.
        logger.warning("ProjectX listed no live contracts; reading the sim contract list")
        body = post("/api/Contract/available", {"live": False}, token=token)
        contracts = body.get("contracts") or []
    return contracts


def master_contract_download():
    """Download the ProjectX contract list and rebuild the symtoken table."""
    logger.info("Downloading ProjectX contract list")
    try:
        token = _stored_token()
        if not token:
            return socketio.emit(
                "master_contract_download",
                {
                    "status": "error",
                    "message": "Log in to ProjectX before downloading its contracts.",
                },
            )

        token_df = contracts_to_dataframe(fetch_contracts(token))
        if token_df.empty:
            # Keep the previous list rather than leave the chart with nothing.
            return socketio.emit(
                "master_contract_download",
                {
                    "status": "error",
                    "message": "ProjectX listed no contracts. The previous list was kept.",
                },
            )

        delete_symtoken_table()
        copy_from_dataframe(token_df)
        return socketio.emit(
            "master_contract_download",
            {"status": "success", "message": f"Downloaded {len(token_df)} ProjectX contracts"},
        )
    except ProjectXError as exc:
        return socketio.emit("master_contract_download", {"status": "error", "message": str(exc)})
    except Exception as exc:
        logger.exception(f"Error during ProjectX master contract download: {exc}")
        return socketio.emit(
            "master_contract_download",
            {"status": "error", "message": "The ProjectX contract list could not be downloaded."},
        )


def search_symbols(symbol, exchange):
    """Contracts whose symbol contains ``symbol`` on ``exchange``."""
    return SymToken.query.filter(
        SymToken.symbol.like(f"%{symbol}%"), SymToken.exchange == exchange
    ).all()
