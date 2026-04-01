"""FHS-compliant path construction for wsprdaemon v4.

All paths are centralized here so nothing else hardcodes directory names.

Layout:
    /etc/wsprdaemon/              — config files, env files
    /etc/wsprdaemon/env/          — generated environment files for systemd
    /etc/wsprdaemon/components.ini — external dependency commit pins
    /var/spool/wsprdaemon/        — spool root (tmpfs-mountable)
    /var/spool/wsprdaemon/recording/  — wav files from recorders
    /var/spool/wsprdaemon/posting/    — decoded spots awaiting upload
    /var/log/wsprdaemon/          — per-daemon log files
    /run/wsprdaemon/              — runtime PID/state (tmpfs, auto-created)
    /usr/local/sbin/              — installed executables
    /opt/wsprdaemon/              — installer, shared libs
    /etc/systemd/system/          — unit files
"""

import os
from pathlib import Path

# --- Root directories ---
ETC_DIR = Path('/etc/wsprdaemon')
SPOOL_DIR = Path('/var/spool/wsprdaemon')
LOG_DIR = Path('/var/log/wsprdaemon')
RUN_DIR = Path('/run/wsprdaemon')
INSTALL_DIR = Path('/opt/wsprdaemon')
SBIN_DIR = Path('/usr/local/sbin')
SYSTEMD_DIR = Path('/etc/systemd/system')

# --- Config files ---
CONF_FILE = ETC_DIR / 'wsprdaemon.conf'
COMPONENTS_FILE = ETC_DIR / 'components.ini'
ENV_DIR = ETC_DIR / 'env'

# --- Spool subdirectories ---
RECORDING_DIR = SPOOL_DIR / 'recording'
POSTING_DIR = SPOOL_DIR / 'posting'


def receiver_recording_dir(receiver_name: str) -> Path:
    """Recording directory for a receiver: /var/spool/wsprdaemon/recording/<RECEIVER>/"""
    return RECORDING_DIR / receiver_name


def band_recording_dir(receiver_name: str, band: str) -> Path:
    """Band-level recording dir: /var/spool/wsprdaemon/recording/<RECEIVER>/<BAND>/"""
    return RECORDING_DIR / receiver_name / band


def band_posting_dir(receiver_name: str, band: str) -> Path:
    """Per-receiver-per-band posting dir for decoded spots."""
    return POSTING_DIR / receiver_name / band


def env_file(service_template: str, instance: str) -> Path:
    """Environment file for a systemd service instance.

    Example: env_file('wd-decode', 'KA9Q_0-80') →
             /etc/wsprdaemon/env/wd-decode@KA9Q_0-80.env
    """
    return ENV_DIR / f'{service_template}@{instance}.env'


def log_file(daemon_name: str) -> Path:
    """Per-daemon log file: /var/log/wsprdaemon/<daemon>.log"""
    return LOG_DIR / f'{daemon_name}.log'


def upload_dir(target: str, call: str, grid: str,
               receiver_name: str = '', band: str = '') -> Path:
    """Upload queue directory.

    For wsprnet:      /var/spool/wsprdaemon/posting/uploads/wsprnet/<CALL>_<GRID>/
    For wsprdaemon:   /var/spool/wsprdaemon/posting/uploads/wsprdaemon/<CALL>/<RECEIVER>/<BAND>/
    """
    base = POSTING_DIR / 'uploads' / target
    if target == 'wsprnet':
        return base / f'{call}_{grid}'
    else:
        return base / call / receiver_name / band


def instance_id(receiver_name: str, band: str) -> Path:
    """Systemd instance identifier: <RECEIVER>-<BAND>"""
    return f'{receiver_name}-{band}'


def all_dirs() -> list:
    """Return all directories that the installer should create."""
    return [
        ETC_DIR,
        ENV_DIR,
        SPOOL_DIR,
        RECORDING_DIR,
        POSTING_DIR,
        LOG_DIR,
        INSTALL_DIR,
    ]
