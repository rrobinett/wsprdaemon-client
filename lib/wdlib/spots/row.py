"""Canonical `wspr.spots` row dataclass.

Mirrors the upstream wsprdaemon-server ClickHouse schema for
`wspr.spots`, with one local-only addition (`uploaded_at`, populated
by wd-upload-wsprnet on successful POST).

When hamsci_ch.Writer serializes a Row, it goes into pending_uploads
as JSON; the hs-uploader translates JSON → columnar at sync time
(see docs/PIPELINE-V2-DESIGN.md for the upload-side query).
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional


SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Row:
    """One decoded spot.

    `time` is the slot start (UTC, second resolution).  All
    decoder-source fields use the decoder's reported value — no
    normalization, no rounding.  Parsers in `wdlib.spots.parsers`
    produce Row instances directly from decoder stdout / file output.
    """
    # Identity / slot
    time: datetime
    band: str
    mode: str            # "W2", "W15", "F2", "F5", "FST4"
    radiod_id: str       # which radiod produced the IQ
    host_id: str         # hostname (multi-station correlation)

    # Decoded payload
    frequency_hz: int    # absolute Rx Hz
    callsign: str
    grid: str            # "" if no locator (type-2 spot)
    snr_db: int          # decoder-reported SNR
    dt: float            # seconds within slot
    drift_hz_per_s: float
    pwr_dbm: int

    # Decoder metadata
    sync_quality: float
    decoder_kind: str    # "wsprd" or "jt9"
    decoder_depth: int   # `-d` flag (typically 3)
    type_2_3: int        # wsprd type-2/3 distinction (column 17)

    # Station identity (local config)
    rx_call: str
    rx_grid: str

    # Lifecycle
    schema_version: int = SCHEMA_VERSION
    uploaded_at: Optional[datetime] = None


def row_to_dict(row: Row) -> dict:
    """JSON-serializable dict for hamsci_ch.Writer.

    Datetimes are rendered as ISO8601 with explicit UTC offset.
    `None` for `uploaded_at` is kept as-is so the upload path can
    distinguish "not yet shipped" from "shipped at unknown time."
    """
    d = asdict(row)
    if isinstance(row.time, datetime):
        d["time"] = _iso_utc(row.time)
    if row.uploaded_at is None:
        d["uploaded_at"] = None
    else:
        d["uploaded_at"] = _iso_utc(row.uploaded_at)
    return d


def _iso_utc(dt: datetime) -> str:
    """ISO8601 string with explicit Z for naive→UTC clarity.

    A tz-naive datetime is interpreted as UTC and tagged Z.  A
    tz-aware datetime is converted to UTC and tagged Z.  Either
    way the output round-trips through `datetime.fromisoformat()`.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
