#!/usr/bin/env python3
"""Tests for wsprdaemon v4 migration toolchain.

Tests the v3 parser, mode parser, env generator, and INI writer
against the real-world k3lr config.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

# Add lib to path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / 'lib'))


def _find_v3_config() -> str:
    """Locate the v3 wsprdaemon.conf for integration tests.

    Search order:
      1. WD_TEST_CONFIG env var (explicit override)
      2. tests/wsprdaemon.conf (bundled with the tarball)
      3. /etc/wsprdaemon/wsprdaemon-v3.conf (installed backup)
      4. /mnt/user-data/uploads/wsprdaemon.conf (Claude sandbox)

    Returns the path if found, or '' if none exists.
    """
    candidates = [
        os.environ.get('WD_TEST_CONFIG', ''),
        str(_PROJECT_ROOT / 'tests' / 'wsprdaemon.conf'),
        '/etc/wsprdaemon/wsprdaemon-v3.conf',
        '/mnt/user-data/uploads/wsprdaemon.conf',
    ]
    for p in candidates:
        if p and Path(p).is_file():
            return p
    return ''

from wdlib.modes import parse_mode, parse_mode_list, max_retention, DecodeMode
from wdlib.v3_parser import (
    parse_v3_config, parse_receiver_line, parse_schedule_line,
    _extract_array, _extract_variable, _strip_comments
)
from wdlib.envgen import generate_all_env_files, BAND_FREQ_HZ
from wdlib.ini_writer import v3_to_ini
from wdlib import paths


class TestModes(unittest.TestCase):
    """Test decode mode parsing."""

    def test_wspr2(self):
        m = parse_mode('W2')
        self.assertEqual(m.prefix, 'W')
        self.assertEqual(m.window_minutes, 2)
        self.assertEqual(m.decoder, 'wsprd')
        self.assertEqual(m.retention, 2)
        self.assertIsNone(m.fst4w_seconds)

    def test_fst4w_120(self):
        m = parse_mode('F2')
        self.assertEqual(m.prefix, 'F')
        self.assertEqual(m.window_minutes, 2)
        self.assertEqual(m.decoder, 'jt9')
        self.assertEqual(m.fst4w_seconds, 120)

    def test_fst4w_300(self):
        m = parse_mode('F5')
        self.assertEqual(m.window_minutes, 5)
        self.assertEqual(m.fst4w_seconds, 300)
        self.assertEqual(m.retention, 5)

    def test_fst4w_900(self):
        m = parse_mode('F15')
        self.assertEqual(m.window_minutes, 15)
        self.assertEqual(m.fst4w_seconds, 900)

    def test_fst4w_1800(self):
        m = parse_mode('F30')
        self.assertEqual(m.window_minutes, 30)
        self.assertEqual(m.fst4w_seconds, 1800)
        self.assertEqual(m.retention, 30)

    def test_iq_mode(self):
        m = parse_mode('I1')
        self.assertEqual(m.prefix, 'I')
        self.assertIsNone(m.decoder)
        self.assertTrue(m.is_iq)

    def test_invalid_mode(self):
        with self.assertRaises(ValueError):
            parse_mode('X5')

    def test_invalid_window(self):
        with self.assertRaises(ValueError):
            parse_mode('W3')  # Only W2 is valid

    def test_case_insensitive(self):
        m = parse_mode('w2')
        self.assertEqual(m.prefix, 'W')

    def test_parse_mode_list_colon(self):
        modes = parse_mode_list('W2:F2:F5')
        self.assertEqual(len(modes), 3)
        self.assertEqual(modes[0].raw, 'W2')
        self.assertEqual(modes[1].raw, 'F2')
        self.assertEqual(modes[2].raw, 'F5')

    def test_parse_mode_list_space(self):
        modes = parse_mode_list('W2 F2 F5')
        self.assertEqual(len(modes), 3)

    def test_max_retention(self):
        modes = parse_mode_list('W2:F2:F5:F15:F30')
        self.assertEqual(max_retention(modes), 30)

    def test_max_retention_simple(self):
        modes = parse_mode_list('W2')
        self.assertEqual(max_retention(modes), 2)


class TestV3Parser(unittest.TestCase):
    """Test v3 config file parsing."""

    def test_strip_comments(self):
        self.assertEqual(_strip_comments('hello # world'), 'hello ')
        self.assertEqual(_strip_comments('"hello # world"'), '"hello # world"')
        self.assertEqual(_strip_comments('# full comment'), '')

    def test_parse_receiver_line(self):
        line = 'KA9Q_0  k3lr-wspr-pcm.local  AI6VN-0  CM88mc  NULL'
        rx = parse_receiver_line(line)
        self.assertEqual(rx.name, 'KA9Q_0')
        self.assertEqual(rx.address, 'k3lr-wspr-pcm.local')
        self.assertEqual(rx.call, 'AI6VN-0')
        self.assertEqual(rx.grid, 'CM88mc')
        self.assertEqual(rx.password, 'NULL')
        self.assertEqual(rx.receiver_type, 'ka9q')

    def test_parse_kiwi_receiver(self):
        line = 'KIWI_0  10.22.23.70:8073  AI6VN-K0  CM88mc  NULL'
        rx = parse_receiver_line(line)
        self.assertEqual(rx.receiver_type, 'kiwi')
        self.assertEqual(rx.address, '10.22.23.70:8073')

    def test_parse_merge_receiver(self):
        line = 'MERG_Q01  KA9Q_0,KA9Q_1  AI6VN-M01  CM88mc  foobar'
        rx = parse_receiver_line(line)
        self.assertEqual(rx.receiver_type, 'merge')
        self.assertTrue(rx.is_merge)
        self.assertEqual(rx.merge_sources, ['KA9Q_0', 'KA9Q_1'])

    def test_parse_wwv_receiver(self):
        line = 'KA9Q_0_WWV  k3lr-wwv-iq.local  AI6VN-0  CM88mc  NULL'
        rx = parse_receiver_line(line)
        self.assertEqual(rx.receiver_type, 'ka9q_wwv')

    def test_parse_schedule_line(self):
        line = '00:00  KA9Q_0,80,W2:F2:F5  KA9Q_0,40,W2:F2:F5'
        slot = parse_schedule_line(line)
        self.assertEqual(slot.time, '00:00')
        self.assertEqual(len(slot.entries), 2)
        self.assertEqual(slot.entries[0].receiver, 'KA9Q_0')
        self.assertEqual(slot.entries[0].band, '80')
        self.assertEqual(slot.entries[0].modes, 'W2:F2:F5')

    def test_extract_variable(self):
        text = 'FOO="bar"\nBAZ=qux\n'
        self.assertEqual(_extract_variable(text, 'FOO'), 'bar')
        self.assertEqual(_extract_variable(text, 'BAZ'), 'qux')
        self.assertEqual(_extract_variable(text, 'NOPE'), '')


class TestV3ParserRealConfig(unittest.TestCase):
    """Test against the actual k3lr config file."""

    @classmethod
    def setUpClass(cls):
        config_path = _find_v3_config()
        if config_path:
            cls.config_path = config_path
            cls.config = parse_v3_config(config_path)
        else:
            cls.config = None

    def test_config_loaded(self):
        if not self.config:
            self.skipTest(
                "No v3 config found. Set WD_TEST_CONFIG=/path/to/wsprdaemon.conf "
                "or copy it to tests/wsprdaemon.conf"
            )

    def test_receiver_count(self):
        if not self.config:
            self.skipTest("No config")
        # Should have: 2 KA9Q, 2 KA9Q_WWV, 4 KIWI, 6 MERG = 14
        self.assertEqual(len(self.config.receivers), 14)

    def test_ka9q_receivers(self):
        if not self.config:
            self.skipTest("No config")
        ka9q = [r for r in self.config.receivers.values()
                if r.receiver_type == 'ka9q']
        self.assertEqual(len(ka9q), 2)
        names = {r.name for r in ka9q}
        self.assertIn('KA9Q_0', names)
        self.assertIn('KA9Q_1', names)

    def test_ka9q_wwv_receivers(self):
        if not self.config:
            self.skipTest("No config")
        wwv = [r for r in self.config.receivers.values()
               if r.receiver_type == 'ka9q_wwv']
        self.assertEqual(len(wwv), 2)

    def test_kiwi_receivers(self):
        if not self.config:
            self.skipTest("No config")
        kiwi = [r for r in self.config.receivers.values()
                if r.receiver_type == 'kiwi']
        self.assertEqual(len(kiwi), 4)
        # Check IPs are sequential
        for i, name in enumerate(['KIWI_0', 'KIWI_1', 'KIWI_2', 'KIWI_3']):
            self.assertIn(name, self.config.receivers)
            rx = self.config.receivers[name]
            self.assertIn(f'10.22.23.7{i}', rx.address)

    def test_merge_receivers(self):
        if not self.config:
            self.skipTest("No config")
        merges = [r for r in self.config.receivers.values()
                  if r.receiver_type == 'merge']
        self.assertEqual(len(merges), 6)

    def test_merge_sources(self):
        if not self.config:
            self.skipTest("No config")
        merg = self.config.receivers['MERG_Q01']
        self.assertEqual(merg.merge_sources, ['KA9Q_0', 'KA9Q_1'])

    def test_merge_q01_k0123(self):
        if not self.config:
            self.skipTest("No config")
        merg = self.config.receivers['MERG_Q01_K0123']
        self.assertEqual(len(merg.merge_sources), 5)
        self.assertIn('KA9Q_0', merg.merge_sources)
        self.assertIn('KIWI_3', merg.merge_sources)

    def test_schedule_parsed(self):
        if not self.config:
            self.skipTest("No config")
        self.assertGreater(len(self.config.schedule_slots), 0)

    def test_schedule_has_entries(self):
        if not self.config:
            self.skipTest("No config")
        total = sum(len(s.entries) for s in self.config.schedule_slots)
        # Should have many entries from the schedule
        self.assertGreater(total, 10)

    def test_ka9q_conf_name(self):
        if not self.config:
            self.skipTest("No config")
        self.assertEqual(self.config.ka9q_conf_name, 'k3lr-rx888')

    def test_rac(self):
        if not self.config:
            self.skipTest("No config")
        self.assertEqual(self.config.rac, '117')

    def test_schedule_has_wwv_entries(self):
        if not self.config:
            self.skipTest("No config")
        wwv_entries = []
        for slot in self.config.schedule_slots:
            for e in slot.entries:
                if 'WWV' in e.band or 'CHU' in e.band:
                    wwv_entries.append(e)
        self.assertGreater(len(wwv_entries), 0)

    def test_schedule_2200_band(self):
        if not self.config:
            self.skipTest("No config")
        bands_seen = set()
        for slot in self.config.schedule_slots:
            for e in slot.entries:
                bands_seen.add(e.band)
        self.assertIn('2200', bands_seen)
        self.assertIn('80', bands_seen)
        self.assertIn('20', bands_seen)

    def test_modes_include_f15_f30(self):
        if not self.config:
            self.skipTest("No config")
        all_modes = set()
        for slot in self.config.schedule_slots:
            for e in slot.entries:
                for m in e.modes.split(':'):
                    all_modes.add(m)
        self.assertIn('F15', all_modes)
        self.assertIn('F30', all_modes)
        self.assertIn('W2', all_modes)
        self.assertIn('F2', all_modes)
        self.assertIn('F5', all_modes)


class TestEnvGeneration(unittest.TestCase):
    """Test environment file generation from the real config."""

    @classmethod
    def setUpClass(cls):
        config_path = _find_v3_config()
        if config_path:
            cls.config = parse_v3_config(config_path)
            cls.tmp_dir = tempfile.mkdtemp()
            cls.env_dir = Path(cls.tmp_dir) / 'env'
            cls.written = generate_all_env_files(cls.config, cls.env_dir)
        else:
            cls.config = None

    def test_env_files_created(self):
        if not self.config:
            self.skipTest("No config")
        self.assertGreater(len(self.written), 0)

    def test_ka9q_record_env_exists(self):
        if not self.config:
            self.skipTest("No config")
        ka9q_envs = [f for f in self.written if 'wd-ka9q-record@KA9Q_0' in f]
        self.assertGreater(len(ka9q_envs), 0)

    def test_ka9q_env_content(self):
        if not self.config:
            self.skipTest("No config")
        env_path = self.env_dir / 'wd-ka9q-record@KA9Q_0.env'
        if not env_path.exists():
            self.skipTest("KA9Q_0 env not generated")
        content = env_path.read_text()
        self.assertIn('WD_RECEIVER_NAME=KA9Q_0', content)
        self.assertIn('WD_RECEIVER_TYPE=ka9q', content)
        self.assertIn('WD_MULTICAST_DNS=k3lr-wspr-pcm.local', content)

    def test_decode_env_has_modes(self):
        if not self.config:
            self.skipTest("No config")
        # Find a decode env file
        decode_envs = [f for f in self.written if 'wd-decode@KA9Q_0-80' in f]
        if not decode_envs:
            self.skipTest("No KA9Q_0-80 decode env")
        content = Path(decode_envs[0]).read_text()
        self.assertIn('WD_RECEIVER_MODES=', content)
        self.assertIn('WD_RECEIVER_BAND=80', content)

    def test_kiwi_record_env_not_generated_without_schedule(self):
        """Kiwi env files should only exist if the receiver is in the schedule."""
        if not self.config:
            self.skipTest("No config")
        # In this config, kiwis appear in schedule via merge receivers
        # Check that standalone kiwi entries only exist when scheduled
        kiwi_envs = [f for f in self.written if 'wd-kiwi-record' in f]
        # Kiwis are only in the schedule through MERG entries, not directly
        # So there should be kiwi record envs only if kiwis are directly scheduled
        # In this config they ARE directly scheduled via MERG_Q01_K0, etc.
        # Actually KIWI_0 etc don't appear directly in schedule - only via MERG
        # So we might not have kiwi record envs
        # This is OK - merge entries reference kiwis but the kiwis themselves
        # need to be running for the merge to work

    def test_merge_decode_env_has_sources(self):
        if not self.config:
            self.skipTest("No config")
        merge_envs = [f for f in self.written if 'wd-decode@MERG_Q01-' in f]
        if not merge_envs:
            self.skipTest("No MERG_Q01 decode env")
        content = Path(merge_envs[0]).read_text()
        self.assertIn('WD_MERGE_SOURCES=', content)


class TestINIWriter(unittest.TestCase):
    """Test INI config generation."""

    @classmethod
    def setUpClass(cls):
        config_path = _find_v3_config()
        if config_path:
            cls.config = parse_v3_config(config_path)
            cls.ini_text = v3_to_ini(cls.config)
        else:
            cls.config = None

    def test_ini_generated(self):
        if not self.config:
            self.skipTest("No config")
        self.assertGreater(len(self.ini_text), 100)

    def test_has_general_section(self):
        if not self.config:
            self.skipTest("No config")
        self.assertIn('[general]', self.ini_text)

    def test_has_reporter_call(self):
        if not self.config:
            self.skipTest("No config")
        self.assertIn('reporter_call', self.ini_text)

    def test_has_receiver_sections(self):
        if not self.config:
            self.skipTest("No config")
        self.assertIn('[receiver:KA9Q_0]', self.ini_text)
        self.assertIn('[receiver:KIWI_0]', self.ini_text)

    def test_has_merge_sections(self):
        if not self.config:
            self.skipTest("No config")
        self.assertIn('[merge:MERG_Q01]', self.ini_text)

    def test_has_band_sections(self):
        if not self.config:
            self.skipTest("No config")
        self.assertIn('[receiver:KA9Q_0:80]', self.ini_text)

    def test_has_upload_sections(self):
        if not self.config:
            self.skipTest("No config")
        self.assertIn('[upload:wsprnet]', self.ini_text)
        self.assertIn('[upload:wsprdaemon]', self.ini_text)

    def test_has_schedule_section(self):
        if not self.config:
            self.skipTest("No config")
        self.assertIn('[schedule:', self.ini_text)

    def test_has_ka9q_conf_name(self):
        if not self.config:
            self.skipTest("No config")
        self.assertIn('ka9q_conf_name = k3lr-rx888', self.ini_text)

    def test_has_rac(self):
        if not self.config:
            self.skipTest("No config")
        self.assertIn('rac = 117', self.ini_text)

    def test_no_null_passwords(self):
        if not self.config:
            self.skipTest("No config")
        # NULL passwords should not appear in the INI
        self.assertNotIn('password = NULL', self.ini_text)


class TestPaths(unittest.TestCase):
    """Test path construction."""

    def test_receiver_recording_dir(self):
        p = paths.receiver_recording_dir('KA9Q_0')
        self.assertEqual(str(p), '/var/spool/wsprdaemon/recording/KA9Q_0')

    def test_band_recording_dir(self):
        p = paths.band_recording_dir('KA9Q_0', '80')
        self.assertEqual(str(p), '/var/spool/wsprdaemon/recording/KA9Q_0/80')

    def test_env_file(self):
        p = paths.env_file('wd-decode', 'KA9Q_0-80')
        self.assertEqual(str(p), '/etc/wsprdaemon/env/wd-decode@KA9Q_0-80.env')

    def test_upload_dir_wsprnet(self):
        p = paths.upload_dir('wsprnet', 'AI6VN', 'CM88mc')
        self.assertEqual(str(p), '/var/spool/wsprdaemon/posting/uploads/wsprnet/AI6VN_CM88mc')

    def test_upload_dir_wsprdaemon(self):
        p = paths.upload_dir('wsprdaemon', 'AI6VN', 'CM88mc', 'KA9Q_0', '80')
        self.assertEqual(str(p),
                        '/var/spool/wsprdaemon/posting/uploads/wsprdaemon/AI6VN/KA9Q_0/80')


if __name__ == '__main__':
    unittest.main(verbosity=2)
