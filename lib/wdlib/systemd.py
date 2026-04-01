"""Systemd service management for wsprdaemon v4.

Thin wrappers around systemctl for starting, stopping, enabling,
and querying services. All operations go through subprocess to
call systemctl directly — no dbus dependency.
"""

import subprocess
from typing import List, Optional


def _run(args: List[str], check: bool = False) -> subprocess.CompletedProcess:
    """Run a systemctl command."""
    cmd = ['systemctl'] + args
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def daemon_reload():
    """Run systemctl daemon-reload after unit file changes."""
    _run(['daemon-reload'], check=True)


def start(unit: str):
    _run(['start', unit], check=True)


def stop(unit: str):
    _run(['stop', unit], check=False)  # Don't fail if already stopped


def restart(unit: str):
    _run(['restart', unit], check=True)


def enable(unit: str):
    _run(['enable', unit], check=True)


def disable(unit: str):
    _run(['disable', unit], check=False)


def is_active(unit: str) -> bool:
    result = _run(['is-active', '--quiet', unit])
    return result.returncode == 0


def is_enabled(unit: str) -> bool:
    result = _run(['is-enabled', '--quiet', unit])
    return result.returncode == 0


def status(unit: str) -> str:
    """Return the full status output for a unit."""
    result = _run(['status', unit])
    return result.stdout


def list_units(pattern: str) -> List[str]:
    """List active units matching a pattern like 'wd-decode@*'."""
    result = _run([
        'list-units', '--type=service', '--no-legend',
        '--no-pager', pattern
    ])
    units = []
    for line in result.stdout.strip().splitlines():
        parts = line.split()
        if parts:
            units.append(parts[0])
    return units


def get_main_pid(unit: str) -> Optional[int]:
    """Get the MainPID of a running service (for signal-based verbosity)."""
    result = _run(['show', '-p', 'MainPID', '--value', unit])
    try:
        pid = int(result.stdout.strip())
        return pid if pid > 0 else None
    except (ValueError, AttributeError):
        return None


def stop_and_disable(unit: str):
    """Stop and disable a unit."""
    stop(unit)
    disable(unit)


def enable_and_start(unit: str):
    """Enable and start a unit."""
    enable(unit)
    start(unit)
