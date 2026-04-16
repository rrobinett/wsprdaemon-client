# Integration with sibling projects

This document describes how wsprdaemon-client fits with the other
HamSCI station services that surround it. For per-unit detail see
[SERVICES.md](SERVICES.md). For sigmond-coordinated operation see
[SIGMOND.md](SIGMOND.md).

## Top-level data flow

```
   ┌───────────┐
   │ SDR HW    │   RX-888, KiwiSDR, …
   └─────┬─────┘
         │
         ▼
   ┌──────────────┐    multicast (RTP, 12 kHz f32)
   │ ka9q-radio   │  ──────────────────────────────────┐
   │ (radiod@…)   │                                    │
   └──────────────┘                                    │
                                                       ▼
            ┌──────────────────────────────────────────────────────────┐
            │  WAV producer (one of the following — never both for    │
            │  the same band):                                         │
            │                                                          │
            │  wspr-recorder@<id>.service  (mijahauan, contract v0.4) │
            │     float32 ring → peak-normalized int16 WAV + .json    │
            │     sidecar with decode_modes / period_seconds           │
            │     output: /var/spool/wsprdaemon/recording/<RX>/<BAND>/ │
            │  ── OR ──                                                │
            │  wd-ka9q-record@<RX>.service  (this repo, native)       │
            │     uses ka9q-python to create channels + record         │
            └──────────────────────────────────────────────────────────┘
                                                       │
                                                       ▼
                                           wd-decode@<RX>-<BAND>
                                              wsprd / jt9 --fst4w
                                                       │
                                                       ▼
                                             wd-post@<RX>-<BAND>
                                              best-SNR merge per band
                                                       │
                          ┌────────────────────────────┴────────────────────┐
                          ▼                                                 ▼
              wd-upload-wsprnet@<CALL=GRID>            wd-upload-wsprdaemon@<CALL=GRID>
                          │                                                 │
                          ▼                                                 ▼
                wsprnet.org/meptspots.php             SFTP graphs.wsprdaemon.org/uploads/
```

