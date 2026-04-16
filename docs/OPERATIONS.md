# wsprdaemon v4 — Operations Runbook

Operator-facing runbook for installing, applying, and maintaining a
wsprdaemon v4 client. For per-command flags see [CLI_REFERENCE.md](CLI_REFERENCE.md);
for INI keys see [CONFIGURATION.md](CONFIGURATION.md); for the design model
see [wd-v4-architecture.md](../wd-v4-architecture.md).

---

## 1. Install

### 1.1 Repo layout (Pattern A)

The canonical source-tree location is `/opt/git/wsprdaemon-client` (Pattern A
multi-machine workflow used across the wsprdaemon/hf-timestd toolchain). Any
location works; `wd-ctl` resolves its own root by looking for `deps.conf`
beside the script, then `/opt/wsprdaemon`, then `/home/wsprdaemon/wsprdaemon-client`.

### 1.2 Prerequisites

- Linux with `systemd`.
- `python3` (stdlib only; no `pip` packages required for the orchestrator
  itself — `wdlib` is pure stdlib).
- Standard build chain (`make`, `cmake`, `gcc`) for ka9q-radio / ka9q-web /
  onion when those peer services are enabled.
- `git`, `inotifywait` (package `inotify-tools`), `bc`, `sox`, `findmnt`,
  `awk`/`sed`/`grep`.
- Hardware-specific drivers (RX888, Fobos, etc.) — installed out-of-band.
- An existing `radiod` (ka9q-radio) for KA9Q receivers, or reachable
  KiwiSDRs for Kiwi receivers.

### 1.3 Run the installer

```
sudo ./install.sh
```

[install.sh](../install.sh) performs the following:

- Creates the `wsprdaemon` system user (in group `radio`).
- Creates the FHS directory tree:
  - `/etc/wsprdaemon/` and `/etc/wsprdaemon/env/` (mode 2775, group `radio`)
  - `/var/spool/wsprdaemon/{recording,posting,posting/uploads/{wsprnet,wsprdaemon}}`
  - `/var/log/wsprdaemon/`
  - `/opt/wsprdaemon/{lib,bin}`
- Copies [lib/wdlib](../lib/wdlib) to `/opt/wsprdaemon/lib/wdlib`.
- Copies [bin/wd-ka9q-record.py](../bin/wd-ka9q-record.py) to
  `/opt/wsprdaemon/bin/`.
- Copies [deps.conf](../deps.conf) to `/opt/wsprdaemon/deps.conf` so the
  installed `wd-ctl` can find dependency pins.
- Installs the bundled decoder binaries (`wsprd`, `jt9` for x86, arm64,
  armhf) to `/opt/wsprdaemon/bin/decoders/`.
- Installs the wrapper executables (`wd-ctl`, `wd-ka9q-record`,
  `wd-kiwi-record`, `wd-kiwi-cleanup`, `wd-decode`, `wd-post`) to
  `/usr/local/sbin/`.
- Installs the systemd templates (`wd-ka9q-record@`, `wd-kiwi-record@`,
  `wd-decode@`, `wd-post@`, `wsprdaemon.target`) to `/etc/systemd/system/`
  and runs `systemctl daemon-reload`.

`install.sh --uninstall` removes binaries and unit files but preserves
`/etc/wsprdaemon`, `/var/log/wsprdaemon`, and `/var/spool/wsprdaemon`.

> Note: `install.sh` ships only the recorder/decoder/poster set. The upload,
> ka9q-web, and spool-clean templates are deployed by `wd-ctl apply` (which
> copies the rest of [systemd/](../systemd/) into place on first apply, see
> [bin/wd-ctl](../bin/wd-ctl) `cmd_apply`).

---

## 2. First-Run Workflow

### 2.1 Provide a config

Two paths:

