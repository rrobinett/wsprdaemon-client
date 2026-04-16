# wsprdaemon v4 — CLI Reference

Reference for every executable shipped under [bin/](../bin/) and installed
to `/usr/local/sbin/`. For the runbook see [OPERATIONS.md](OPERATIONS.md);
for INI keys see [CONFIGURATION.md](CONFIGURATION.md).

Conventions:

- Synopsis lines use `[option]` for optional and `<arg>` for required.
- "Systemd-only" scripts are intended to be run by their unit's
  `ExecStart=` line (env vars sourced from `/etc/wsprdaemon/env/<unit>.env`)
  and are not safe to invoke by hand without setting those vars.
- Exit codes follow the Unix convention: `0` success, non-zero failure.

---

## A. `wd-ctl` — orchestrator

Source: [bin/wd-ctl](../bin/wd-ctl). Subcommand dispatch in `main()`. All
subcommands write to `/etc/wsprdaemon/env/`, `/etc/systemd/system/`, etc.,
so most require `sudo`.

### A.1 `wd-ctl migrate-config <input> [-o DIR] [--hostname HOST] [--force]`

Convert a v3 bash-array `wsprdaemon.conf` to the v4 INI format.

| Flag | Meaning |
| --- | --- |
| `<input>` | Path to the v3 config (positional). |
| `-o`, `--output-dir` | Output directory; defaults to `/etc/wsprdaemon`. |
| `--hostname` | Override the hostname used to select a `case` block in the v3 file (defaults to `socket.gethostname()`). |
| `--force` | Overwrite an existing `wsprdaemon.conf`. |

Calls [v3_parser.parse_v3_config](../lib/wdlib/v3_parser.py) then
[ini_writer.write_v4_ini](../lib/wdlib/ini_writer.py) and reports the
receiver/slot count summary. Exit 1 if the v3 file fails to parse or the
output already exists without `--force`.

### A.2 `wd-ctl apply`

Reconcile system state with `/etc/wsprdaemon/wsprdaemon.conf`.

No flags. The behavior is described in [OPERATIONS.md §3.2](OPERATIONS.md);
the implementation is `cmd_apply` in [bin/wd-ctl](../bin/wd-ctl) and covers:
config parse, env-file generation, unit installation, peer services
(`hf-timestd`, `ka9q-web`, RAC), tmpfs sizing/mount, service start,
`radiod@` reconciliation, and CPU affinity. Returns 0 on completion;
non-zero on parse error or when not running as root.

### A.3 `wd-ctl teardown`

Stop and disable every wsprdaemon-managed service plus the local
`radiod@` instances declared in the config. Uses
`systemctl stop --ignore-not-found` for each unit family, then
`systemctl reset-failed`. Does not delete env files, configs, or spool data.

### A.4 `wd-ctl status`

Print one line per unit matched against the patterns
`wd-ka9q-record@*`, `wd-kiwi-record@*`, `wd-decode@*`, `wd-post@*`,
`wd-upload-*`, `wd-ka9q-web@*`. Unit listing comes from
[wdlib.systemd.list_units](../lib/wdlib/systemd.py); active state from
`is_active`.

### A.5 `wd-ctl validate [-c CONFIG] [--json]`

Run the contract v0.4 validation pass; do **not** mutate state.

| Flag | Meaning |
| --- | --- |
| `-c`, `--config` | Override config path (defaults to `contract.DEFAULT_CONFIG_PATH = /etc/wsprdaemon/wsprdaemon.conf`). |
| `--json` | Emit the full payload on stdout (JSON). All non-JSON output is redirected to stderr (`StderrRedirect`). |

Checks: schedule sanity, entry-point reachability (§12.1), KA9Q SSRC
uniqueness (§12.2), ka9q-python version floor (§12.6). Exit 0 if no
`error`-severity issues; 1 otherwise.

### A.6 `wd-ctl inventory [-c CONFIG] [--json]`

Emit the contract v0.4 inventory payload (always JSON; `--json` is on by
default). Implementation: `cmd_inventory` →
[contract.build_inventory](../lib/wdlib/contract.py). Always exits 0; an
absent config yields a `warn` issue and an empty `instances` list.

### A.7 `wd-ctl version`

Emit `{client, version, contract_version}` on stdout. `version` is
`git describe --tags --always --dirty` against the source tree, falling
back to `0.0.0-unreleased`. Always exits 0.

### A.8 `wd-ctl verbosity <up|down> <service|all>`

