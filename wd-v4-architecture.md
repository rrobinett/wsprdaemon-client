# wsprdaemon v4 — Service-Oriented Architecture Specification

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
         │               │               ├─ ka9q_recording_daemon() &   # runs pcmrecord (1→N bands)
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
- **File-based IPC**: Recording → Decoding via wav files in `/dev/shm/wsprdaemon/recording.d/`.
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
│              NEW FIRST-CLASS WD SERVICES                    │
│       (systemd template units, managed by wd-ctl)           │
├─────────────────────────────────────────────────────────────┤
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
| `wd-kiwi-record@` | `RECEIVER-BAND` | `wd-kiwi-record@KIWI_0-80.service` |
| `wd-ka9q-record@` | `RECEIVER` | `wd-ka9q-record@KA9Q_0.service` |
| `wd-decode@` | `RECEIVER-BAND` | `wd-decode@KA9Q_0-40.service` |
| `wd-post@` | `RECEIVER-BAND` | `wd-post@MERG_0-80.service` |

Notes:
- KA9Q recording is per-receiver (not per-band) because one `pcmrecord` process outputs
  wav files for all bands from a single multicast stream.
- Kiwi recording is per-receiver-per-band because each `kiwi_recorder.py` handles one channel.
- Decoding and posting are always per-receiver-per-band.

### 2.3 Environment / Config Passing

Each service instance gets its configuration from a generated environment file:

```
/etc/wsprdaemon/env.d/wd-decode@KA9Q_0-40.env
```

The orchestrator generates these from `wsprdaemon.conf`. Example contents:

```bash
# Auto-generated by wd-ctl from wsprdaemon.conf — do not edit
WD_RECEIVER_NAME=KA9Q_0
WD_RECEIVER_BAND=40
WD_RECEIVER_MODES=W2:F2:F5
WD_RECEIVER_IP=wspr-pcm.local
WD_RECEIVER_CALL=AI6VN
WD_RECEIVER_GRID=CM87tj
WD_RECEIVER_FREQ_KHZ=7040100
WD_RECORDING_DIR=/dev/shm/wsprdaemon/recording.d/KA9Q_0
WD_DECODING_DIR=/dev/shm/wsprdaemon/recording.d/KA9Q_0/40
WD_UPLOAD_DIR=/home/wsprdaemon/wsprdaemon/uploads/wsprnet/AI6VN_CM87tj/KA9Q_0/40
WD_ROOT_DIR=/home/wsprdaemon/wsprdaemon
WD_TMP_DIR=/dev/shm/wsprdaemon
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
EnvironmentFile=/etc/wsprdaemon/env.d/wd-kiwi-record@%i.env
ExecStart=/opt/wsprdaemon/libexec/wd-kiwi-record
WorkingDirectory=${WD_RECORDING_DIR}
RuntimeDirectory=wsprdaemon/recording.d/%i
Nice=-15

# Restart policy: always restart with backoff
Restart=always
RestartSec=10
StartLimitIntervalSec=300
StartLimitBurst=10

# Resource limits
MemoryMax=256M
CPUQuota=25%

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=wd-kiwi-record@%i

[Install]
WantedBy=wsprdaemon.target
```

### 3.2 wd-ka9q-record@ — KA9Q Recording (1:N)

```ini
# /etc/systemd/system/wd-ka9q-record@.service
[Unit]
Description=wsprdaemon KA9Q recorder for %i
After=network-online.target
Requires=radiod@%i.service
After=radiod@%i.service

[Service]
Type=simple
User=wsprdaemon
Group=radio
EnvironmentFile=/etc/wsprdaemon/env.d/wd-ka9q-record@%i.env
ExecStart=/opt/wsprdaemon/libexec/wd-ka9q-record
WorkingDirectory=${WD_RECORDING_DIR}
RuntimeDirectory=wsprdaemon/recording.d/%i
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

### 3.3 wd-decode@ — Decoding (per receiver+band)

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
EnvironmentFile=/etc/wsprdaemon/env.d/wd-decode@%i.env
ExecStart=/opt/wsprdaemon/libexec/wd-decode
WorkingDirectory=${WD_DECODING_DIR}

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

### 3.4 wd-post@ — Posting (per logical receiver+band)

```ini
# /etc/systemd/system/wd-post@.service
[Unit]
Description=wsprdaemon spot poster for %i

