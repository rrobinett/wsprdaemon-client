"""HamSCI client contract v0.4 surfaces for wsprdaemon-client.

Implements inventory and validate payloads for `wd-ctl inventory --json`
and `wd-ctl validate --json`. See /home/mjh/git/sigmond/docs/CLIENT-CONTRACT.md
for the authoritative schema; CLIENT_NAME/CONTRACT_VERSION here are the
declared conformance level.
"""

import configparser
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from wdlib.config import WdConfig
from wdlib.envgen import BAND_FREQ_HZ


CLIENT_NAME = "wsprdaemon"
CONTRACT_VERSION = "0.4"
DEFAULT_CONFIG_PATH = "/etc/wsprdaemon/wsprdaemon.conf"


# ── §11 log level resolution ────────────────────────────────────────────────

def resolve_log_level() -> str:
    """Resolve effective log level per contract §11 precedence.

    Checks WSPRDAEMON_LOG_LEVEL, then CLIENT_LOG_LEVEL, then defaults to INFO.
    Config-file level (step 4) not consulted here — multi-daemon: each daemon
    honors the env on startup; this aggregates what sigmond would currently
    publish to the fleet.
    """
    for var in ("WSPRDAEMON_LOG_LEVEL", "CLIENT_LOG_LEVEL"):
        val = os.environ.get(var, "").strip().upper()
        if val in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            return val
    return "INFO"


# ── §10 log paths discovery ─────────────────────────────────────────────────

def discover_log_paths(log_dir: str = "/var/log/wsprdaemon") -> Dict[str, Any]:
    """Scan /var/log/wsprdaemon/ for daemon log files.

    Returns a flat dict mapping basename-minus-ext → absolute path. Caller
    omits the key from inventory if the dict is empty.
    """
    out: Dict[str, Any] = {}
    try:
        for entry in sorted(Path(log_dir).iterdir()):
            if entry.is_file() and entry.suffix in (".log", ".txt"):
                out[entry.stem] = str(entry)
    except (FileNotFoundError, PermissionError):
        return {}
    return out


# ── git describe ────────────────────────────────────────────────────────────

def git_info(repo_root: Path) -> Optional[Dict[str, Any]]:
    """Return {sha, short, ref, dirty} for the repo at repo_root, or None."""
    def _git(*args: str) -> Optional[str]:
        try:
            r = subprocess.run(
                ["git", "-C", str(repo_root), *args],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                return None
            return r.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None

    sha = _git("rev-parse", "HEAD")
    if not sha:
        return None
    short = _git("rev-parse", "--short", "HEAD") or sha[:7]
    ref = _git("rev-parse", "--abbrev-ref", "HEAD") or ""
    status = _git("status", "--porcelain")
    dirty = bool(status) if status is not None else False
    return {"sha": sha, "short": short, "ref": ref, "dirty": dirty}


def package_version(repo_root: Path) -> str:
    """Best-effort package version. No pyproject.toml — use git describe."""
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_root), "describe", "--tags", "--always", "--dirty"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "0.0.0-unreleased"


# ── deps.conf → inventory.deps ──────────────────────────────────────────────

def parse_deps_conf(deps_path: Path) -> Dict[str, List[Dict[str, str]]]:
    """Convert deps.conf to the contract's {git: [...], pypi: [...]} shape."""
    out: Dict[str, List[Dict[str, str]]] = {"git": [], "pypi": []}
    if not deps_path.exists():
        return out
    cp = configparser.ConfigParser()
    cp.read(deps_path)
    for section in cp.sections():
        s = cp[section]
        kind = s.get("type", "").strip()
        if kind == "git":
            out["git"].append({
                "name":   section,
                "url":    s.get("url", "").strip(),
                "commit": s.get("commit", "").strip(),
            })
        elif kind == "pypi":
            out["pypi"].append({
                "name":    section,
                "version": s.get("commit", "").strip(),
            })
    return out


# ── §12.6 ka9q-python PyPI-lag check ────────────────────────────────────────

def check_ka9q_python_version(deps_path: Path) -> Optional[Dict[str, str]]:
    """Compare installed ka9q-python version to deps.conf pin.

    Returns an issue dict if installed < pinned, else None. Absent install
    returns a warn (so operators see the gap).
    """
    cp = configparser.ConfigParser()
    cp.read(deps_path)
    if not cp.has_section("ka9q-python"):
        return None
    pinned = cp["ka9q-python"].get("commit", "").strip()
    if not pinned:
        return None
    try:
        import ka9q  # type: ignore
        installed = getattr(ka9q, "__version__", None)
    except ImportError:
        return {
            "severity": "warn",
            "message":  f"ka9q-python not importable; deps.conf pins {pinned}",
        }
    if installed is None:
        return {
            "severity": "warn",
            "message":  f"ka9q-python installed but exposes no __version__; deps.conf pins {pinned}",
        }
    if _version_tuple(installed) < _version_tuple(pinned):
        return {
            "severity": "warn",
            "message":  f"ka9q-python installed={installed} < deps.conf pin {pinned} (run wd-ctl install-deps)",
        }
    return None


def _version_tuple(s: str) -> Tuple[int, ...]:
    try:
        return tuple(int(p) for p in s.split(".") if p.isdigit())
    except Exception:
        return (0,)


# ── §12.1 entry-point reachability ──────────────────────────────────────────

PYTHON_ENTRY_POINTS = [
    ("bin/wd-ctl",             "main"),
    ("bin/wd-ka9q-record.py",  None),   # script form — just needs a shebang + module body
]


