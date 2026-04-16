# wsprdaemon v4 — Service-Oriented Architecture Specification (v0.10, 2026-03-31)

> **Reading this document.** This is the design spec, not an implementation
> status page. Some sections describe pieces already in the tree; others
> describe intended behavior that has not yet landed. Running-code references
> below track the state as of 2026-04-15.
>
> **Implemented:** §2.1 service taxonomy (units present in [systemd/](systemd/)),
> §2.2 instance naming, §2.3 env/config passing (see [lib/wdlib/envgen.py](lib/wdlib/envgen.py)),
> §2.4 INI format (see [lib/wdlib/v4_parser.py](lib/wdlib/v4_parser.py) and
> [tests/wsprdaemon.conf](tests/wsprdaemon.conf)), §3.1 `wd-kiwi-record@`,
> §3.3 `wd-ka9q-record@`, §3.4 `wd-decode@`, §3.5 `wd-post@` including
> best-SNR merging, §3.6 `wd-upload-wsprnet`/`wd-upload-wsprdaemon`,
> housekeeping via `wd-spool-clean.service`/`.timer`, §4 `wd-ctl` orchestrator
> verbs (`apply`/`teardown`/`status`/`validate`/`inventory`/`version`/
> `verbosity`/`migrate-config`), §10 log paths, §11 SIGHUP log-level re-read,
> §12 validate hardening.
>
> **Spec-only (not yet in `systemd/` or implemented):**
>
> - §3.2 `wd-hftime@INSTANCE.service` — hf-timestd integration; no unit file yet.
> - §3.6 `wd-upload-grape.service` — optional GRAPE uploader; not started.
> - §3.7 `wsprdaemon.service` + `wsprdaemon.timer` orchestrator — `wsprdaemon.target`
>   exists, but the schedule-evaluator service/timer pair is not in the tree.
> - §2.1.2 SDRplay API auto-install path — the Fobos/RX888 paths are defined,
>   but SDRplay (closed-source) requires manual setup at this time.
>
> For operator-facing walkthroughs, see [README.md](README.md) and
> [docs/](docs/). This spec is referenced from those docs but is not the
> place to start reading.

## 1. Current Architecture Summary

wsprdaemon today is a ~15,700-line monolithic bash program. All `.sh` files are `source`'d
into a single process space from `wsprdaemon.sh`. The runtime process tree looks like this:

```
systemd
 └─ wsprdaemon.service (wsprdaemon.sh -A)
     └─ watchdog_daemon()                          # loops every ~2 min in WSPRDAEMON_ROOT_DIR
         ├─ spawn_upload_daemons()                  # wsprnet + wsprdaemon.org uploaders
         ├─ update_running_jobs_to_match_expected_jobs()
         │   ├─ setup_expected_jobs_file()          # reads wsprdaemon.conf WSPR_SCHEDULE[]
         │   └─ for each job: start_stop_job()
         │       └─ spawn_posting_daemon()          # one per logical receiver+band
         │           └─ posting_daemon() &          # infinite loop, polls for spot files
         │               ├─ run_recording_daemons()
         │               │   └─ spawn_decoding_daemon()  # one per real_receiver+band
         │               │       └─ decoding_daemon() &  # watches for wav files, runs wsprd/jt9
         │               │           └─ spawn_wav_recording_daemon()
         │               │               ├─ ka9q_recording_daemon() &   # runs wd_record (1→N bands)
         │               │               └─ kiwirecorder_manager_daemon() &  # runs kiwi_recorder.py (1→1)
         │               └─ post_files()            # merges spots, copies to upload queue
         ├─ ka9q_web_daemon()                       # if KA9Q receivers configured
         └─ grape_upload_daemon()                   # if GRAPE configured
```

### Key characteristics of the current design

- **Inverted dependency**: `posting_daemon()` spawns `decoding_daemon()`, which spawns the
  recording daemon. This creates a top-down spawn chain where the consumer starts the producer.
- **PID-file supervision**: Every daemon writes a `.pid` file; parent loops poll `ps` to check
  liveness. The watchdog re-checks every 2 minutes via `check_for_zombies()`.
- **File-based IPC**: Recording → Decoding via wav files in `/dev/shm/wsprdaemon/recording/`.
  Decoding → Posting via `*_spots.txt` files in `DECODING_CLIENTS_SUBDIR`. Posting → Upload
  via spot files in `~/wsprdaemon/uploads/`.
- **Config re-sourcing**: Many daemons do `source ${WSPRDAEMON_CONFIG_FILE}` mid-loop to pick
  up config changes. `RECEIVER_LIST[]` and `WSPR_SCHEDULE[]` are bash arrays declared in the
  config file.
- **Schedule-driven**: `WSPR_SCHEDULE[]` maps time-of-day → list of (receiver, band, mode) jobs.
  The watchdog evaluates this every 2 minutes and starts/stops jobs to match.

---

## 2. Target Architecture

wsprdaemon becomes a **fleet orchestrator / configurator**. It reads `wsprdaemon.conf`, computes
the desired set of systemd services, and uses `systemctl` to ensure they exist, are correctly
configured, and are running. It does not contain the work itself.

### 2.1 Service Taxonomy

```
┌──────────────────────────────────────────────────────────────────────┐
│                    EXTERNAL SOURCE SERVICES                          │
│  (wsprdaemon installs/configures/enables but doesn't own)            │
├──────────────────────────────────────────────────────────────────────┤
│  radiod@INSTANCE.service      — ka9q-radio SDR daemon               │
│    Source: https://github.com/ka9q/ka9q-radio                        │
│    Depends on hardware drivers (see §2.1.2)                          │
│                                                                      │
│  ft4-ft8-decode.service       — FT4/FT8 decoder                     │
│    Source: https://github.com/ka9q/ft8_lib                           │
│    Depends on: ka9q-radio (multicast streams)                        │
│                                                                      │
│  pskreporter-upload.service   — PSK Reporter spot uploader           │
│    Source: https://github.com/pjsg/ftlib-pskreporter                 │
│    Depends on: ft8_lib (decoded FT4/FT8 spots)                      │
│                                                                      │
│  ka9q-web.service             — KA9Q web UI                          │
│    Source: https://github.com/wa2n-code/ka9q-web                     │
│    Depends on: libonion (https://github.com/davidmoreno/onion)       │
│                                                                      │
│  dumphfdl.service             — HFDL decoder                         │
│    Source: https://github.com/ka9q/dumphfdl                          │
│    Depends on: ka9q-radio (multicast streams)                        │
│                                                                      │
│  kiwi-web@INSTANCE.service    — KiwiSDR web (existing unit)          │
└──────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────┐
│                    HARDWARE DRIVER LIBRARIES                         │
│  (installed only when matching hardware is configured locally)        │
├──────────────────────────────────────────────────────────────────────┤
│  libfobos  — Fobos SDR driver library                                │
│    Source: https://github.com/ka9q/libfobos                          │
│    Required when: a Fobos SDR is configured as a local device        │
│                                                                      │
│  SDRplay API — SDRplay device driver (closed-source)                 │
│    Source: downloaded from SDRplay website (not a git repo)           │
│    Required when: an SDRplay device is configured as a local device   │
│    Note: closed-source; must be downloaded and installed separately   │
│    before ka9q-radio can communicate with SDRplay hardware            │
│                                                                      │
│  (RX-888 support is built into ka9q-radio natively — no extra lib)   │
└──────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────┐
│              PYTHON DEPENDENCIES                                     │
│       (installed into /opt/wsprdaemon/python/)                       │
├──────────────────────────────────────────────────────────────────────┤
│  ka9q-python (pip: ka9q-python)                                      │
│    — Pure-Python radiod control API (dynamic channel                 │
│      creation, discovery, RTP recording, GPS/RTP timing).            │
│    — Source: https://github.com/mijahauan/ka9q-python                │
│    — Used by: wd-ka9q-record, wd-hftime                             │
│                                                                      │
│  hf-timestd (pip or source install)                                  │
│    — HF time-standard service: listens to WWV/WWVH via              │
│      ka9q-python, detects second-tick tones to calibrate             │
│      wav recording start times to sub-millisecond accuracy.          │
│    — Source: https://github.com/mijahauan/hf-timestd                 │
│    — Used by: wd-hftime.service                                      │
└──────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────┐
│              NEW FIRST-CLASS WD SERVICES                             │
│       (systemd template units, managed by wd-ctl)                    │
├──────────────────────────────────────────────────────────────────────┤
│  TIME CALIBRATION LAYER (KA9Q systems only)                          │
│  └─ wd-hftime@INSTANCE.service       (one per radiod inst)          │
│                                                                      │
│  RECORDING LAYER                                                     │
│  ├─ wd-kiwi-record@INSTANCE.service   (1:1, one per chan)           │
│  └─ wd-ka9q-record@INSTANCE.service   (1:N, one per mcast)         │
│                                                                      │
│  DECODING LAYER                                                      │
│  └─ wd-decode@INSTANCE.service        (one per rx+band)             │
│                                                                      │
│  POSTING LAYER                                                       │
│  └─ wd-post@INSTANCE.service          (one per logical rx)          │
│                                                                      │
│  UPLOAD LAYER                                                        │
│  ├─ wd-upload-wsprnet.service         (singleton)                   │
│  ├─ wd-upload-wsprdaemon.service      (singleton)                   │
│  └─ wd-upload-grape.service           (singleton, optional)         │
│                                                                      │
│  ORCHESTRATOR                                                        │
│  ├─ wsprdaemon.service                (the new wd-ctl)              │
│  └─ wsprdaemon.timer                  (schedule evaluator)          │
└──────────────────────────────────────────────────────────────────────┘
```