Currently a stub: prints "not yet implemented" and exits 0. The intended
implementation is to send `SIGUSR1`/`SIGUSR2` to the named unit's
`MainPID`. See [OPERATIONS.md §5](OPERATIONS.md) for the workaround.

### A.9 `wd-ctl install-deps`

Iterate over [deps.conf](../deps.conf) and install/verify each entry.

- `type = git`: clone `url` to `install_to` if missing, then
  `git checkout commit`. Mismatches print a remediation hint; the
  command returns non-zero when any dep is wrong.
- `type = pypi`: install into `/opt/wsprdaemon/venv` via `pip`. Handles
  both PyPI names and `git+…` URLs. Creates the venv on first run.

Not in the public contract but exposed in `main()`'s subparser table.

---

## B. Per-Daemon Executables

Each of these is invoked as the `ExecStart=` of the matching systemd
unit template under [systemd/](../systemd/). Env vars come from
`/etc/wsprdaemon/env/<unit>.env`, written by
[envgen.generate_all_env_files](../lib/wdlib/envgen.py).

### B.1 `wd-ka9q-record` — KA9Q WSPR recorder (systemd-only)

Source: [bin/wd-ka9q-record](../bin/wd-ka9q-record) (bash wrapper),
[bin/wd-ka9q-record.py](../bin/wd-ka9q-record.py) (Python body).

