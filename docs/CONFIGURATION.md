# wsprdaemon v4 — Configuration Reference

INI reference for `/etc/wsprdaemon/wsprdaemon.conf`. The authoritative
parser is [v4_parser.parse_v4_config](../lib/wdlib/v4_parser.py); the
data model is [config.WdConfig](../lib/wdlib/config.py); the example
shapes that follow track the parser exactly. For operator workflow see
[OPERATIONS.md](OPERATIONS.md); for what each script reads from the
generated env files see [CLI_REFERENCE.md](CLI_REFERENCE.md).

---

## 1. Canonical Path

```
/etc/wsprdaemon/wsprdaemon.conf
```

This is the single source of truth. `wd-ctl apply` regenerates everything
under `/etc/wsprdaemon/env/` from this file on every run; the env files
are auto-generated and must not be edited by hand.

---

## 2. File Format

Standard Python `configparser` INI:

- Comments: `;` and `#`, both line-leading and inline.
- No interpolation (`%` characters in values are literal — important for
  `storage_quota = 70%`).
- Strict mode is off, so duplicate keys do not raise (last one wins).
- Boolean values follow `configparser` convention: `true/false`,
  `yes/no`, `on/off`, `1/0` (case-insensitive).

Section names are colon-separated and case-sensitive in the prefix
(`receiver`, `merge`, `schedule`) but receivers are looked up
case-insensitively from schedule keys (`v4_parser` upper-cases the key).

> The shipped [tests/wsprdaemon.conf](../tests/wsprdaemon.conf) is a v3
> bash sample (input for `wd-ctl migrate-config`), not a v4 INI. The
> example below shows the v4 shape produced by
> [ini_writer.write_v4_ini](../lib/wdlib/ini_writer.py); see also the
> annotated INI in [wd-v4-architecture.md §2.4](../wd-v4-architecture.md).

### 2.1 Minimal example

```ini
[general]
ka9q_conf_name = k3lr-rx888
rac            = 117

[receiver:KA9Q_0]
address  = k3lr-wspr-pcm.local
call     = AI6VN-0
grid     = CM88mc
password = NULL

[receiver:KA9Q_0:80]
modes = W2 F2 F5

[receiver:KA9Q_0:40]
modes = W2 F2

[receiver:KA9Q_0:20]
modes = W2 F2 F5

[receiver:KIWI_0]
address  = 10.22.23.70:8073
call     = AI6VN-K0
grid     = CM88mc
password = NULL

[receiver:KIWI_0:40]
modes = W2

[merge:MERG_Q01_K0]
sources = KA9Q_0 KIWI_0
call    = AI6VN-M01
grid    = CM88mc

[merge:MERG_Q01_K0:20]
modes = W2 F2 F5

[schedule:main]
time   = 00:00
KA9Q_0 = 80 40 20
KIWI_0 = 40
MERG_Q01_K0 = 20
```

---

## 3. Sections and Keys

### 3.1 `[general]`

Parsed in `parse_v4_config` lines 50–60.

| Key | Type | Default | Purpose |
| --- | --- | --- | --- |
| `ka9q_conf_name` | string | `''` | Name of the local `radiod@<name>.conf` instance. Used to infer `locality=local` and `radiod_name` for KA9Q receivers whose `address` shares the same hostname prefix. |
| `ka9q_web_dns` | string | `''` | Status DNS name advertised for ka9q-web (informational; not used by the recorder path). |
| `rac` | string (int) | `''` | Remote Access Channel number. When set, `wd-ctl apply` deploys the `wd-remote-access.service` frpc tunnel; SSH→`35800+rac`, web→`45800+rac`. |
| `rac_token` | string | `''` | Shared secret for `frps-secure` (port 35736). When empty, the legacy port 35735 (no auth, no TLS) is used. |
| `rac_server` | string | `remote.wsprdaemon.org` | Primary frps host. |
| `rac_fallback_server` | string | `''` | Optional secondary frps host; the generated `wd-rac-connect` script probes the primary every 60 s and switches back when it returns. |
| `rac_tls_ca` | string | `''` | Path to the gateway CA bundle for self-signed TLS verification. Default: looks for `/etc/wsprdaemon/gw-ca-bundle.crt` then `/etc/wsprdaemon/gw2-ca.crt`. |
| `reserved_cpus` | string | `''` | Space- or comma-separated CPU IDs reserved for manually-managed services. Excluded from the worker-CPU mask `wd-ctl` writes via `99-wdctl-cpu-affinity.conf` drop-ins. |