#### 2.1.1 Config-Driven Service Installation

The set of external services that wsprdaemon installs and configures is **derived
from the receiver and schedule configuration** — it is not manually specified.  When
`wd-ctl apply` parses `wsprdaemon.conf`, it walks the receiver definitions and
determines which external services are required:

- **Receiver names starting with `KA9Q`** imply that ka9q-radio multicast streams
  are needed.  For each such receiver, wsprdaemon determines whether the stream
  source is **local** or **remote**:

  - **Local**: A USB-attached SDR device is present on this host.  wsprdaemon is
    responsible for installing ka9q-radio, the appropriate hardware driver (see
    §2.1.2), and configuring and starting `radiod@INSTANCE.service`.  The receiver
    section in `wsprdaemon.conf` must specify the hardware type and the device's
    USB serial number (see §2.1.3).

  - **Remote**: The multicast stream is generated by a `radiod` instance running on
    another host on the LAN.  wsprdaemon only listens to (and potentially tunes)
    the remote multicast stream.  It is **not** responsible for installing,
    configuring, or managing the remote `radiod` — only for ensuring that the
    local recording and decoding services can receive the stream.

- **FT4/FT8 decoding and PSK Reporter uploading** are enabled when the config
  includes receivers that carry FT4/FT8 traffic.  These services depend on
  ka9q-radio multicast streams and are installed from their respective repos.

- **ka9q-web** is installed when any KA9Q receiver is configured (local or remote)
  to provide the web monitoring UI.  It requires the **onion** HTTP library, which
  is built from source as a prerequisite.

- **dumphfdl** is installed when HFDL decoding is enabled in the configuration.

- **Kiwi receivers** (`type = kiwi`) use `kiwi_recorder.py` (already tracked in
  `components.ini`) and do not require ka9q-radio.

All external services that come from git repositories are subject to commit
pinning via `components.ini` (see §10).  Each service's installed version is
verified by `wd-ctl apply` at startup.

#### 2.1.2 Hardware Drivers for Local SDR Devices

When a KA9Q receiver is configured as local, the type of USB-attached SDR
determines which driver library is needed:

| Device Type | Driver | Source | Notes |
|-------------|--------|--------|-------|
| RX-888 (RX888, RX888 Mk2) | Built into ka9q-radio | — | Most common device; no extra library needed |
| Fobos SDR | libfobos | https://github.com/ka9q/libfobos | Open-source; built from pinned commit |
| SDRplay (RSP1, RSPdx, etc.) | SDRplay API | SDRplay website (closed-source) | Must be downloaded separately; not a git repo |

The SDRplay API is the only closed-source dependency.  Because it is not available
as a git repository, it cannot be pinned via `components.ini`.  The installer
downloads and installs it from the SDRplay website.  The installed API version is
recorded in the wsprdaemon log for diagnostic purposes.

After the appropriate driver is installed, ka9q-radio can communicate with the
locally attached device and generate multicast streams from it.

#### 2.1.3 USB SDR Serial Numbers

Each locally attached SDR device **must** have a serial number specified in
`wsprdaemon.conf`, because a single host may have multiple SDR devices of the
same type on the USB bus.  The serial number associates a specific physical
device with the `radiod@INSTANCE` configuration that generates multicast streams
from it.

Example receiver section with serial number:

```ini
[receiver:KA9Q_0]
type     = ka9q
location = local
hardware = rx888
serial   = 2A4B00003
ip       = wspr-pcm.local
```

**Serial number discovery**: If the operator does not know the serial numbers of
their attached SDR devices, `wd-ctl` provides a discovery command:

```bash
wd-ctl list-devices
```

This command scans the USB bus for known SDR device types (RX-888, Fobos, SDRplay)
and displays each device's type, serial number, and USB bus/port location.  The
operator can then copy the serial numbers into `wsprdaemon.conf`.

If an SDR device does not have a serial number programmed (some early RX-888 units
ship without one), `wd-ctl list-devices` reports this and provides instructions
for programming a serial number using the device's manufacturer utility.

### 2.2 Instance Naming Convention

Template services use `@INSTANCE` where INSTANCE encodes the identity:

| Service | Instance Format | Example |
|---------|----------------|---------|
| `wd-hftime@` | `RECEIVER` | `wd-hftime@KA9Q_0.service` |
| `wd-kiwi-record@` | `RECEIVER-BAND` | `wd-kiwi-record@KIWI_0-80.service` |
| `wd-ka9q-record@` | `RECEIVER` | `wd-ka9q-record@KA9Q_0.service` |
| `wd-decode@` | `RECEIVER-BAND` | `wd-decode@KA9Q_0-40.service` |
| `wd-post@` | `RECEIVER-BAND` | `wd-post@MERG_0-80.service` |

Notes:
- KA9Q recording is per-receiver (not per-band) because one `wd_record` process listens to
  a single multicast stream and outputs wav files for all bands contained in that stream.
- Kiwi recording is per-receiver-per-band because each `kiwi_recorder.py` handles one channel.
- Decoding and posting are always per-receiver-per-band.
- `kiwi_recorder.py` is used as-is; it is not modified by wsprdaemon.
- `wd-hftime` is mandatory for every KA9Q receiver.  It runs the `hf-timestd` daemon,
  which uses `ka9q-python` to dynamically create a WWV channel on radiod, listens
  for second-tick tones, and publishes a time-offset calibration file that the
  recording daemon reads to align wav file start times.
- `wd-ka9q-record` uses `ka9q-python` to dynamically create all WSPR/FST4W channels
  on radiod at startup.  The `radiod@.conf` file no longer needs `[channels]` sections
  for wsprdaemon bands — only `[global]` and `[hardware]` are required.  This makes
  `wsprdaemon.conf` the single source of truth for which frequencies are recorded.

### 2.3 Environment / Config Passing

Each service instance gets its configuration from a generated environment file:

```
/etc/wsprdaemon/env/wd-decode@KA9Q_0-40.env
```

The orchestrator generates these from `wsprdaemon.conf`. Example contents:

```ini
# Auto-generated by wd-ctl from wsprdaemon.conf — do not edit
WD_RECEIVER_NAME=KA9Q_0
WD_RECEIVER_BAND=40
WD_RECEIVER_MODES=W2:F2:F5
WD_RECEIVER_IP=wspr-pcm.local
WD_RECEIVER_CALL=AI6VN
WD_RECEIVER_GRID=CM87tj
WD_RECEIVER_FREQ_KHZ=7040100
WD_RECORDING_DIR=/var/spool/wsprdaemon/recording/KA9Q_0
WD_DECODING_DIR=/var/spool/wsprdaemon/recording/KA9Q_0/40
WD_UPLOAD_DIR=/var/spool/wsprdaemon/uploads/wsprnet/AI6VN_CM87tj/KA9Q_0/40
WD_LOG_DIR=/var/log/wsprdaemon
WD_RUN_DIR=/run/wsprdaemon
```

### 2.4 Configuration File Format (INI)

The v4 config file moves from bash arrays to INI format for readability and
compatibility with non-bash tooling. The orchestrator parses this to generate
environment files and compute the desired service set.

```ini
# /etc/wsprdaemon/wsprdaemon.conf

[general]
reporter_call = AI6VN
reporter_grid = CM87tj

[receiver:KA9Q_0]
type     = ka9q
location = local
hardware = rx888
serial   = 2A4B00003
timesync = auto
ip       = wspr-pcm.local

[receiver:KA9Q_0:80]
; Decode modes determine how many 1-minute wav files are retained.
; W2 = WSPR-2 (2 min), F2 = FST4W-120 (2 min), F5 = FST4W-300 (5 min),
; F15 = FST4W-900 (15 min), F30 = FST4W-1800 (30 min).
; The longest mode (F5 = 5 min) means the decoder must retain at least
; 5 one-minute wav files.  During decode, wsprd concatenates retained
; files into a single multi-minute wav, temporarily doubling disk usage.
modes = W2 F2 F5

[receiver:KA9Q_0:40]
modes = W2 F2

[receiver:KA9Q_0:30]
modes = W2

[receiver:KA9Q_0:20]
modes = W2 F2 F5

[receiver:KA9Q_0:17]
modes = W2

[receiver:KA9Q_0:15]
modes = W2

[receiver:KA9Q_0:12]
modes = W2

[receiver:KA9Q_0:10]
modes = W2

[receiver:KIWI_0]
type = kiwi
ip   = kiwisdr-0.local
channels = 2

[receiver:KIWI_0:80]
modes = W2 F2

[receiver:KIWI_0:40]
modes = W2

[merge:MERG_0]
sources = KA9Q_0 KIWI_0
band = 80

[schedule:default]
; The default schedule applies at all times unless overridden.
; Only relevant for Kiwi receivers with limited channels.
; KA9Q receivers MUST NOT appear in schedule transitions — their
; recording services run continuously and unconditionally.
; If a schedule entry references a KA9Q receiver for start/stop,
; wd-ctl treats that as a configuration error and logs an alert.
bands = 80 40

[schedule:night]
start = sunset+30
stop  = sunrise-30
bands = 160 80 60 40

[upload:wsprnet]
enabled = true
call    = AI6VN
grid    = CM87tj

[upload:wsprdaemon]
enabled = true

[upload:grape]
enabled = false
```