Role: thin wrapper that validates env vars and execs the AC0G
[wspr-recorder](https://github.com/mijahauan/wspr-recorder) under
`/opt/wsprdaemon/venv/bin/wspr-recorder`. The Python body composes a
TOML config from env vars plus the per-band `WD_RECEIVER_MODES` it reads
from sibling `wd-decode@<rx>-<band>.env` files; the recorder then handles
channel lifecycle (via ka9q-python `RadiodControl`), RTP reception,
minute-aligned WAV writes, gap detection, and per-period peak-normalized
int16 WAVs with JSON sidecars.

Required env (set by `envgen.generate_ka9q_record_env`):

- `WD_RECEIVER_NAME`, `WD_RECEIVER_TYPE` (`ka9q` or `ka9q_wwv`)
- `WD_KA9Q_LOCALITY` (`local` | `remote`)
- `WD_KA9Q_RADIOD_NAME`, `WD_RADIOD_STATUS`
  (the latter is read from `/etc/radio/radiod@<name>.conf` when local,
  else `<radiod_name>-status.local`)
- `WD_RECEIVER_CALL`, `WD_RECEIVER_GRID`
- `WD_RECORDING_DIR`, `WD_LOG_DIR`, `WD_RUN_DIR`
- `WD_BANDS` — space-separated band labels, sorted by frequency
- `WD_GAIN_DB` — optional (default `0`)
- `WD_ENV_DIR` — optional override for the per-band env lookup
  (default `/etc/wsprdaemon/env`)

Outputs WAVs into `WD_RECORDING_DIR/<band>/` named
`YYYYMMDDTHHMMSSz_<freq_hz>_usb_<period>.wav` with `.json` sidecars.

### B.2 `wd-ka9q-record.py` (systemd-only via `wd-ka9q-record`)

The Python body. Same env contract as B.1; it is what
`/usr/local/sbin/wd-ka9q-record` execs into. Logs a structured TOML
config to `/run/wsprdaemon/wspr-rec-<RX>.sock` and a status JSON to
`<recording_dir>/wspr-recorder-status.json`.

### B.3 `wd-kiwi-record` — Kiwi recorder (systemd-only)

Source: [bin/wd-kiwi-record](../bin/wd-kiwi-record). Wrapper for
`/opt/wsprdaemon/kiwiclient/kiwirecorder.py` — one instance per
`<RECEIVER>-<BAND>`. Writes minute-aligned 16-bit PCM 12 kHz mono WAVs
named `YYYYMMDDTHHMMSSz_<freq_hz>_usb.wav` into `WD_RECORDING_DIR`.

Required env (from `envgen.generate_kiwi_record_env`):

- `WD_RECEIVER_NAME`
- `WD_KIWI_ADDRESS` (`host:port`)
- `WD_RECEIVER_FREQ_HZ`
- `WD_RECEIVER_BAND`, `WD_RECEIVER_MODES`
- `WD_RECEIVER_CALL`, `WD_RECEIVER_GRID`
- `WD_KIWI_PASSWORD` (empty if `password = NULL`)
- `WD_RECORDING_DIR`, `WD_LOG_DIR`, `WD_RUN_DIR`

`exec`s `kiwirecorder.py` so SIGTERM from systemd reaches the recorder
directly. The currently-being-written WAV is truncated on shutdown — see
B.4.

### B.4 `wd-kiwi-cleanup` — `ExecStopPost` for `wd-kiwi-record@`

Source: [bin/wd-kiwi-cleanup](../bin/wd-kiwi-cleanup).

```
wd-kiwi-cleanup <RECEIVER-BAND>
```

Examines the most recently modified `*.wav` in
`/var/spool/wsprdaemon/recording/<RECEIVER>/<BAND>/`; deletes it if
under `1 440 044 − 48 000 = 1 392 044` bytes (i.e. ≥2 s short of a full
60-s recording). Safe to invoke manually. Exit 0 always.

### B.5 `wd-decode` — decoder loop (systemd-only)

Source: [bin/wd-decode](../bin/wd-decode). One instance per
`<RECEIVER>-<BAND>`. Watches `WD_RECORDING_DIR` for completed WAVs:

- KA9Q sources: `inotifywait -e moved_to` on `*.wav`.
- Kiwi sources: `sleep 60` polling (kiwi recorder opens/closes
  continuously during writes).

For each ready period-length WAV it dispatches per `WD_RECEIVER_MODES`:

- `W` modes → `wsprd` (architecture-selected from
  `/opt/wsprdaemon/bin/decoders/wsprd-{x86,arm64,armhf}-vNN`).
- `F` modes → `jt9 --fst4w -p <period_sec>`.
- `I` modes → archive only, no decode.

Spot lines are written to a posting file
`<RECEIVER>_<BAND>_<MODE>_<UTC_TS>_spots.txt` and `mv`'d into
`WD_POSTING_DIR`. Sidecar honoring (`*.json` next to `*.wav`) gates each
mode against `decode_modes` / `period_seconds` from wspr-recorder.

Required env:
`WD_RECEIVER_NAME`, `WD_RECEIVER_TYPE`, `WD_RECEIVER_BAND`,
`WD_RECEIVER_MODES`, `WD_RECEIVER_FREQ_HZ`,
`WD_RECORDING_DIR`, `WD_POSTING_DIR`, `WD_RUN_DIR`.

Sources [lib/wd-loglevel.sh](../lib/wd-loglevel.sh) for §11 log-level
resolution + SIGHUP handler.

### B.6 `wd-post` — posting daemon (systemd-only)

Source: [bin/wd-post](../bin/wd-post). One instance per
`<RECEIVER>-<BAND>`. Watches `WD_POSTING_DIR` for `*_spots.txt`
(via `inotifywait -e moved_to,close_write -t 5`), then:

- Drops files older than 600 s.
- Copies each into `WD_UPLOAD_WSPRDAEMON_DIR` (per-receiver) and
  `WD_UPLOAD_WSPRNET_DIR` (per call+grid) when those env vars are set.
- Removes the source after fan-out.

Required env: `WD_RECEIVER_NAME`, `WD_RECEIVER_BAND`, `WD_POSTING_DIR`,
`WD_RECEIVER_CALL`, `WD_RECEIVER_GRID`. Optional:
`WD_UPLOAD_WSPRNET_DIR`, `WD_UPLOAD_WSPRDAEMON_DIR`.

### B.7 `wd-upload-wsprnet` — wsprnet.org uploader

Source: [bin/wd-upload-wsprnet](../bin/wd-upload-wsprnet). One instance
per reporter `CALL+GRID`. Polls `WD_UPLOAD_WSPRNET_DIR` (default 5 s,
`WD_POLL_INTERVAL`); after `WD_STABLE_POLLS` consecutive equal-count
polls (default 1) it batches up to 999 spots, sorts by date/time/freq,
deduplicates near-duplicates (1 kHz window, best SNR wins), and
multipart-POSTs to `http://wsprnet.org/meptspots.php`.

Side-effects:

- `/var/log/wspr.log` — appended in KA9Q standard format; capped at
  ~1 MB with 25 % oldest discarded.
- `/var/log/wsprdaemon/wsprnet-uploads/` — last 96 batches (env
  `WD_UPLOAD_ARCHIVE_KEEP`).
- `/var/log/wsprdaemon/wsprnet-partial/` — last 10 dumps of partial /
  rejected uploads with server response and spot analysis.
- `/var/log/wsprdaemon/wsprnet-partial/dedup_rejected.txt` — rolling
  500 KB cap.

Other env knobs: `WD_VERSION` (default `4.0`), `WD_DIAGNOSTIC_UPLOAD=1`
to switch to per-spot diagnostic POSTs as the primary upload path.

Suitable for manual invocation when debugging — but only with the env
vars present.

### B.8 `wd-upload-wsprdaemon` — wsprdaemon.org uploader

Source: [bin/wd-upload-wsprdaemon](../bin/wd-upload-wsprdaemon). One
instance per reporter `CALL+GRID`. Bundles spot files into a `.tbz` tar
and uploads via SFTP (preferred) or FTP (fallback). Also systemd-only in
practice; safe to run manually for diagnostics with env populated.

Env (highlights):

- `WD_UPLOAD_WSPRDAEMON_DIR`, `WD_RECEIVER_CALL`, `WD_UPLOAD_ID`
- SFTP: `WD_SFTP_SERVER`, `WD_SFTP_PATH`,
  `WD_SFTP_CONNECT_TIMEOUT`, `WD_SFTP_XFER_TIMEOUT`
- FTP fallback: `WD_FTP_SERVER`, `WD_FTP_USER`, `WD_FTP_PASSWORD`,
  `WD_FTP_PATH`
- `WD_MAX_FILES`, `WD_BURST_THRESHOLD`, `WD_VERSION`

Upload protocol writes `NAME.tbz.part` then renames to `NAME.tbz`,
matching the v3 atomic-publish convention. SSH host-key changes are
auto-handled (`ssh-keygen -R` + `accept-new`).

### B.9 `wd-spool-clean` — spool janitor

Source: [bin/wd-spool-clean](../bin/wd-spool-clean). Run by
`wd-spool-clean.timer` every 5 minutes. Reads every
`/etc/wsprdaemon/env/wd-*.env`, resolves the per-directory retention as
`max(MODE_WINDOW[m] for m in WD_RECEIVER_MODES) + 2` minutes, and
deletes `*.wav` older than that. Idempotent and safe to run by hand:

```
sudo systemctl start wd-spool-clean.service
```

`MODE_WINDOW`: `I1=1`, `W2=2`, `F2=2`, `F5=5`, `F15=15`, `F30=30`.

### B.10 `wdwatch` — pipeline tail (interactive)

Source: [bin/wdwatch](../bin/wdwatch). Operator-facing tool; no env
needed beyond `sudo` for `journalctl -f`.

```
wdwatch                # all bands, all modes
wdwatch 20             # 20 m only
wdwatch 20 W2          # 20 m W2 mode only
```

Filters `wd-decode@*` and `wd-upload-wsprnet@*` journal output to the
keywords `DECODE START|DONE`, `UPLOAD START|DONE|FAILED`, `MULTI CYCLE`,
`PARTIAL DUMP`, `DIAG START|DONE|REJECT|REPORT`, and reformats
ISO-8601 timestamps to the spot-file `YYMMDD HH:MM:SS` form.

---

## C. Quick "what runs where" matrix

| Tool | Invoked by | Lives in repo |
| --- | --- | --- |
| `wd-ctl` | operator (sudo) | [bin/wd-ctl](../bin/wd-ctl) |
| `wdwatch` | operator | [bin/wdwatch](../bin/wdwatch) |
| `wd-kiwi-cleanup` | `ExecStopPost=` of `wd-kiwi-record@` | [bin/wd-kiwi-cleanup](../bin/wd-kiwi-cleanup) |
| `wd-spool-clean` | `wd-spool-clean.timer` (also operator-safe) | [bin/wd-spool-clean](../bin/wd-spool-clean) |
| `wd-ka9q-record(.py)` | `wd-ka9q-record@.service` | [bin/wd-ka9q-record](../bin/wd-ka9q-record), [bin/wd-ka9q-record.py](../bin/wd-ka9q-record.py) |
| `wd-kiwi-record` | `wd-kiwi-record@.service` | [bin/wd-kiwi-record](../bin/wd-kiwi-record) |
| `wd-decode` | `wd-decode@.service` | [bin/wd-decode](../bin/wd-decode) |
| `wd-post` | `wd-post@.service` | [bin/wd-post](../bin/wd-post) |
| `wd-upload-wsprnet` | `wd-upload-wsprnet@.service` | [bin/wd-upload-wsprnet](../bin/wd-upload-wsprnet) |
| `wd-upload-wsprdaemon` | `wd-upload-wsprdaemon@.service` | [bin/wd-upload-wsprdaemon](../bin/wd-upload-wsprdaemon) |
