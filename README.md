# wsprdaemon-client (v4)

Linux client for decoding WSPR and FST4W weak-signal transmissions and uploading spots to [wsprnet.org](https://wsprnet.org) and [wsprdaemon.org](https://wsprdaemon.org).

v4 is a complete rewrite of the original ~15,700-line monolithic bash program ([rrobinett/wsprdaemon](https://github.com/rrobinett/wsprdaemon)) into a **systemd service-oriented architecture** with a Python control plane. The controller, `wd-ctl`, reads an INI config and ensures the right set of per-receiver/per-band systemd units exists, is configured, and is running.

Maintainer: Rob Robinett (AI6VN).

## Status

Pre-beta. The service set, config parser, and contract-v0.5 conformance surfaces are in place and exercised on a Beelink test bench with RX888 and KiwiSDR receivers. Do not deploy on a production station yet. See [wd-v4-architecture.md](wd-v4-architecture.md) for the design spec and which parts of it are already implemented.

## What it does

For each configured receiver/band pair, wsprdaemon-client orchestrates:

1. **Recording** — WAV capture from an SDR multicast stream (KA9Q receivers via `wd-ka9q-record`, one recorder per radiod multicast producing WAVs for all bands on that receiver) or from a KiwiSDR channel (`wd-kiwi-record`, one per band).
2. **Decoding** — `wd-decode` runs `wsprd` (for W-prefix modes) or `jt9` (for F-prefix modes) on each WAV and emits spot files.
3. **Posting** — `wd-post` merges spots across MERG'd receivers (best-SNR union) and hands them to the upload queue.
4. **Uploading** — `wd-upload-wsprnet` and `wd-upload-wsprdaemon` deliver spots to the two online databases.

Housekeeping (`wd-spool-clean.timer`) trims the WAV spool and posting queue.

## Install

Canonical install path is `/opt/git/sigmond/wsprdaemon-client` (Pattern A repo layout — see [docs/SIGMOND.md](docs/SIGMOND.md)):

```bash
sudo install -d -m 0755 /opt/git
sudo install -d -o <maintainer> -g <service-group> -m 2775 /opt/git/sigmond
cd /opt/git/sigmond
sudo -u <maintainer> git clone https://github.com/rrobinett/wsprdaemon-client.git
cd wsprdaemon-client
sudo ./install.sh
```

`install.sh` creates the `wsprdaemon:radio` user/group, the FHS-compliant path set (`/etc/wsprdaemon`, `/var/spool/wsprdaemon`, `/var/log/wsprdaemon`, `/run/wsprdaemon`, `/opt/wsprdaemon-client`), installs executables under `/usr/local/sbin/`, and drops the systemd unit templates into `/etc/systemd/system/`. See [docs/OPERATIONS.md](docs/OPERATIONS.md) for first-run steps and [deploy.toml](deploy.toml) for the canonical install manifest.

## Quick start

```bash
# 1. Write a config (or migrate from v3)
sudo cp tests/wsprdaemon.conf /etc/wsprdaemon/wsprdaemon.conf
sudo $EDITOR /etc/wsprdaemon/wsprdaemon.conf

# 2. Validate before starting anything
wd-ctl validate

# 3. Apply — wd-ctl computes the desired service set and reconciles
sudo wd-ctl apply

# 4. Check what's running
wd-ctl status
```

To stop everything: `sudo wd-ctl teardown`.

## Documentation

- **[docs/OPERATIONS.md](docs/OPERATIONS.md)** — install, day-to-day operation, logs, troubleshooting
- **[docs/CONFIGURATION.md](docs/CONFIGURATION.md)** — `wsprdaemon.conf` INI reference, receiver and schedule semantics, v3→v4 migration
- **[docs/SERVICES.md](docs/SERVICES.md)** — every systemd unit: purpose, inputs/outputs, dependencies
- **[docs/CLI_REFERENCE.md](docs/CLI_REFERENCE.md)** — every `wd-ctl` subcommand and `wd-*` helper
- **[docs/INTEGRATION.md](docs/INTEGRATION.md)** — relationship to `wspr-recorder`, `ka9q-python`, `hf-timestd`, `ka9q-radio`, and the upload sinks
- **[docs/SIGMOND.md](docs/SIGMOND.md)** — running under the sigmond coordinator; contract-v0.5 conformance
- **[wd-v4-architecture.md](wd-v4-architecture.md)** — full v0.10 design spec
- **[deps.conf](deps.conf)** — pinned versions of every external dependency
- **[deploy.toml](deploy.toml)** — contract §5 deploy manifest

## Companion projects

- [ka9q-python](https://github.com/mijahauan/ka9q-python) — Python control of ka9q-radio; used by the KA9Q recorder
- [wspr-recorder](https://github.com/mijahauan/wspr-recorder) — contract-v0.5-compliant WSPR WAV producer; preferred over the built-in recorder
- [hf-timestd](https://github.com/mijahauan/hf-timestd) — sub-ms HF time standard (planned integration via `wd-hftime@`)
- [sigmond](https://github.com/mijahauan/sigmond) — multi-client coordinator; defines the contract this client implements
- [ka9q-radio](https://github.com/ka9q/ka9q-radio) — Phil Karn's `radiod` daemon

## License

See parent project [rrobinett/wsprdaemon](https://github.com/rrobinett/wsprdaemon).