---

## 3. Service Unit File Templates

### 3.1 wd-kiwi-record@ — Kiwi Recording (1:1)

```ini
# /etc/systemd/system/wd-kiwi-record@.service
[Unit]
Description=wsprdaemon Kiwi recorder for %i
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=wsprdaemon
Group=radio
EnvironmentFile=/etc/wsprdaemon/env/wd-kiwi-record@%i.env
ExecStart=/usr/local/sbin/wd-kiwi-record
WorkingDirectory=/var/spool/wsprdaemon/recording/%i
Nice=-15

# Restart policy: always restart with backoff
Restart=always
RestartSec=10
StartLimitIntervalSec=300
StartLimitBurst=10

# Resource limits
MemoryMax=256M
CPUQuota=25%

# Logging — wd-logger handles per-daemon log files;
# journal captures stdout/stderr as a fallback
StandardOutput=journal
StandardError=journal
SyslogIdentifier=wd-kiwi-record@%i

[Install]
WantedBy=wsprdaemon.target
```

### 3.2 wd-hftime@ — HF Time Calibration (KA9Q systems only)

Recording start times should be determined as accurately as possible.  The
`wd-hftime@` service provides sub-millisecond time calibration for KA9Q
receiver instances.  It is **optional but enabled by default** — if disabled,
the recorder falls back to the system clock.

#### 3.2.1 Time Source Priority

The recording daemon (`wd-ka9q-record`) selects the best available time source
using the following priority order:

| Priority | Source | Accuracy | When Used |
|----------|--------|----------|-----------|
| 1 (highest) | Turn Island Systems TimeSync | ~1 μs | TimeSync hardware injector detected |
| 2 | WWV/NCHU via hf-timestd | sub-ms | No TimeSync; wideband SDR with WWV in bandwidth |
| 3 (fallback) | Linux system clock (NTP) | ~1–10 ms | Neither TimeSync nor WWV available |

**Turn Island Systems TimeSync** (highest priority):  The TimeSync hardware
injector creates an 82 MHz carrier that is BPSK-modulated with a precision
timing signal.  The `wd-ka9q-record` utility (via `wd_record`) can be
instructed to look for the alias of this 82 MHz signal at approximately
42 MHz within the SDR's receive bandwidth.  When configured, `wd_record`
detects the PPS (pulse-per-second) transitions in the BPSK modulation and
uses them to determine the precise start time of wav file recordings for
all signals coming from the same SDR device.

If multiple RX-888 devices are attached to the same host, each gets its own
`wd-ka9q-record` instance.  Each instance independently detects the same
TimeSync signal at ~42 MHz, so wav files produced by multiple SDR devices
are synchronized to within approximately 1 microsecond of each other.  This
level of synchronization is very useful for time-of-flight measurements,
though not accurate enough for beam steering or beam forming.

Detection of the TimeSync signal is described in §3.2.2.

**WWV/NCHU via hf-timestd** (second priority):  If no TimeSync hardware is
present, `wd-hftime@` runs the `hf-timestd` daemon.  It uses `ka9q-python`
to dynamically create a WWV (or WWVH) AM channel on radiod, listens for the
second-tick tones (5 ms bursts of 1200 Hz at the top of each second) and the
minute markers (800 ms tone at 1000 Hz), and computes the precise offset
between the system clock and actual RF-received wall-clock time.  The computed
offset is written to a calibration file that `wd-ka9q-record` reads to align
wav recording start times to sub-millisecond accuracy.  The WWV channel is
created dynamically via `ka9q-python` at service start and removed on stop;
it does not require any entry in `radiod@.conf`.

**System clock** (fallback):  If neither TimeSync hardware nor WWV reception
is available (e.g., a narrowband SDR that cannot receive WWV), the recorder
uses the Linux system clock directly.  The system clock is typically
synchronized via NTP to within a few milliseconds, which is adequate for WSPR
decoding but not for precision time-of-flight work.

#### 3.2.2 TimeSync Signal Detection

When `wd-ka9q-record` starts, it checks whether TimeSync hardware is present
by examining the SDR's receive bandwidth for the ~42 MHz alias of the 82 MHz
BPSK carrier.  The detection procedure is:

1. The receiver configuration in `wsprdaemon.conf` may include a
   `timesync = true` flag indicating that TimeSync hardware is connected
   to this SDR.  If set, `wd_record` tunes to the expected alias frequency
   and attempts to lock onto the BPSK PPS signal.
2. If the `timesync` flag is not explicitly set, `wd_record` can optionally
   perform an auto-detect scan at startup (enabled by `timesync = auto`).
3. If TimeSync is detected (or confirmed via config), `wd_record` uses
   the BPSK PPS transitions as the primary time reference.  The hf-timestd
   WWV calibration is still run but serves as a secondary/monitoring source.
4. If TimeSync is not detected, `wd_record` falls back to the hf-timestd
   calibration file or the system clock per the priority table above.

#### 3.2.3 wd-hftime@ Unit File

```ini
# /etc/systemd/system/wd-hftime@.service
[Unit]
Description=wsprdaemon HF time calibration for %i
After=radiod@%i.service
Requires=radiod@%i.service

[Service]
Type=simple
User=wsprdaemon
Group=radio
EnvironmentFile=/etc/wsprdaemon/env/wd-hftime@%i.env
ExecStart=/opt/wsprdaemon/python/bin/python3 -m hf_timestd \
    --radiod-host ${WD_RADIOD_STATUS_ADDRESS} \
    --calib-file /run/wsprdaemon/%i/hftime.json
WorkingDirectory=/run/wsprdaemon

Restart=always
RestartSec=10

StandardOutput=journal
StandardError=journal
SyslogIdentifier=wd-hftime@%i

[Install]
WantedBy=wsprdaemon.target
```

The calibration file (`/run/wsprdaemon/KA9Q_0/hftime.json`) is written to tmpfs and
contains at minimum:

```json
{
  "source": "wwv",
  "wwv_freq_hz": 10000000,
  "offset_ns": -1423,
  "uncertainty_ns": 250,
  "last_update": "2026-03-27T14:02:01.003Z"
}
```

When TimeSync is the active source, the `source` field reads `"timesync"` and
the `offset_ns` reflects the BPSK PPS-derived offset.

The recorder reads `offset_ns` to shift its recording window start time so that the
wav file boundaries align with the true second edge as received at the antenna.

### 3.3 wd-ka9q-record@ — KA9Q Recording (1:N, dynamic channels)

This service replaces the old static-channel `wd_record` approach.  Instead of
depending on pre-configured `[channels]` sections in `radiod@.conf`, it uses
`ka9q-python` (`RadiodControl`) to dynamically create all needed WSPR/FST4W
channels on radiod at startup.  Channels are created based on the band list in
`wsprdaemon.conf` — making `wsprdaemon.conf` the single source of truth.

At startup the service:

1. Checks for Turn Island Systems TimeSync signal at the expected alias
   frequency (~42 MHz) if `timesync = true` or `timesync = auto` is set
   in the receiver config (see §3.2.2).
2. If no TimeSync is detected, waits for `wd-hftime@INSTANCE` to publish
   its first WWV calibration (via a `Wants=` on the hftime service and a
   brief poll of the calib file).  If hftime is disabled, proceeds with
   system clock.
3. Reads the band/mode configuration from its environment file.
4. Uses `RadiodControl.create_channel()` to create one radiod channel per band
   (frequency, preset=usb, sample_rate=12000 for WSPR; preset=iq for WWV-IQ, etc.).
5. Uses `RTPRecorder` to receive the RTP streams and write 1-minute wav files into
   the spool directory, using the best available time offset (TimeSync PPS,
   hftime WWV calibration, or system clock) to align start times.
6. On `SIGTERM` (service stop), calls `RadiodControl.remove_channel()` for each
   channel it created, cleaning up radiod state.

Because channels are created dynamically, `radiod@.conf` only needs:

```ini
[global]
; ... hardware-independent defaults ...
mode = usb              ; default mode for dynamic channel creation
status = KA9Q_0-status.local

[rx888]
; ... hardware-specific settings ...
device = rx888
```

No `[WSPR]`, `[FT4]`, `[FT8]`, or `[WWV]` channel sections are needed.  These are
all created at runtime by the wsprdaemon recording service and by the FT4/FT8
decoder services.

```ini
# /etc/systemd/system/wd-ka9q-record@.service
[Unit]
Description=wsprdaemon KA9Q recorder for %i
After=network-online.target
Requires=radiod@%i.service
After=radiod@%i.service
Wants=wd-hftime@%i.service
After=wd-hftime@%i.service

[Service]
Type=simple
User=wsprdaemon
Group=radio
EnvironmentFile=/etc/wsprdaemon/env/wd-ka9q-record@%i.env
ExecStart=/usr/local/sbin/wd-ka9q-record
WorkingDirectory=/var/spool/wsprdaemon/recording/%i
Nice=-10

Restart=always
RestartSec=10
StartLimitIntervalSec=300
StartLimitBurst=10

MemoryMax=512M

StandardOutput=journal
StandardError=journal
SyslogIdentifier=wd-ka9q-record@%i

[Install]
WantedBy=wsprdaemon.target
```

