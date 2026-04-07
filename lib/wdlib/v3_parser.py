"""Parse wsprdaemon v3 bash-format configuration files.

Supports the RECEIVER_LIST global array and per-hostname case blocks that
contain WSPR_SCHEDULE declarations.  The hostname used defaults to the
current machine's hostname but can be overridden for testing.
"""

import re
import socket
from pathlib import Path
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_v3_config(config_path: str, hostname: Optional[str] = None) -> dict:
    """Parse a v3 wsprdaemon.conf and return a normalised dict.

    Returned dict shape::

        {
            'hostname':      str,
            'receivers':     [ {name, address, call, grid, password}, ... ],
            'host_settings': {KEY: VALUE, ...},
            'schedule':      [
                {
                    'time':    'HH:MM',
                    'entries': [ {receiver, band, modes}, ... ],
                },
                ...
            ],
        }
    """
    if hostname is None:
        hostname = socket.gethostname()

    text = Path(config_path).read_text()

    return {
        'hostname':      hostname,
        'receivers':     _parse_receiver_list(text),
        'host_settings': _parse_host_settings(text, hostname),
        'schedule':      _parse_schedule(text, hostname),
    }


# ---------------------------------------------------------------------------
# RECEIVER_LIST
# ---------------------------------------------------------------------------

def _parse_receiver_list(text: str) -> List[dict]:
    """Extract all non-commented entries from the RECEIVER_LIST bash array."""
    m = re.search(r'declare\s+RECEIVER_LIST\s*=\s*\((.*?)\)', text, re.DOTALL)
    if not m:
        return []

    block = m.group(1)
    receivers = []

    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith('#'):
            continue
        qm = re.search(r'"([^"]+)"', stripped)
        if not qm:
            continue
        entry = qm.group(1).strip()
        fields = entry.split()
        if len(fields) < 5:
            continue

        name     = fields[0]
        address  = fields[1]   # multicast DNS name or CSV source list for MERG_
        call     = fields[2]
        grid     = fields[3]
        password = fields[4]

        receivers.append({
            'name':     name,
            'address':  address,
            'call':     call,
            'grid':     grid,
            'password': password,
        })

    return receivers


# ---------------------------------------------------------------------------
# Per-hostname case block
# ---------------------------------------------------------------------------

def _extract_case_block(text: str, hostname: str) -> str:
    """Return the raw text inside the ``hostname)`` ... ``;;`` case block."""
    # Find the line that starts the case block for this hostname.
    # Pattern: beginning-of-line, optional spaces, hostname, then )
    pattern = re.compile(
        r'^\s*' + re.escape(hostname) + r'\s*\)\s*$',
        re.MULTILINE,
    )
    start_m = pattern.search(text)
    if not start_m:
        raise ValueError(f"No case block found for hostname {hostname!r} in config")

    block_start = start_m.end()

    # Find the closing ;; at the same indentation level.
    # We scan line-by-line from block_start.
    rest = text[block_start:]
    # Locate first ;; that ends the case entry
    end_m = re.search(r'^\s*;;\s*$', rest, re.MULTILINE)
    if end_m:
        return rest[: end_m.start()]
    return rest


def _parse_host_settings(text: str, hostname: str) -> dict:
    """Extract scalar variable assignments from the hostname's case block.

    Returns a dict with normalised keys, e.g.::

        {
            'rac':            '27',
            'rac_id':         'AC0G-BEE1',
            'ka9q_conf_name': 'ac0g-bee1-rx888',
            'grape_psws_id':  'S000171_172',
            ...
        }
    """
    block = _extract_case_block(text, hostname)

    # Map v3 variable names → v4 setting names
    key_map = {
        'REMOTE_ACCESS_CHANNEL':  'rac',
        'REMOTE_ACCESS_ID':       'rac_id',
        'KA9Q_CONF_NAME':         'ka9q_conf_name',
        'KA9Q_RUNS_ONLY_REMOTELY': 'ka9q_runs_only_remotely',
        'GRAPE_PSWS_ID':          'grape_psws_id',
        'SIGNAL_LEVEL_UPLOAD':    'signal_level_upload',
        'SIGNAL_LEVEL_UPLOAD_ID': 'signal_level_upload_id',
        'SIGNAL_LEVEL_UPLOAD_GRAPHS': 'signal_level_upload_graphs',
        'CPU_CORE_KHZ':           'cpu_core_khz',
        'WD_CPU_CORES':           'wd_cpu_cores',
        'RADIOD_CPU_CORES':       'radiod_cpu_cores',
    }

    settings: dict = {}
    for v3_key, v4_key in key_map.items():
        # Only match on non-commented lines:
        #   ^<whitespace><VAR>=<value>
        pattern = re.compile(
            r'^[^#\n]*\b' + re.escape(v3_key) + r'\s*=\s*"?([^"\n#]+)"?',
            re.MULTILINE,
        )
        for m in pattern.finditer(block):
            val = m.group(1).strip().strip('"')
            if val:
                settings[v4_key] = val

    return settings


