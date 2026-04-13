# CLAUDE.md — wsprdaemon v4 Development Briefing

## What this project is

wsprdaemon is a Linux system that decodes WSPR and FST4W weak-signal radio
transmissions and reports spots to online databases. This is a major rewrite
from a monolithic ~15,700-line bash script to a **systemd service-oriented
architecture with Python core logic**.

The developer is Rob (callsign AI6VN, grid CM87tj).

## Architecture overview

wsprdaemon v4 is a **control plane** that reads configuration and ensures
external services are installed, configured, and running. The core is Python
(stdlib only, no pip deps). Bash is used only for thin systemd ExecStart wrappers.

### Service pipeline

```
Config → wd-ctl apply → env files → systemd services
                                      ├── wd-ka9q-record@RX  (1:N, continuous)
                                      ├── wd-kiwi-record@RX-BAND (1:1, schedulable)
                                      ├── wd-decode@RX-BAND (per band)
                                      └── wd-post@RX-BAND (per band)
```

### Key domain rules

- **KA9Q receivers**: 1-to-N model. One `wd_record` process listens to a single
  multicast stream and produces wav files for all component bands. **Never
  schedule-toggled.** Any schedule entry referencing KA9Q for start/stop is a
  config error.
- **Kiwi recorders**: 1:1 per channel. Subject to schedule transitions.
  `kiwi_recorder.py` is used as-is (not modified). It truncates the current wav
  file on SIGTERM — the wrapper deletes the truncated file.
- **Decode modes**: `W` prefix → `wsprd` (after sox concat + 16-bit PCM conversion);
  `F` prefix → `jt9` with period flag. Number = window minutes.
  Valid: W2, F2, F5, F15, F30, I1.
- **inotifywait**: Works for KA9Q wav files (clean open/close). Does NOT work
  for Kiwi wav files (continuous open/close during write) — Kiwi uses polling.
- **BindsTo=**: KA9Q decoders use BindsTo= on their recorder. If recorder fails,
  all downstream decoders restart.

### File layout (FHS compliant)

```
/etc/wsprdaemon/              Config, env files
/etc/wsprdaemon/env/          Generated .env files for systemd
/var/spool/wsprdaemon/        Spool root (tmpfs-mountable)
  recording/                  Wav files from recorders
  posting/                    Decoded spots awaiting upload
/var/log/wsprdaemon/          Per-daemon log files
/run/wsprdaemon/              Runtime state (tmpfs)
/usr/local/sbin/              Installed executables
/opt/wsprdaemon/              Installer, shared libs
/etc/systemd/system/          Unit files
```

### Python package: wdlib (stdlib only)

```
lib/wdlib/
  __init__.py
  modes.py       — Decode mode parsing (W2→wsprd, F2→jt9, etc.)
  paths.py       — FHS path construction
  logger.py      — Per-daemon log files, 1MB cap, 25% truncation
  systemd.py     — systemctl wrapper
  envgen.py      — Environment file generation
  v3_parser.py   — V3 bash-array config parser
  ini_writer.py  — V3→V4 INI config converter
```

### wd-ctl subcommands

- `migrate-config INPUT [-o DIR]` — Convert v3 bash config → INI + env files
- `apply` — Parse config, generate env files, reconcile running services
- `teardown` — Stop and disable all services
- `status` — Show service status
- `validate [--json]` — Check config for errors (contract §12)
- `inventory` — Emit contract v0.4 inventory JSON (§3)
- `version` — Emit client/contract version JSON
- `verbosity up|down SERVICE|all` — Dynamic log verbosity via USR1/USR2

## Key collaborator

Michael Hauan (AC0G, GitHub: mijahauan) — `hf-timestd` project integrated
as an external dependency.

## Testing

Run tests: `python3 -m unittest tests.test_migration -v`
Currently 57 tests, 56 pass, 1 skip (merge env naming edge case).

## What's next

1. `wd-ctl apply` implementation (compute desired service set, diff with running)
2. BindsTo= drop-in generation for KA9Q decoder→recorder dependency
3. Schedule evaluation (Kiwi channel management)
4. tmpfs sizing calculator in the installer
5. Upload daemons (wd-upload-wsprnet, wd-upload-wsprdaemon)