The `wd-ka9q-record` wrapper script (bash) calls a Python helper that uses
`ka9q-python` for channel management and RTP recording, then writes wav files
to the spool directory.  The bash wrapper handles signal trapping and cleanup.

**Stale wav file protection** (see section 5.4).

### 3.4 wd-decode@ — Decoding (per receiver+band)

```ini
# /etc/systemd/system/wd-decode@.service
[Unit]
Description=wsprdaemon decoder for %i
# Dynamically generated dependency on the appropriate recorder:
# For KA9Q: BindsTo=wd-ka9q-record@RECEIVER.service
# For Kiwi: BindsTo=wd-kiwi-record@RECEIVER-BAND.service
# These are written into drop-in files by the orchestrator.

[Service]
Type=simple
User=wsprdaemon
Group=radio
EnvironmentFile=/etc/wsprdaemon/env/wd-decode@%i.env
ExecStart=/usr/local/sbin/wd-decode
WorkingDirectory=/var/spool/wsprdaemon/recording/%I

Restart=always
RestartSec=5

# Decoding is CPU-intensive but bursty
Nice=5
CPUQuota=100%

StandardOutput=journal
StandardError=journal
SyslogIdentifier=wd-decode@%i

[Install]
WantedBy=wsprdaemon.target
```

### 3.5 wd-post@ — Posting (per logical receiver+band)

The posting daemon is responsible for collecting decoded spots from all receiver
sources that contribute to a given band, merging them, and queuing the results
for upload.  A posting daemon instance may receive spots from any mixture of
source types — KA9Q, Kiwi, or other SDR sources.

#### 3.5.1 Spot Merging (Best-SNR Union)

For each decode window (e.g., each 2-minute WSPR cycle), the posting daemon:

1. **Waits** for all of its configured source decoders to deposit their spot
   files for the current time window.  The set of source directories is listed
   in the `WD_POST_SUPPLIER_DIRS` environment variable.
2. **Reads** each source's spot file and builds a per-transmitter table.  Each
   spot is identified by its transmitting callsign and grid.
3. **Selects the best SNR** for each transmitter across all sources.  SNR values
   are typically negative (e.g., -15 dB is better than -20 dB — a higher, less
   negative value means a stronger signal).  The spot with the highest (least
   negative) SNR wins.
4. **Writes** the merged best-of-union spot set to the wsprnet upload queue.
   This merged file is what gets uploaded to wsprnet.org, identified by the
   reporter callsign and grid (e.g., `AI6VN_CM87tj`).

#### 3.5.2 Dual Upload Path

The merged spot set and the per-receiver spot sets are uploaded to different
destinations:

- **wsprnet.org** (`wd-upload-wsprnet`): Receives the **merged best-SNR union**
  of spots.  This is one set of spots per reporter per band per time window.
  The reporter is identified by callsign + grid (e.g., `AI6VN / CM87tj`).

- **wsprdaemon.org** (`wd-upload-wsprdaemon`): Receives **per-receiver spot sets**
  — one upload per receiver per band, without merging.  The wsprdaemon server
  maintains spots indexed by **reporter ID + receiver name** (e.g.,
  `AI6VN / KA9Q_0 / 40m` and `AI6VN / KIWI_0 / 40m` are distinct spot sets).
  This preserves the full per-receiver detail for analysis, propagation studies,
  and receiver performance comparison.

Both upload paths operate in parallel.  The posting daemon writes to both upload
queues simultaneously; the upload daemons (`wd-upload-wsprnet` and
`wd-upload-wsprdaemon`) consume their respective queues independently.

#### 3.5.3 Unit File

```ini
# /etc/systemd/system/wd-post@.service
[Unit]
Description=wsprdaemon spot poster for %i

[Service]
Type=simple
User=wsprdaemon
Group=radio
EnvironmentFile=/etc/wsprdaemon/env/wd-post@%i.env
ExecStart=/usr/local/sbin/wd-post
WorkingDirectory=/var/spool/wsprdaemon/posting/%i

Restart=always
RestartSec=5

StandardOutput=journal
StandardError=journal
SyslogIdentifier=wd-post@%i

[Install]
WantedBy=wsprdaemon.target
```

### 3.6 Upload Services (singletons)

```ini
# /etc/systemd/system/wd-upload-wsprnet.service
[Unit]
Description=wsprdaemon wsprnet.org uploader
After=network-online.target

[Service]
Type=simple
User=wsprdaemon
Group=radio
EnvironmentFile=/etc/wsprdaemon/env/wd-upload-wsprnet.env
ExecStart=/usr/local/sbin/wd-upload-wsprnet
WorkingDirectory=/var/spool/wsprdaemon/uploads/wsprnet

Restart=always
RestartSec=30

StandardOutput=journal
StandardError=journal
SyslogIdentifier=wd-upload-wsprnet

[Install]
WantedBy=wsprdaemon.target
```

### 3.7 The Orchestrator

```ini
# /etc/systemd/system/wsprdaemon.service
[Unit]
Description=wsprdaemon fleet orchestrator
After=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
User=wsprdaemon
Group=radio
ExecStart=/usr/local/sbin/wd-ctl apply
ExecReload=/usr/local/sbin/wd-ctl apply
ExecStop=/usr/local/sbin/wd-ctl teardown

StandardOutput=journal
StandardError=journal
SyslogIdentifier=wsprdaemon

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/wsprdaemon.timer
[Unit]
Description=wsprdaemon schedule re-evaluator (every 2 min)

[Timer]
OnCalendar=*:0/2
Persistent=true

[Install]
WantedBy=timers.target
```

The timer fires `wsprdaemon-schedule.service` (a oneshot that runs `wd-ctl apply`)
every 2 minutes, replacing the current watchdog loop.

---

## 4. The Orchestrator: `wd-ctl`

This is what wsprdaemon.sh becomes. It's a bash script (or set of scripts) whose job is:

### 4.1 `wd-ctl apply` (the core loop)

```
1. Parse /etc/wsprdaemon/wsprdaemon.conf (INI format)
2. Evaluate [schedule:*] sections for current time-of-day
   (same sunrise/sunset logic as current update_hhmm_sched_file)
3. Validate: if any schedule entry references a KA9Q receiver for
   start/stop, log an error and skip that entry — KA9Q recording
   services run unconditionally and must not be schedule-toggled.
4. Compute DESIRED_SERVICES[] — the full set of service instances
   that should be running right now
5. Query systemd for RUNNING_SERVICES[] — what's actually running
6. Diff:
   a. Services in DESIRED but not RUNNING → generate env file, start
   b. Services in RUNNING but not DESIRED → stop, remove env file
   c. Services in both → verify env file matches config, reload if changed
7. For each new service:
   a. Generate /etc/wsprdaemon/env/SERVICE.env
   b. Generate drop-in files for dynamic dependencies (e.g., wd-decode
      binds to the correct recorder type)
   c. systemctl daemon-reload (if any unit files changed)
   d. systemctl start SERVICE
```

### 4.2 Handling Dynamic Dependencies

The tricky part: `wd-decode@KA9Q_0-40.service` needs to depend on
`wd-ka9q-record@KA9Q_0.service`, but `wd-decode@KIWI_0-80.service` needs to depend on
`wd-kiwi-record@KIWI_0-80.service`. This can't be in the template — it varies per instance.

Solution: **drop-in files** generated by the orchestrator:

```bash
# Generated by wd-ctl for a KA9Q decode job
mkdir -p /etc/systemd/system/wd-decode@KA9Q_0-40.service.d/
cat > /etc/systemd/system/wd-decode@KA9Q_0-40.service.d/recorder.conf <<EOF
[Unit]
BindsTo=wd-ka9q-record@KA9Q_0.service
After=wd-ka9q-record@KA9Q_0.service
EOF
```

### 4.3 Handling MERG'd Receivers

A MERG'd receiver (e.g., `MERG_0` merging `KIWI_0` and `KA9Q_0` on band 80) creates:

- `wd-kiwi-record@KIWI_0-80.service` — records from the Kiwi
- `wd-ka9q-record@KA9Q_0.service` — records from the RX-888 (all bands)
- `wd-decode@KIWI_0-80.service` — decodes Kiwi's wav files
- `wd-decode@KA9Q_0-80.service` — decodes KA9Q's wav files for band 80
- `wd-post@MERG_0-80.service` — merges spots from both decoders

The posting service's env file lists the supplier decode dirs:

```ini
WD_POST_SUPPLIER_DIRS="/var/spool/wsprdaemon/recording/KIWI_0/80/posting/MERG_0 /var/spool/wsprdaemon/recording/KA9Q_0/80/posting/MERG_0"
```

And the drop-in makes it depend on both decoders:

```ini
[Unit]
BindsTo=wd-decode@KIWI_0-80.service
BindsTo=wd-decode@KA9Q_0-80.service
After=wd-decode@KIWI_0-80.service
After=wd-decode@KA9Q_0-80.service
```

### 4.4 `wd-ctl teardown`

```
1. List all wd-* service instances
2. systemctl stop each one
3. Remove env files and drop-in dirs
4. systemctl daemon-reload
```

### 4.5 `wd-ctl status`

Replaces `wsprdaemon.sh -s`:

```
1. List all wd-* services, grouped by receiver
2. For each: show systemd state, journal tail, spot counts
3. Show upload queue depths
```

