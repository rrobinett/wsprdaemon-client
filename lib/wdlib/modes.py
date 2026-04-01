"""Decode mode parsing for wsprdaemon v4.

Mode strings encode the decoder binary and window duration:
    W2   → wsprd,  2-minute window  (WSPR-2)
    F2   → jt9,    2-minute window  (FST4W-120)
    F5   → jt9,    5-minute window  (FST4W-300)
    F15  → jt9,   15-minute window  (FST4W-900)
    F30  → jt9,   30-minute window  (FST4W-1800)
    I1   → (IQ/WWV recording mode — no decode, just record)

The window duration also determines how many 1-minute wav files
the recording directory must retain for that band.
"""

import re
from dataclasses import dataclass
from typing import List, Optional

# Valid mode pattern: prefix letter + digits
_MODE_RE = re.compile(r'^([WFI])(\d+)$')

# Map prefix → decoder binary
_DECODER_MAP = {
    'W': 'wsprd',
    'F': 'jt9',
    'I': None,   # IQ recording only, no decode
}

# Valid window durations per prefix (minutes)
_VALID_WINDOWS = {
    'W': {2},
    'F': {2, 5, 15, 30},
    'I': {1},
}


@dataclass(frozen=True)
class DecodeMode:
    """A parsed decode mode."""
    raw: str            # Original string, e.g. "W2"
    prefix: str         # 'W', 'F', or 'I'
    window_minutes: int # Duration of the decode window
    decoder: Optional[str]  # 'wsprd', 'jt9', or None for IQ
    retention: int      # Number of 1-min wav files to retain

    @property
    def fst4w_seconds(self) -> Optional[int]:
        """For F-modes, return the FST4W period in seconds for jt9 -p flag."""
        if self.prefix == 'F':
            return self.window_minutes * 60
        return None

    @property
    def is_iq(self) -> bool:
        return self.prefix == 'I'


def parse_mode(mode_str: str) -> DecodeMode:
    """Parse a single mode string like 'W2' or 'F5'.

    Raises ValueError for invalid mode strings.
    """
    m = _MODE_RE.match(mode_str.strip().upper())
    if not m:
        raise ValueError(f"Invalid mode string: {mode_str!r}")

    prefix = m.group(1)
    window = int(m.group(2))

    if window not in _VALID_WINDOWS[prefix]:
        valid = sorted(_VALID_WINDOWS[prefix])
        raise ValueError(
            f"Invalid window {window} for prefix {prefix}; "
            f"valid windows: {valid}"
        )

    return DecodeMode(
        raw=mode_str.strip().upper(),
        prefix=prefix,
        window_minutes=window,
        decoder=_DECODER_MAP[prefix],
        retention=window,  # retain N one-minute wav files
    )


def parse_mode_list(mode_str: str) -> List[DecodeMode]:
    """Parse a colon-separated or space-separated mode list.

    Examples:
        "W2:F2:F5"   → [DecodeMode(...), ...]
        "W2 F2 F5"   → [DecodeMode(...), ...]
        "W2:F2:F5:F15:F30" → [DecodeMode(...), ...]
    """
    # Normalize: replace colons with spaces, then split
    tokens = mode_str.replace(':', ' ').split()
    if not tokens:
        raise ValueError("Empty mode list")
    return [parse_mode(t) for t in tokens]


def max_retention(modes: List[DecodeMode]) -> int:
    """Return the maximum wav file retention count across a list of modes.

    This determines how many 1-minute wav files must be kept in the
    recording directory for a given band.
    """
    if not modes:
        return 2  # default: WSPR-2 minimum
    return max(m.retention for m in modes)