### 3.2 `[receiver:NAME]`

One section per real receiver. NAME prefix determines `receiver_type`
(via `Receiver.receiver_type` in [config.py](../lib/wdlib/config.py)):

| Prefix | `receiver_type` |
| --- | --- |
| `KA9Q_…` (without `_WWV`) | `ka9q` |
| `KA9Q_…_WWV…` | `ka9q_wwv` |
| `KIWI_…` | `kiwi` |
| `MERG_…` | `merge` (use `[merge:NAME]` instead — see §3.4) |
| anything else | `unknown` |

| Key | Type | Default | Purpose |
| --- | --- | --- | --- |
| `address` | string | `''` | Multicast DNS for KA9Q (e.g. `k3lr-wspr-pcm.local`) or `host:port` for Kiwi (e.g. `10.22.23.70:8073`). |
| `call` | string | `''` | Reporter callsign for this receiver. |
| `grid` | string | `''` | Reporter grid square. |
| `password` | string | `NULL` | Receiver password (Kiwi). The literal string `NULL` (or empty) means no password; envgen translates that to an empty `WD_KIWI_PASSWORD`. |
| `locality` | string | inferred | `local` (radiod runs on this host) or `remote`. Inferred from `general.ka9q_conf_name` when blank; always `remote` for KA9Q with no address-prefix match. |
| `radiod_name` | string | inferred | The `radiod@<name>` instance providing this stream. Inferred from `address` prefix matching `ka9q_conf_name`. |

### 3.3 `[receiver:NAME:BAND]`

Per-band overlay for receiver `NAME`. The presence of this section is
what enrolls the band into the schedule generator.

| Key | Type | Default | Purpose |
| --- | --- | --- | --- |
| `modes` | string | `W2` | Space-separated decode modes (e.g. `W2 F2 F5`). Internally stored colon-separated (`W2:F2:F5`). |
| `freq_hz` | int | from `BAND_FREQ_HZ` | Optional explicit Hz override. Normally omitted; the parser falls back to the band table in [envgen.BAND_FREQ_HZ](../lib/wdlib/envgen.py). |

Valid mode tokens: `W2`, `F2`, `F5`, `F15`, `F30`, `I1` (IQ archive
only). See [wd-spool-clean](../bin/wd-spool-clean) `MODE_WINDOW` for the
authoritative window minutes.

Known band labels (from `BAND_FREQ_HZ`): `2200`, `630`, `160`, `80`,
`80eu`, `60`, `60eu`, `40`, `30`, `22`, `20`, `17`, `15`, `12`, `10`,
plus `WWV_2_5`, `WWV_5`, `WWV_10`, `WWV_15`, `WWV_20`, `WWV_25`,
`CHU_3`, `CHU_7`, `CHU_14`.

### 3.4 `[merge:NAME]` and `[merge:NAME:BAND]`

Merge "receivers" combine spots from multiple real receivers post-decode.
`NAME` should start with `MERG_` (parser convention).

| Key | Type | Default | Purpose |
| --- | --- | --- | --- |
| `sources` | string | `''` | Space-separated source-receiver names (e.g. `KA9Q_0 KIWI_0 KIWI_1`). Stored internally as a comma-joined `address`. |
| `call` | string | `''` | Reporter call for the merged spot stream. |
| `grid` | string | `''` | Reporter grid for the merged stream. |

`[merge:NAME:BAND]` carries `modes` exactly like `[receiver:NAME:BAND]`.
A merge band is automatically expanded by
[envgen.generate_all_env_files](../lib/wdlib/envgen.py) so every source
receiver also records the band — the merge happens after decoding.

