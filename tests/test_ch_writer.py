"""Tests for wsprdaemon-client's ch_writer (CONTRACT v0.6 §17 wiring).

Verify:
  - parser produces rows wire-compatible with wsprdaemon-server's
    tar-bulk-loader (matching field names and types).
  - rows_from_spots_file reconstructs path metadata correctly.
  - the writer factory hands back a NoOp writer when CH is unset.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

# Pull in sigmond's writer too; tests below use its NoOp behavior.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "sigmond" / "lib"))

from wdlib.ch_writer import (
    band_str_to_meters,
    build_writer,
    parse_spots_path,
    parse_wd_spots_line,
    rows_from_spots_file,
    spot_to_ch_row,
)


# A realistic 34-field spot line as wd-extend-spots emits it.
SAMPLE_LINE = (
    "260507 1234 "                  # 0,1: date HHMM
    "1.0 -22 0.5 14.097100 "        # 2-5: sync, snr, dt, freq_mhz
    "K1ABC EM26 30 0 "              # 6-9: tx_sign, tx_loc, power, drift
    "1234 0 256 0 1 1 0 1 "         # 10-17: cycles..code
    "-65.0 -68.0 "                  # 18-19: rms, c2 noise
    "20 "                           # 20: band_m
    "EN16ov AC0G/ND 1234 "          # 21-23: rx_loc, rx_sign, distance
    "45.5 39.5 -98.5 "              # 24-26: rx_az, rx_lat, rx_lon
    "270.5 42.5 -71.5 "             # 27-29: tx_az, tx_lat, tx_lon
    "40.5 -85.0 "                   # 30-31: v_lat, v_lon
    "0 0"                           # 32-33: ov_count, proxy_upload
)


class TestBandParsing(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(band_str_to_meters("20"), 20)
        self.assertEqual(band_str_to_meters("160"), 160)

    def test_with_suffix(self):
        self.assertEqual(band_str_to_meters("60eu"), 60)
        self.assertEqual(band_str_to_meters("80jp"), 80)

    def test_invalid(self):
        self.assertIsNone(band_str_to_meters(""))
        self.assertIsNone(band_str_to_meters("abc"))


class TestPathMetadata(unittest.TestCase):
    def test_full_layout(self):
        p = Path("/var/spool/wsprdaemon/posting/AC0G/EN16ov/KA9Q_DXE/20/260507_1234_wd_spots.txt")
        meta = parse_spots_path(p)
        # The agent indexes [-2,-3,-4] from the leaf; given this layout:
        #   leaf=...txt, -2=20, -3=KA9Q_DXE, -4=EN16ov
        self.assertEqual(meta["band"], "20")
        self.assertEqual(meta["receiver"], "KA9Q_DXE")
        # rx_call/rx_grid extracted from the site dir; layout used here
        # treats "EN16ov" as a single token (no embedded slash).
        self.assertEqual(meta["rx_grid"], "")           # no slash in token

    def test_url_encoded_site(self):
        p = Path("/spool/AC0G%2FEN16ov/KA9Q_DXE/20/file.txt")
        meta = parse_spots_path(p)
        self.assertEqual(meta["rx_call"], "AC0G")
        self.assertEqual(meta["rx_grid"], "EN16ov")


class TestLineParser(unittest.TestCase):
    def test_full_line_round_trips(self):
        spot = parse_wd_spots_line(SAMPLE_LINE)
        self.assertIsNotNone(spot)
        self.assertEqual(spot["tx_sign"], "K1ABC")
        self.assertEqual(spot["tx_loc"], "EM26")
        self.assertEqual(spot["snr"], -22)
        self.assertEqual(spot["power_dbm"], 30)
        self.assertEqual(spot["band_m"], 20)
        self.assertAlmostEqual(spot["freq_hz"], 14_097_100.0, places=2)
        self.assertEqual(spot["distance"], 1234)
        self.assertEqual(spot["ov_count"], 0)

    def test_short_line_returns_none(self):
        self.assertIsNone(parse_wd_spots_line("260507 1234 -22 0.5"))

    def test_garbled_field_returns_none(self):
        bad = SAMPLE_LINE.replace(" 14.097100 ", " not-a-number ")
        self.assertIsNone(parse_wd_spots_line(bad))

    def test_none_grid_normalized_to_empty(self):
        line = SAMPLE_LINE.replace("EM26", "none")
        spot = parse_wd_spots_line(line)
        self.assertIsNotNone(spot)
        self.assertEqual(spot["tx_loc"], "")


class TestSpotToRow(unittest.TestCase):
    def test_row_columns_match_wspr_spots_schema(self):
        spot = parse_wd_spots_line(SAMPLE_LINE)
        row = spot_to_ch_row(spot, band=20, rx_sign="AC0G", rx_id="KA9Q_DXE",
                             client_version="4.0.0-dev")

        # All wspr.spots non-alias columns must be present.  Cross-check
        # against the vendored sigmond-clickhouse DDL.
        expected_cols = {
            "time", "band", "rx_sign", "rx_lat", "rx_lon", "rx_loc",
            "tx_sign", "tx_lat", "tx_lon", "tx_loc",
            "distance", "azimuth", "rx_azimuth",
            "frequency", "power", "snr", "drift",
            "version", "code", "frequency_mhz", "rx_id",
            "v_lat", "v_lon", "c2_noise",
            "sync_quality", "dt", "decode_cycles", "jitter",
            "rms_noise", "blocksize", "metric", "osd_decode",
            "nhardmin", "ipass", "proxy_upload", "ov_count",
            "rx_status", "band_m",
        }
        self.assertEqual(set(row.keys()), expected_cols)
        self.assertEqual(row["band"], 20)
        self.assertEqual(row["rx_sign"], "AC0G")
        self.assertEqual(row["rx_id"], "KA9Q_DXE")
        self.assertEqual(row["version"], "4.0.0-dev")
        self.assertEqual(row["rx_status"], "No Info")
        self.assertEqual(row["frequency"], 14_097_100)
        self.assertAlmostEqual(row["frequency_mhz"], 14.0971, places=4)
        self.assertEqual(row["time"],
                         datetime(2026, 5, 7, 12, 34))


class TestRowsFromFile(unittest.TestCase):
    def test_yields_rows_for_each_valid_line(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            posting = Path(td) / "AC0G" / "EN16ov" / "KA9Q_DXE" / "20"
            posting.mkdir(parents=True)
            spots_file = posting / "260507_1234_wd_spots.txt"
            spots_file.write_text(
                SAMPLE_LINE + "\n"
                + "\n"                    # blank line, skipped
                + SAMPLE_LINE + "\n"
                + "garbled too short\n"   # 4 fields, skipped
            )
            rows = list(rows_from_spots_file(spots_file,
                                             client_version="4.0.0-dev"))
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["band"], 20)
            # rx_sign comes from the path's site dir (here the dir is
            # plain "EN16ov" with no slash, so site_raw=EN16ov →
            # rx_call=EN16ov).  In production the site dir is
            # `<call>/<grid>` (or url-encoded equivalent).
            self.assertEqual(row["rx_id"], "KA9Q_DXE")


class TestBuildWriter(unittest.TestCase):
    """build_writer hands back a NoOp writer when CH is not configured."""

    def setUp(self):
        self._saved_url = os.environ.pop("SIGMOND_CLICKHOUSE_URL", None)

    def tearDown(self):
        if self._saved_url is not None:
            os.environ["SIGMOND_CLICKHOUSE_URL"] = self._saved_url

    def test_noop_when_url_unset(self):
        w = build_writer()
        self.assertTrue(w.is_noop)
        # Inserting a row is a free call; no network attempt.
        w.insert([{"x": 1}])
        w.close()


if __name__ == "__main__":
    unittest.main()