---

## 5. Data Flow — File-Based IPC (Retained)

The current file-based IPC is well-suited to systemd services. The directory structure
becomes a contract between services:

```
/var/spool/wsprdaemon/                        # WD_SPOOL_DIR (persistent spool)
├── recording/
│   ├── KA9Q_0/                               # wd-ka9q-record@KA9Q_0 writes here
│   │   ├── 2506_KA9Q_0_007040100_usb_iq.wav  # wd_record output (all bands from one mcast)
│   │   ├── 40/                               # wd-decode@KA9Q_0-40 works here
│   │   │   ├── decode.log
│   │   │   └── posting/
│   │   │       └── MERG_0/                   # symlink target for wd-post@MERG_0-40
│   │   │           └── 250625_1200_spots.txt # decoded spots deposited here
│   │   └── 80/                               # wd-decode@KA9Q_0-80 works here
│   │       └── ...
│   ├── KIWI_0/
│   │   └── 80/                               # wd-kiwi-record@KIWI_0-80 writes here
│   │       ├── *.wav
│   │       └── posting/
│   │           └── MERG_0/
│   │               └── 250625_1200_spots.txt
│   └── (no top-level posting dir needed — systemd manages posting)
│
├── uploads/                                  # persistent storage (survives reboot)
│   ├── wsprnet/
│   │   └── AI6VN_CM87tj/
│   │       └── KA9Q_0/40/                   # wd-post writes here
│   │           └── 250625_1200_spots.txt    # wd-upload-wsprnet reads here
│   ├── wsprdaemon/
│   │   ├── spots/                            # wd-upload-wsprdaemon reads here
│   │   └── noise/
│   └── grape/                                # wd-upload-grape reads here

/run/wsprdaemon/                              # WD_RUN_DIR (tmpfs, runtime state)
└── (runtime pidfiles, lock files if needed — mostly replaced by systemd)

/var/log/wsprdaemon/                          # WD_LOG_DIR (per-daemon log files)
├── wd-kiwi-record@KIWI_0-80.log
├── wd-ka9q-record@KA9Q_0.log
├── wd-decode@KA9Q_0-40.log
└── ...
```

**Note on spool location**: The recording directory tree lives under `/var/spool/wsprdaemon/`
following the FHS convention for application spool data.  The recording subtree should be
mounted as tmpfs for performance.  The installer calculates the required size automatically
from the configuration.

### 5.3 tmpfs Sizing (Auto-Calculated by Installer)

The tmpfs mount for `/var/spool/wsprdaemon/recording/` must be large enough to hold all
retained wav files at peak usage.  The required size is derived directly from the config:

**Per-band retention rule**: The decode `modes` list for each band determines how many
1-minute wav files must be retained.  The longest mode sets the retention window:

| Mode | Duration | 1-min wav files retained |
|------|----------|------------------------|
| W2 (WSPR-2) | 2 min | 2 |
| F2 (FST4W-120) | 2 min | 2 |
| F5 (FST4W-300) | 5 min | 5 |
| F15 (FST4W-900) | 15 min | 15 |
| F30 (FST4W-1800) | 30 min | 30 |

If a band specifies `modes = W2 F2 F5`, the longest is F5 (5 minutes), so 5 wav files
are retained.  Each 2-minute decode (W2, F2) runs as soon as 2 files are available;
the 5-minute decode (F5) waits until all 5 are present.

**Concatenation doubling**: When the decoder runs, it copies the retained 1-minute wav
files into a single concatenated multi-minute wav file (e.g., 5 × 1-min → 1 × 5-min).
This temporarily doubles the disk usage for that band.  The sizing formula must account
for this peak.

**Sizing formula**:

```
For each band on each receiver:
    wav_size_1min  = sample_rate × bits_per_sample / 8 × 60    # ~1.92 MB for 16-bit 16 kHz
    retention      = max(duration_minutes(mode) for mode in band.modes)
    band_peak      = wav_size_1min × retention × 2              # ×2 for concatenation copy

total_recording_bytes = sum(band_peak for all bands on all receivers)
tmpfs_size            = total_recording_bytes × 1.2             # 20% headroom
```

**Example** (KA9Q_0 with 8 bands, longest mode F5 on bands 80 and 20, W2 on the rest):

```
Bands with F5 (5 min): 80, 20  → 2 bands × 1.92 MB × 5 × 2 = 38.4 MB
Bands with W2 (2 min): 40, 30, 17, 15, 12, 10  → 6 bands × 1.92 MB × 2 × 2 = 46.1 MB
Subtotal KA9Q_0: 84.5 MB
Plus KIWI_0 (2 bands, F2): 2 × 1.92 × 2 × 2 = 15.4 MB
Total: ~100 MB × 1.2 headroom ≈ 120 MB
```

The installer writes the fstab entry with the computed size:

```
tmpfs  /var/spool/wsprdaemon/recording  tmpfs  defaults,size=120M,mode=0750,uid=wsprdaemon,gid=radio  0 0
```

`wd-ctl apply` also validates at startup that the mounted tmpfs has sufficient free space
for the current configuration and logs a warning if it does not.

### 5.4 Stale Wav File Cleanup (tmpfs Overflow Protection)

If the decoding daemon (`wd-decode@`) is not running — due to a crash, a stopped
service, or a misconfiguration — wav files will accumulate in the recording spool
directory without being consumed and deleted.  Because the spool lives on a
size-limited tmpfs, this will eventually fill the filesystem and cause the recorder
to fail as well, cascading a total system outage.

To prevent this, the recording daemons (`wd-ka9q-record` and `wd-kiwi-record`)
include a **stale wav reaper** that runs as part of their main loop:

**Rule**: After writing each new wav file, the recorder checks for wav files in
the band's spool directory that are older than the maximum useful age for any
configured decode mode.  Any wav file older than this threshold is deleted.

**Maximum useful age** is derived from the longest decode mode configured for
that band, plus a safety margin:

```
max_age = max(duration_minutes(mode) for mode in band.modes) + 2 minutes
```

For example, if a band has `modes = W2 F2 F5`, the longest mode is F5 (5 min).
The max useful age is 5 + 2 = 7 minutes.  Any wav file older than 7 minutes is
deleted by the recorder, because it could never be part of a valid decode window.

**Implementation**: The check is a simple `find` in bash:

```bash
find "${WD_SPOOL_BAND_DIR}" -name '*.wav' -mmin +${MAX_AGE_MIN} -delete
```

This runs in the recorder's main loop (not in a separate timer), so it is
always active whenever the recorder is producing files.  The cost is negligible.

**Effect**: If the decoder stops, wav files accumulate for at most `max_age`
minutes before the recorder starts deleting them.  The tmpfs never overflows.
When the decoder is restarted, it picks up from the current window — there is
no attempt to process historical files.

### 5.1 Logging

Each daemon writes its own log file via `wd-logger`, following the current convention:

- Log files live in `/var/log/wsprdaemon/`, one per daemon instance.
- `wd-logger` caps each file at 1 MB by default.  When a file hits the limit,
  `wd-logger` truncates it by keeping the first 25% of lines and discarding the rest,
  preventing unbounded growth.
- Services also log to journald via `StandardOutput=journal` so that
  `journalctl -u wd-decode@KA9Q_0-40` works as expected for interactive debugging.

### 5.2 File-Watch Mechanisms

| Source | Watch method | Rationale |
|--------|-------------|-----------|
| KA9Q wav files (`wd_record` output) | `inotifywait` | `wd_record` writes and closes each wav file cleanly; `inotifywait -e close_write` reliably detects completed files. |
| Kiwi wav files (`kiwi_recorder.py` output) | Polling (sleep + check) | `kiwi_recorder.py` opens and closes files continuously during the 1-minute write cycle, making `inotifywait` unreliable. Polling remains the correct approach. |
| Spot files (decoding→posting) | `inotifywait` | Spot files are written atomically. |
| Upload queue files (posting→upload) | `inotifywait` | Same — atomic writes. |

### What changes

| Current | v4 |
|---------|-----|
| `.pid` files everywhere | systemd manages all lifecycle |
| Parent polls `ps` for child liveness | systemd `BindsTo=` / `Restart=always` |
| `check_for_zombies()` | Gone — systemd's `MainPID` tracking handles this |
| `wd_kill` / `wd_kill_and_wait_for_death` | `systemctl stop` with `TimeoutStopSec` |
| `wd_sleep` polling loops | `inotifywait` for KA9Q wav + spot files; polling for Kiwi wav |
| `source ${WSPRDAEMON_CONFIG_FILE}` mid-loop | `EnvironmentFile=` + `systemctl reload` |
| `RUNNING_JOBS_FILE` / `EXPECTED_JOBS_FILE` | systemd is the source of truth |
| Bash-array config (RECEIVER_LIST[], WSPR_SCHEDULE[]) | INI config file |
| `/dev/shm/wsprdaemon/recording.d/` | `/var/spool/wsprdaemon/recording/` (tmpfs-mountable) |
| Per-daemon `.pid` and ad-hoc log files | `wd-logger` managed files in `/var/log/wsprdaemon/` |
| Static channel sections in `radiod@.conf` | Dynamic channels via `ka9q-python` at runtime |
| Dual config (wsprdaemon.conf + radiod@.conf channels) | `wsprdaemon.conf` is single source of truth |
| NTP-only recording start alignment | TimeSync PPS → WWV calibration → system clock priority (§3.2) |
| No protection against tmpfs overflow | Stale wav reaper in recorder (section 5.4) |
| No dependency version tracking | Pinned commit hashes in `components.ini` (section 10) |
| No formal versioning scheme | `MAJOR.MINOR-COMMIT` versioning with `VERSION` file (section 11) |
| Manual external service installation | Config-driven: services derived from receiver config (§2.1.1) |
| No hardware driver management | Auto-install correct driver for SDR type (§2.1.2) |
| No USB device discovery | `wd-ctl list-devices` scans bus for SDR serial numbers (§2.1.3) |

