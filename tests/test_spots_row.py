"""Tests for the canonical Row dataclass + JSON serialization."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from wdlib.spots import Row, SCHEMA_VERSION, row_to_dict


def _make_row(**overrides) -> Row:
    base = dict(
        time=datetime(2026, 5, 9, 21, 6, tzinfo=timezone.utc),
        band="20",
        mode="W2",
        radiod_id="B4-100-rx888mk2",
        host_id="B4-100",
        frequency_hz=14_097_138,
        callsign="<KM4BWW>",
        grid="EM60OJ",
        snr_db=-12,
        dt=-1.94,
        drift_hz_per_s=0.0,
        pwr_dbm=23,
        sync_quality=0.38,
        decoder_kind="wsprd",
        decoder_depth=3,
        type_2_3=3,
        rx_call="AC0G/B4",
        rx_grid="EM38ww",
    )
    base.update(overrides)
    return Row(**base)


class TestRow(unittest.TestCase):

    def test_row_is_frozen(self):
        """Frozen so accidental mutation in the writer path can't
        silently corrupt a serialized batch."""
        r = _make_row()
        with self.assertRaises(Exception):
            r.snr_db = -10  # type: ignore[misc]

    def test_row_to_dict_iso_utc(self):
        r = _make_row()
        d = row_to_dict(r)
        self.assertEqual(d["time"], "2026-05-09T21:06:00Z")
        self.assertIsNone(d["uploaded_at"])
        self.assertEqual(d["schema_version"], SCHEMA_VERSION)

    def test_row_to_dict_uploaded_at_round_trip(self):
        uploaded = datetime(2026, 5, 9, 21, 7, 15, tzinfo=timezone.utc)
        r = _make_row(uploaded_at=uploaded)
        d = row_to_dict(r)
        self.assertEqual(d["uploaded_at"], "2026-05-09T21:07:15Z")

    def test_row_to_dict_naive_time_is_tagged_utc(self):
        """Slot times that arrive tz-naive get treated as UTC (wsprd's
        convention) rather than the producer's local timezone."""
        naive = datetime(2026, 5, 9, 21, 6)
        r = _make_row(time=naive)
        d = row_to_dict(r)
        self.assertEqual(d["time"], "2026-05-09T21:06:00Z")

    def test_row_to_dict_local_time_is_converted(self):
        """A tz-aware non-UTC time is converted to UTC at serialize
        time so the wire format is unambiguous regardless of producer
        locale."""
        tz_minus5 = timezone(timedelta(hours=-5))
        local = datetime(2026, 5, 9, 16, 6, tzinfo=tz_minus5)
        r = _make_row(time=local)
        d = row_to_dict(r)
        self.assertEqual(d["time"], "2026-05-09T21:06:00Z")


if __name__ == "__main__":
    unittest.main()