### 3.5 `[schedule:LABEL]`

| Key | Type | Default | Purpose |
| --- | --- | --- | --- |
| `time` | string `HH:MM` | `00:00` | Slot start time. `00:00` denotes always-on (the only mode the v4 implementation currently honors for KA9Q; see §4 below). |
| `<RECEIVER_NAME>` | string | — | Space-separated band list to enable for that receiver in this slot. Receiver lookup is case-insensitive (the key is upper-cased before matching). |

The mode list for each (receiver, band) is taken from the corresponding
`[receiver:NAME:BAND]` / `[merge:NAME:BAND]` `modes` key, defaulting to
`W2` if absent.

### 3.6 `[hf-timestd]`

Parsed in `parse_v4_config` lines 138–149; backed by
`HfTimestdConfig` in [config.py](../lib/wdlib/config.py).

| Key | Type | Default | Purpose |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | If true, `wd-ctl apply` clones [hf-timestd](https://github.com/mijahauan/hf-timestd) per [deps.conf](../deps.conf), writes `/etc/hf-timestd/timestd-config.toml` from the primary local KA9Q radiod, and runs `scripts/deploy.sh --yes`. |
| `timing_authority` | string | `rtp` | One of `rtp`, `fusion`, `auto`. Drives the generated TOML's `[timing] authority`. `rtp`/`fusion` set `rtp_expected_accuracy_ms = 0.001`; otherwise `1.0`. |
| `compression` | string | `zstd` | `none`, `zstd`, or `lz4` — passed to the recorder block. |
| `uploader_enabled` | bool | `false` | Sets `[uploader] enabled` in the timestd TOML. |
| `physics_enabled` | bool | `false` | Toggles the ionospheric pipeline (`timestd-physics.service` and the IONEX/iono-reanalysis/chrony-monitor timers). When false, those units are explicitly disabled to override `deploy.sh`'s default-enable. |
| `storage_quota` | string | `70%` | Disk-usage threshold at which the recorder begins deleting oldest data. |
| `archive_path` | string | `''` | Optional symlink/path to an external long-term raw IQ archive. |

### 3.7 `[ka9q-web]`

| Key | Type | Default | Purpose |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Deploy one `wd-ka9q-web@<radiod_name>.service` per unique local KA9Q radiod (stable name-sorted). |
| `base_port` | int | `8081` | First instance HTTP port; successive instances increment by 1. |

---

## 4. Receiver Types — Critical Rules

From `CLAUDE.md` and architecture §2:

- **KA9Q (`ka9q`, `ka9q_wwv`) receivers are 1-to-N.** A single recorder
  process listens to one multicast stream and produces WAV files for all
  configured bands. They run **continuously**. They must never appear in
  any `[schedule:LABEL]` whose `time` is anything other than `00:00`.
  `wd-ctl validate` flags this as an `error` (see contract §12 schedule
  sanity check in [contract.py](../lib/wdlib/contract.py)).
- **Kiwi receivers are 1:1.** One `wd-kiwi-record@<RX>-<BAND>` per
  channel. They are subject to schedule transitions (only one band per
  channel at a time). `kiwi_recorder.py` is used as-is and truncates the
  in-flight WAV on SIGTERM; [wd-kiwi-cleanup](../bin/wd-kiwi-cleanup)
  removes the truncated tail.
- **Merge receivers (`merge`) record nothing.** They only have decode +
  post env files. Each merge band is automatically projected onto its
  source receivers' band sets so the source recorders supply the WAVs.

---

## 5. Schedule Semantics

The current implementation in
[envgen.generate_all_env_files](../lib/wdlib/envgen.py) iterates every
slot's entries and unions the (receiver, band, modes) triples into one
env-file set; there is no time-of-day evaluator running on a timer. In
practice that means slots are merged: any band listed in any slot will
have its env files generated and its services started.

The `wsprdaemon.service` / `.timer` orchestrator that would re-evaluate
schedule slots periodically (architecture §3.7) is spec-only. The
`wsprdaemon.target` exists in [systemd/](../systemd/) and is the
`WantedBy=` target the service templates use; the schedule-evaluating
sibling timer has not yet landed.

Until that lands, treat every schedule entry as always-on. The
`time = 00:00` convention is enforced for KA9Q (see §4).

---

## 6. Generated Env Files

`wd-ctl apply` writes one file per service instance into
`/etc/wsprdaemon/env/`:

| File | Producer | Consumer |
| --- | --- | --- |
| `wd-ka9q-record@<RX>.env` | `generate_ka9q_record_env` | `wd-ka9q-record@.service` |
| `wd-kiwi-record@<RX>-<BAND>.env` | `generate_kiwi_record_env` | `wd-kiwi-record@.service` |
| `wd-decode@<RX>-<BAND>.env` | `generate_decode_env` | `wd-decode@.service` |
| `wd-post@<RX>-<BAND>.env` | `generate_post_env` | `wd-post@.service` |
| `wd-ka9q-web@<RADIOD>.env` | `_apply_ka9q_web` in [bin/wd-ctl](../bin/wd-ctl) | `wd-ka9q-web@.service` |

Each file begins with `# Auto-generated by wd-ctl — do not edit manually`.
The systemd unit pulls it via `EnvironmentFile=/etc/wsprdaemon/env/<unit>.env`.
For the per-key contract see [CLI_REFERENCE.md](CLI_REFERENCE.md) §B.

### 6.1 `coordination.env` (sigmond integration)

When `/etc/sigmond/coordination.env` is present, the Python uploaders'
`SIGHUP` handler re-sources it before re-resolving the log level. This
is how a sigmond coordinator pushes fleet-wide `CLIENT_LOG_LEVEL` (and
similar) to running daemons without requiring a unit restart (contract
§4 and §11).

---

## 7. Migration from v3

```
sudo wd-ctl migrate-config /path/to/wsprdaemon.conf
```

[v3_parser.py](../lib/wdlib/v3_parser.py) reads the bash-array form of
`wsprdaemon.conf` (selecting the `case $(hostname)` block matching this
host or the `--hostname` override) and extracts:

- `RECEIVER_LIST` → `[receiver:NAME]` and `[merge:NAME]` sections.
- `WSPR_SCHEDULE` (and the per-host indirect arrays it expands) →
  `[schedule:LABEL]` slots and per-band `modes` keys.
- A small set of host-level vars: `KA9Q_CONF_NAME` → `general.ka9q_conf_name`,
  `RAC` → `general.rac`.

What is not converted: every other v3 host setting (sample at the
bottom of [tests/wsprdaemon.conf](../tests/wsprdaemon.conf) such as
`CPU_CORE_KHZ`, `GRAPE_PSWS_ID`) is preserved as a trailing comment
block under `# --- Settings from v3 config not yet mapped to v4 ---`.
Set them by hand if needed.

---

## 8. Validation Rules

`wd-ctl validate` / `wd-ctl validate --json` runs the contract v0.4
checks. Authoritative implementations live in
[lib/wdlib/contract.py](../lib/wdlib/contract.py); see also
[OPERATIONS.md §6](OPERATIONS.md).

| Check | Severity | Source |
| --- | --- | --- |
| Schedule references undeclared receiver | error | `build_validate` |
| KA9Q receiver scheduled outside `00:00` | error | `build_validate` |
| Python entry-point missing `if __name__ == '__main__':` (§12.1) | error | `check_entry_points` |
| Duplicate band frequency on a single KA9Q receiver (§12.2) | error | `check_ssrc_uniqueness` |
| `ka9q-python` not importable (§12.6) | warn | `check_ka9q_python_version` |
| Installed `ka9q-python` < `deps.conf` pin (§12.6) | warn | `check_ka9q_python_version` |

The JSON payload always includes `config_path` (absolute, resolved) and
`contract_version`. `ok` is `true` only when no `error`-severity issues
remain.
