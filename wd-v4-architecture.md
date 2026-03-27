# wsprdaemon v4 — Service-Oriented Architecture Specification (v0.8, 2026-03-27)

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
┌─────────────────────────────────────────────────────────────┐
│                    EXTERNAL SERVICES                        │
│  (wsprdaemon installs/configures/enables but doesn't own)   │
├─────────────────────────────────────────────────────────────┤
│  radiod@INSTANCE.service      — ka9q-radio SDR daemon      │
│  ft4-decode.service           — FT4 decoder                │
│  ft8-decode.service           — FT8 decoder                │
│  ka9q-web.service             — KA9Q web UI                │
│  kiwi-web@INSTANCE.service    — KiwiSDR web (existing unit)│
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│              PYTHON DEPENDENCIES                            │
│       (installed into /opt/wsprdaemon/python/)              │
├─────────────────────────────────────────────────────────────┤
│  ka9q-python (pip: ka9q-python)                             │
│    — Pure-Python radiod control API (dynamic channel        │
│      creation, discovery, RTP recording, GPS/RTP timing).   │
│    — Source: https://github.com/mijahauan/ka9q-python       │
│    — Used by: wd-ka9q-record, wd-hftime                    │
│                                                             │
│  hf-timestd (pip or source install)                         │
│    — HF time-standard service: listens to WWV/WWVH via     │
│      ka9q-python, detects second-tick tones to calibrate    │
│      wav recording start times to sub-millisecond accuracy. │
│    — Source: https://github.com/mijahauan/hf-timestd        │
│    — Used by: wd-hftime.service                             │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│              NEW FIRST-CLASS WD SERVICES                    │
│       (systemd template units, managed by wd-ctl)           │
├─────────────────────────────────────────────────────────────┤
│  TIME CALIBRATION LAYER (KA9Q systems only)                 │
│  └─ wd-hftime@INSTANCE.service       (one per radiod inst) │
│                                                             │
│  RECORDING LAYER                                            │
│  ├─ wd-kiwi-record@INSTANCE.service   (1:1, one per chan)  │
│  └─ wd-ka9q-record@INSTANCE.service   (1:N, one per mcast)│
│                                                             │
│  DECODING LAYER                                             │
│  └─ wd-decode@INSTANCE.service        (one per rx+band)    │
│                                                             │
│  POSTING LAYER                                              │
│  └─ wd-post@INSTANCE.service          (one per logical rx) │
│                                                             │
│  UPLOAD LAYER                                               │
│  ├─ wd-upload-wsprnet.service         (singleton)          │
│  ├─ wd-upload-wsprdaemon.service      (singleton)          │
│  └─ wd-upload-grape.service           (singleton, optional)│
│                                                             │
│  ORCHESTRATOR                                               │
│  ├─ wsprdaemon.service                (the new wd-ctl)     │
│  └─ wsprdaemon.timer                  (schedule evaluator) │
└─────────────────────────────────────────────────────────────┘
```

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
type = ka9q
ip   = wspr-pcm.local

[receiver:KA9Q_0:80]
; Decode modes determine how many 1-minute wav files are retained.
; W2 = WSPR-2 (2 min), F2 = FST4W-120 (2 min), F5 = FST4W-300 (5 min).
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

### 3.2 wd-hftime@ — HF Time Calibration (KA9Q systems only, mandatory)

This service runs the `hf-timestd` daemon for each KA9Q receiver instance.  It uses
`ka9q-python` to dynamically create a WWV (or WWVH) AM channel on radiod, listens for
the second-tick tones (5 ms bursts of 1200 Hz at the top of each second) and the minute
markers (800 ms tone at 1000 Hz), and computes the precise offset between the system
clock and actual RF-received wall-clock time.  The computed offset is written to a
calibration file that `wd-ka9q-record` reads to align wav recording start times to
sub-millisecond accuracy.

On any system with an RX-888 (or other wideband SDR), `wd-hftime` is mandatory and
started automatically by `wd-ctl apply` — it is not optional.  The WWV channel is
created dynamically via `ka9q-python` at service start and removed on stop; it does
not require any entry in `radiod@.conf`.

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
  "wwv_freq_hz": 10000000,
  "offset_ns": -1423,
  "uncertainty_ns": 250,
  "last_update": "2026-03-27T14:02:01.003Z"
}
```

The recorder reads `offset_ns` to shift its recording window start time so that the
wav file boundaries align with the true second edge as received at the antenna.

### 3.3 wd-ka9q-record@ — KA9Q Recording (1:N, dynamic channels)

This service replaces the old static-channel `wd_record` approach.  Instead of
depending on pre-configured `[channels]` sections in `radiod@.conf`, it uses
`ka9q-python` (`RadiodControl`) to dynamically create all needed WSPR/FST4W
channels on radiod at startup.  Channels are created based on the band list in
`wsprdaemon.conf` — making `wsprdaemon.conf` the single source of truth.

At startup the service:

1. Waits for `wd-hftime@INSTANCE` to publish its first calibration (via a
   `Wants=` on the hftime service and a brief poll of the calib file).
2. Reads the band/mode configuration from its environment file.
3. Uses `RadiodControl.create_channel()` to create one radiod channel per band
   (frequency, preset=usb, sample_rate=12000 for WSPR; preset=iq for WWV-IQ, etc.).
4. Uses `RTPRecorder` to receive the RTP streams and write 1-minute wav files into
   the spool directory, using the hftime calibration offset to align start times.
5. On `SIGTERM` (service stop), calls `RadiodControl.remove_channel()` for each
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

**Stale wav file protection** (see section 5.5).

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
└── (runtime state files — mostly replaced by systemd)

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

### 5.1 Logging

Each daemon writes its own log file via `wd-logger`, following the current convention:

- Log files live in `/var/log/wsprdaemon/`, one per daemon instance.
- `wd-logger` caps each file at 1 MB by default.  When a file hits the limit,
  `wd-logger` truncates it by keeping the first 25% of lines and discarding the rest,
  preventing unbounded growth.
- Services also log to journald via `StandardOutput=journal` so that
  `journalctl -u wd-decode@KA9Q_0-40` works as expected for interactive debugging.

### 5.2 Dynamic Verbosity (Signal-Based, No Restart)

Every `wd-*` daemon process traps `SIGUSR1` and `SIGUSR2` to adjust log verbosity
at runtime without restarting the service.  This is essential for debugging
production systems where hundreds of recording, decoding, and posting jobs may
be running — restarting a service to increase verbosity would lose the error
context that the operator is trying to capture.

**Signal convention**:

| Signal | Effect |
|--------|--------|
| `USR1` | Increment verbosity (e.g., `WARN` → `INFO` → `DEBUG` → `TRACE`) |
| `USR2` | Decrement verbosity (e.g., `TRACE` → `DEBUG` → `INFO` → `WARN`) |

Verbosity levels saturate at the boundaries — `USR1` at `TRACE` is a no-op,
`USR2` at `WARN` (the default) is a no-op.

**Implementation in each daemon** (bash):

```bash
declare -i WD_VERBOSITY=0    # 0=WARN (default), 1=INFO, 2=DEBUG, 3=TRACE

trap 'wd_verbosity_up'   USR1
trap 'wd_verbosity_down' USR2

wd_verbosity_up() {
    (( WD_VERBOSITY < 3 )) && (( WD_VERBOSITY++ ))
    wd_logger 0 "Verbosity increased to level ${WD_VERBOSITY}"
}

wd_verbosity_down() {
    (( WD_VERBOSITY > 0 )) && (( WD_VERBOSITY-- ))
    wd_logger 0 "Verbosity decreased to level ${WD_VERBOSITY}"
}
```

The `wd-logger` function checks `WD_VERBOSITY` against the message level and
suppresses messages above the current threshold.

**Finding the PID — no `.pid` files needed**: systemd tracks the main PID of
every service.  The PID is retrieved via `systemctl show`:

```bash
kill -USR1 $(systemctl show -p MainPID --value wd-decode@KA9Q_0-40.service)
```

**The `wd-ctl verbosity` helper** wraps this for convenience:

```bash
# Increment verbosity for one service
wd-ctl verbosity up wd-decode@KA9Q_0-40

# Decrement verbosity for one service
wd-ctl verbosity down wd-decode@KA9Q_0-40

# Increment verbosity for ALL wd-* services at once
wd-ctl verbosity up all

# Show current verbosity (queries each daemon via a status mechanism)
wd-ctl verbosity show
```

The `up` and `down` subcommands resolve the service name to a `MainPID` via
`systemctl show` and send the appropriate signal.  The `all` target iterates
over every running `wd-*` service.

**Why not `.pid` files?**  In the v3 architecture, `.pid` files were necessary
because wsprdaemon managed its own process tree.  In v4, systemd tracks all
PIDs authoritatively via `MainPID`.  Using `systemctl show` to retrieve the PID
eliminates the failure modes of stale `.pid` files (process died without cleanup,
PID reuse, etc.) while preserving the exact same USR1/USR2 trap mechanism in the
daemons themselves.

### 5.3 File-Watch Mechanisms

| Source | Watch method | Rationale |
|--------|-------------|-----------|
| KA9Q wav files (`wd_record` output) | `inotifywait` | `wd_record` writes and closes each wav file cleanly; `inotifywait -e close_write` reliably detects completed files. |
| Kiwi wav files (`kiwi_recorder.py` output) | Polling (sleep + check) | `kiwi_recorder.py` opens and closes files continuously during the 1-minute write cycle, making `inotifywait` unreliable. Polling remains the correct approach. |
| Spot files (decoding→posting) | `inotifywait` | Spot files are written atomically. |
| Upload queue files (posting→upload) | `inotifywait` | Same — atomic writes. |

### 5.4 tmpfs Sizing (Auto-Calculated by Installer)

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

### 5.5 Stale Wav File Cleanup (tmpfs Overflow Protection)

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

### 5.6 What Changes

| Current | v4 |
|---------|-----|
| `.pid` files everywhere | systemd `MainPID` tracking; `wd-ctl verbosity` for USR1/USR2 |
| Parent polls `ps` for child liveness | systemd `BindsTo=` / `Restart=always` |
| `check_for_zombies()` | Gone — systemd's `MainPID` tracking handles this |
| `wd_kill` / `wd_kill_and_wait_for_death` | `systemctl stop` with `TimeoutStopSec` |
| `wd_sleep` polling loops | `inotifywait` for KA9Q wav + spot files; polling for Kiwi wav |
| `source ${WSPRDAEMON_CONFIG_FILE}` mid-loop | `EnvironmentFile=` + `systemctl reload` |
| `RUNNING_JOBS_FILE` / `EXPECTED_JOBS_FILE` | systemd is the source of truth |
| Bash-array config (RECEIVER_LIST[], WSPR_SCHEDULE[]) | INI config file |
| `/dev/shm/wsprdaemon/recording.d/` | `/var/spool/wsprdaemon/recording/` (tmpfs-mountable) |
| `kill -USR1 $(cat pidfile)` for verbosity | `wd-ctl verbosity up SERVICE` (uses systemd MainPID) |
| Per-daemon `.pid` and ad-hoc log files | `wd-logger` managed files in `/var/log/wsprdaemon/` |
| Static channel sections in `radiod@.conf` | Dynamic channels via `ka9q-python` at runtime |
| Dual config (wsprdaemon.conf + radiod@.conf channels) | `wsprdaemon.conf` is single source of truth |
| NTP-only recording start alignment | `hf-timestd` WWV calibration for sub-ms accuracy |
| No protection against tmpfs overflow | Stale wav reaper in recorder (section 5.5) |
| No dependency version tracking | Pinned commit hashes in `components.ini` (section 10) |
| No formal versioning scheme | `MAJOR.MINOR-COMMITINDEX` version scheme (section 11) |

### 5.7 What Stays the Same

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

**Stale wav file reaper**: As described in section 5.5, the recorder deletes wav
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
├── VERSION                                   # MAJOR.MINOR version (e.g., "4.0")
├── lib/                                      # Shared bash libraries (wd-logger, etc.)
├── share/                                    # Data files, templates, etc.
│   └── components.ini                        # Default component pins (copied to /etc/)
├── src/                                      # Cloned component source trees
│   ├── ka9q-radio/
│   ├── ka9q-python/
│   ├── hf-timestd/
│   ├── wsjtx/                                # wsprd and jt9 source
│   └── kiwiclient/                           # kiwi_recorder.py source
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
   size from the configuration (see section 5.4) and writes the fstab entry automatically.
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
   - `F` prefix (e.g., F2, F5, F15) → run `jt9` with the appropriate FST4W
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

7. **HF time calibration via hf-timestd** (v0.5): Every KA9Q receiver instance
   runs a mandatory `wd-hftime@` service that uses `hf-timestd` to listen to
   WWV/WWVH second-tick tones (received via a dynamically created AM channel on
   radiod) and compute the precise offset between the system clock and actual
   RF-received wall-clock time.  This offset is published to a JSON calibration
   file in `/run/wsprdaemon/` that the recording daemon reads to align wav file
   start times to sub-millisecond accuracy.  The service is mandatory (not
   optional) for any system with an RX-888 or other wideband SDR, because these
   systems always have WWV within their receive bandwidth.  The WWV channel is
   created and torn down dynamically via `ka9q-python` — no radiod config needed.
   Source: `hf-timestd` (https://github.com/mijahauan/hf-timestd).

8. **Stale wav file protection** (v0.5): Recording daemons (`wd-ka9q-record` and
   `wd-kiwi-record`) include a stale wav reaper that deletes wav files older than
   the maximum useful decode age plus a 2-minute safety margin.  This prevents
   tmpfs overflow if the downstream decoder is not running.  See section 5.5 for
   the full specification.  The reaper runs in the recorder's main loop with
   negligible overhead.

9. **External dependency version pinning** (v0.6, updated v0.7): All external
   component projects that wsprdaemon depends on are pinned to specific commit
   hashes in `/etc/wsprdaemon/components.ini`.  The installer checks out each
   component at the pinned commit, and `wd-ctl apply` verifies at startup that
   installed versions match.  If a component's commit field is empty or missing,
   the installer fetches HEAD and writes the resolved hash back into the file —
   enabling both reproducible production deploys and developer workflows where
   latest HEAD is desired.  See section 10 for the full specification.

10. **wsprdaemon version scheme** (v0.7): wsprdaemon uses a `MAJOR.MINOR-COMMITINDEX`
    version scheme (e.g., `4.0-4837`).  `MAJOR.MINOR` is explicitly managed in a
    `VERSION` file; `COMMITINDEX` is auto-computed from `git rev-list --count HEAD`.
    This replaces the previous four-field `3.3.2-N` scheme.  See section 11.

11. **Dynamic verbosity via signals** (v0.8): Every `wd-*` daemon traps `SIGUSR1`
    (increment verbosity) and `SIGUSR2` (decrement verbosity), preserving the v3
    mechanism for live debugging without restarting services.  PID lookup uses
    systemd's `MainPID` instead of `.pid` files, wrapped in `wd-ctl verbosity`
    for convenience.  See section 5.2.

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
- Additional components may be added to this file as the project evolves.

### 10.2 Installer Behavior

The wsprdaemon installer (`/opt/wsprdaemon/install.sh`) reads `components.ini`
and performs the following for each entry:

1. If the component source directory does not exist, clone the repository.
2. Fetch the latest refs from the remote.
3. Check out the pinned commit hash (`git checkout <commit>`).
4. Build/install the component as appropriate (e.g., `make` for `ka9q-radio`,
   `pip install` for Python packages, `cmake && make` for WSJT-X binaries).

**If a component's `commit` field is empty or the entire section is missing**,
the installer fetches HEAD of the component's default branch, installs it, and
writes the resolved commit hash back into `components.ini`:

```
For each [component] in components.ini:
    if commit is empty or missing:
        (cd /opt/wsprdaemon/src/<component> && git pull)
        resolved_hash = (cd /opt/wsprdaemon/src/<component> && git rev-parse HEAD)
        write resolved_hash back to components.ini[component].commit
        log INFO: "<component>: no pinned commit — installed HEAD (<resolved_hash>)"
```

This auto-populate behavior serves two purposes:

- **Fresh installs**: A first-time install with no `components.ini` (or an empty
  one) will fetch HEAD of every component and record what was installed.  The
  resulting file becomes the baseline for future verification.
- **Developer workflow**: A developer can `git pull` wsprdaemon, delete some or
  all `commit` values from `components.ini`, and run `wd-ctl update-components`
  to get the latest HEAD of those components.  The resolved hashes are written
  back, so the developer knows exactly what is installed and can commit the
  updated `components.ini` when satisfied.

When all `commit` fields are populated (the normal shipped state), the installer
uses only the explicit hashes — it never contacts the remote for those entries.
This ensures reproducible installs from a fully-pinned file.

### 10.3 Startup Verification

When `wd-ctl apply` runs (either at boot via `wsprdaemon.service` or on the
2-minute timer), it reads `components.ini` and verifies each installed component:

```
For each [component] in components.ini:
    installed_commit = (cd /opt/wsprdaemon/src/<component> && git rev-parse HEAD)
    expected_commit  = components.ini[component].commit
    if expected_commit is empty:
        log INFO: "<component>: no pinned commit (running HEAD)"
    elif installed_commit != expected_commit:
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

1. Edit `/etc/wsprdaemon/components.ini` — change the `commit` value
   (or clear it to get HEAD on next update).
2. Run `wd-ctl update-components` — this reads the file, checks out
   each component to its pinned commit (or fetches HEAD if empty),
   writes resolved hashes back for any empty entries, and rebuilds as needed.

When a new wsprdaemon release ships, it includes an updated `components.ini`
with commit hashes that have been tested together.  The migration path is:

```bash
cd /opt/wsprdaemon && git pull          # update wsprdaemon itself
cp share/components.ini /etc/wsprdaemon/components.ini   # update pins
wd-ctl update-components                # checkout + rebuild
sudo systemctl restart wsprdaemon       # restart with verified versions
```

---

## 11. wsprdaemon Version Scheme

wsprdaemon uses a three-field version scheme: **`MAJOR.MINOR-COMMITINDEX`**.

### 11.1 Version Fields

| Field | Meaning | Who sets it |
|-------|---------|-------------|
| `MAJOR` | Architecture generation.  Currently `4` (this document describes v4). | Changed when a fundamental redesign occurs. |
| `MINOR` | Feature milestone within a generation (e.g., `0`, `1`, `2`). | Explicitly incremented by the developer when a meaningful milestone is reached. |
| `COMMITINDEX` | Sequential commit count from `git rev-list --count HEAD`. | Auto-computed from git — never manually edited. |

**Examples**: `4.0-4837`, `4.1-4902`, `4.2-5011`.

This replaces the previous four-field `3.3.2-N` scheme.  Two explicitly managed
fields (`MAJOR.MINOR`) plus one auto-computed field (`COMMITINDEX`) is cleaner
and avoids the unused middle digits.

### 11.2 Where the MAJOR.MINOR Is Stored

The `MAJOR.MINOR` value is stored in a file at the root of the repository:

```
/opt/wsprdaemon/VERSION
```

Contents (plain text, single line):

```
4.0
```

The developer edits this file and commits it when bumping the minor version.
The commit index is never stored — it is always computed at runtime.

### 11.3 Computing the Full Version String

```bash
wd_version() {
    local version_file="/opt/wsprdaemon/VERSION"
    local major_minor
    major_minor=$(<"${version_file}")
    local commit_index
    commit_index=$(cd /opt/wsprdaemon && git rev-list --count HEAD)
    local short_sha
    short_sha=$(cd /opt/wsprdaemon && git rev-parse --short HEAD)
    echo "${major_minor}-${commit_index} (${short_sha})"
}
```

### 11.4 Displaying the Version

`wd-ctl --version` displays the full version string:

```
wsprdaemon 4.0-4837 (a1b2c3d4)
```

The version is also included in:
- Log messages (the `wd-logger` prefix includes `wd/4.0-4837`)
- Spot reports uploaded to wsprdaemon.org (as a metadata field)
- `wd-ctl status` output

### 11.5 Checking Out a Specific Commit Index

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