### What stays the same

- Wav files as the recording→decoding interface
- `*_spots.txt` files in `posting/` subdirs as the decoding→posting interface
- Spot files in the uploads tree as the posting→upload interface

---

## 6. Schedule Transitions and Receiver Types

### 6.1 KA9Q Receivers: No Schedule Transitions

KA9Q recording services (`wd-ka9q-record@`) run continuously and unconditionally.
A single `wd_record` process listens to one multicast stream and produces wav files
for all bands in that stream.  There is no reason to dynamically start or stop it.

**If a schedule entry references a KA9Q receiver for start/stop, `wd-ctl apply`
treats this as a configuration error**, logs an alert, and skips the entry.

The `wd-decode@` instances downstream of a KA9Q recorder also run continuously —
they simply process whatever wav files appear.

### 6.2 Kiwi Receivers: Schedule Transitions Required

Kiwi receivers have a limited number of receive channels.  Schedule transitions
(e.g., switching bands at sunrise/sunset) require stopping one `kiwi_recorder.py`
instance and starting another.

The orchestrator handles this by:

1. Identifying the Kiwi recording service(s) to stop.
2. Sending `systemctl stop wd-kiwi-record@KIWI_0-OLD_BAND`.
   Ideally this results in a graceful shutdown of `kiwi_recorder.py`
   (SIGTERM → the process finishes writing, closes the wav file, and exits).
3. Once the old service has stopped, starting the replacement:
   `systemctl start wd-kiwi-record@KIWI_0-NEW_BAND`.

**Design note**: This is an argument for keeping `kiwi_recorder.py` in
one-band-per-instance mode rather than multi-band mode.  With one band per
process, a schedule transition can stop and replace a single channel without
disturbing the others.  If `kiwi_recorder.py` were handling multiple bands in
one process, stopping it for a schedule change would take down all bands.

### 6.3 `wd-ka9q-record` (KA9Q) Lifecycle

`wd-ka9q-record` is never restarted by the scheduler.  Once started, it runs
indefinitely unless there is an error or the system is shut down.  If the
process exits unexpectedly, systemd's `Restart=always` brings it back.

**Dynamic channel management**: On startup, `wd-ka9q-record` uses `ka9q-python`
(`RadiodControl.create_channel()`) to create all WSPR/FST4W channels on radiod.
On clean shutdown (`SIGTERM`), it calls `RadiodControl.remove_channel()` for each
channel, setting their frequency to 0 so radiod's poller garbage-collects them.
If the recorder crashes without cleanup, `ka9q-python`'s `ChannelMonitor` (or a
subsequent restart) handles re-creation.  Stale channels with frequency=0 are
cleaned up automatically by radiod's internal poller.

**Stale wav file reaper**: As described in section 5.4, the recorder deletes wav
files older than the maximum useful decode age, preventing tmpfs overflow if the
downstream decoder is not running.

---

## 7. Installation Layout (FHS-Compliant)

```
/usr/local/sbin/                              # Daemon executables and wd-ctl
├── wd-ctl                                    # Orchestrator / control command
├── wd-kiwi-record                            # Kiwi recording wrapper
├── wd-ka9q-record                            # KA9Q recording wrapper (uses ka9q-python)
├── wd-decode                                 # Decoding wrapper
├── wd-post                                   # Posting wrapper
├── wd-upload-wsprnet                         # Wsprnet uploader
├── wd-upload-wsprdaemon                      # Wsprdaemon.org uploader
└── wd-upload-grape                           # GRAPE uploader

/etc/wsprdaemon/                              # Configuration
├── wsprdaemon.conf                           # Main config (INI format)
├── components.ini                            # Pinned external dependency commits (see §10)
└── env/                                      # Generated per-service env files
    ├── wd-hftime@KA9Q_0.env
    ├── wd-kiwi-record@KIWI_0-80.env
    ├── wd-ka9q-record@KA9Q_0.env
    ├── wd-decode@KA9Q_0-40.env
    └── ...

/etc/systemd/system/                          # Systemd unit files
├── wsprdaemon.service
├── wsprdaemon.timer
├── wsprdaemon.target
├── wd-hftime@.service
├── wd-kiwi-record@.service
├── wd-ka9q-record@.service
├── wd-decode@.service
├── wd-post@.service
├── wd-upload-wsprnet.service
├── wd-upload-wsprdaemon.service
├── wd-upload-grape.service
└── wd-decode@KA9Q_0-40.service.d/           # Generated drop-in dirs
    └── recorder.conf

/var/spool/wsprdaemon/                        # Spool data (wav files, spot files, uploads)
├── recording/
└── uploads/

/var/log/wsprdaemon/                          # Per-daemon log files (managed by wd-logger)

/run/wsprdaemon/                              # Runtime state (tmpfs)
├── KA9Q_0/
│   └── hftime.json                           # Time calibration from wd-hftime

/opt/wsprdaemon/                              # Installer and supporting files
├── install.sh                                # Installer script
├── lib/                                      # Shared bash libraries (wd-logger, etc.)
├── share/                                    # Data files, templates, etc.
└── python/                                   # Python virtual environment
    ├── bin/python3                            # venv interpreter
    └── lib/python3.x/site-packages/
        ├── ka9q/                              # ka9q-python library
        └── hf_timestd/                        # hf-timestd library
```

**Key principles**:
- Executables that are invoked directly (daemons, `wd-ctl`) go in `/usr/local/sbin/`.
  They are *not* executed from the source/install directory.
- The installer lives in `/opt/wsprdaemon/` and is responsible for copying executables,
  unit files, and config templates into their FHS locations.
- Shared libraries and helper functions (like `wd-logger`) live in `/opt/wsprdaemon/lib/`
  and are sourced by the executables at runtime.
- Unit files go in `/etc/systemd/system/` (the standard location for admin-installed units).
- Python dependencies (`ka9q-python`, `hf-timestd`) are installed into an isolated
  virtual environment at `/opt/wsprdaemon/python/`.  The installer creates this venv
  and runs `pip install ka9q-python hf-timestd` into it.  Bash wrapper scripts invoke
  the venv's interpreter directly (e.g., `/opt/wsprdaemon/python/bin/python3 -m ...`)
  so there is no dependency on the system Python path.

---

## 8. Migration Strategy

### Phase 1: Extract recording services (lowest risk)

1. Create `/etc/wsprdaemon/components.ini` with pinned commit hashes for all
   external dependencies.  Verify each component can be cloned and checked out
   at the pinned commit.
2. Install Python dependencies: create `/opt/wsprdaemon/python/` venv, install
   `ka9q-python` and `hf-timestd` at their pinned commits via pip.
3. Create `wd-hftime` unit file and wrapper — verify it creates a WWV channel on
   radiod dynamically and publishes a calibration file to `/run/wsprdaemon/`.
4. Create `wd-ka9q-record` wrapper that uses `ka9q-python` to create channels
   dynamically (replacing static `[channels]` in `radiod@.conf`) and records RTP
   streams to wav files in the spool directory with hftime-calibrated start times.
5. Create `wd-kiwi-record` wrapper script that runs `kiwirecorder_manager_daemon()` logic.
6. Create template unit files for all three services.
7. Test: start recording services manually, verify wav files appear in the expected dirs
   and that the stale wav reaper deletes old files when the decoder is not running.
8. Strip `[WSPR]`, `[FT4]`, `[FT8]`, `[WWV]` channel sections from `radiod@.conf` —
   verify radiod starts cleanly with only `[global]` + `[hardware]`.
9. Implement `wd-ctl apply` component version verification — confirm it reads
   `components.ini` and logs warnings for mismatched commits.
10. The rest of wsprdaemon continues running unchanged — it just skips
   `spawn_wav_recording_daemon()` if it detects the systemd service is already running

### Phase 2: Extract decoding services

1. Create `wd-decode` wrapper that runs `decoding_daemon()` logic
2. Handle the config that `decoding_daemon()` currently reads from the environment
   (frequency adjustments, noise levels, ADC overload tracking)
3. Test: verify `*_spots.txt` files appear correctly

### Phase 3: Extract posting and upload services

1. Create `wd-post` wrapper for `posting_daemon()`
2. Create `wd-upload-wsprnet` and `wd-upload-wsprdaemon` wrappers
3. The MERG'd receiver logic needs careful handling — the symlink dance
   currently in `spawn_posting_daemon()` moves into the orchestrator

### Phase 4: Build the orchestrator

1. `wd-ctl apply` replaces `update_running_jobs_to_match_expected_jobs()`
2. `wd-ctl migrate-config` converts old bash-array config to INI format
3. `wsprdaemon.timer` replaces the watchdog loop
4. `wd-ctl status` replaces `show_running_jobs()`
5. The old `-A` / `-Z` flags become `systemctl start/stop wsprdaemon`