[Service]
Type=simple
User=wsprdaemon
Group=radio
EnvironmentFile=/etc/wsprdaemon/env.d/wd-post@%i.env
ExecStart=/opt/wsprdaemon/libexec/wd-post
WorkingDirectory=${WD_TMP_DIR}/posting.d/%i

Restart=always
RestartSec=5

StandardOutput=journal
StandardError=journal
SyslogIdentifier=wd-post@%i

[Install]
WantedBy=wsprdaemon.target
```

### 3.5 Upload Services (singletons)

```ini
# /etc/systemd/system/wd-upload-wsprnet.service
[Unit]
Description=wsprdaemon wsprnet.org uploader
After=network-online.target

[Service]
Type=simple
User=wsprdaemon
Group=radio
EnvironmentFile=/etc/wsprdaemon/env.d/wd-upload-wsprnet.env
ExecStart=/opt/wsprdaemon/libexec/wd-upload-wsprnet
WorkingDirectory=${WD_ROOT_DIR}/uploads/wsprnet

Restart=always
RestartSec=30

StandardOutput=journal
StandardError=journal
SyslogIdentifier=wd-upload-wsprnet

[Install]
WantedBy=wsprdaemon.target
```

### 3.6 The Orchestrator

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
ExecStart=/opt/wsprdaemon/bin/wd-ctl apply
ExecReload=/opt/wsprdaemon/bin/wd-ctl apply
ExecStop=/opt/wsprdaemon/bin/wd-ctl teardown

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
1. Source /etc/wsprdaemon/wsprdaemon.conf
2. Evaluate WSPR_SCHEDULE[] for current time-of-day
   (same sunrise/sunset logic as current update_hhmm_sched_file)
3. Compute DESIRED_SERVICES[] — the full set of service instances
   that should be running right now
4. Query systemd for RUNNING_SERVICES[] — what's actually running
5. Diff:
   a. Services in DESIRED but not RUNNING → generate env file, start
   b. Services in RUNNING but not DESIRED → stop, remove env file
   c. Services in both → verify env file matches config, reload if changed
6. For each new service:
   a. Generate /etc/wsprdaemon/env.d/SERVICE.env
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

```bash
WD_POST_SUPPLIER_DIRS="/dev/shm/wsprdaemon/recording.d/KIWI_0/80/posting.d/MERG_0 /dev/shm/wsprdaemon/recording.d/KA9Q_0/80/posting.d/MERG_0"
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
/dev/shm/wsprdaemon/                           # WD_TMP_DIR (tmpfs)
└── recording.d/
    ├── KA9Q_0/                                # wd-ka9q-record@KA9Q_0 writes here
    │   ├── 2506_KA9Q_0_007040100_usb_iq.wav   # pcmrecord output (all bands)
    │   ├── 40/                                # wd-decode@KA9Q_0-40 works here
    │   │   ├── decode.log
    │   │   ├── posting.d/
    │   │   │   └── MERG_0/                    # symlink target for wd-post@MERG_0-40
    │   │   │       └── 250625_1200_spots.txt  # decoded spots deposited here
    │   │   └── decoding_daemon.pid            # REMOVED in v4 (systemd manages lifecycle)
    │   └── 80/                                # wd-decode@KA9Q_0-80 works here
    │       └── ...
    ├── KIWI_0/
    │   └── 80/                                # wd-kiwi-record@KIWI_0-80 writes here
    │       ├── *.wav
    │       └── posting.d/
    │           └── MERG_0/
    │               └── 250625_1200_spots.txt
    └── posting.d/                             # not needed in v4 (systemd manages posting)