A sibling [`hf-timestd`](https://github.com/mijahauan/hf-timestd) daemon
runs alongside on KA9Q stations to emit WWV/CHU timing measurements
(arch §3.2). Its hook into the recorder is currently spec-only (see
[Section 4](#4-hf-timestd) below).

---

## 1. Role summary

wsprdaemon-client is a **control plane** plus a **per-band decode/post/
upload worker fleet**:

- **Control plane**: `wd-ctl` (in [../bin/wd-ctl](../bin/wd-ctl)) parses
  `/etc/wsprdaemon/wsprdaemon.conf`, generates per-instance environment
  files under `/etc/wsprdaemon/env/`, and reconciles the running
  systemd service set against the desired one. It also serves the
  contract v0.4 self-describe surface (`inventory`, `validate`,
  `version` — see [SIGMOND.md](SIGMOND.md)).
- **Workers**: `wd-decode@`, `wd-post@`, `wd-upload-wsprnet@`,
  `wd-upload-wsprdaemon@`, plus the optional `wd-ka9q-record@` /
  `wd-kiwi-record@` recorders when this repo is also doing recording.

The client does **not** talk to SDR hardware directly. RTP arrives via
multicast from a `radiod` instance — possibly on another host — and a
recorder process turns it into WAVs that the client decodes and posts.

---

## 2. Producer/consumer relationship with wspr-recorder

### Why split

[wspr-recorder](https://github.com/mijahauan/wspr-recorder) (Michael
Hauan, AC0G) is the preferred WAV producer for KA9Q-fed stations. It
predates the client/server split and was pulled out of the wsprdaemon
pipeline so the recording surface could be tested, sized, and timed
independently of decoding.

wspr-recorder is the **producer**; wsprdaemon-client is the
**consumer**. The boundary is the spool directory plus a JSON sidecar
contract.

### What wspr-recorder writes

For each band on a radiod, wspr-recorder emits:

- A WAV named `YYYYMMDDTHHMMSSz_<freq_hz>_usb_<period_seconds>.wav`,
  e.g. `20260408T014800Z_7038600_usb_120.wav`. PCM signed 16-bit, 12 000 Hz,
  mono. Peak-normalized at write time (full int16 dynamic range).
- A JSON sidecar with the same stem, e.g.
  `20260408T014800Z_7038600_usb_120.json`, containing at minimum:
    - `decode_modes` — list, e.g. `["W2","F2","F5"]`.
    - `period_seconds` — int, the WAV's actual length (120, 300, 900,
      1800).
    - `int16_scale`, `float32_peak` — for absolute amplitude reconstruction.
    - drift / timing metadata (source, uncertainty, tier).

WAVs are written `.tmp` then renamed (atomic publication).

The contract details — emitted shape, naming, atomicity — are
documented at [wspr-recorder
README](/home/mjh/git/wspr-recorder/README.md) and
[wspr-recorder/docs/SIGMOND-CONTRACT.md](/home/mjh/git/wspr-recorder/docs/SIGMOND-CONTRACT.md).

### Spool directory

By default wspr-recorder writes under `/dev/shm/wspr-recorder/<band>/`,
but the consumer side resolves the directory per-band from the
generated `wd-decode@<RX>-<BAND>.env` file (`WD_RECORDING_DIR`). The
deployed configuration aligns these so wd-decode reads from where
wspr-recorder writes.

### What wd-decode honors from the sidecar

[../bin/wd-decode](../bin/wd-decode) (`sidecar_permits_mode`) opens the
`.json` sidecar next to each candidate WAV and:

- Skips the WAV for a given mode if `decode_modes` is present and the
  requested mode is not in the list.
- Skips the WAV if `period_seconds` is present and does not match the
  expected window for the requested mode (`window_minutes * 60`).
- Treats absent or unparseable sidecars as "no opinion" — decoding
  proceeds. This keeps the Kiwi path (which has no sidecar) working
  unchanged.

This makes the sidecar the **authoritative** source of truth for what
to do with a WAV; the consumer does not second-guess by inspecting
audio.

### Native alternative: wd-ka9q-record

[../bin/wd-ka9q-record](../bin/wd-ka9q-record) +
[../bin/wd-ka9q-record.py](../bin/wd-ka9q-record.py) is the in-repo
recorder. It uses `ka9q-python`'s `RadiodControl` to create channels
on radiod and records WAVs into the same spool tree. Use it when
wspr-recorder is not deployed (small stations, simple installs).

The contract-v0.4-compliant production path is **wspr-recorder →
wd-decode**: wspr-recorder owns the timing-aware sync strategy, ring
buffer sizing, and per-period peak normalization; wsprdaemon-client
owns decode/post/upload. The native recorder is a convenience for
single-binary deploys and keeps the code path alive while wspr-recorder
matures.

---

## 3. ka9q-python

[`ka9q-python`](https://github.com/mijahauan/ka9q-python) is the pure
Python interface to Phil Karn's `radiod`. Both this repo's
`wd-ka9q-record.py` and wspr-recorder use it for:

- `RadiodControl` — dynamic channel creation (no static `[channels]`
  sections in `radiod@.conf`).
- `MultiStream` — one socket per multicast group, with packet
  resequencing, S16BE/float32 decoding, and stream-quality metadata.

The pin lives in [../deps.conf](../deps.conf) `[ka9q-python]` (PyPI
install). The contract validator checks the floor:
[`check_ka9q_python_version`](../lib/wdlib/contract.py) (contract §12.6)
warns if the importable `ka9q.__version__` is less than the deps.conf
pin.

---

## 4. hf-timestd

[`hf-timestd`](https://github.com/mijahauan/hf-timestd) is a separate
daemon (Michael Hauan, AC0G) that listens to WWV/WWVH/CHU/BPM via
ka9q-radio and produces sub-millisecond UTC timing measurements
(D_clock) for Chrony.

The architecture spec (arch §3.2) describes a `wd-hftime@INSTANCE.service`
that would run inside this repo, drive `hf-timestd`, and publish a
calibration JSON at `/run/wsprdaemon/<RX>/hftime.json`. The recorder
would then read `offset_ns` from that file and shift WAV start times.

**Status: not wired in this repo yet.** No `wd-hftime@.service` file
exists in [../systemd/](../systemd) (see
[SERVICES.md](SERVICES.md#spec-only-units-not-in-systemd-yet)). On
deployed stations, hf-timestd runs as its own peer service (managed
out of [/home/mjh/git/hf-timestd](../../hf-timestd) via its own
deploy.sh and systemd units). When the integration lands, the
intended behaviour is:

- `wd-hftime@<RX>.service` invokes `hf-timestd` with `--radiod-host`
  pointed at the receiver's status multicast.
- hf-timestd creates a WWV channel via `ka9q-python`, locks to the
  second-tick tones, writes calibration to
  `/run/wsprdaemon/<RX>/hftime.json`.
- `wd-ka9q-record` reads that file at WAV-write time and stamps
  the corrected start time into the WAV header (or the JSON sidecar
  if the producer is wspr-recorder).

The deps pin lives in [../deps.conf](../deps.conf) `[hf-timestd]` even
though the unit is unbuilt — the source tree is staged at
`/home/wsprdaemon/hf-timestd` ready for the systemd unit when it lands.

---

## 5. ka9q-radio (radiod)

`radiod` is Phil Karn's SDR daemon
([ka9q/ka9q-radio](https://github.com/ka9q/ka9q-radio)). The pin is in
[../deps.conf](../deps.conf) `[ka9q-radio]` and the binary installs to
`/usr/local/sbin/radiod`.

Two deployment modes (arch §2.1.1):

- **Local**: a USB-attached SDR is on this host. wsprdaemon-client
  installs ka9q-radio, the right hardware driver (RX-888 built in,
  Fobos via libfobos, SDRplay via the closed-source SDRplay API), and
  configures `radiod@<INSTANCE>.service`.
- **Remote**: radiod runs on another host. wsprdaemon-client only
  consumes its multicast. The shipped
  [wd-ka9q-record@.service](../systemd/wd-ka9q-record@.service) does
  **not** declare `Requires=radiod@%i.service` (deviating from arch
  §3.3) precisely so the remote case works without a local radiod
  unit.

The static `[channels]` sections in `radiod@.conf` are not needed for
WSPR/FT4/FT8 — both the in-repo recorder and wspr-recorder create
channels dynamically via `ka9q-python`.

---

## 6. Other external services (installed, not owned)

These are installed and enabled by the wsprdaemon installer when the
configuration calls for them. wsprdaemon-client does not own their
codebases.

- **ft8_lib** — [ka9q/ft8_lib](https://github.com/ka9q/ft8_lib).
  FT4/FT8 decoder library. Pin: [../deps.conf](../deps.conf)
  `[ft8_lib]`. Used by `wd-decode` for FT linkage. Standalone
  `ft4-ft8-decode.service` is owned by ft8_lib's repo.
- **ftlib-pskreporter** —
  [pjsg/ftlib-pskreporter](https://github.com/pjsg/ftlib-pskreporter).
  PSKReporter spot uploader for FT4/FT8. Pin:
  [../deps.conf](../deps.conf) `[ftlib-pskreporter]`. Used by `wd-post`.
- **dumphfdl** — [ka9q/dumphfdl](https://github.com/ka9q/dumphfdl).
  HFDL decoder, only installed when HFDL is enabled in the config. Not
  in the current `deps.conf` pin set.
- **ka9q-web** —
  [wa2n-code/ka9q-web](https://github.com/wa2n-code/ka9q-web). Browser
  status UI; managed via the in-repo
  [wd-ka9q-web@.service](../systemd/wd-ka9q-web@.service) on hosts that
  run a local radiod. Build-time dependency on the libonion HTTP
  library — both pinned in [../deps.conf](../deps.conf) `[ka9q-web]`
  (note: `onion_url` / `onion_commit` keys nested in the same section
  rather than a separate `[onion]` section).
- **kiwiclient (kiwirecorder.py)** —
  [jks-prv/kiwiclient](https://github.com/jks-prv/kiwiclient). Used
  as-is by [../bin/wd-kiwi-record](../bin/wd-kiwi-record). Pin:
  [../deps.conf](../deps.conf) `[kiwiclient]`.

The `wsprd` and `jt9` decoder binaries themselves are checked into
[../bin/decoders/](../bin/decoders) per architecture (x86_64,
aarch64, armhf), so wsprdaemon-client does not build them at install
time.

---

## 7. Upload sinks

Upload daemons consume from `/var/spool/wsprdaemon/uploads/` and ship
to two databases. Both run as instanced singletons, one per reporter
identity (`<safe-call>=<grid>`).

### wsprnet.org

- Daemon:
  [wd-upload-wsprnet@.service](../systemd/wd-upload-wsprnet@.service) +
  [../bin/wd-upload-wsprnet](../bin/wd-upload-wsprnet).
- Reads: `${WD_UPLOAD_WSPRNET_DIR}` (typically
  `/var/spool/wsprdaemon/uploads/wsprnet/<safe_call>=<grid>/`).
- Payload: best-SNR-merged spot files emitted by `wd-post`. One spot
  set per reporter callsign+grid per band per cycle.
- Transport: HTTP POST to `http://wsprnet.org/meptspots.php`. Batches
  ≤999 spots per upload; sorts by date/time/freq.
- Cadence: polls every `WD_POLL_INTERVAL` seconds (default 5); fires
  after `WD_STABLE_POLLS` consecutive idle polls (default 1) so upload
  follows the last spot landing by ~5–10 seconds.
- Side effects: appends to `/var/log/wspr.log`, deletes uploaded
  files on success.

### wsprdaemon.org

- Daemon:
  [wd-upload-wsprdaemon@.service](../systemd/wd-upload-wsprdaemon@.service)
  + [../bin/wd-upload-wsprdaemon](../bin/wd-upload-wsprdaemon).
- Reads: `${WD_UPLOAD_WSPRDAEMON_DIR}` tree
  (`/var/spool/wsprdaemon/uploads/wsprdaemon/<safe_call>=<grid>/...`).
- Payload: per-receiver spot sets — preserves the full per-receiver
  detail (`reporter / receiver_name / band`) for analysis,
  propagation studies, and per-receiver performance comparison. Not
  merged.
- Transport: bzip2 tar bundle, SFTP preferred (write
  `uploads/NAME.tbz.part`, rename to clear `.part`), FTP fallback to
  `graphs.wsprdaemon.org`.
- Cadence: triggered when bundle reaches `WD_BURST_THRESHOLD` files
  (default 15) or after RETRY_SECS on failure.

---

## Cross-references

- Per-unit detail and dependencies: [SERVICES.md](SERVICES.md).
- Sigmond conformance and contract v0.4 surfaces: [SIGMOND.md](SIGMOND.md).
- Architecture spec: [../wd-v4-architecture.md](../wd-v4-architecture.md).
- Deploy manifest: [../deploy.toml](../deploy.toml).
- Pinned external versions: [../deps.conf](../deps.conf).
