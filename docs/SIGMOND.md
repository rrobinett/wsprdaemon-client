# Sigmond integration

This document describes how wsprdaemon-client interacts with sigmond,
the HamSCI station coordinator, and which contract v0.4 surfaces it
implements. For per-unit detail see [SERVICES.md](SERVICES.md); for
producer/consumer relationships with sibling repos see
[INTEGRATION.md](INTEGRATION.md).

## 1. What sigmond is

[sigmond](https://github.com/mijahauan/sigmond) is the HamSCI
multi-receiver station coordinator. It runs as a separate daemon and
manages cross-client effects (log levels, radiod address overrides,
fleet start/stop) across the clients that opt in via the
[client contract v0.4](/home/mjh/git/sigmond/docs/CLIENT-CONTRACT.md):
`hf-timestd`, `wsprdaemon-client`, `psk-recorder`, `wspr-recorder`,
`ka9q-web`. Sigmond never imports a client's code, never edits its
config files, and never shells into a client's processes. The contract
is the only allowed interface.

## 2. Running standalone vs. under sigmond

Both modes use the **same unit files** and the **same binaries**.
Conformance is "the binary runs unchanged under both regimes"
(contract §intro).

### Standalone

- Operator edits `/etc/wsprdaemon/wsprdaemon.conf` directly.
- Operator runs `wd-ctl apply` (manually or via cron).
- `/etc/sigmond/coordination.env` does not exist. Every shipped unit
  declares `EnvironmentFile=-/etc/sigmond/coordination.env` — the `-`
  makes it optional, so missing-file is not an error. See for example
  [../systemd/wd-decode@.service](../systemd/wd-decode@.service) line 20.
- `wd-ctl inventory --json` and `wd-ctl validate --json` still work;
  they just have no caller.

### Under sigmond

- Sigmond writes `/etc/sigmond/coordination.env`. The same units pick
  it up at start.
- Sigmond invokes `wd-ctl inventory --json`, `wd-ctl validate --json`,
  and `wd-ctl version` to learn the client's instance set and current
  state.
- Sigmond may push per-unit drop-ins under `<unit>.d/` to override
  individual settings (e.g. log level for one decoder).
- Sigmond may publish a fleet log level via
  `WSPRDAEMON_LOG_LEVEL` / `CLIENT_LOG_LEVEL` in `coordination.env`.
  Long-lived bash daemons re-source the file on `SIGHUP` via
  [../lib/wd-loglevel.sh](../lib/wd-loglevel.sh) (`wd_loglevel_apply`),
  so verbosity flips without restarting RTP-attached processes.

The client therefore needs no sigmond-mode toggle. The presence or
absence of `coordination.env` alone selects the regime.

## 3. Contract v0.4 conformance surfaces

The client's declared conformance level is `contract_version = "0.4"`,
emitted by [`../lib/wdlib/contract.py`](../lib/wdlib/contract.py)
(`CLIENT_NAME = "wsprdaemon"`, `CONTRACT_VERSION = "0.4"`).

| Section | Surface | Where it lives |
|---------|---------|----------------|
| §3 Self-describe CLI | `wd-ctl inventory --json`, `wd-ctl validate --json`, `wd-ctl version` | [../bin/wd-ctl](../bin/wd-ctl) dispatches into [`build_inventory`](../lib/wdlib/contract.py) and [`build_validate`](../lib/wdlib/contract.py). `StderrRedirect` keeps stdout pure JSON. |
| §4 Coordination env  | `EnvironmentFile=-/etc/sigmond/coordination.env` on every long-lived unit | All 8 service units in [../systemd/](../systemd) (every `@.service` and `wd-ka9q-web@.service`). The leading `-` makes the file optional — see [SERVICES.md](SERVICES.md). |
| §5 Deploy manifest   | `deploy.toml` at repo root | [../deploy.toml](../deploy.toml) — `[install.steps]` enumerates every link/copy/render; `[systemd]` lists the unit set sigmond enumerates. |
| §10 Log paths        | Surfaced as `log_paths` in inventory | [`discover_log_paths`](../lib/wdlib/contract.py) scans `/var/log/wsprdaemon/` for `.log` / `.txt` files. The key is omitted from inventory if the dict is empty. |
| §11 Log level        | `WSPRDAEMON_LOG_LEVEL` / `CLIENT_LOG_LEVEL`, re-read on SIGHUP | Resolution: [`resolve_log_level`](../lib/wdlib/contract.py) for the inventory snapshot; [`../lib/wd-loglevel.sh`](../lib/wd-loglevel.sh) for the in-process bash daemons (`wd_loglevel_apply` is registered with `trap … HUP`). Precedence: CLI flag → `WSPRDAEMON_LOG_LEVEL` → `CLIENT_LOG_LEVEL` → `INFO`. |
| §12.1 Entry-point reachability (MUST) | `if __name__ == "__main__":` guard on every Python entry-point | [`check_entry_points`](../lib/wdlib/contract.py) covers `bin/wd-ctl` and `bin/wd-ka9q-record.py`. Errors surface in `validate.issues`. |
| §12.2 SSRC uniqueness (MUST) | One (freq, preset, rate, encoding) tuple per radiod per band | [`check_ssrc_uniqueness`](../lib/wdlib/contract.py) — wsprdaemon-client always uses `preset=usb`, `rate=12000`, `enc=f32`, so the check reduces to "no two bands on the same KA9Q receiver share a frequency". |
| §12.3 config_path disclosure (MUST) | `config_path` resolved to absolute path in inventory and validate output | [`build_inventory`](../lib/wdlib/contract.py) and [`build_validate`](../lib/wdlib/contract.py) call `Path(config_path).resolve()`. |
| §12.5 Pattern A repo layout (SHOULD) | `/opt/git/wsprdaemon-client` group-writable + `~/wsprdaemon-client` symlink | See [Section 4](#4-pattern-a-repo-layout-125) below. |
| §12.6 ka9q-python PyPI lag (SHOULD) | Warn if installed `ka9q.__version__` < deps.conf pin | [`check_ka9q_python_version`](../lib/wdlib/contract.py) reads the `[ka9q-python]` pin from [../deps.conf](../deps.conf) and compares to the imported `ka9q.__version__`. Returns a warn issue if low. |

`build_validate` aggregates schedule-sanity checks (KA9Q receivers must
not appear in non-`00:00` time slots) plus the §12 hardening checks and
sets `ok = False` if any `severity == "error"` issue is present.

§12.4 (decoder mutation of spool) is not applicable here — wsprdaemon-client
is the consumer side, and the producer surface lives in
[wspr-recorder](https://github.com/mijahauan/wspr-recorder). See
[INTEGRATION.md §2](INTEGRATION.md#2-producerconsumer-relationship-with-wspr-recorder).

## 4. Pattern A repo layout (§12.5)

The contract specifies `/opt/git/<client>` as the canonical repo
location for a HamSCI client on a managed host:

- Path: `/opt/git/wsprdaemon-client`.
- Owner: `mjh:<service-group>` (typically `mjh:wsprdaemon`), mode-775
  (group-writable).
- Service user (`wsprdaemon`) is a member of the service group.
- Convenience symlink: `~/wsprdaemon-client → /opt/git/wsprdaemon-client`
  in the maintainer's home directory.

### Why

System services run as `User=wsprdaemon` and need to read from the repo
clone (or its installed copies under `/opt/wsprdaemon/` and
`/usr/local/sbin/`). If the clone lives under `~mjh/git/wsprdaemon-client`,
mode-700 home directories block the service user from traversing into
it. Putting the canonical clone under `/opt/git/` and giving the
service group read/exec satisfies the traversability constraint without
loosening the maintainer's home permissions.

### Anti-pattern (banned)

`install.sh` writing the **reverse** symlink — `/opt/git/<client> →
~/git/<client>` — re-introduces the home-directory traversal problem
because the service user follows the symlink into mode-700 territory.
The contract calls this out as the trap `hf-timestd` and `psk-recorder`
both originally hit. New install scripts must not ship the reverse
symlink. wsprdaemon-client's [../install.sh](../install.sh) follows
Pattern A.

## 5. Where sigmond itself lives

- Repo: [sigmond](https://github.com/mijahauan/sigmond) (Michael
  Hauan, AC0G).
- Local clone: [/home/mjh/git/sigmond](../../sigmond).
- Authoritative contract spec: [/home/mjh/git/sigmond/docs/CLIENT-CONTRACT.md](../../sigmond/docs/CLIENT-CONTRACT.md).
- v0.5 draft (next contract revision): [/home/mjh/git/sigmond/docs/CONTRACT-v0.5-DRAFT.md](../../sigmond/docs/CONTRACT-v0.5-DRAFT.md).

The contract is the only allowed interface between sigmond and any
client — including this one. If sigmond ever needs to know something
new about wsprdaemon-client, the answer is to grow the contract and
extend `build_inventory` / `build_validate`, not to teach sigmond about
wsprdaemon's internals.
