# wsprdaemon v4 — Service-Oriented Architecture Specification (v0.4, 2026-03-25)

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
- KA9Q recording is per-receiver (not per-band) because one `wd_record` process listens to
  a single multicast stream and outputs wav files for all bands contained in that stream.
- Kiwi recording is per-receiver-per-band because each `kiwi_recorder.py` handles one channel.
- Decoding and posting are always per-receiver-per-band.
- `kiwi_recorder.py` is used as-is; it is not modified by wsprdaemon.
- `wd_record` is used as-is; once started on a multicast stream it runs indefinitely.

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

### 3.2 wd-ka9q-record@ — KA9Q Recording (1:N)

This service runs a single `wd_record` process that listens to one multicast
stream and writes wav files for all bands contained in that stream.  Once started,
it runs indefinitely — there is no schedule-driven start/stop for KA9Q recording.

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

### 3.4 wd-post@ — Posting (per logical receiver+band)

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

### 6.3 `wd_record` (KA9Q) Lifecycle

`wd_record` is never restarted by the scheduler.  Once started, it runs
indefinitely unless there is an error or the system is shut down.  If the
process exits unexpectedly, systemd's `Restart=always` brings it back.

---

## 7. Installation Layout (FHS-Compliant)

```
/usr/local/sbin/                              # Daemon executables and wd-ctl
├── wd-ctl                                    # Orchestrator / control command
├── wd-kiwi-record                            # Kiwi recording wrapper
├── wd-ka9q-record                            # KA9Q recording wrapper
├── wd-decode                                 # Decoding wrapper
├── wd-post                                   # Posting wrapper
├── wd-upload-wsprnet                         # Wsprnet uploader
├── wd-upload-wsprdaemon                      # Wsprdaemon.org uploader
└── wd-upload-grape                           # GRAPE uploader

/etc/wsprdaemon/                              # Configuration
├── wsprdaemon.conf                           # Main config (INI format)
└── env/                                      # Generated per-service env files
    ├── wd-kiwi-record@KIWI_0-80.env
    ├── wd-ka9q-record@KA9Q_0.env
    ├── wd-decode@KA9Q_0-40.env
    └── ...

/etc/systemd/system/                          # Systemd unit files
├── wsprdaemon.service
├── wsprdaemon.timer
├── wsprdaemon.target
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

/opt/wsprdaemon/                              # Installer and supporting files
├── install.sh                                # Installer script
├── lib/                                      # Shared bash libraries (wd-logger, etc.)
└── share/                                    # Data files, templates, etc.
```

**Key principles**:
- Executables that are invoked directly (daemons, `wd-ctl`) go in `/usr/local/sbin/`.
  They are *not* executed from the source/install directory.
- The installer lives in `/opt/wsprdaemon/` and is responsible for copying executables,
  unit files, and config templates into their FHS locations.
- Shared libraries and helper functions (like `wd-logger`) live in `/opt/wsprdaemon/lib/`
  and are sourced by the executables at runtime.
- Unit files go in `/etc/systemd/system/` (the standard location for admin-installed units).

---

## 8. Migration Strategy

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
   - `F` prefix (e.g., F2, F5, F15) → run `jt9` with the appropriate FST4W
     duration argument.
   The number after the prefix is the decode window in minutes, which determines
   how many 1-minute wav files must be retained and when the decoder is triggered
   (e.g., W2 triggers after every even-odd minute pair; F5 triggers after 5
   consecutive 1-minute files are available).