1. **Migrate from v3.** If a working `wsprdaemon.conf` (bash-array form)
   already exists:
   ```
   sudo wd-ctl migrate-config /path/to/old/wsprdaemon.conf
   ```
   This invokes [v3_parser.py](../lib/wdlib/v3_parser.py) and
   [ini_writer.py](../lib/wdlib/ini_writer.py) and writes
   `/etc/wsprdaemon/wsprdaemon.conf`. Use `--hostname HOST` to override the
   case-block selector and `--force` to overwrite an existing v4 file.

2. **Greenfield.** Author `/etc/wsprdaemon/wsprdaemon.conf` directly. See
   [CONFIGURATION.md](CONFIGURATION.md) for every section and key the v4
   parser ([v4_parser.py](../lib/wdlib/v4_parser.py)) understands.

### 2.2 Validate before applying

```
sudo wd-ctl validate
```

Runs schedule sanity, entry-point reachability, SSRC uniqueness, and the
ka9q-python version floor (see §6 below). Use `--json` to consume the
contract v0.4 payload programmatically.

### 2.3 Apply

```
sudo wd-ctl apply
```

`apply` is idempotent and reconciles the running fleet. Re-run after every
config edit. See §3 for what it does step-by-step.

---

## 3. Day-to-Day Operations

### 3.1 Status

```
sudo wd-ctl status
```

Lists every `wd-ka9q-record@*`, `wd-kiwi-record@*`, `wd-decode@*`,
`wd-post@*`, `wd-upload-*`, and `wd-ka9q-web@*` unit with its current active
state. Equivalent to a hand-rolled `systemctl list-units 'wd-*'` filtered to
the wsprdaemon unit set.

### 3.2 Apply after a config change

```
sudo wd-ctl apply
```

`apply` will:

1. Parse `/etc/wsprdaemon/wsprdaemon.conf`.
2. Regenerate every `/etc/wsprdaemon/env/*.env` from the parsed model
   ([envgen.py](../lib/wdlib/envgen.py)).
3. Reinstall systemd unit templates if the source tree has newer copies.
4. Optionally deploy hf-timestd, ka9q-web, and the Remote Access Channel
   based on `[hf-timestd]`, `[ka9q-web]`, and `general.rac`.
5. Recompute the tmpfs sizing for `/var/spool/wsprdaemon` (with WSPR/IQ/Kiwi
   per-band byte estimates × retention windows), write
   `/etc/systemd/system/var-spool-wsprdaemon.mount`, and ensure the tmpfs
   is mounted.
6. `systemctl enable --now` every recorder, decoder, post, upload, and
   spool-clean unit derived from the env file set.
7. Pin local `radiod@` instances and worker services to disjoint CPU sets
   (HT pairs from `/sys/devices/system/cpu/...`) and write
   `99-wdctl-cpu-affinity.conf` drop-ins. `general.reserved_cpus` is
   honored.

### 3.3 Stop everything

```
sudo wd-ctl teardown
```

Stops (with `--ignore-not-found`) the recorder, decoder, poster, ka9q-web,
hf-timestd, spool-clean, and any local `radiod@` instance the config
references. Does not delete env files, configs, or spool data.

---

## 4. Logs

Each daemon writes to `/var/log/wsprdaemon/<name>.log` via
[logger.py](../lib/wdlib/logger.py).

- File cap: `MAX_LOG_BYTES = 1 048 576` (1 MB). When exceeded the file is
  truncated to the **first 25%** of lines (preserving startup context) plus
  a one-line `--- log truncated: kept N/M lines ---` marker.
- Each handler also writes to `stderr`; under systemd that is captured by
  journald, so `journalctl -u <unit>` is the always-available fallback.
- `wd-decode` and `wd-post` source `lib/wd-loglevel.sh` for contract §11
  level resolution; `wd-upload-wsprnet` and `wd-upload-wsprdaemon` resolve
  it natively in Python.

### 4.1 Effective log level (contract §11)

