"""Tests for the wsprd ALL_WSPR.TXT and wspr_spots.txt parsers.

Golden fixtures live in `tests/fixtures/spots/` — real lines pulled
from a B4-100 production capture.  Edge cases for the no-grid /
compound-callsign / type-3 hashed-call shapes live in a separate
hand-curated file so the parser's disambiguation logic stays
exercised regardless of which lines happened to be in the prod
capture at fixture-creation time.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from wdlib.spots.parsers import parse_all_wspr_line, parse_wspr_spots_line


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "spots"


_SLOT_CTX = dict(
    band="20",
    radiod_id="B4-100-rx888mk2",
    host_id="B4-100",
    rx_call="AC0G/B4",
    rx_grid="EM38ww",
)


class TestParseAllWsprLine(unittest.TestCase):

    def test_basic_4char_grid(self):
        """Standard line: 4-char grid, integer drift, simple call."""
        line = ("260509 2106 -16 -1.00  14.0971433  "
                "AB0VZ DM79 30           0  0.45  1  1    0  0   4     1   547")
        row = parse_all_wspr_line(line, **_SLOT_CTX)
        self.assertIsNotNone(row)
        self.assertEqual(row.time,
                         datetime(2026, 5, 9, 21, 6, tzinfo=timezone.utc))
        self.assertEqual(row.callsign, "AB0VZ")
        self.assertEqual(row.grid, "DM79")
        self.assertEqual(row.snr_db, -16)
        self.assertEqual(row.dt, -1.00)
        self.assertEqual(row.frequency_hz, 14_097_143)
        self.assertEqual(row.pwr_dbm, 30)
        self.assertEqual(row.drift_hz_per_s, 0.0)
        self.assertEqual(row.sync_quality, 0.45)
        self.assertEqual(row.decoder_kind, "wsprd")

    def test_type3_hashed_call_uppercase_extended_grid(self):
        """Type-3 spot: <hashed-call> + uppercase 6-char extended
        grid (EM60OJ — note caps, not the formal Maidenhead lowercase)."""
        line = ("260509 2106 -12 -1.94  14.0971382  "
                "<KM4BWW> EM60OJ 23      0  0.38  1  1    0  0  11     1   419")
        row = parse_all_wspr_line(line, **_SLOT_CTX)
        self.assertIsNotNone(row)
        self.assertEqual(row.callsign, "<KM4BWW>")
        self.assertEqual(row.grid, "EM60OJ")
        self.assertEqual(row.snr_db, -12)
        self.assertEqual(row.pwr_dbm, 23)

    def test_compound_call_no_grid(self):
        """Type-2 / compound call with no transmitted locator.
        Column 6 holds PWR, not GRID — parser must shift down."""
        line = ("260509 2110 -22 -2.37  14.0969923  "
                "W4UK/P 23               0  0.40  1  1    0  0   8     1   488")
        row = parse_all_wspr_line(line, **_SLOT_CTX)
        self.assertIsNotNone(row)
        self.assertEqual(row.callsign, "W4UK/P")
        self.assertEqual(row.grid, "")
        self.assertEqual(row.pwr_dbm, 23)
        self.assertEqual(row.snr_db, -22)

    def test_nonzero_drift_converts_to_hz_per_sec(self):
        """wsprd reports drift in Hz/min; schema is Hz/sec."""
        line = ("260509 2108 -23 -0.62  14.0970640  "
                "KJ6ST CM97 30          -2  0.21  1  1    0  0  24    23   -29")
        row = parse_all_wspr_line(line, **_SLOT_CTX)
        self.assertIsNotNone(row)
        self.assertAlmostEqual(row.drift_hz_per_s, -2 / 60.0)

    def test_short_line_returns_none(self):
        """Truncated lines must return None, not crash."""
        self.assertIsNone(parse_all_wspr_line("260509 2106 -16", **_SLOT_CTX))
        self.assertIsNone(parse_all_wspr_line("", **_SLOT_CTX))

    def test_bad_date_returns_none(self):
        line = ("BADBAD 2106 -16 -1.00  14.0971433  "
                "AB0VZ DM79 30           0  0.45  1  1    0  0   4     1   547")
        self.assertIsNone(parse_all_wspr_line(line, **_SLOT_CTX))

    def test_bad_freq_returns_none(self):
        line = ("260509 2106 -16 -1.00  XX.XXXXXX  "
                "AB0VZ DM79 30           0  0.45  1  1    0  0   4     1   547")
        self.assertIsNone(parse_all_wspr_line(line, **_SLOT_CTX))

    def test_fixture_full_pass(self):
        """Parse every line of the production-capture fixture —
        zero parse failures from real data."""
        path = FIXTURE_DIR / "all_wspr_20m_sample.txt"
        text = path.read_text()
        rows = [parse_all_wspr_line(l, **_SLOT_CTX)
                for l in text.splitlines() if l.strip()]
        self.assertEqual(len(rows), 15)
        self.assertTrue(all(r is not None for r in rows))

    def test_edge_case_fixture(self):
        """Every hand-curated edge case must parse, and the no-grid
        line must land with grid == ''."""
        path = FIXTURE_DIR / "all_wspr_edge_cases.txt"
        text = path.read_text()
        rows = [parse_all_wspr_line(l, **_SLOT_CTX)
                for l in text.splitlines() if l.strip()]
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(r is not None for r in rows))
        no_grid = [r for r in rows if r.callsign == "W4UK/P"]
        self.assertEqual(len(no_grid), 1)
        self.assertEqual(no_grid[0].grid, "")


class TestParseWsprSpotsLine(unittest.TestCase):

    def test_basic_4char_grid(self):
        line = ("260512 1408   4 -19 -0.0  14.097021  "
                "VE3OCL FN12 10          1     1    0")
        row = parse_wspr_spots_line(line, **_SLOT_CTX)
        self.assertIsNotNone(row)
        self.assertEqual(row.time,
                         datetime(2026, 5, 12, 14, 8, tzinfo=timezone.utc))
        self.assertEqual(row.callsign, "VE3OCL")
        self.assertEqual(row.grid, "FN12")
        self.assertEqual(row.snr_db, -19)
        self.assertEqual(row.dt, -0.0)
        self.assertEqual(row.frequency_hz, 14_097_021)
        self.assertEqual(row.pwr_dbm, 10)
        self.assertAlmostEqual(row.drift_hz_per_s, 1 / 60.0)

    def test_fixture_full_pass(self):
        path = FIXTURE_DIR / "wspr_spots_20m_sample.txt"
        text = path.read_text()
        rows = [parse_wspr_spots_line(l, **_SLOT_CTX)
                for l in text.splitlines() if l.strip()]
        self.assertEqual(len(rows), 10)
        self.assertTrue(all(r is not None for r in rows))

    def test_short_line_returns_none(self):
        self.assertIsNone(parse_wspr_spots_line("260512 1408 1 -22",
                                                **_SLOT_CTX))


if __name__ == "__main__":
    unittest.main()
