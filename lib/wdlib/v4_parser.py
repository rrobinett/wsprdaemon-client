"""Parse wsprdaemon v4 INI configuration files.

Reads /etc/wsprdaemon/wsprdaemon.conf (INI format) and returns a V3Config
so the rest of the pipeline (envgen, wd-ctl) works unchanged.

Section shapes:
  [general]
      reporter_call, reporter_grid, rac, ka9q_conf_name

  [receiver:NAME]
      type, locality, radiod_name, address, call, grid, password

  [receiver:NAME:BAND]
      modes   (space-separated, e.g. "W2 F2 F5")
      freq_hz (integer Hz — optional; falls back to BAND_FREQ_HZ table)

  [merge:NAME]
      sources (space-separated receiver names)
      call, grid

  [merge:NAME:BAND]
      modes

  [schedule:LABEL]
      time = HH:MM
      RECEIVER_NAME = BAND BAND ...   (one key per receiver)
"""

import configparser
from pathlib import Path
from typing import Dict, List

from wdlib.v3_parser import (
    V3Config, V3Receiver, V3ScheduleEntry, V3ScheduleSlot,
)
from wdlib.envgen import BAND_FREQ_HZ


def parse_v4_config(config_path: str) -> V3Config:
    """Parse a v4 INI config file and return a V3Config."""
    cfg = configparser.ConfigParser(
        comment_prefixes=(';', '#'),
        inline_comment_prefixes=(';', '#'),
        strict=False,
    )
    cfg.read(config_path)

    result = V3Config()

    # ── [general] ────────────────────────────────────────────────────────────
    if cfg.has_section('general'):
        g = cfg['general']
        result.rac            = g.get('rac', '').strip()
        result.ka9q_conf_name = g.get('ka9q_conf_name', '').strip()
        result.ka9q_web_dns   = g.get('ka9q_web_dns', '').strip()

    # ── [receiver:NAME] and [receiver:NAME:BAND] ──────────────────────────────
    # First pass: build receiver objects
    for section in cfg.sections():
        parts = section.split(':')
        if parts[0] != 'receiver' or len(parts) != 2:
            continue
        rx_name = parts[1]
        s = cfg[section]

        rx = V3Receiver(
            name      = rx_name,
            address   = s.get('address', '').strip(),
            call      = s.get('call', '').strip(),
            grid      = s.get('grid', '').strip(),
            password  = s.get('password', 'NULL').strip() or 'NULL',
            locality  = s.get('locality', '').strip(),
            radiod_name = s.get('radiod_name', '').strip(),
        )
        result.receivers[rx_name] = rx

    # ── [merge:NAME] ─────────────────────────────────────────────────────────
    for section in cfg.sections():
        parts = section.split(':')
        if parts[0] != 'merge' or len(parts) != 2:
            continue
        rx_name = parts[1]
        s = cfg[section]
        sources = s.get('sources', '').split()
        rx = V3Receiver(
            name     = rx_name,
            address  = ','.join(sources),   # merge address = CSV of source names
            call     = s.get('call', '').strip(),
            grid     = s.get('grid', '').strip(),
            password = 'NULL',
        )
        result.receivers[rx_name] = rx

    # ── [schedule:LABEL] ─────────────────────────────────────────────────────
    # Collect per-band modes from receiver and merge band sections
    rx_band_modes: Dict[str, Dict[str, str]] = {}   # rx → band → "W2 F2 F5"

    for section in cfg.sections():
        parts = section.split(':')
        if len(parts) != 3:
            continue
        kind, rx_name, band = parts[0], parts[1], parts[2]
        if kind not in ('receiver', 'merge'):
            continue
        modes_raw = cfg[section].get('modes', '').strip()
        if modes_raw:
            if rx_name not in rx_band_modes:
                rx_band_modes[rx_name] = {}
            # normalise: space-separated → colon-separated
            rx_band_modes[rx_name][band] = ':'.join(modes_raw.split())

    for section in cfg.sections():
        parts = section.split(':')
        if parts[0] != 'schedule' or len(parts) != 2:
            continue
        s    = cfg[section]
        time = s.get('time', '00:00').strip()
        slot = V3ScheduleSlot(time=time)

        for key, val in s.items():
            if key == 'time':
                continue
            rx_name = key.upper()
            bands   = val.split()
            for band in bands:
                modes_str = rx_band_modes.get(rx_name, {}).get(band, 'W2')
                # modes_str is already colon-separated
                slot.entries.append(V3ScheduleEntry(
                    receiver = rx_name,
                    band     = band,
                    modes    = modes_str,
                ))
        result.schedule_slots.append(slot)

    # ── Derived mappings (mirrors parse_v3_config) ────────────────────────────
    for slot in result.schedule_slots:
        for entry in slot.entries:
            result.receiver_bands.setdefault(entry.receiver, set()).add(entry.band)
            for mode in entry.modes.split(':'):
                result.band_modes.setdefault(entry.band, set()).add(mode)

    # ── Infer locality/radiod_name for receivers that still have blanks ───────
    if result.ka9q_conf_name:
        local_prefix = result.ka9q_conf_name.split('-')[0].lower()
        for rx in result.receivers.values():
            if rx.receiver_type not in ('ka9q', 'ka9q_wwv'):
                continue
            if rx.locality and rx.radiod_name:
                continue   # already set in INI
            addr_prefix = rx.address.split('-')[0].lower() if rx.address else ''
            if addr_prefix == local_prefix:
                rx.locality     = rx.locality or 'local'
                rx.radiod_name  = rx.radiod_name or result.ka9q_conf_name
            else:
                rx.locality     = rx.locality or 'remote'

    return result