### Phase 5: Cleanup

1. Remove PID file management, zombie checking, wd_kill infrastructure
2. Remove spawn_* functions
3. The remaining code is config parsing, schedule evaluation, and systemd orchestration

---

## 9. Resolved Design Decisions

1. **KA9Q recorder dependency fan-out** (was Q1): Use `BindsTo=`.  If the KA9Q recorder
   fails, all downstream decoders should be restarted.  A recorder failure means the
   wav file stream has stopped; decoders watching stale files would produce no useful
   output anyway.  `BindsTo=` ensures they are stopped when the recorder stops and
   restarted when it comes back (via `Restart=always` on the recorder).

2. **tmpfs mount strategy** (was Q2): The installer auto-calculates the required tmpfs
   size from the configuration (see section 5.3) and writes the fstab entry automatically.
   `wd-ctl apply` validates at startup that sufficient space is available.

3. **Config migration tool** (was Q3): Yes — `wd-ctl migrate-config` reads the old
   bash-array `wsprdaemon.conf` (RECEIVER_LIST[], WSPR_SCHEDULE[]) and writes a new
   INI-format config.  This is a one-time migration aid.

4. **Kiwi shutdown and truncated wav files** (was Q4): `kiwi_recorder.py` truncates
   the wav file mid-write when it receives SIGTERM — there is no graceful finish.
   The `wd-kiwi-record` wrapper should delete the truncated (incomplete) wav file
   for the current band after `kiwi_recorder.py` exits.  The decoder is never invoked
   on it because a valid decode window (2, 5, 15, or 30 minutes) cannot be assembled
   from a set of files where the last one is truncated.  No special partial-file
   handling is needed in the decoder.

5. **Decode mode → decoder binary mapping** (was Q5): The mode prefix determines
   the decoder binary — this is implicit and does not need to be specified in config:
   - `W` prefix (e.g., W2) → run `wsprd`.  The recording pipeline uses `sox` to
     concatenate the retained 1-minute floating-point wav files into a single
     multi-minute file and convert to 16-bit integer PCM, because `wsprd` only
     accepts 16-bit input.
   - `F` prefix (e.g., F2, F5, F15, F30) → run `jt9` with the appropriate FST4W
     duration argument.
   The number after the prefix is the decode window in minutes, which determines
   how many 1-minute wav files must be retained and when the decoder is triggered
   (e.g., W2 triggers after every even-odd minute pair; F5 triggers after 5
   consecutive 1-minute files are available).