~/wsprdaemon/uploads/                          # persistent storage (survives reboot)
├── wsprnet/
│   └── AI6VN_CM87tj/
│       └── KA9Q_0/40/                        # wd-post writes here
│           └── 250625_1200_spots.txt          # wd-upload-wsprnet reads here
├── wsprdaemon/
│   ├── spots/                                 # wd-upload-wsprdaemon reads here
│   └── noise/
└── grape/                                     # wd-upload-grape reads here
```

### What changes

| Current | v4 |
|---------|-----|
| `.pid` files everywhere | systemd manages all lifecycle |
| Parent polls `ps` for child liveness | systemd `BindsTo=` / `Restart=always` |
| `check_for_zombies()` | Gone — systemd's `MainPID` tracking handles this |
| `wd_kill` / `wd_kill_and_wait_for_death` | `systemctl stop` with `TimeoutStopSec` |
| `wd_sleep` polling loops | `inotifywait` or `systemd-path` for file watches |
| `source ${WSPRDAEMON_CONFIG_FILE}` mid-loop | `EnvironmentFile=` + `systemctl reload` |
| `RUNNING_JOBS_FILE` / `EXPECTED_JOBS_FILE` | systemd is the source of truth |

### What stays the same

- Wav files in `/dev/shm/wsprdaemon/recording.d/` as the recording→decoding interface
- `*_spots.txt` files in `posting.d/` subdirs as the decoding→posting interface
- Spot files in `~/wsprdaemon/uploads/` as the posting→upload interface
- The `wsprdaemon.conf` file format (RECEIVER_LIST[], WSPR_SCHEDULE[])

---

## 6. Migration Strategy

### Phase 1: Extract recording services (lowest risk)

1. Create `wd-kiwi-record` wrapper script that runs `kiwirecorder_manager_daemon()` logic
2. Create `wd-ka9q-record` wrapper script that runs `ka9q_recording_daemon()` logic
3. Create template unit files
4. Test: start recording services manually, verify wav files appear in the expected dirs
5. The rest of wsprdaemon continues running unchanged — it just skips
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
2. `wsprdaemon.timer` replaces the watchdog loop
3. `wd-ctl status` replaces `show_running_jobs()`
4. The old `-A` / `-Z` flags become `systemctl start/stop wsprdaemon`

### Phase 5: Cleanup

1. Remove PID file management, zombie checking, wd_kill infrastructure
2. Remove spawn_* functions
3. The remaining code is config parsing, schedule evaluation, and systemd orchestration

---

## 7. Open Design Questions

1. **Config file format**: Keep the current bash-array format (`RECEIVER_LIST[]`,
   `WSPR_SCHEDULE[]`) or move to INI/TOML/YAML? The bash format is convenient for
   `wd-ctl` (just `source` it) but opaque to non-bash tools.

2. **Journal vs. log files**: Should services log to journald (and let `journalctl -u
   wd-decode@KA9Q_0-40` be the interface) or continue writing their own log files?
   Journald is the systemd-native answer but the current per-job log files are useful.

3. **inotifywait vs. polling**: The decoding daemon currently sleeps and checks for wav
   files. `inotifywait` on the recording dir would be more responsive and lighter, but
   adds a dependency. Alternatively, `systemd-path` units could trigger decoding.

4. **Graceful schedule transitions**: When the schedule changes (e.g., at sunrise), the
   orchestrator needs to stop old jobs and start new ones. Should it drain in-progress
   decoding first, or hard-stop? The current code has a `STOPPING_MIN_WAIT_SECS` delay.

5. **KA9Q recorder dependency fan-out**: `wd-ka9q-record@KA9Q_0` serves multiple
   `wd-decode@KA9Q_0-BAND` instances. If the recorder restarts, all decoders should
   restart too. `BindsTo=` handles this, but the fan-out means a recorder blip takes
   down all bands simultaneously. Is `Wants=` + decoder-side resilience better?

6. **Installation path**: `/opt/wsprdaemon/` (FHS-compliant) vs. `~/wsprdaemon/` (current)?
   The libexec scripts could live in either. Unit files go in `/etc/systemd/system/`.
