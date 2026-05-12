"""Decoder-output parsers that produce `Row` instances.

The contract: each parser takes one line of decoder stdout (or one
line of a decoder-produced file) plus the slot context (radiod_id,
band, host_id, rx_call/grid), and returns either a `Row` or `None`
if the line doesn't parse as a spot.

Parsers MUST NOT raise on malformed input — they return `None`.
Caller is expected to count parse failures for observability.
"""
from .wsprd import parse_all_wspr_line, parse_wspr_spots_line

__all__ = ["parse_all_wspr_line", "parse_wspr_spots_line"]
