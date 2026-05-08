"""Parse wsprdaemon v4 INI configuration files.

Reads /etc/wsprdaemon/wsprdaemon.conf (INI format) and returns a WdConfig.

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
import os
from typing import Dict

from wdlib.config import (
    WdConfig, Receiver, ScheduleEntry, ScheduleSlot,
    HfTimestdConfig, Ka9qWebConfig,
)
from wdlib.envgen import BAND_FREQ_HZ


def parse_v4_config(config_path: str) -> WdConfig:
    """Parse a v4 INI config file and return a WdConfig.

    Operator-identity resolution for each receiver / merge:

        receiver.call ← [receiver:X].call   if non-empty
                      ↓
                       [general].reporter_call   if non-empty
                      ↓
                       $STATION_CALL              (sigmond-published)

    Same chain for grid.  This means a single-receiver host can omit
    receiver-level call/grid entirely once `smd config identity` has
    captured the operator identity at the sigmond level — the values
    flow through automatically.
    """
    cfg = configparser.ConfigParser(
        comment_prefixes=(';', '#'),
        inline_comment_prefixes=(';', '#'),
        strict=False,
        interpolation=None,   # prevent % in values (e.g. storage_quota) from being treated as interpolation
    )
    cfg.read(config_path)

    result = WdConfig()

    # ── [general] ────────────────────────────────────────────────────────────
    if cfg.has_section('general'):
        g = cfg['general']
        result.rac                 = g.get('rac', '').strip()
        result.rac_token           = g.get('rac_token', '').strip()
        result.rac_server          = g.get('rac_server', 'remote.wsprdaemon.org').strip()
        result.rac_fallback_server = g.get('rac_fallback_server', '').strip()
        result.rac_tls_ca          = g.get('rac_tls_ca', '').strip()
        result.ka9q_conf_name      = g.get('ka9q_conf_name', '').strip()
        result.ka9q_web_dns        = g.get('ka9q_web_dns', '').strip()
        result.reserved_cpus       = g.get('reserved_cpus', '').strip()
        result.reporter_call       = g.get('reporter_call', '').strip()
        result.reporter_grid       = g.get('reporter_grid', '').strip()

    # Operator-identity fallbacks: [general] → STATION_* env var.
    # Computed once, applied per receiver/merge below.
    default_call = result.reporter_call or os.environ.get('STATION_CALL', '').strip()
    default_grid = result.reporter_grid or os.environ.get('STATION_GRID', '').strip()

    # ── [receiver:NAME] ──────────────────────────────────────────────────────
    for section in cfg.sections():
        parts = section.split(':')
        if parts[0] != 'receiver' or len(parts) != 2:
            continue
        rx_name = parts[1]
        s = cfg[section]
        result.receivers[rx_name] = Receiver(
            name        = rx_name,
            address     = s.get('address', '').strip(),
            call        = s.get('call', '').strip() or default_call,
            grid        = s.get('grid', '').strip() or default_grid,
            password    = s.get('password', 'NULL').strip() or 'NULL',
            locality    = s.get('locality', '').strip(),
            radiod_name = s.get('radiod_name', '').strip(),
        )

    # ── [merge:NAME] ─────────────────────────────────────────────────────────
    for section in cfg.sections():
        parts = section.split(':')
        if parts[0] != 'merge' or len(parts) != 2:
            continue
        rx_name = parts[1]
        s = cfg[section]
        sources = s.get('sources', '').split()
        result.receivers[rx_name] = Receiver(
            name     = rx_name,
            address  = ','.join(sources),   # merge address = CSV of source names
            call     = s.get('call', '').strip() or default_call,
            grid     = s.get('grid', '').strip() or default_grid,
            password = 'NULL',
        )

    # ── Band modes from receiver/merge band sections ──────────────────────────
    rx_band_modes: Dict[str, Dict[str, str]] = {}   # rx → band → "W2:F2:F5"

    for section in cfg.sections():
        parts = section.split(':')
        if len(parts) != 3:
            continue
        kind, rx_name, band = parts[0], parts[1], parts[2]
        if kind not in ('receiver', 'merge'):
            continue
        modes_raw = cfg[section].get('modes', '').strip()
        if modes_raw:
            rx_band_modes.setdefault(rx_name, {})[band] = ':'.join(modes_raw.split())

    # ── [schedule:LABEL] ─────────────────────────────────────────────────────
    for section in cfg.sections():
        parts = section.split(':')
        if parts[0] != 'schedule' or len(parts) != 2:
            continue
        s    = cfg[section]
        time = s.get('time', '00:00').strip()
        slot = ScheduleSlot(time=time)

        for key, val in s.items():
            if key == 'time':
                continue
            rx_name = key.upper()
            for band in val.split():
                modes_str = rx_band_modes.get(rx_name, {}).get(band, 'W2')
                slot.entries.append(ScheduleEntry(
                    receiver = rx_name,
                    band     = band,
                    modes    = modes_str,
                ))
        result.schedule_slots.append(slot)

    # ── Derived mappings ──────────────────────────────────────────────────────
    for slot in result.schedule_slots:
        for entry in slot.entries:
            result.receiver_bands.setdefault(entry.receiver, set()).add(entry.band)
            for mode in entry.modes.split(':'):
                result.band_modes.setdefault(entry.band, set()).add(mode)

    # ── [hf-timestd] ─────────────────────────────────────────────────────────
    if cfg.has_section('hf-timestd'):
        h = cfg['hf-timestd']
        result.hf_timestd = HfTimestdConfig(
            enabled          = h.getboolean('enabled', False),
            timing_authority = h.get('timing_authority', 'rtp').strip(),
            compression      = h.get('compression', 'zstd').strip(),
            uploader_enabled = h.getboolean('uploader_enabled', False),
            physics_enabled  = h.getboolean('physics_enabled', False),
            storage_quota    = h.get('storage_quota', '70%').strip(),
            archive_path     = h.get('archive_path', '').strip(),
        )

    # ── [ka9q-web] ───────────────────────────────────────────────────────────
    if cfg.has_section('ka9q-web'):
        kw = cfg['ka9q-web']
        result.ka9q_web = Ka9qWebConfig(
            enabled   = kw.getboolean('enabled', False),
            base_port = kw.getint('base_port', 8081),
        )

    # ── Infer locality/radiod_name for receivers that still have blanks ───────
    if result.ka9q_conf_name:
        local_prefix = result.ka9q_conf_name.split('-')[0].lower()
        for rx in result.receivers.values():
            if rx.receiver_type not in ('ka9q', 'ka9q_wwv'):
                continue
            if rx.locality and rx.radiod_name:
                continue
            addr_prefix = rx.address.split('-')[0].lower() if rx.address else ''
            if addr_prefix == local_prefix:
                rx.locality    = rx.locality or 'local'
                rx.radiod_name = rx.radiod_name or result.ka9q_conf_name
            else:
                rx.locality    = rx.locality or 'remote'

    # ── Final fallback: scan /etc/radio/ for a single local radiod conf ───────
    # The radiod conf is the canonical source of truth for the status DNS.
    # Operators shouldn't have to duplicate that name in wsprdaemon.conf when
    # there's an unambiguous local radiod on the host.  Multi-radiod hosts
    # still need an explicit radiod_name (or address-prefix match above).
    import glob as _scan_radiod_confs
    confs = sorted(_scan_radiod_confs.glob('/etc/radio/radiod@*.conf'))
    if len(confs) == 1:
        from pathlib import Path as _P
        only = _P(confs[0]).stem.split('@', 1)[1]
        for rx in result.receivers.values():
            if rx.receiver_type not in ('ka9q', 'ka9q_wwv'):
                continue
            if rx.radiod_name:
                continue
            rx.radiod_name = only
            # Force local: the conf exists on disk, so this radiod is local.
            rx.locality = 'local'

    return result
