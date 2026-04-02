"""Convert parsed v3 config to v4 INI format.

Writes /etc/wsprdaemon/wsprdaemon.conf in the new INI format,
preserving all receiver definitions, band assignments, modes,
and schedule structure from the v3 bash-array config.
"""

from configparser import ConfigParser
from io import StringIO
from pathlib import Path
from typing import Dict, Set

from wdlib.v3_parser import V3Config
from wdlib.envgen import BAND_FREQ_HZ
from wdlib.modes import mode_sort_key


def v3_to_ini(config: V3Config) -> str:
    """Convert a V3Config to INI-format string.

    Returns the INI text with comments explaining each section.
    """
    out = []
    out.append('; wsprdaemon v4 configuration')
    out.append('; Auto-generated from v3 wsprdaemon.conf by wd-ctl migrate-config')
    out.append('; Review and edit as needed.')
    out.append(';')
    out.append('; Decode modes: W2=WSPR-2, F2=FST4W-120, F5=FST4W-300,')
    out.append(';   F15=FST4W-900, F30=FST4W-1800, I1=IQ-record.')
    out.append('; The longest mode per band determines wav file retention count.')
    out.append('')

    # --- [general] ---
    # Find the first non-merge receiver's call/grid
    call = ''
    grid = ''
    for rx in config.receivers.values():
        if not rx.is_merge and rx.call:
            call = rx.call
            grid = rx.grid
            break

    out.append('[general]')
    out.append(f'reporter_call = {call}')
    out.append(f'reporter_grid = {grid}')
    if config.rac:
        out.append(f'rac = {config.rac}')
    if config.ka9q_conf_name:
        out.append(f'ka9q_conf_name = {config.ka9q_conf_name}')
    if config.ka9q_web_dns:
        out.append(f'ka9q_web_dns = {config.ka9q_web_dns}')
    out.append('')

    # --- Receiver sections ---
    # Collect bands and modes per receiver from schedule
    rx_band_modes: Dict[str, Dict[str, Set[str]]] = {}
    for slot in config.schedule_slots:
        for entry in slot.entries:
            if entry.receiver not in rx_band_modes:
                rx_band_modes[entry.receiver] = {}
            if entry.band not in rx_band_modes[entry.receiver]:
                rx_band_modes[entry.receiver][entry.band] = set()
            for m in entry.modes.split(':'):
                rx_band_modes[entry.receiver][entry.band].add(m)

    # Write non-merge receivers first
    for rx_name, rx in sorted(config.receivers.items()):
        if rx.is_merge:
            continue

        out.append(f'[receiver:{rx_name}]')
        out.append(f'type = {rx.receiver_type}')
        out.append(f'address = {rx.address}')
        out.append(f'call = {rx.call}')
        out.append(f'grid = {rx.grid}')
        if rx.password and rx.password != 'NULL':
            out.append(f'password = {rx.password}')
        out.append('')

        # Per-band sections
        if rx_name in rx_band_modes:
            for band in sorted(rx_band_modes[rx_name].keys(),
                             key=lambda b: BAND_FREQ_HZ.get(b, 0)):
                modes = rx_band_modes[rx_name][band]
                modes_str = ' '.join(sorted(modes, key=mode_sort_key))
                freq = BAND_FREQ_HZ.get(band, 0)
                out.append(f'[receiver:{rx_name}:{band}]')
                out.append(f'modes = {modes_str}')
                out.append(f'freq_hz = {freq}')
                out.append('')

    # Write merge receivers
    for rx_name, rx in sorted(config.receivers.items()):
        if not rx.is_merge:
            continue

        out.append(f'[merge:{rx_name}]')
        out.append(f'sources = {" ".join(rx.merge_sources)}')
        out.append(f'call = {rx.call}')
        out.append(f'grid = {rx.grid}')
        out.append('')

        # Per-band sections for merge
        if rx_name in rx_band_modes:
            for band in sorted(rx_band_modes[rx_name].keys(),
                             key=lambda b: BAND_FREQ_HZ.get(b, 0)):
                modes = rx_band_modes[rx_name][band]
                modes_str = ' '.join(sorted(modes, key=mode_sort_key))
                out.append(f'[merge:{rx_name}:{band}]')
                out.append(f'modes = {modes_str}')
                out.append('')

    # --- Schedule section ---
    # For now, write a single default schedule
    # (v3 configs with one time slot at 00:00 are effectively always-on)
    if config.schedule_slots:
        out.append('; Schedule')
        out.append('; KA9Q receivers MUST NOT appear in schedule start/stop transitions.')
        out.append('; Only Kiwi receivers with limited channels use schedule transitions.')
        for i, slot in enumerate(config.schedule_slots):
            sect_name = 'default' if slot.time == '00:00' else f'time_{slot.time.replace(":", "")}'
            out.append(f'[schedule:{sect_name}]')
            out.append(f'time = {slot.time}')

            # Group entries by receiver
            rx_entries: Dict[str, list] = {}
            for entry in slot.entries:
                if entry.receiver not in rx_entries:
                    rx_entries[entry.receiver] = []
                rx_entries[entry.receiver].append(f'{entry.band}')

            for rx, bands in rx_entries.items():
                out.append(f'{rx} = {" ".join(bands)}')
            out.append('')

    # --- Upload sections ---
    out.append('[upload:wsprnet]')
    out.append('enabled = true')
    out.append(f'call = {call}')
    out.append(f'grid = {grid}')
    out.append('')

    out.append('[upload:wsprdaemon]')
    out.append('enabled = true')
    out.append('')

    return '\n'.join(out) + '\n'
