"""Parsers for wsprd output.

Two file formats live alongside the WAV in each band's recording
directory:

  * ``ALL_WSPR.TXT`` — wsprd's canonical 17-field append-only log.
    One line per decoded spot, all metrics.  Authoritative.

  * ``wspr_spots.txt`` — wsprdaemon's per-cycle digest (subset of
    ALL_WSPR.TXT), 12 fields.  Easier to parse, lossier.  We
    accept it as a fallback in case ALL_WSPR.TXT is unavailable.

Both formats embed the slot start time in the first two fields
(YYMMDD HHMM); we tag it tz-aware as UTC since wsprd writes UTC.

Sample ALL_WSPR.TXT line (the leading whitespace is intentional —
some columns can be empty strings)::

  260512 1406 -12 -1.94  14.0971382  <KM4BWW> EM60OJ 23      0  0.38  1  1    0  0  11     1   419

Field layout (1-indexed in wsprdaemon docs; 0-indexed here):

  [0]  YYMMDD          decimal date (UTC)
  [1]  HHMM            slot start hh:mm (UTC, second = :00)
  [2]  SNR             signed integer dB
  [3]  DT              float, seconds within slot
  [4]  FREQ_MHZ        float MHz (absolute Rx frequency)
  [5]  CALL            callsign (possibly `<hashed>` for type-3 spots)
  [6]  GRID            4 or 6 char Maidenhead, OR PWR (for type-2 spots
                       with no locator).  Disambiguated by lexical shape.
  [7]  PWR  *or*  shifted-down field if [6] was PWR
  ...
  [16] type-2/3 marker (UInt8)

Compound calls with no transmitted grid (`W4UK/P`) produce 16-field
lines instead of 17 — column [6] is then PWR, not GRID.  We detect
this via the Maidenhead grid regex (same approach as the parser in
`smd watch wspr`).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from ..row import Row


# 4- or 6-char Maidenhead locator.  WSPR ALL.TXT renders extended
# (6-char) grids in UPPERCASE, e.g. ``EN21BE``; the formal convention
# is lowercase.  Accept both so type-3 hashed-call spots like
# ``<KB0VYG/P> EN21BE 23 0 3`` parse correctly.
_GRID_RE = re.compile(r"^[A-R]{2}[0-9]{2}([A-Xa-x]{2})?$")


def parse_all_wspr_line(
    line: str,
    *,
    band: str,
    radiod_id: str,
    host_id: str,
    rx_call: str,
    rx_grid: str,
    mode: str = "W2",
    decoder_depth: int = 3,
) -> Optional[Row]:
    """Parse one ALL_WSPR.TXT line into a Row.  Returns None on failure.

    The slot context (band, radiod_id, host_id, rx_call, rx_grid,
    mode, decoder_depth) is supplied by the caller because the line
    itself doesn't carry it — wsprd is invoked per-band so the caller
    always knows.
    """
    parts = line.split()
    if len(parts) < 16:
        # Even the most minimal compound-call line has 16 columns.
        return None

    try:
        ts = datetime.strptime(
            parts[0] + parts[1], "%y%m%d%H%M"
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None

    try:
        snr_db = int(parts[2])
        dt = float(parts[3])
        freq_mhz = float(parts[4])
    except ValueError:
        return None

    callsign = parts[5]

    # Disambiguate GRID vs PWR at column 6.  If it looks like a
    # Maidenhead grid, it's the grid; otherwise this is a compound-
    # callsign spot with no locator and parts[6] is actually PWR.
    if _GRID_RE.match(parts[6]):
        grid = parts[6]
        off = 7
    else:
        grid = ""
        off = 6

    try:
        pwr_dbm = int(parts[off])
        drift = int(parts[off + 1])      # wsprd drift is integer Hz/min
        sync_quality = float(parts[off + 2])
    except (ValueError, IndexError):
        return None

    # wsprd's drift field is Hz/minute, but our schema is Hz/sec for
    # parity with jt9.  Convert.
    drift_hz_per_s = drift / 60.0

    # Type-2/3 marker is the LAST field on the line.
    try:
        type_2_3 = int(parts[-1])
    except ValueError:
        type_2_3 = 0

    return Row(
        time=ts,
        band=band,
        mode=mode,
        radiod_id=radiod_id,
        host_id=host_id,
        frequency_hz=int(round(freq_mhz * 1_000_000)),
        callsign=callsign,
        grid=grid,
        snr_db=snr_db,
        dt=dt,
        drift_hz_per_s=drift_hz_per_s,
        pwr_dbm=pwr_dbm,
        sync_quality=sync_quality,
        decoder_kind="wsprd",
        decoder_depth=decoder_depth,
        type_2_3=type_2_3,
        rx_call=rx_call,
        rx_grid=rx_grid,
    )


def parse_wspr_spots_line(
    line: str,
    *,
    band: str,
    radiod_id: str,
    host_id: str,
    rx_call: str,
    rx_grid: str,
    mode: str = "W2",
    decoder_depth: int = 3,
) -> Optional[Row]:
    """Parse one wspr_spots.txt digest line into a Row.

    Sample::

      260512 1406   2 -22 -0.1  14.097002  NM7J DM26 23            0     1    0

    Columns (subset of ALL_WSPR.TXT):

      [0] YYMMDD
      [1] HHMM
      [2] sync                (Int)
      [3] SNR                 (Int)
      [4] DT                  (float)
      [5] FREQ_MHZ            (float)
      [6] CALL
      [7] GRID-or-PWR         (same disambiguation as ALL_WSPR.TXT)
      [8] PWR-or-DRIFT
      [9] (drift or filler)
      [10] (sync-related ?)
      [11] type_2_3 marker

    Drift here is integer Hz/minute, sync is the per-band sync score.
    """
    parts = line.split()
    if len(parts) < 11:
        return None

    try:
        ts = datetime.strptime(
            parts[0] + parts[1], "%y%m%d%H%M"
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None

    try:
        sync_quality = float(parts[2])
        snr_db = int(parts[3])
        dt = float(parts[4])
        freq_mhz = float(parts[5])
    except ValueError:
        return None

    callsign = parts[6]
    if _GRID_RE.match(parts[7]):
        grid = parts[7]
        off = 8
    else:
        grid = ""
        off = 7

    try:
        pwr_dbm = int(parts[off])
        drift = int(parts[off + 1])
    except (ValueError, IndexError):
        return None
    drift_hz_per_s = drift / 60.0

    try:
        type_2_3 = int(parts[-1])
    except ValueError:
        type_2_3 = 0

    return Row(
        time=ts,
        band=band,
        mode=mode,
        radiod_id=radiod_id,
        host_id=host_id,
        frequency_hz=int(round(freq_mhz * 1_000_000)),
        callsign=callsign,
        grid=grid,
        snr_db=snr_db,
        dt=dt,
        drift_hz_per_s=drift_hz_per_s,
        pwr_dbm=pwr_dbm,
        sync_quality=sync_quality,
        decoder_kind="wsprd",
        decoder_depth=decoder_depth,
        type_2_3=type_2_3,
        rx_call=rx_call,
        rx_grid=rx_grid,
    )
