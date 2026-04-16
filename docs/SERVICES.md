# wsprdaemon-client systemd service reference

This document is the per-unit reference for everything in
[../systemd/](../systemd). For each shipped unit it records the template
shape, what binary it runs, what it depends on, what it produces, and
where it sits in the runtime topology.

For the high-level rationale and end-to-end design, see
[../wd-v4-architecture.md](../wd-v4-architecture.md). For the
producer/consumer story with sibling repos see
[INTEGRATION.md](INTEGRATION.md). For sigmond coordination see
[SIGMOND.md](SIGMOND.md).

## Topology

```
                 ┌──────────────────────────── KA9Q path ────────────────────────────┐
                 │                                                                   │
 ka9q-radio:                                                                         │
   radiod@<id>.service  (external, may live on another host)                         │
        │                                                                            │
        │  RTP multicast (12 kHz f32, one stream per band SSRC)                      │
        ▼                                                                            │
   wd-ka9q-record@RX.service ── 1:N, one per receiver/radiod                         │
        │  WAVs + JSON sidecars in /var/spool/wsprdaemon/recording/<RX>/<BAND>/      │
        ▼                                                                            │
   wd-decode@RX-BAND.service ── BindsTo wd-ka9q-record@RX                            │
        │  spot files in /var/spool/wsprdaemon/posting/<RX>/<BAND>/                  │
        ▼                                                                            │
   wd-post@RX-BAND.service                                                           │
        │  per-receiver and merged spot files written to two upload trees            │
        ▼                                                                            │
   wd-upload-wsprnet@CALL=GRID.service     wd-upload-wsprdaemon@CALL=GRID.service   │
        │                                          │                                 │
        ▼                                          ▼                                 │
   wsprnet.org/meptspots.php           graphs.wsprdaemon.org (SFTP/FTP tar)         │
                                                                                     │
                 └────────────────────────────────────────────────────────────────────┘

                 ┌──────────────────────────── Kiwi path ────────────────────────────┐
                 │                                                                   │
   wd-kiwi-record@RX-BAND.service ── 1:1, one per channel, schedule-toggled         │
        │  WAVs in /var/spool/wsprdaemon/recording/<RX>/<BAND>/                      │
        ▼                                                                            │
   wd-decode@RX-BAND.service  (joins KA9Q path here, same template)                  │
                                                                                     │
                 └────────────────────────────────────────────────────────────────────┘

 Housekeeping:
   wd-spool-clean.timer ──fires─▶ wd-spool-clean.service   (every 5 min)
   wd-ka9q-web@INSTANCE.service                            (per local radiod)
   wsprdaemon.target                                       (start/stop the fleet)
```

All long-lived units carry `EnvironmentFile=-/etc/sigmond/coordination.env`
(`-` makes it optional) so the same unit files run identically standalone
or under [sigmond](SIGMOND.md). Sigmond never edits any of these files.

## Spool layout

```
/var/spool/wsprdaemon/                 tmpfs mount (var-spool-wsprdaemon.mount)
├── recording/<RX>/<BAND>/             WAV + JSON sidecar from recorders
└── posting/<RX>/<BAND>/               spot files from wd-decode

/var/spool/wsprdaemon/uploads/wsprnet/<CALL=GRID>/        wd-post → wd-upload-wsprnet
/var/spool/wsprdaemon/uploads/wsprdaemon/<CALL=GRID>/...  wd-post → wd-upload-wsprdaemon

/var/log/wsprdaemon/                   per-daemon logs (contract §10)
/run/wsprdaemon/                       runtime state (tmpfs)
```

## Shipped vs. spec-only

