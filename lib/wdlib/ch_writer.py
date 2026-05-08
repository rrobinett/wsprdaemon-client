"""Parse `*_wd_spots.txt` files and write rows to the local CH staging tier.

Mirrors the parser in `wsprdaemon-server/tar-bulk-loader.py` so that rows
the edge node writes locally are byte-identical to rows the central
server writes from the same input.  When the upstream parser changes,
update this module *and* SCHEMA_PROVENANCE in sigmond-clickhouse.

This module is import-safe even when `sigmond.hamsci_ch` /
`clickhouse-connect` are not installed: the import is lazy inside
`build_writer()`.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


# ── Path metadata extraction ────────────────────────────────────────────────

def parse_spots_path(path: Path) -> Dict[str, Any]:
    """Extract (rx_site, receiver, band) from a posting-dir path.

    The wsprdaemon convention is:
      /var/spool/wsprdaemon/posting/<site>/<receiver>/<band>/<file>.txt
    where <site> is `<call>/<grid>` (e.g. `AC0G/EN16ov`) — the slash is
    URL-encoded as `%2F` on disk, but in the .tbz upload it round-trips
    cleanly.  We accept either form.
    """
    parts = path.parts
    # Walk up at most 4 levels looking for the conventional layout.
    band = parts[-2] if len(parts) >= 2 else ""
    receiver = parts[-3] if len(parts) >= 3 else ""
    site_raw = parts[-4] if len(parts) >= 4 else ""
    site = site_raw.replace("%2F", "/").replace("__", "/")
    if "/" in site:
        rx_call, rx_grid = site.split("/", 1)
    else:
        rx_call, rx_grid = site, ""
    return {
        "rx_call":  rx_call,
        "rx_grid":  rx_grid,
        "receiver": receiver,
        "band":     band,
    }


def band_str_to_meters(band_str: str) -> Optional[int]:
    """`'20'` → 20, `'60eu'` → 60.  Returns None on unparseable input."""
    digits: List[str] = []
    for ch in band_str:
        if ch.isdigit():
            digits.append(ch)
        else:
            break
    if not digits:
        return None
    try:
        return int("".join(digits))
    except ValueError:
        return None


# ── Spot line parser (mirrors tar-bulk-loader) ──────────────────────────────

def parse_wd_spots_line(line: str) -> Optional[Dict[str, Any]]:
    """Parse one 34-field `_wd_spots.txt` line.  Returns None on failure.

    Field layout (from `wd-extend-spots`):
      0: YYMMDD     1: HHMM
      2: sync_quality   3: snr   4: dt   5: freq_mhz
      6: tx_sign    7: tx_loc    8: power_dbm   9: drift
      10: decode_cycles  11: jitter   12: blocksize  13: metric
      14: osd_decode    15: ipass    16: nhardmin   17: code
      18: rms_noise     19: c2_noise
      20: band_m
      21: rx_loc        22: rx_sign  23: distance
      24: rx_azimuth    25: rx_lat   26: rx_lon
      27: azimuth       28: tx_lat   29: tx_lon
      30: v_lat         31: v_lon
      32: ov_count      33: proxy_upload
    """
    parts = line.strip().split()
    if len(parts) < 34:
        return None
    try:
        return {
            "date":          parts[0],
            "time_str":      parts[1],
            "sync_quality":  float(parts[2]),
            "snr":           int(float(parts[3])),
            "dt":            float(parts[4]),
            "freq_hz":       float(parts[5]) * 1_000_000.0,
            "tx_sign":       parts[6],
            "tx_loc":        parts[7] if parts[7].lower() != "none" else "",
            "power_dbm":     int(float(parts[8])),
            "drift":         int(float(parts[9])),
            "decode_cycles": int(float(parts[10])),
            "jitter":        int(float(parts[11])),
            "blocksize":     int(float(parts[12])),
            "metric":        int(float(parts[13])),
            "osd_decode":    int(float(parts[14])),
            "ipass":         int(float(parts[15])),
            "nhardmin":      int(float(parts[16])),
            "code":          int(float(parts[17])),
            "rms_noise":     float(parts[18]),
            "c2_noise":      float(parts[19]),
            "band_m":        int(float(parts[20])),
            "rx_loc":        parts[21],
            "rx_sign_file":  parts[22],
            "distance":      int(float(parts[23])),
            "rx_azimuth":    float(parts[24]),
            "rx_lat":        float(parts[25]),
            "rx_lon":        float(parts[26]),
            "azimuth":       float(parts[27]),
            "tx_lat":        float(parts[28]),
            "tx_lon":        float(parts[29]),
            "v_lat":         float(parts[30]),
            "v_lon":         float(parts[31]),
            "ov_count":      int(float(parts[32])),
            "proxy_upload":  int(float(parts[33])),
        }
    except (ValueError, IndexError):
        return None


def spot_to_ch_row(
    spot: Dict[str, Any],
    *,
    band: int,
    rx_sign: str,
    rx_id: str,
    client_version: Optional[str] = None,
) -> Dict[str, Any]:
    """Map a parsed spot dict onto a wspr.spots row.

    Mirrors `convert_spot_to_clickhouse` in tar-bulk-loader.py so the
    same input produces identical rows on edge and central CH.

    `tx_sign` is normalised through ``CallHashTable.normalise_brackets``
    when the ``callhash`` library is available so wsprd's bracketed
    compound-callsign output (``<K1ABC/QRP>``) lands as plain
    ``K1ABC/QRP`` in the row, and the literal unresolved-hash
    placeholder ``<...>`` becomes an empty string (the schema's tx_sign
    column is non-nullable).
    """
    date_str = spot["date"]
    time_str = spot["time_str"]
    ts = datetime(
        2000 + int(date_str[0:2]),
        int(date_str[2:4]),
        int(date_str[4:6]),
        int(time_str[0:2]),
        int(time_str[2:4]),
    )
    freq_hz = spot["freq_hz"]
    raw_tx_sign = spot.get("tx_sign", "") or ""
    tx_sign = _normalise_call_brackets(raw_tx_sign)
    return {
        "time":          ts,
        "band":          band,
        "rx_sign":       rx_sign,
        "rx_lat":        spot.get("rx_lat", 0.0),
        "rx_lon":        spot.get("rx_lon", 0.0),
        "rx_loc":        spot.get("rx_loc", ""),
        "tx_sign":       tx_sign,
        "tx_loc":        spot.get("tx_loc", ""),
        "tx_lat":        spot.get("tx_lat", 0.0),
        "tx_lon":        spot.get("tx_lon", 0.0),
        "distance":      spot.get("distance", 0),
        "azimuth":       int(spot.get("azimuth", 0)),
        "rx_azimuth":    int(spot.get("rx_azimuth", 0)),
        "frequency":     int(freq_hz),
        "frequency_mhz": freq_hz / 1_000_000.0,
        "power":         spot["power_dbm"],
        "snr":           spot["snr"],
        "drift":         spot.get("drift", 0),
        "rx_id":         rx_id,
        "dt":            spot["dt"],
        "sync_quality":  int(spot.get("sync_quality", 0)),
        "decode_cycles": spot.get("decode_cycles", 0),
        "jitter":        spot.get("jitter", 0),
        "blocksize":     spot.get("blocksize", 0),
        "metric":        spot.get("metric", 0),
        "osd_decode":    spot.get("osd_decode", 0),
        "nhardmin":      spot.get("nhardmin", 0),
        "ipass":         spot.get("ipass", 0),
        "code":          spot.get("code", 0),
        "rms_noise":     spot.get("rms_noise", 0.0),
        "c2_noise":      spot.get("c2_noise", 0.0),
        "v_lat":         spot.get("v_lat", 0.0),
        "v_lon":         spot.get("v_lon", 0.0),
        "ov_count":      spot.get("ov_count", 0),
        "proxy_upload":  spot.get("proxy_upload", 0),
        "band_m":        spot.get("band_m", band),
        "version":       client_version,
        "rx_status":     "No Info",
    }


def rows_from_spots_file(
    path: Path,
    *,
    rx_id: Optional[str] = None,
    client_version: Optional[str] = None,
    callhash_table: Optional[Any] = None,
) -> Iterable[Dict[str, Any]]:
    """Yield wspr.spots rows for every parseable line in `path`.

    When ``callhash_table`` is supplied (a
    ``callhash.CallHashTable`` instance), every raw spot line
    is fed to ``observe()`` so any ``<call>`` markers wsprd emitted
    while resolving hashes from its session table are accumulated in
    the operator-side cache.  Persistence (load/save) is the caller's
    concern — the table is observed in place.
    """
    meta = parse_spots_path(path)
    band_m = band_str_to_meters(meta["band"]) or 0
    rx_sign = meta["rx_call"]
    rx_id_resolved = rx_id or meta["receiver"]
    with open(path) as fh:
        for line in fh:
            if callhash_table is not None:
                try:
                    callhash_table.observe(line)
                except Exception:
                    pass
            spot = parse_wd_spots_line(line)
            if spot is None:
                continue
            yield spot_to_ch_row(
                spot,
                band=band_m,
                rx_sign=rx_sign,
                rx_id=rx_id_resolved,
                client_version=client_version,
            )


# ── Bracket normalisation (callhash bridge) ────────────────────────────────

def _normalise_call_brackets(raw: str) -> str:
    """Strip WSJT-X angle-bracket markers from a callsign field.

    Delegates to ``callhash.CallHashTable.normalise_brackets`` when
    available (canonical implementation, used by both psk-recorder and
    wsprdaemon-client).  Falls back to a small inline equivalent when
    the callhash library isn't installed so wsprdaemon-client stays
    runnable standalone.

    Behaviour:
      * ``<K1ABC>`` or ``<K1ABC/QRP>`` → strip brackets, return inner.
      * ``<...>`` literal placeholder  → return ``""`` (schema's
        ``tx_sign`` column is non-nullable; empty is the data-quality
        signal that the decoder couldn't resolve the hash).
      * Anything else (already plain) → passthrough.
    """
    if not raw:
        return raw
    try:
        from callhash import CallHashTable  # type: ignore[import-not-found]
        result = CallHashTable.normalise_brackets(raw)
    except ImportError:
        # Inline fallback — keep the data-quality fix even if the
        # callhash library isn't available.
        s = raw.strip()
        if s == "<...>":
            return ""
        if s.startswith("<") and s.endswith(">") and len(s) > 2:
            return s[1:-1]
        return raw
    return "" if result is None else result


# ── Writer factory ──────────────────────────────────────────────────────────

def build_writer(*, batch_rows: int = 50_000) -> Any:
    """Return a configured `sigmond.hamsci_ch.Writer` for `wspr.spots`.

    Lazy import so installs that don't opt into CH (no
    `sigmond[clickhouse]`, no SIGMOND_CLICKHOUSE_URL) never trigger the
    `clickhouse-connect` import.  When the URL is unset the writer is a
    no-op and `insert()` is a free call.
    """
    from sigmond.hamsci_ch import Writer
    return Writer.from_env(
        table="spots",
        mode="wspr",
        schema_version=1,
        batch_rows=batch_rows,
    )