6. **Dynamic channel creation via ka9q-python** (v0.5): The KA9Q recording service
   no longer depends on pre-configured `[channels]` sections in `radiod@.conf`.
   Instead, `wd-ka9q-record` uses the `ka9q-python` library (`RadiodControl`) to
   dynamically create and remove radiod channels at runtime.  This eliminates the
   requirement to keep `radiod@.conf` and `wsprdaemon.conf` in sync — wsprdaemon's
   INI config is now the single source of truth for which frequencies are recorded.
   The `radiod@.conf` file only needs `[global]` (with a default `mode` set to
   enable dynamic channel creation) and the `[hardware]` section.  Channels are
   created on recorder start and removed (frequency set to 0) on clean shutdown.
   Source: `ka9q-python` v3.4+ (https://github.com/mijahauan/ka9q-python, MIT license).

7. **HF time calibration via hf-timestd** (v0.5, revised v0.9): Each KA9Q receiver
   instance runs a `wd-hftime@` service that uses `hf-timestd` to listen to
   WWV/WWVH second-tick tones (received via a dynamically created AM channel on
   radiod) and compute the precise offset between the system clock and actual
   RF-received wall-clock time.  This offset is published to a JSON calibration
   file in `/run/wsprdaemon/` that the recording daemon reads to align wav file
   start times to sub-millisecond accuracy.  The service is **optional but enabled
   by default**.  If Turn Island Systems TimeSync hardware is detected, the BPSK
   PPS signal takes priority over WWV (~1 μs accuracy vs. sub-ms).  If neither
   TimeSync nor WWV is available, the system clock is used as a fallback.  See
   §3.2 for the full time source priority hierarchy.
   Source: `hf-timestd` (https://github.com/mijahauan/hf-timestd).

8. **Stale wav file protection** (v0.5): Recording daemons (`wd-ka9q-record` and
   `wd-kiwi-record`) include a stale wav reaper that deletes wav files older than
   the maximum useful decode age plus a 2-minute safety margin.  This prevents
   tmpfs overflow if the downstream decoder is not running.  See section 5.4 for
   the full specification.  The reaper runs in the recorder's main loop with
   negligible overhead.

9. **External dependency version pinning** (v0.6): All external component projects
   that wsprdaemon depends on are pinned to specific commit hashes in
   `/etc/wsprdaemon/components.ini`.  The installer checks out each component at
   the pinned commit, and `wd-ctl apply` verifies at startup that installed
   versions match.  This ensures a well-tested, reproducible set of dependencies
   across all installations.  See section 10 for the full specification.

10. **wsprdaemon versioning** (v0.6, revised v0.10): wsprdaemon uses a `MAJOR.MINOR-COMMIT`
    version string (e.g., `4.0-4837`).  MAJOR and MINOR are stored in a `VERSION` file
    and manually incremented for architecture changes and functional changes respectively.
    COMMIT is the monotonically increasing `git rev-list --count HEAD` from the v4 base.
    See section 11 for details.

11. **Config-driven external service installation** (v0.9): The set of external
    services installed by wsprdaemon is derived from the receiver and schedule
    configuration — not manually specified.  Receiver names, hardware types,
    and enabled features in `wsprdaemon.conf` determine which repos are cloned,
    built, and managed.  Components not needed by the current config are skipped.
    See §2.1.1 for the derivation logic.

12. **Full external service catalog** (v0.9): The external services taxonomy
    (§2.1) now covers all known upstream projects: ka9q-radio, ft8_lib,
    ftlib-pskreporter, ka9q-web (+ onion), dumphfdl, and hardware driver
    libraries (libfobos, SDRplay API).  All git-hosted components are tracked
    in `components.ini`; the SDRplay API is handled separately as closed-source.

13. **Turn Island Systems TimeSync support** (v0.9): When TimeSync hardware
    is present, its 82 MHz BPSK-modulated carrier (aliased to ~42 MHz in the
    SDR bandwidth) provides PPS-level timing (~1 μs) that takes priority over
    WWV calibration.  Multiple SDR devices on the same host each independently
    detect the TimeSync signal, synchronizing their recordings to ~1 μs.  See
    §3.2.1–§3.2.2 for the priority hierarchy and detection procedure.

14. **USB SDR serial number requirement** (v0.9): Each locally attached SDR
    device must have its serial number specified in `wsprdaemon.conf` to
    disambiguate multiple devices of the same type on the USB bus.  `wd-ctl
    list-devices` provides a discovery mechanism for operators.  See §2.1.3.

15. **Per-component commit override** (v0.9): Individual components in
    `components.ini` can be overridden to `HEAD` (latest commit on default
    branch) for developer testing.  The default remains pinned SHAs for
    reproducibility.  See §10.1.

16. **Posting daemon spot merging — best-SNR union** (v0.10): The posting daemon
    collects spots from all receiver sources (any mixture of KA9Q, Kiwi, or
    other SDR types) for a given band and time window, then selects the best
    SNR per transmitter to produce a merged spot set.  SNR values are typically
    negative (e.g., -15 dB beats -20 dB).  See §3.5.1.

17. **Dual upload path** (v0.10): Merged best-SNR spots are uploaded to
    wsprnet.org (identified by reporter callsign + grid).  Unmerged per-receiver
    spot sets are uploaded in parallel to wsprdaemon.org (identified by reporter
    ID + receiver name), preserving full per-receiver detail for analysis.
    See §3.5.2.

18. **User interface roadmap** (v0.10): Initial v4 interaction is direct config
    file editing and `wd-ctl` commands.  A character-mode (TUI) interface for
    configuration and monitoring is planned after the core pipeline is stable.
    A graphical interface is a long-term goal.  See section 12.

---

## 10. External Dependency Version Pinning

wsprdaemon depends on several external projects that are developed independently.
To ensure reproducible, well-tested deployments, every external dependency is
pinned to a specific git commit hash in a configuration file.

### 10.1 The Components File: `/etc/wsprdaemon/components.ini`

This INI-format file lists each external dependency with its source URL and the
full 40-character commit hash that has been tested with the current wsprdaemon
release.  The installer ships a default version of this file; operators may
override individual entries if they need to track a different branch or commit
(e.g., for testing), but the defaults represent the tested baseline.

```ini
# /etc/wsprdaemon/components.ini
#
# External dependency version pins for wsprdaemon.
# Each section names a component project.  The 'url' is the git clone URL.
# The 'commit' is the full 40-character SHA that wsprdaemon has been tested with.
#
# The installer checks out each component at the pinned commit.
# wd-ctl apply verifies installed versions match at startup.
#
# Per-component override: set commit = HEAD to track the latest commit on
# the default branch.  This is useful for developers testing unreleased
# changes but is NOT recommended for production deployments.
#
# WARNING: Changing a commit hash to an untested version may cause failures.
# Only modify these values if you understand the implications.

[ka9q-radio]
url    = https://github.com/ka9q/ka9q-radio.git
commit = xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

[ka9q-python]
url    = https://github.com/mijahauan/ka9q-python.git
commit = xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

[hf-timestd]
url    = https://github.com/mijahauan/hf-timestd.git
commit = xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

[ft8_lib]
# FT4/FT8 decoder service.
url    = https://github.com/ka9q/ft8_lib.git
commit = xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

[ftlib-pskreporter]
# PSK Reporter uploader for FT4/FT8 spots.
url    = https://github.com/pjsg/ftlib-pskreporter.git
commit = xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

[ka9q-web]
# KA9Q web monitoring UI.
url    = https://github.com/wa2n-code/ka9q-web.git
commit = xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

[onion]
# libonion HTTP library — build dependency for ka9q-web.
url    = https://github.com/davidmoreno/onion.git
commit = xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

[dumphfdl]
# HFDL decoder.
url    = https://github.com/ka9q/dumphfdl.git
commit = xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

[libfobos]
# Fobos SDR driver library.  Only installed if a Fobos device is configured.
url    = https://github.com/ka9q/libfobos.git
commit = xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

[wsprd]
# wsprd is built from the WSJT-X source tree.
url    = https://sourceforge.net/p/wsjt/wsjtx/ci/master/tree/
commit = xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

[jt9]
# jt9 is built from the same WSJT-X source tree as wsprd.
url    = https://sourceforge.net/p/wsjt/wsjtx/ci/master/tree/
commit = xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

[kiwi_recorder]
# kiwi_recorder.py — used as-is, never modified by wsprdaemon.
url    = https://github.com/jks-prv/kiwiclient.git
commit = xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

**Notes**:
- Commit hashes shown as `xxxx...` are placeholders.  The actual file shipped
  with each wsprdaemon release contains real, tested commit hashes.
- `wsprd` and `jt9` share the same WSJT-X source tree and will normally have
  the same commit hash, but they are listed separately so they can be pinned
  independently if needed (e.g., if wsprdaemon patches one but not the other).
- Setting `commit = HEAD` for any component causes the installer to check out
  the latest commit on the default branch instead of a specific SHA.  This is
  a developer convenience for testing; it sacrifices reproducibility.
- **SDRplay API** is not listed here because it is closed-source and not
  available as a git repository.  It is downloaded separately by the installer
  from the SDRplay website when an SDRplay device is configured.
- Not all components are installed on every system.  The installer checks the
  receiver configuration and only installs components that are needed (e.g.,
  `libfobos` is skipped if no Fobos SDR is configured; `dumphfdl` is skipped
  if HFDL is not enabled).

### 10.2 Installer Behavior

The wsprdaemon installer (`/opt/wsprdaemon/install.sh`) reads `components.ini`
and the receiver configuration in `wsprdaemon.conf`, then performs the following
for each required component:

1. **Determine if this component is needed**: The installer checks
   `wsprdaemon.conf` to see if the component is required by the current
   configuration (e.g., `libfobos` is only needed if a Fobos SDR is
   configured; `dumphfdl` only if HFDL is enabled).  Components not required
   by the current configuration are skipped.
2. If the component source directory does not exist, clone the repository.
3. Fetch the latest refs from the remote.
4. **Resolve the commit**:
   - If `commit` is a 40-character SHA, check out that exact commit.
   - If `commit = HEAD`, check out the latest commit on the default branch.
     A warning is logged: this sacrifices reproducibility and is intended
     for developer testing only.
5. Build/install the component as appropriate (e.g., `make` for `ka9q-radio`,
   `pip install` for Python packages, `cmake && make` for WSJT-X binaries).

When using a pinned commit (the default), running the installer at two
different times produces identical results.  When `HEAD` is specified, the
installed version depends on what was pushed to the remote at the time of
installation.

### 10.3 Startup Verification

When `wd-ctl apply` runs (either at boot via `wsprdaemon.service` or on the
2-minute timer), it reads `components.ini` and verifies each installed component:

```
For each [component] in components.ini:
    installed_commit = (cd /opt/wsprdaemon/src/<component> && git rev-parse HEAD)
    expected_commit  = components.ini[component].commit
    if installed_commit != expected_commit:
        log WARNING: "<component> version mismatch:
            installed=<installed_commit>
            expected=<expected_commit>"
```

**The verification is warn-only** — wsprdaemon does not refuse to start if a
component is at the wrong commit.  This is intentional: an operator may be
testing a newer version of a component, or a component may have been updated
outside of wsprdaemon's installer.  The warning ensures the mismatch is logged
and visible in `wd-ctl status` output.

### 10.4 Updating Components

To update a component to a new tested commit:

1. Edit `/etc/wsprdaemon/components.ini` — change the `commit` value.
2. Run `wd-ctl update-components` — this reads the file and checks out
   each component to its pinned commit, rebuilding as needed.

When a new wsprdaemon release ships, it includes an updated `components.ini`
with commit hashes that have been tested together.  The migration path is:

```bash
cd /opt/wsprdaemon && git pull          # update wsprdaemon itself
cp share/components.ini /etc/wsprdaemon/components.ini   # update pins
wd-ctl update-components                # checkout + rebuild
sudo systemctl restart wsprdaemon       # restart with verified versions
```

---

## 11. wsprdaemon Versioning

wsprdaemon uses a simple three-part version string stored in a `VERSION` file
at the root of the repository:

```
MAJOR.MINOR-COMMIT
```

| Field | Meaning | Example |
|-------|---------|---------|
| MAJOR | Major architecture generation (currently `4` for the v4 rewrite) | `4` |
| MINOR | Incremented for functional changes (new features, behavior changes) | `0` |
| COMMIT | Monotonically increasing commit count from the v4 base, computed by `git rev-list --count HEAD` | `4837` |

The default version at the start of v4 development is `4.0-1`.  A typical
version string looks like `4.0-4837`.

### 11.1 The VERSION File

The version string lives in `/opt/wsprdaemon/VERSION` (and in the repo root).
The file contains a single line with the `MAJOR.MINOR` portion:

```
4.0
```

The COMMIT index is always computed at runtime from git, never stored in the
file.  The full version string is assembled by:

```bash
echo "$(cat VERSION)-$(git rev-list --count HEAD)"
```

This means MAJOR and MINOR are manually edited (when a functional change or
architecture change warrants it), while COMMIT is always automatic and
monotonically increasing.

### 11.2 Displaying the Version

`wd-ctl --version` displays the full version string and the short SHA for
cross-referencing with GitHub:

```
wsprdaemon 4.0-4837 (a1b2c3d4)
```

The version string is also included in:
- Log messages (the `wd-logger` prefix includes `wd/4.0-4837`)
- Spot reports uploaded to wsprdaemon.org (as a metadata field)
- `wd-ctl status` output

### 11.3 Checking Out a Specific Version

If a user is told "use version 4.0-4837", they can check it out with:

```bash
cd /opt/wsprdaemon
git checkout $(git rev-list --reverse HEAD | sed -n '4837p')
```

Or equivalently, using the `wd-ctl` helper:

```bash
wd-ctl checkout-version 4837
```

This resolves the commit index to the corresponding SHA and runs `git checkout`.

**Important**: The commit index is only meaningful on a single branch (typically
`main`).  If the repository has been rebased or the user is on a different branch,
the index-to-SHA mapping may differ.  `wd-ctl checkout-version` validates that
the current branch is `main` before proceeding and warns if it is not.

### 11.4 When to Bump MAJOR vs. MINOR

- **MAJOR** changes when the architecture fundamentally changes (e.g., the v3→v4
  rewrite from monolithic bash to systemd services).  This is rare.
- **MINOR** increments when a user-visible functional change is made — a new
  feature, a changed behavior, or a config format change that requires user
  action.  Bug fixes and refactors that don't change behavior do not bump MINOR.
- **COMMIT** never needs manual action — it advances automatically with every
  commit on `main`.

---

## 12. User Interface Roadmap

### 12.1 Phase 1: Direct Config File Editing (Current)

The initial v4 interface is direct editing of `/etc/wsprdaemon/wsprdaemon.conf`
and `/etc/wsprdaemon/components.ini` with a text editor.  Configuration changes
take effect when `wd-ctl apply` runs (either manually or on the 2-minute timer).
System monitoring is via `wd-ctl status` on the command line and `journalctl`
for log inspection.

This is the minimum viable interface and is sufficient for the initial v4 rollout.

### 12.2 Phase 2: Character-Mode Interface (TUI)

A terminal-based user interface (curses/TUI) for configuration and monitoring.
This would provide:

- A live dashboard showing all active services, their systemd state, spot
  counts, and upload queue depths — replacing the need to run `wd-ctl status`
  repeatedly or watch journal output.
- An interactive configuration editor that validates changes before writing
  them to `wsprdaemon.conf` and triggering `wd-ctl apply`.
- Receiver and band status at a glance, including decode rates, SNR
  distributions, and error counts.

The TUI is planned but not scheduled.  It will be developed after the core
v4 pipeline is stable and tested.

### 12.3 Phase 3: Graphical Interface (Future)

A web-based or desktop graphical interface for configuration and monitoring.
Scope and technology are TBD — this is a long-term goal, not a near-term
commitment.  The TUI must come first to validate the information architecture
and interaction patterns before investing in a graphical frontend.
