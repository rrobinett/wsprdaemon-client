"""Parse wsprdaemon v3 bash-array configuration files.

This module reads the old bash-syntax wsprdaemon.conf (with RECEIVER_LIST,
WSPR_SCHEDULE, etc.) and produces structured Python data that can be
written out as INI-format v4 config.

The v3 format uses bash arrays:
    declare RECEIVER_LIST=(
        "NAME   IP_OR_MULTICAST   CALL   GRID   PASSWORD"
        ...
    )
    declare WSPR_SCHEDULE=(
        "HH:MM  RECEIVER,BAND,MODES  RECEIVER,BAND,MODES  ..."
        ...
    )

This parser uses regex and string splitting — it does NOT eval bash.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass
class V3Receiver:
    """A receiver from the v3 RECEIVER_LIST array."""
    name: str
    address: str       # IP:port or multicast DNS name
    call: str
    grid: str
    password: str      # 'NULL' or actual password
    # v4 management fields — inferred during v3 parse or set explicitly in v4 INI
    locality: str = ''      # 'local' (radiod on this host) | 'remote' (radiod elsewhere)
    radiod_name: str = ''   # e.g. 'k3lr-rx888'; status DNS = <radiod_name>-status.local

    @property
    def receiver_type(self) -> str:
        """Infer receiver type from name prefix."""
        upper = self.name.upper()
        if upper.startswith('KA9Q_') and '_WWV' in upper:
            return 'ka9q_wwv'
        elif upper.startswith('KA9Q_'):
            return 'ka9q'
        elif upper.startswith('KIWI_'):
            return 'kiwi'
        elif upper.startswith('MERG_'):
            return 'merge'
        else:
            return 'unknown'

    @property
    def is_merge(self) -> bool:
        return self.receiver_type == 'merge'

    @property
    def merge_sources(self) -> List[str]:
        """For merge receivers, parse the comma-separated source list."""
        if not self.is_merge:
            return []
        return [s.strip() for s in self.address.split(',')]


@dataclass
class V3ScheduleEntry:
    """A single receiver+band+modes assignment in a schedule slot."""
    receiver: str
    band: str
    modes: str      # Colon-separated, e.g. "W2:F2:F5"


@dataclass
class V3ScheduleSlot:
    """A time slot in the schedule (one line of WSPR_SCHEDULE)."""
    time: str       # "HH:MM" or "00:00" for always-on
    entries: List[V3ScheduleEntry] = field(default_factory=list)


@dataclass
class V3Config:
    """Complete parsed v3 configuration."""
    receivers: Dict[str, V3Receiver] = field(default_factory=dict)
    schedule_slots: List[V3ScheduleSlot] = field(default_factory=list)
    ka9q_conf_name: str = ''
    ka9q_web_dns: str = ''
    rac: str = ''
    cpu_core_khz: str = ''

    # Derived data computed after parsing
    # band → set of modes seen across all schedule entries for that band
    band_modes: Dict[str, set] = field(default_factory=dict)
    # receiver_name → set of bands assigned in schedule
    receiver_bands: Dict[str, set] = field(default_factory=dict)


def _strip_comments(line: str) -> str:
    """Remove bash comments, respecting quoted strings (simple version)."""
    # Find # not inside quotes
    in_quote = False
    quote_char = None
    for i, ch in enumerate(line):
        if ch in ('"', "'") and not in_quote:
            in_quote = True
            quote_char = ch
        elif ch == quote_char and in_quote:
            in_quote = False
        elif ch == '#' and not in_quote:
            return line[:i]
    return line


def _extract_array(text: str, array_name: str) -> List[str]:
    """Extract elements from a bash declare array.

    Handles:
        declare ARRAY_NAME=(
            "element 1"
            "element 2"
        )
    Also handles:
        declare ARRAY_NAME=( "${OTHER_ARRAY[@]}" )
    """
    # First check for array reference like: declare X=( "${Y[@]}" )
    ref_pat = re.compile(
        rf'declare\s+{re.escape(array_name)}\s*=\s*\(\s*'
        rf'"\$\{{(\w+)\[@\]\}}"'
        rf'\s*\)',
        re.DOTALL
    )
    ref_match = ref_pat.search(text)
    if ref_match:
        # Recursively extract the referenced array
        return _extract_array(text, ref_match.group(1))

    # Standard array extraction
    pat = re.compile(
        rf'declare\s+{re.escape(array_name)}\s*=\s*\((.*?)\)',
        re.DOTALL
    )
    match = pat.search(text)
    if not match:
        return []

    body = match.group(1)
    # Extract all double-quoted strings
    elements = re.findall(r'"([^"]*)"', body)
    return elements


def _extract_variable(text: str, var_name: str) -> str:
    """Extract a simple VAR=value or VAR="value" assignment."""
    pat = re.compile(
        rf'^\s*{re.escape(var_name)}\s*=\s*"?([^"\n]*)"?',
        re.MULTILINE
    )
    match = pat.search(text)
    return match.group(1).strip() if match else ''


def parse_receiver_line(line: str) -> V3Receiver:
    """Parse one element of RECEIVER_LIST.

    Format: "NAME   ADDRESS   CALL   GRID   PASSWORD"
    Fields are whitespace-separated.
    """
    parts = line.split()
    if len(parts) < 5:
        raise ValueError(f"Receiver line needs 5 fields, got {len(parts)}: {line!r}")

    return V3Receiver(
        name=parts[0],
        address=parts[1],
        call=parts[2],
        grid=parts[3],
        password=parts[4],
    )


def parse_schedule_line(line: str) -> V3ScheduleSlot:
    """Parse one element of WSPR_SCHEDULE.

    Format: "HH:MM  RX,BAND,MODES  RX,BAND,MODES  ..."
    Each RX,BAND,MODES is comma-separated with no spaces.
    """
    tokens = line.split()
    if not tokens:
        raise ValueError("Empty schedule line")

    time_str = tokens[0]
    entries = []

    for token in tokens[1:]:
        parts = token.split(',')
        if len(parts) < 3:
            continue  # Skip malformed entries
        entries.append(V3ScheduleEntry(
            receiver=parts[0],
            band=parts[1],
            modes=parts[2],
        ))

    return V3ScheduleSlot(time=time_str, entries=entries)


def parse_v3_config(config_path: str) -> V3Config:
    """Parse a complete v3 wsprdaemon.conf file.

    Returns a V3Config with all receivers, schedule entries,
    and derived band/mode mappings.
    """
    text = Path(config_path).read_text()

    # Strip comments (line by line)
    lines = []
    for line in text.splitlines():
        stripped = _strip_comments(line)
        lines.append(stripped)
    clean_text = '\n'.join(lines)

    config = V3Config()

    # Extract simple variables
    config.rac = _extract_variable(clean_text, 'RAC')
    config.ka9q_conf_name = _extract_variable(clean_text, 'KA9Q_CONF_NAME')
    config.ka9q_web_dns = _extract_variable(clean_text, 'KA9Q_WEB_DNS')
    config.cpu_core_khz = _extract_variable(clean_text, 'CPU_CORE_KHZ')

    # Parse RECEIVER_LIST
    receiver_lines = _extract_array(clean_text, 'RECEIVER_LIST')
    for rline in receiver_lines:
        try:
            rx = parse_receiver_line(rline)
            config.receivers[rx.name] = rx
        except ValueError:
            pass  # Skip malformed lines

    # Parse WSPR_SCHEDULE — check for indirection first
    # The config may define WSPR_SCHEDULE_xxx=(...) and then
    # WSPR_SCHEDULE=( "${WSPR_SCHEDULE_xxx[@]}" )
    schedule_lines = _extract_array(clean_text, 'WSPR_SCHEDULE')

    # Also check for named schedule arrays (WSPR_SCHEDULE_*)
    named_pat = re.compile(r'declare\s+(WSPR_SCHEDULE_\w+)\s*=\s*\(', re.DOTALL)
    for m in named_pat.finditer(clean_text):
        arr_name = m.group(1)
        if arr_name != 'WSPR_SCHEDULE':
            named_lines = _extract_array(clean_text, arr_name)
            # If the main schedule didn't pick these up via indirection, add them
            if not schedule_lines:
                schedule_lines = named_lines

    for sline in schedule_lines:
        try:
            slot = parse_schedule_line(sline)
            config.schedule_slots.append(slot)
        except ValueError:
            pass

    # Infer locality and radiod_name for KA9Q receivers from v3 global KA9Q_CONF_NAME.
    # Heuristic: extract the site prefix (first hyphen-delimited component) from both
    # the receiver's audio stream DNS address and the global conf name.
    # Match → local (radiod on this host); no match → remote (user-managed radiod).
    if config.ka9q_conf_name:
        local_prefix = config.ka9q_conf_name.split('-')[0].lower()
        for rx in config.receivers.values():
            if rx.receiver_type not in ('ka9q', 'ka9q_wwv'):
                continue
            addr_prefix = rx.address.split('-')[0].lower() if rx.address else ''
            if addr_prefix == local_prefix:
                rx.locality    = 'local'
                rx.radiod_name = config.ka9q_conf_name
            else:
                rx.locality    = 'remote'
                # radiod_name for remote cannot be inferred from v3 — left empty
                # so the user can fill it in the generated v4 INI.

    # Build derived mappings
    for slot in config.schedule_slots:
        for entry in slot.entries:
            # Track which bands each receiver uses
            if entry.receiver not in config.receiver_bands:
                config.receiver_bands[entry.receiver] = set()
            config.receiver_bands[entry.receiver].add(entry.band)

            # Track modes per band (global)
            if entry.band not in config.band_modes:
                config.band_modes[entry.band] = set()
            for mode in entry.modes.split(':'):
                config.band_modes[entry.band].add(mode)

    return config