The architecture document
([../wd-v4-architecture.md](../wd-v4-architecture.md) §3) lists several
units that are not yet present in [../systemd/](../systemd). This file
covers the shipped set; the gap list is at the bottom under
[Spec-only units](#spec-only-units-not-in-systemd-yet).

Shipped (10 files in [../systemd/](../systemd)):

- `wsprdaemon.target`
- `wd-ka9q-record@.service`
- `wd-ka9q-web@.service`
- `wd-kiwi-record@.service`
- `wd-decode@.service`
- `wd-post@.service`
- `wd-upload-wsprnet@.service`
- `wd-upload-wsprdaemon@.service`
- `wd-spool-clean.service`
- `wd-spool-clean.timer`

---

## wsprdaemon.target

File: [../systemd/wsprdaemon.target](../systemd/wsprdaemon.target)

Master start/stop handle for the fleet. `WantedBy=multi-user.target`;
every wsprdaemon unit declares `WantedBy=wsprdaemon.target` and
`PartOf=wsprdaemon.target`, so `systemctl stop wsprdaemon.target` cleanly
takes the entire pipeline down without touching unrelated services.

- Type: target (no `[Service]`).
- Dependencies: `After=network-online.target`.
- No environment, no executable.

The orchestrator service `wsprdaemon.service` and its companion
`wsprdaemon.timer` from arch §3.7 are **not** in the tree yet — see the
spec-only list. Today's start path is `systemctl start wsprdaemon.target`
(or starting individual units).

---

## wd-ka9q-record@.service

File: [../systemd/wd-ka9q-record@.service](../systemd/wd-ka9q-record@.service)

KA9Q multicast recorder. Listens to one radiod's status multicast,
creates per-band channels via `ka9q-python`'s `RadiodControl`, and
writes WAV + JSON sidecar files for every band carried in that stream.
This is the **1:N** model: one process per receiver, all bands inside.

- Instance `%i`: receiver name from `wsprdaemon.conf`,
  e.g. `wd-ka9q-record@KA9Q_0.service`.
- ExecStart: [`/usr/local/sbin/wd-ka9q-record`](../bin/wd-ka9q-record),
  a thin bash wrapper that exec's
  [`/opt/wsprdaemon/bin/wd-ka9q-record.py`](../bin/wd-ka9q-record.py)
  under the project venv at `/opt/wsprdaemon/venv/bin/python3`.
- WorkingDirectory: `/var/spool/wsprdaemon/recording/%i`.
- EnvironmentFile (in order):
    - `-/etc/sigmond/coordination.env` (optional, sigmond drop-in)
    - `/etc/wsprdaemon/env/wd-ka9q-record@%i.env` (mandatory, generated
      by `wd-ctl`).
- Required env: `WD_RECEIVER_NAME`, `WD_RADIOD_STATUS`,
  `WD_RECORDING_DIR`, `WD_BANDS`.
- Dependencies: `After=network-online.target var-spool-wsprdaemon.mount`,
  `Wants=network-online.target`,
  `Requires=var-spool-wsprdaemon.mount`. Marks `PartOf=wsprdaemon.target`.
- Note on radiod: the unit deliberately does **not** declare
  `Requires=radiod@%i.service`. radiod typically lives on another host
  in this deployment; the recorder waits on the multicast at runtime.
  The architecture spec (§3.3) shows a `Requires=radiod@%i.service` line
  but the shipped unit drops it on purpose for the remote-radiod case.
- Restart policy: `Restart=always`, `RestartSec=5`.
- Produces:
    - `/var/spool/wsprdaemon/recording/<RX>/<BAND>/<UTC>_<freq>_usb_<period>.wav`
    - `… .json` sidecar (decode_modes, period_seconds, peak/scale, etc.)
- **KA9Q recorders run continuously and unconditionally.** They are
  never schedule-toggled. The contract validator in
  [../lib/wdlib/contract.py](../lib/wdlib/contract.py)
  (`build_validate`) errors if a schedule entry references a KA9Q
  receiver outside the `00:00` default slot.

---

## wd-kiwi-record@.service

File: [../systemd/wd-kiwi-record@.service](../systemd/wd-kiwi-record@.service)

KiwiSDR recorder. **1:1** per channel — one instance per (receiver,
band) tuple, because `kiwirecorder.py` handles a single channel and
KiwiSDRs have a small fixed channel count that the schedule rotates
through.

- Instance `%i`: `RECEIVER-BAND`, e.g. `wd-kiwi-record@KIWI_0-80.service`.
- ExecStart: [`/usr/local/sbin/wd-kiwi-record`](../bin/wd-kiwi-record),
  a bash wrapper that exec's
  `/opt/wsprdaemon/kiwiclient/kiwirecorder.py` (pinned commit in
  [../deps.conf](../deps.conf) `[kiwiclient]`).
- ExecStopPost: [`/usr/local/sbin/wd-kiwi-cleanup`](../bin/wd-kiwi-cleanup) `%i`
  — deletes the truncated WAV that `kiwirecorder.py` leaves behind on
  SIGTERM (it does not finish the in-flight minute cleanly).
- WorkingDirectory: `/var/spool/wsprdaemon/recording`.
- EnvironmentFile:
    - `-/etc/sigmond/coordination.env`
    - `/etc/wsprdaemon/env/wd-kiwi-record@%i.env`
- Required env: `WD_RECEIVER_NAME`, `WD_KIWI_ADDRESS`,
  `WD_RECEIVER_FREQ_HZ`, `WD_RECORDING_DIR`. Optional:
  `WD_KIWI_PASSWORD`.
- Dependencies: `After=network-online.target var-spool-wsprdaemon.mount`,
  `Requires=var-spool-wsprdaemon.mount`, `PartOf=wsprdaemon.target`.
- Restart: `Restart=always`, `RestartSec=5`.
- Produces 1-minute WAVs named `YYYYMMDDTHHMMSSz_<freq_hz>_usb.wav`
  (PCM signed 16-bit, 12 000 Hz, mono) under `WD_RECORDING_DIR`.
- Schedule transitions are owned by the orchestrator (arch §6.2): stop
  one instance, start another for the new band on the same Kiwi.

---

## wd-decode@.service

File: [../systemd/wd-decode@.service](../systemd/wd-decode@.service)

Per-band decoder. Watches `WD_RECORDING_DIR` for completed WAVs and
runs `wsprd` (W modes) or `jt9 --fst4w` (F modes). Mode prefixes:

| Prefix | Decoder | Examples            |
|--------|---------|---------------------|
| W      | wsprd   | W2                  |
| F      | jt9     | F2, F5, F15, F30    |
| I      | none    | I1 (IQ archive only) |

- Instance `%i`: `RECEIVER-BAND`, e.g. `wd-decode@KA9Q_0-80.service`.
- ExecStart: [`/usr/local/sbin/wd-decode`](../bin/wd-decode). Decoder
  binaries live in `/opt/wsprdaemon/bin/decoders/` (architecture-keyed:
  `wsprd-x86-v27`, `wsprd-arm64-v27`, `wsprd-armhf-v26`, etc.).
- WorkingDirectory: `/tmp` (the script picks per-instance paths from env).
- EnvironmentFile:
    - `-/etc/sigmond/coordination.env`
    - `/etc/wsprdaemon/env/wd-decode@%i.env`
- Required env: `WD_RECEIVER_NAME`, `WD_RECEIVER_TYPE`,
  `WD_RECEIVER_BAND`, `WD_RECEIVER_MODES` (colon-separated, e.g.
  `W2:F2:F5`), `WD_RECEIVER_FREQ_HZ`, `WD_RECORDING_DIR`,
  `WD_POSTING_DIR`, `WD_RUN_DIR`.
- Dependencies (template): `After=network-online.target`,
  `PartOf=wsprdaemon.target`.
- Drop-in dependencies (generated by `wd-ctl apply`, see arch §4.2):
    - For KA9Q sources:
      `BindsTo=wd-ka9q-record@<RX>.service` plus matching `After=`.
      A KA9Q recorder failure tears down all its decoders; they restart
      when the recorder comes back. The drop-in lands at
      `/etc/systemd/system/wd-decode@<RX>-<BAND>.service.d/recorder.conf`.
    - For Kiwi sources:
      `BindsTo=wd-kiwi-record@<RX>-<BAND>.service` plus matching
      `After=`.
- File-watch method: `inotifywait -e moved_to` for `ka9q`/`ka9q_wwv`
  receivers (clean rename on close); polling (`sleep 60`) for Kiwi
  (kiwirecorder reopens mid-write). See `wd-decode` `use_inotify` block.
- Sidecar honoring: when a `.json` sidecar lives next to the WAV, the
  `sidecar_permits_mode` shell helper consults `decode_modes` and
  `period_seconds` and skips the WAV if the sidecar's authoritative
  list excludes the requested mode. This is how the wspr-recorder
  producer surface (see [INTEGRATION.md](INTEGRATION.md)) gates the
  decoder.
- Produces: spot files written to `${WD_POSTING_DIR}` with names like
  `<RX>_<BAND>_<MODE>_<UTC>_spots.txt` (11-field wsprnet MEPT format).
- Restart: `Restart=always`, `RestartSec=5`.

---

## wd-post@.service

File: [../systemd/wd-post@.service](../systemd/wd-post@.service)

Per-band poster. Reads `${WD_POSTING_DIR}` for spot files, performs
best-SNR merging across multiple receiver sources for a logical
band/reporter, and writes two upload streams in parallel.

- Instance `%i`: `RECEIVER-BAND`, e.g. `wd-post@KA9Q_0-80.service`.
  For MERG'd receivers the instance name uses the merge identifier
  (e.g. `wd-post@MERG_0-80.service`, with supplier dirs listed in
  `WD_POST_SUPPLIER_DIRS`).
- ExecStart: [`/usr/local/sbin/wd-post`](../bin/wd-post).
- WorkingDirectory: `/var/spool/wsprdaemon/posting`.
- EnvironmentFile:
    - `-/etc/sigmond/coordination.env`
    - `/etc/wsprdaemon/env/wd-post@%i.env`
- Required env: `WD_RECEIVER_NAME`, `WD_RECEIVER_BAND`,
  `WD_POSTING_DIR`, `WD_RECEIVER_CALL`, `WD_RECEIVER_GRID`. Optional:
  `WD_UPLOAD_WSPRNET_DIR`, `WD_UPLOAD_WSPRDAEMON_DIR`.
- Dependencies: `After=network-online.target`, `PartOf=wsprdaemon.target`.
- Watches the posting dir with `inotifywait` on `moved_to` and
  `close_write` plus a 5-second poll fallback.
- Produces:
    - `${WD_UPLOAD_WSPRNET_DIR}/...` — merged best-SNR-per-transmitter
      union (one set per reporter call+grid per band per cycle).
    - `${WD_UPLOAD_WSPRDAEMON_DIR}/...` — per-receiver spot sets
      (preserve full per-receiver detail for analysis).
- Restart: `Restart=always`, `RestartSec=5`.

---

## wd-upload-wsprnet@.service

File: [../systemd/wd-upload-wsprnet@.service](../systemd/wd-upload-wsprnet@.service)

Uploads merged spot files to wsprnet.org via the MEPT bulk transfer
endpoint.

- Instance `%i`: `<safe-call>=<grid>`, e.g.
  `wd-upload-wsprnet@AC0G=B1_EM38ww.service`. The `=` and uppercase grid
  are part of the safe-name encoding so one upload daemon serves one
  reporter identity.
- ExecStart: [`/usr/local/sbin/wd-upload-wsprnet`](../bin/wd-upload-wsprnet)
  (Python). Polls the queue every `WD_POLL_INTERVAL` seconds (default 5);
  fires an upload after `WD_STABLE_POLLS` consecutive idle polls.
- EnvironmentFile:
    - `-/etc/sigmond/coordination.env`
    - `/etc/wsprdaemon/env/wd-upload-wsprnet@%I.env` (note `%I`
      — the unescaped instance name, since the safe-name uses `=`).
- Required env: `WD_UPLOAD_WSPRNET_DIR`, `WD_RECEIVER_CALL`,
  `WD_RECEIVER_GRID`. Optional: `WD_VERSION`, `WD_POLL_INTERVAL`,
  `WD_STABLE_POLLS`.
- Dependencies: `After=network-online.target`, `Wants=network-online.target`,
  `PartOf=wsprdaemon.target`.
- Side effects: appends to `/var/log/wspr.log`, deletes uploaded files
  from queue on success.
- Restart: `Restart=always`, `RestartSec=10`.

---

## wd-upload-wsprdaemon@.service

File: [../systemd/wd-upload-wsprdaemon@.service](../systemd/wd-upload-wsprdaemon@.service)

Uploads per-receiver spot bundles to wsprdaemon.org. Bundles are
bzip2-tar'd and pushed via SFTP (preferred) with FTP as a fallback.

- Instance `%i`: same `<safe-call>=<grid>` shape as above, e.g.
  `wd-upload-wsprdaemon@AC0G=B1_EM38ww.service`.
- ExecStart: [`/usr/local/sbin/wd-upload-wsprdaemon`](../bin/wd-upload-wsprdaemon)
  (Python).
- EnvironmentFile:
    - `-/etc/sigmond/coordination.env`
    - `/etc/wsprdaemon/env/wd-upload-wsprdaemon@%I.env`
- Required env: `WD_UPLOAD_WSPRDAEMON_DIR`, `WD_RECEIVER_CALL`,
  `WD_UPLOAD_ID`. SFTP: `WD_SFTP_SERVER` (`user@host`), optional
  `WD_SFTP_PATH`, `WD_SFTP_CONNECT_TIMEOUT`, `WD_SFTP_XFER_TIMEOUT`.
  FTP fallback: `WD_FTP_SERVER` (default `graphs.wsprdaemon.org`).
- SFTP protocol: writes `uploads/NAME.tbz.part` then renames, so the
  server never observes a partial bundle. Auto-recovers from changed
  host keys (`ssh-keygen -R` then retry with
  `StrictHostKeyChecking=accept-new`).
- Dependencies: `After=network-online.target`, `Wants=network-online.target`,
  `PartOf=wsprdaemon.target`.
- Restart: `Restart=always`, `RestartSec=10`.

---

## wd-ka9q-web@.service

File: [../systemd/wd-ka9q-web@.service](../systemd/wd-ka9q-web@.service)

ka9q-web browser UI bound to one local radiod's status multicast. One
instance per local radiod (the unit is unused when the only radiod the
station consumes is on another host). The binary is built from
[../deps.conf](../deps.conf) `[ka9q-web]` plus its libonion build
prerequisite.

- Instance `%i`: radiod config name, e.g.
  `wd-ka9q-web@k3lr-rx888.service`.
- ExecStart: `/usr/local/sbin/ka9q-web -m ${KA9Q_WEB_STATUS}
  -p ${KA9Q_WEB_PORT} -d /usr/local/share/ka9q-web/html`.
- EnvironmentFile:
    - `-/etc/sigmond/coordination.env`
    - `/etc/wsprdaemon/env/wd-ka9q-web@%i.env`
- Required env: `KA9Q_WEB_STATUS` (radiod status mDNS name),
  `KA9Q_WEB_PORT` (HTTP port).
- Dependencies: `After=network-online.target`, `Wants=network-online.target`,
  `PartOf=wsprdaemon.target`.
- Restart: `Restart=always`, `RestartSec=5`.

---

## wd-spool-clean.service / wd-spool-clean.timer

Files:
[../systemd/wd-spool-clean.service](../systemd/wd-spool-clean.service),
[../systemd/wd-spool-clean.timer](../systemd/wd-spool-clean.timer).

Janitor that deletes stale WAVs from `/var/spool/wsprdaemon/recording/`.
Acts as a safety net against tmpfs overflow when a decoder is stopped
or wedged. Once `wd-decode@…` is consuming files normally, the janitor
mostly finds nothing to do.

### wd-spool-clean.service

- Type: `oneshot`.
- ExecStart: [`/usr/local/sbin/wd-spool-clean`](../bin/wd-spool-clean)
  (Python).
- Conditions: `ConditionPathIsMountPoint=/var/spool/wsprdaemon`. Deferred
  until `var-spool-wsprdaemon.mount` is present (`After=`).
- Algorithm: scans every `/etc/wsprdaemon/env/*.env`, reads
  `WD_RECORDING_DIR` and `WD_RECEIVER_MODES`, computes
  `retention = max(window_for_mode) + 2 minutes`, and deletes WAVs
  older than that retention from the corresponding directory. Mode →
  window mapping in the script: `I1=1, W2=2, F2=2, F5=5, F15=15, F30=30`.
- Typical retentions:
    - I1-only (WWV/CHU IQ): 3 min
    - W2/F2/F5 dirs: 7 min
    - F15: 17 min
    - F30: 32 min

### wd-spool-clean.timer

- Schedule: `OnBootSec=5min`, `OnUnitActiveSec=5min`,
  `RandomizedDelaySec=15`. (The header comment says "every 2 minutes"
  but the active values fire every 5 minutes; the comment is slightly
  stale.)
- Install: `WantedBy=timers.target`.

---

## Resource limits

The shipped units do not set `MemoryMax=`, `CPUQuota=`, or `Nice=` —
the architecture spec proposes them (e.g. `Nice=-15` for kiwi recorder,
`MemoryMax=512M` for KA9Q recorder, `CPUQuota=100%` for decoder) but
they are not yet applied. Add them as drop-ins if needed for cohabiting
workloads.

---

## Spec-only units (not in `systemd/` yet)

These appear in [../wd-v4-architecture.md](../wd-v4-architecture.md)
but are not in the shipped tree. They are listed here so the gap
between spec and implementation is explicit.

| Unit                              | Spec section | Status        |
|-----------------------------------|--------------|---------------|
| `wd-hftime@INSTANCE.service`      | §3.2         | SPEC-ONLY. No unit file. The integration with `hf-timestd` is described in [INTEGRATION.md](INTEGRATION.md) but unwired in the client. |
| `wsprdaemon.service` (orchestrator) | §3.7       | SPEC-ONLY. Today the operator runs `wd-ctl apply` directly. |
| `wsprdaemon.timer` (2-min re-evaluator) | §3.7   | SPEC-ONLY. |
| `wd-upload-grape.service`         | §2.1, §3.6   | SPEC-ONLY. GRAPE upload daemon not started. |

Verification: `ls systemd/` shows exactly the 10 files enumerated under
[Shipped vs. spec-only](#shipped-vs-spec-only). The spec-only set is
called out at the bottom of [../CLAUDE.md](../CLAUDE.md) ("What's next").