# ---------------------------------------------------------------------------
# WSPR_SCHEDULE
# ---------------------------------------------------------------------------

def _extract_receiver_vars(block: str) -> dict:
    """Extract scalar ``receiver`` or ``receivers`` variables from a case block."""
    vars_: dict = {}

    # declare receiver="KA9Q_SAS2"  or  receiver="KA9Q_SAS2"
    m = re.search(r'(?:declare\s+)?receiver\s*=\s*"([^"]+)"', block)
    if m:
        vars_['receiver'] = m.group(1).strip()

    # receivers=("KA9Q_DXE")  — single element arrays
    m = re.search(r'receivers\s*=\s*\(\s*"([^"]+)"\s*\)', block)
    if m:
        vars_['receiver'] = m.group(1).strip()

    return vars_


def _apply_var_substitution(text: str, variables: dict) -> str:
    """Replace ``${VAR}`` and ``$VAR`` occurrences in text."""
    for name, value in variables.items():
        text = text.replace('${' + name + '}', value)
        text = re.sub(r'\$' + re.escape(name) + r'\b', value, text)
    return text


def _parse_schedule_entries(raw: str) -> List[dict]:
    """Parse a whitespace-separated list of ``RX,BAND,MODES`` tokens.

    The first token may be a ``HH:MM`` time; remaining tokens are
    ``RECEIVER,BAND,MODES`` triples.  Returns a flat list of entry dicts.
    """
    tokens = raw.split()
    if not tokens:
        return []

    time = '00:00'
    start = 0

    if re.match(r'^\d{2}:\d{2}$', tokens[0]):
        time = tokens[0]
        start = 1

    entries = []
    for tok in tokens[start:]:
        parts = tok.split(',')
        if len(parts) != 3:
            continue
        receiver, band, modes = parts
        entries.append({
            'time':     time,
            'receiver': receiver.strip(),
            'band':     band.strip(),
            'modes':    modes.strip(),
        })
    return entries


def _parse_schedule(text: str, hostname: str) -> List[dict]:
    """Extract and parse the WSPR_SCHEDULE from the hostname's case block.

    Returns a list of slot dicts::

        [ {'time': 'HH:MM', 'entries': [{receiver, band, modes}, ...]}, ... ]
    """
    block = _extract_case_block(text, hostname)

    # Substitute ${receiver} etc.
    variables = _extract_receiver_vars(block)
    block = _apply_var_substitution(block, variables)

    # Find  declare WSPR_SCHEDULE=( "..." "..." )
    # The array may span multiple lines and contain multi-line quoted strings.
    m = re.search(r'declare\s+WSPR_SCHEDULE\s*=\s*\((.*?)\)', block, re.DOTALL)
    if not m:
        return []

    array_block = m.group(1)

    # Extract all double-quoted strings from the array block
    raw_entries: List[str] = re.findall(r'"([^"]*)"', array_block)

    # Build slots grouped by time
    slots_by_time: Dict[str, List[dict]] = {}
    for raw in raw_entries:
        for entry in _parse_schedule_entries(raw):
            t = entry['time']
            slots_by_time.setdefault(t, []).append(entry)

    return [
        {'time': t, 'entries': entries}
        for t, entries in slots_by_time.items()
    ]