def check_entry_points(repo_root: Path) -> List[Dict[str, str]]:
    """Assert every Python entry-point reaches main() via __main__ guard.

    Conservative: grep-style check for `if __name__ == '__main__':` presence.
    """
    issues: List[Dict[str, str]] = []
    for rel, _ in PYTHON_ENTRY_POINTS:
        path = repo_root / rel
        if not path.exists():
            issues.append({"severity": "error", "message": f"entry-point missing: {rel}"})
            continue
        text = path.read_text(errors="replace")
        if "__name__" not in text or "__main__" not in text:
            issues.append({
                "severity": "error",
                "message":  f"{rel}: no `if __name__ == '__main__':` guard (contract §12.1)",
            })
    return issues


# ── §12.2 SSRC uniqueness within a radiod block ─────────────────────────────

def check_ssrc_uniqueness(config: WdConfig) -> List[Dict[str, str]]:
    """Reject duplicate (freq, preset, rate, enc) tuples per KA9Q receiver.

    wsprdaemon-client always uses preset=usb, rate=12000, enc=f32 (see
    wd-ka9q-record.py channel_defaults). Only the frequency varies per band,
    so the check reduces to: each KA9Q receiver's band set must have unique
    frequencies.
    """
    issues: List[Dict[str, str]] = []
    for rx_name, bands in config.receiver_bands.items():
        rx = config.receivers.get(rx_name)
        if rx is None or rx.receiver_type not in ("ka9q", "ka9q_wwv"):
            continue
        seen: Dict[int, str] = {}
        for band in sorted(bands):
            freq = BAND_FREQ_HZ.get(band)
            if freq is None:
                continue
            if freq in seen:
                issues.append({
                    "severity": "error",
                    "instance": f"{rx_name}-{band}",
                    "message": (
                        f"SSRC collision: {rx_name} bands {seen[freq]} and {band} "
                        f"both resolve to {freq} Hz with default preset/rate/encoding "
                        f"(contract §12.2)"
                    ),
                })
            else:
                seen[freq] = band
    return issues


# ── Instance enumeration ────────────────────────────────────────────────────

def enumerate_instances(config: WdConfig) -> List[Dict[str, Any]]:
    """One instance per (receiver, band) — matches wd-decode@%i naming."""
    instances: List[Dict[str, Any]] = []
    for rx_name, bands in sorted(config.receiver_bands.items()):
        rx = config.receivers.get(rx_name)
        if rx is None:
            continue
        for band in sorted(bands):
            freq = BAND_FREQ_HZ.get(band)
            modes = sorted(config.band_modes.get(band, set()))
            instances.append({
                "instance":      f"{rx_name}-{band}".lower(),
                "receiver":      rx_name,
                "receiver_type": rx.receiver_type,
                "band":          band,
                "radiod_id":     rx.radiod_name or None,
                "frequency_hz":  freq,
                "decode_modes":  modes,
                # chain_delay_ns applied is a wspr-recorder-internal concern;
                # wsprdaemon-client does not apply it, so it is not surfaced here.
            })
    return instances


# ── Inventory / validate payload builders ───────────────────────────────────

def build_inventory(config: WdConfig, config_path: str, repo_root: Path) -> Dict[str, Any]:
    deps = parse_deps_conf(repo_root / "deps.conf")
    log_paths = discover_log_paths()
    payload: Dict[str, Any] = {
        "client":           CLIENT_NAME,
        "version":          package_version(repo_root),
        "contract_version": CONTRACT_VERSION,
        "config_path":      str(Path(config_path).resolve()),
        "log_level":        resolve_log_level(),
        "git":              git_info(repo_root),
        "instances":        enumerate_instances(config),
        "deps":             deps,
        "issues":           [],
    }
    if log_paths:
        payload["log_paths"] = log_paths
    return payload


def build_validate(config: WdConfig, config_path: str, repo_root: Path) -> Dict[str, Any]:
    issues: List[Dict[str, Any]] = []

    # existing schedule-sanity checks (ported from cmd_validate)
    for slot in config.schedule_slots:
        for entry in slot.entries:
            rx = config.receivers.get(entry.receiver)
            if not rx:
                issues.append({
                    "severity": "error",
                    "message":  f"schedule references unknown receiver: {entry.receiver}",
                })
                continue
            if rx.receiver_type == "ka9q" and slot.time != "00:00":
                issues.append({
                    "severity": "error",
                    "instance": entry.receiver.lower(),
                    "message": (
                        f"KA9Q receiver {entry.receiver} appears in time-slot "
                        f"{slot.time}; KA9Q recorders must not be schedule-toggled"
                    ),
                })

    issues.extend(check_entry_points(repo_root))
    issues.extend(check_ssrc_uniqueness(config))
    ka9q_issue = check_ka9q_python_version(repo_root / "deps.conf")
    if ka9q_issue:
        issues.append(ka9q_issue)

    ok = not any(i.get("severity") == "error" for i in issues)
    return {
        "ok":               ok,
        "config_path":      str(Path(config_path).resolve()),
        "contract_version": CONTRACT_VERSION,
        "issues":           issues,
    }


# ── Stdout-cleanliness helper ───────────────────────────────────────────────

class StderrRedirect:
    """Context manager that moves sys.stdout writes to sys.stderr for §3.

    wd-ctl's config-load paths use print() and may pollute stdout. Wrap the
    JSON-emitting codepaths with this to keep stdout pure JSON.
    """
    def __enter__(self):
        self._saved = sys.stdout
        sys.stdout = sys.stderr
        return self
    def __exit__(self, *exc):
        sys.stdout = self._saved
        return False