[contract.py:resolve_log_level](../lib/wdlib/contract.py) walks this
precedence:

1. `WSPRDAEMON_LOG_LEVEL` (per-client override)
2. `CLIENT_LOG_LEVEL` (sigmond fleet-wide)
3. `INFO` (default)

Acceptable values: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`.

### 4.2 SIGHUP re-read

Each long-running Python daemon installs a `SIGHUP` handler that re-sources
`/etc/sigmond/coordination.env` (if present) and re-resolves the log level
in place. Send a HUP rather than restarting the unit:

```
sudo systemctl kill --signal=SIGHUP wd-upload-wsprnet@AC0G_EM38ww.service
```

### 4.3 Live pipeline view

```
wdwatch [BAND [MODE]]
```

[wdwatch](../bin/wdwatch) tails `wd-decode@*` and `wd-upload-wsprnet@*`
journals, reformats ISO-8601 timestamps to `YYMMDD HH:MM:SS`, and filters
to the lifecycle keywords (`DECODE START`, `DECODE DONE`, `UPLOAD START`,
`UPLOAD DONE`, `UPLOAD FAILED`, `MULTI CYCLE`, `PARTIAL DUMP`, `DIAG …`).

---

## 5. Dynamic Verbosity

```
sudo wd-ctl verbosity up|down <service>|all
```

Per [bin/wd-ctl](../bin/wd-ctl) `cmd_verbosity`, this is currently a stub
that prints a "not yet implemented" notice. The intended behavior is to
send `SIGUSR1` (raise verbosity one level: `INFO → DEBUG`) or `SIGUSR2`
(lower one level) to the named unit's `MainPID`, or to all `wd-*` units
when the target is `all`. Until it lands, raise verbosity by setting
`WSPRDAEMON_LOG_LEVEL=DEBUG` in the unit drop-in (or in
`/etc/sigmond/coordination.env`) and `systemctl kill --signal=SIGHUP …`.

---

## 6. Validation (`wd-ctl validate`)

[contract.py:build_validate](../lib/wdlib/contract.py) runs the following
checks; everything is also reachable as JSON via `wd-ctl validate --json`.

- **Schedule sanity** — every `[schedule:*]` entry must reference a
  declared receiver; KA9Q receivers must not appear in any non-`00:00`
  time slot (the v3 ⟶ v4 invariant: KA9Q recorders are 1-to-N and never
  schedule-toggled).
- **Entry-point reachability (contract §12.1)** —
  [bin/wd-ctl](../bin/wd-ctl) and
  [bin/wd-ka9q-record.py](../bin/wd-ka9q-record.py) must contain an
  `if __name__ == '__main__':` guard.
- **SSRC uniqueness (contract §12.2)** — within a single KA9Q receiver,
  two bands resolving to the same `BAND_FREQ_HZ` value would collide on
  the radiod side (preset/rate/encoding are constants:
  `usb / 12000 / f32`).
- **ka9q-python version floor (contract §12.6)** — compares the installed
  `ka9q.__version__` against the `[ka9q-python]` `commit` pin in
  [deps.conf](../deps.conf); not-importable or below-pin yields a `warn`.

### 6.1 Sample `--json` output

```json
{
  "ok": true,
  "config_path": "/etc/wsprdaemon/wsprdaemon.conf",
  "contract_version": "0.4",
  "issues": [
    {
      "severity": "warn",
      "message": "ka9q-python installed=3.7.2 < deps.conf pin 3.8.0 (run wd-ctl install-deps)"
    }
  ]
}
```

`ok` is `false` if any issue has `severity: error`. The `config_path` is
always the absolute resolved path (contract §3 disclosure rule).

---

## 7. Inventory and Version

Sigmond-facing self-describe surfaces:

```
sudo wd-ctl inventory --json
sudo wd-ctl version
```

`inventory --json` returns the contract v0.4 payload: `client`, `version`,
`contract_version`, `config_path`, `log_level`, `git`, the per-instance
list (`receiver`, `band`, `radiod_id`, `frequency_hz`, `decode_modes`),
the parsed `deps.conf` (`{git: [...], pypi: [...]}`), and any open
`issues`. `log_paths` is included when `/var/log/wsprdaemon` is readable.

`version` returns `{client, version, contract_version}` only — useful for
quick scrape checks before committing to a fuller inventory pull.

Both commands wrap their config-load path in `contract.StderrRedirect()`
so stdout stays pure JSON even when the parser logs warnings.

---

## 8. Troubleshooting

### 8.1 `wd-ctl apply` fails: "config not found"

`/etc/wsprdaemon/wsprdaemon.conf` does not exist. Either run
`wd-ctl migrate-config` or create one by hand
([CONFIGURATION.md](CONFIGURATION.md)).

### 8.2 `wd-ka9q-record@<rx>` keeps restarting

Likely causes:

- **ka9q-python missing or wrong version.** Check
  `/opt/wsprdaemon/venv/bin/python3 -c 'import ka9q; print(ka9q.__version__)'`.
  Run `sudo wd-ctl install-deps` to install/upgrade per
  [deps.conf](../deps.conf).
- **wspr-recorder missing.** `wd-ka9q-record` execs
  `/opt/wsprdaemon/venv/bin/wspr-recorder`. Verify it is installed:
  `ls -l /opt/wsprdaemon/venv/bin/wspr-recorder`.
- **radiod unreachable.** Verify `<radiod_name>-status.local` resolves
  (mDNS) and the multicast group is reachable:
  `getent hosts <radiod_name>-status.local`,
  `journalctl -u radiod@<radiod_name>.service -n 50`.

### 8.3 SSRC collision warning during validate

Two bands on the same receiver point at the same RF frequency. Remove the
duplicate from the `[receiver:NAME:BAND]` set or move it to a separate
receiver. `wd-ctl validate --json` lists the offending instance under
`issues[*].instance`.

### 8.4 Spool full / tmpfs full

Inspect: `df -h /var/spool/wsprdaemon`. The size is computed by
`wd-ctl apply` (see §3.2 step 5). If your `[receiver:*:BAND] modes` set
grew (especially with `F30`), re-run apply so the mount unit is rewritten.
Manual cleanup: `sudo systemctl start wd-spool-clean.service`. The janitor
([wd-spool-clean](../bin/wd-spool-clean)) keeps each band's recording
dir trimmed to `max(mode_window) + 2 min`.

### 8.5 Kiwi WAV truncation on stop

Expected. `kiwi_recorder.py` truncates the in-flight WAV when it receives
SIGTERM. [wd-kiwi-cleanup](../bin/wd-kiwi-cleanup) runs as
`ExecStopPost=` and deletes any newest file smaller than
`1 440 044 - 48 000` bytes (≥2 s short of a full 60-s 16-kHz mono PCM
recording). If this is firing repeatedly, check why the recorder is being
stopped (schedule, manual restart, OOM).

### 8.6 wsprnet partial uploads

Watch for `PARTIAL DUMP` lines in `wdwatch` or
`/var/log/wsprdaemon/wd-upload-wsprnet@*.log`. Each partial-acceptance
upload is dumped to `/var/log/wsprdaemon/wsprnet-partial/` along with the
server's verbatim response and an automated spot analysis (grid format,
WSPR power level standardness, intra-batch duplicates) from
`_analyze_spots()` in [wd-upload-wsprnet](../bin/wd-upload-wsprnet).

### 8.7 Log file not growing

Two confounders:

1. The 25%-truncation in [logger.py](../lib/wdlib/logger.py) keeps only
   the **first** 25% on overflow. Recent activity is in journald, not in
   the file. Use `journalctl -u <unit> -f`.
2. Effective level is higher than expected. Check it via
   `wd-ctl inventory --json | jq .log_level`.
