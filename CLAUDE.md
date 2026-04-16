# CLAUDE.md — wsprdaemon v4 Development Briefing

## Generic Workflow Orchestration

### 1. Plan Mode Default
- Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions)
- If something goes sideways, STOP and re-plan immediately — don't keep pushing
- Use plan mode for verification steps, not just building
- Write detailed specs upfront to reduce ambiguity

### 2. Subagent Strategy
- Use subagents liberally to keep main context window clean
- Offload research, exploration, and parallel analysis to subagents
- For complex problems, throw more compute at it via subagents
- One task per subagent for focused execution

### 3. Self-Improvement Loop
- After ANY correction from the user: update `tasks/lessons.md` with the pattern
- Write rules for yourself that prevent the same mistake
- Ruthlessly iterate on these lessons until mistake rate drops
- Review lessons at session start for relevant project

### 4. Verification Before Done
- Never mark a task complete without proving it works
- Diff behavior between main and your changes when relevant
- Ask yourself: "Would a staff engineer approve this?"
- Run tests, check logs, demonstrate correctness

### 5. Demand Elegance (Balanced)
- For non-trivial changes: pause and ask "is there a more elegant way?"
- If a fix feels hacky: "Knowing everything I know now, implement the elegant solution"
- Skip this for simple, obvious fixes — don't over-engineer
- Challenge your own work before presenting it

### 6. Autonomous Bug Fixing
- When given a bug report: just fix it. Don't ask for hand-holding
- Point at logs, errors, failing tests — then resolve them
- Zero context switching required from the user
- Go fix failing CI tests without being told how

## Task Management

1. **Plan First**: Write plan to `tasks/todo.md` with checkable items
2. **Verify Plan**: Check in before starting implementation
3. **Track Progress**: Mark items complete as you go
4. **Explain Changes**: High-level summary at each step
5. **Document Results**: Add review section to `tasks/todo.md`
6. **Capture Lessons**: Update `tasks/lessons.md` after corrections

## Core Principles

- **Simplicity First**: Make every change as simple as possible. Impact minimal code.
- **No Laziness**: Find root causes. No temporary fixes. Senior developer standards.
- **Minimal Impact**: Changes should only touch what's necessary. Avoid introducing bugs.


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

`tests/` currently holds only a sample `wsprdaemon.conf` used as the canonical
minimal config. The Python unittest suite referenced in earlier drafts of this
briefing is not in the tree. New tests should land in `tests/` and be runnable
with `python3 -m unittest discover tests`.

## Documentation layout

- `README.md` — project entry point, install, quick start
- `docs/OPERATIONS.md` — operator runbook
- `docs/CONFIGURATION.md` — INI config reference
- `docs/SERVICES.md` — per-systemd-unit reference
- `docs/CLI_REFERENCE.md` — `wd-ctl` and `wd-*` script reference
- `docs/INTEGRATION.md` — relationships with wspr-recorder, ka9q-python, hf-timestd, radiod
- `docs/SIGMOND.md` — coordinator integration, contract v0.4 conformance
- `wd-v4-architecture.md` — v0.10 design spec (some sections spec-only)
- `deploy.toml`, `deps.conf` — contract §5 manifest and pinned dependency list

## What's next (open work, 2026-04-15)

1. `wd-hftime@INSTANCE.service` — no unit in `systemd/` yet; integration with
   hf-timestd is spec-only (see arch §3.2).
2. `wsprdaemon.target` exists; `wsprdaemon.service`/`.timer` orchestrator
   (arch §3.7) still to land.
3. tmpfs sizing calculator in the installer.
4. `wd-upload-grape` daemon (arch §3.6) — optional, not started.
5. Python test suite for wdlib (parsers, envgen, contract surfaces).
