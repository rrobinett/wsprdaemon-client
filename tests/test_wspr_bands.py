"""Tests for the canonical WSPR band list.

Locks the default-band tuple, the band→frequency map, and the
relationship between the two so a future addition / removal can't
silently desync (e.g. listing a band in `WSPR_BANDS_DEFAULT` but
forgetting to add it to `BAND_FREQ_HZ` would render the rest of
the pipeline broken at config-render time).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from wdlib.config_init import WSPR_BANDS_DEFAULT, LONG_WINDOW_BANDS
from wdlib.envgen import BAND_FREQ_HZ


class TestWsprBandDefaults(unittest.TestCase):

    def test_default_band_count(self):
        """17 bands ship as the default WSPR coverage as of 2026-05.

        Pinning the count here so a future addition / removal is a
        deliberate test-edit rather than a silent rendering change."""
        self.assertEqual(len(WSPR_BANDS_DEFAULT), 17)

    def test_default_bands_include_eu_and_vhf(self):
        """The 17-band set adds 80eu / 60eu (EU secondary
        allocations), 8 (UK 40 MHz experimental), and 6 (sporadic-E
        / meteor scatter)."""
        bands = set(WSPR_BANDS_DEFAULT)
        for required in ("80eu", "60eu", "8", "6"):
            self.assertIn(required, bands)

    def test_every_default_band_has_a_frequency(self):
        """A band listed in WSPR_BANDS_DEFAULT but missing from
        BAND_FREQ_HZ would render an env file with WD_BANDS=... but
        no usable channel — caught here so it fails CI, not
        deployment."""
        for band in WSPR_BANDS_DEFAULT:
            self.assertIn(band, BAND_FREQ_HZ,
                          msg=f"band {band!r} in default list but "
                              f"missing from BAND_FREQ_HZ")

    def test_8m_frequency_matches_wsjtx(self):
        """8m experimental WSPR carrier is 40.680400 MHz per WSJT-X.
        Locking this so a typo in the table is caught."""
        self.assertEqual(BAND_FREQ_HZ["8"], 40_680_400)

    def test_6m_frequency_matches_wsjtx(self):
        """6m WSPR carrier is 50.293000 MHz."""
        self.assertEqual(BAND_FREQ_HZ["6"], 50_293_000)

    def test_eu_secondary_bands_at_expected_freqs(self):
        """EU 80m and 60m secondary WSPR allocations."""
        self.assertEqual(BAND_FREQ_HZ["80eu"], 3_592_600)
        self.assertEqual(BAND_FREQ_HZ["60eu"], 5_364_700)

    def test_long_window_bands_subset_of_defaults(self):
        """LF/MF bands that get the long-window FST4W modes
        must also be in the default set."""
        for band in LONG_WINDOW_BANDS:
            self.assertIn(band, WSPR_BANDS_DEFAULT)


if __name__ == "__main__":
    unittest.main()
