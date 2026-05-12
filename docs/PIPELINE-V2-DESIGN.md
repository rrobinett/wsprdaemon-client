# wsprdaemon-client pipeline v2 — DB-direct decode + dedup-at-upload

**Status:** proposal, agreed in principle 2026-05-12 by Rob (architect)
and Michael (author).  Authoring on a topic branch; staged migration.

## Background — what we have today

Spots flow through four systemd-service families:

```
   ┌────────────────────┐    WAV files in
   │ wd-ka9q-record@... │ →  /var/spool/wsprdaemon/recording/<host>/<band>/
   └────────────────────┘                      │
                                               │ inotifywait
                                               ▼
                                ┌────────────────────────────┐
                                │ wd-decode@<host>-<band>... │  (13 services)
                                │  bash + wsprd / jt9        │
                                └────────────────────────────┘
                                               │ writes ALL_WSPR.TXT,
                                               │ wspr_spots.txt files
                                               ▼
                                ┌────────────────────────────┐
                                │ wd-post@<host>-<band>...   │  (13 services)
                                │  bash; moves files into    │
                                │  posting/uploads/wsprnet/  │
                                └────────────────────────────┘
                                               │
                                               ▼
                                ┌────────────────────────────┐
                                │ wd-upload-wsprnet@<call>   │
                                │  reads upload spool,       │
                                │  POSTs to WSPRnet API      │
                                └────────────────────────────┘
```

Costs of the current design:

- **~26 always-on services** per host (13 decode + 13 post), each in
  its own cgroup with its own systemd drop-ins for CPU affinity.
- **Polling/inotify latency** between recorder write and decoder
  pickup; sporadic `.wd-decode.lock` stale-file issues are symptoms.
- **Filesystem as IPC** — spots traverse three on-disk hops
  (recording → spool → posting) before they reach the uploader.
- **No DB until the very end** — the uploader doesn't know about its
  upstream sibling psk-recorder, which already writes spots directly
  to `psk.spots` via the canonical `hamsci_ch` Writer.
- **Cross-band dedup absent** — same-callsign duplicates within a
  cycle pass through unreconciled; the WSPRnet API handles it
  server-side via near-frequency-cluster detection (visible as
  `DIAG SIMILAR` lines).

## Goals

1. **Match the psk-recorder pattern** — recorder spawns decoder per
   WAV, decoder writes directly to the canonical sink, uploader
   queries the sink.  Single source of truth.
2. **Drop ~26 systemd services** — `wd-decode@*` and `wd-post@*`
   become in-process workers of `wd-ka9q-record`.
3. **Move dedup into SQL** — `SELECT … MAX(snr_db) … GROUP BY (cycle,
   callsign)` replaces the implicit "let WSPRnet figure it out" model.
4. **Preserve diagnostics** — partial-report files (used by
   `smd watch wspr`) and per-band cycle traces stay.

## Non-goals (this redesign does NOT)

- Touch psk-recorder.  Its pipeline is the proven model; we're
  catching up to it.
- Change the WAV format, sample rate, or recording cadence.
- Change the upload protocol to WSPRnet.
- Migrate the upstream ClickHouse `wspr.spots` schema (already
  defined; producer side is what's missing locally).
- Touch Kiwi-source decoding paths.  The current bash `wd-decode`
  with polling stays for Kiwi-source rigs; the new model is
  KA9Q-source only.  (Long-term: Kiwi support follows.)

## Target architecture

```
   ┌──────────────────────────────────────────┐
   │ wd-ka9q-record@<host>                    │
   │                                          │
   │   per cycle, per band:                   │
   │     • close WAV                          │
   │     • spawn wsprd (W*) or jt9 (F*)       │  → /var/spool/.../recording/...wav
   │       in subprocess pool                 │     (kept for diagnostics)
   │     • parse decoder stdout               │
   │     • write canonical row →              │
   │       hamsci_ch.Writer(table="spots",    │
   │                        mode="wspr")      │  → wspr.spots (SQLite or CH)
   └──────────────────────────────────────────┘
                                               │
                                               ▼
                                ┌────────────────────────────┐
                                │ wd-upload-wsprnet@<call>   │
                                │   per cycle boundary:      │
                                │     • SELECT … FROM        │
                                │       wspr.spots WHERE     │
                                │       cycle = ? AND        │
                                │       uploaded_at IS NULL  │
                                │     • dedup (best SNR per  │
                                │       (cycle, callsign))   │
                                │     • POST to WSPRnet      │
                                │     • mark uploaded        │
                                └────────────────────────────┘
```

Services that survive: `wd-ka9q-record@<host>`,
`wd-upload-wsprnet@<call>`, `wd-spool-clean.{service,timer}`.

Services that go away: `wd-decode@<host>-<band>` × 13,
`wd-post@<host>-<band>` × 13.

## `wspr.spots` row shape

Matches the columnar tier the upstream `wspr.spots` table in
ClickHouse already defines (Phil's wsprdaemon-server schema), so a
later hs-uploader sync is straightforward.

| field             | type      | source                                    |
|-------------------|-----------|-------------------------------------------|
| `time`            | DateTime  | slot start (UTC), second resolution       |
| `band`            | String    | "20", "40", "FST4", etc.                  |
| `frequency_hz`    | Int64     | absolute Rx freq = band_carrier + offset  |
| `callsign`        | String    | from wsprd `CALL` field                   |
| `grid`            | String    | "" if no locator (type-2 spot)            |
| `snr_db`          | Int16     | wsprd-reported SNR                        |
| `dt`              | Float32   | seconds, position within slot             |
| `drift_hz_per_s`  | Float32   | wsprd `drift` field                       |
| `pwr_dbm`         | Int16     | wsprd `pwr` field                         |
| `sync_quality`    | Float32   | wsprd sync                                |
| `mode`            | String    | "W2", "W15", "F2", "F5", "FST4"           |
| `decoder_kind`    | String    | "wsprd", "jt9"                            |
| `decoder_depth`   | UInt8     | `-d` flag (typically 3)                   |
| `type_2_3`        | UInt8     | wsprd type-2/3 distinction                |
| `rx_call`         | String    | station callsign                          |
| `rx_grid`         | String    | station grid                              |
| `radiod_id`       | String    | which radiod produced the IQ              |
| `host_id`         | String    | hostname (for multi-station correlation)  |
| `schema_version`  | UInt8     | 1                                         |
| `uploaded_at`     | DateTime? | populated by uploader on success          |

The `uploaded_at` column is local-only (not in upstream schema);
hs-uploader treats `NULL` as "ship me upstream."

Local DB: `/var/lib/sigmond/sink.db`, table `wspr.spots` (SqliteWriter
flattens the namespace to `wspr_spots`).  Schema is created on first
insert via the existing `hamsci_ch` migration plumbing.

## Migration phases

Each phase is independently testable and reversible — none of them
require coordinated deploys with sigmond.

### Phase 1 — schema groundwork + parser library  (no deploys)

Build the row-shape contract and decoder-output parsers without
changing any running process.

- Add `wdlib/spots/` module (Python) with:
  - `row.py` — canonical `wspr.spots` row dataclass + dict factory.
  - `parsers/wsprd.py` — parse wsprd's `ALL_WSPR.TXT` lines + the
    `wspr_spots.txt` summary file into row dicts.
  - `parsers/jt9.py` — parse jt9 output for FT4/FT8 (and FST4)
    modes that wsprdaemon-client decodes (psk-recorder owns its
    own decoders, but wsprdaemon-client decodes additional modes).
- Test coverage with golden-file fixtures (real `ALL_WSPR.TXT`
  samples from `/var/log/wsprdaemon/wsprnet-partial/`).
- Document the row contract here and in `wdlib/spots/CONTRACT.md`.

**Risk:** low — pure library code, no service-level changes.
**Effort:** 1-2 days.

### Phase 2 — DB-direct decode worker (dual-write)

Add a Python worker inside `wd-ka9q-record` that spawns decoders per
WAV and writes rows to `wspr.spots`.  Run it **alongside** the
existing bash `wd-decode@*` services.

- New module `wdlib/decode_pool.py`:
  - Subprocess pool (bounded concurrency, per-band CPU affinity
    inherited from a config table — replaces sigmond's per-service
    drop-ins).
  - Spawn on WAV close; reap with timeout; parse stdout/ALL_WSPR.TXT;
    write to `hamsci_ch.Writer`.
- Per-cycle metric: rows-written count, parse-fail count, decode
  timeout count.  Surface in `wd-ka9q-record`'s log so
  `smd watch wspr` (or a follow-on `smd watch wspr-decode`) can
  show it.
- Dual-write gate via env var: `WD_DECODE_VIA_DB=1` enables; off
  by default for the initial deploy.

The existing `wd-decode@*` services keep running and producing
spots through the old path.  Spots from BOTH paths flow into the
spool; uploader keeps reading the spool.  Verification: compare
DB row count vs spool spot count per cycle for N days.

**Risk:** medium — CPU concurrency under load is the main worry;
mitigation is a bounded subprocess pool with a configurable cap
(default = number of bands).
**Effort:** 3-5 days.

### Phase 3 — switch uploader to DB-as-source

Change `wd-upload-wsprnet` to read from `wspr.spots` instead of
the upload spool.

- Per-cycle query:
  ```sql
  WITH per_callsign AS (
    SELECT callsign, MAX(snr_db) AS best_snr
    FROM wspr_spots
    WHERE time = :cycle AND uploaded_at IS NULL
    GROUP BY callsign
  )
  SELECT s.*
  FROM wspr_spots s
  JOIN per_callsign p
    ON s.callsign = p.callsign AND s.snr_db = p.best_snr
  WHERE s.time = :cycle AND s.uploaded_at IS NULL
  ```
  (Ties on best_snr resolved by `MIN(frequency_hz)` for stability.)
- After successful POST, `UPDATE wspr_spots SET uploaded_at = NOW()
  WHERE time = :cycle AND … the rows we just shipped`.
- Continue writing the partial-report file under
  `/var/log/wsprdaemon/wsprnet-partial/` for `smd watch wspr`.

Switch trigger: env var `WD_UPLOAD_VIA_DB=1`.  Once enabled, the
old spool path is unused.

**Risk:** medium — the dedup query semantics need explicit review
with Rob.  Cross-band same-callsign reports are NOT deduped (those
are valid distinct propagation reports).  Confirm: dedup is
within-cycle same-callsign across bands → pick best SNR.
Open question: should we instead keep all band reports and only
dedup within-band near-frequency clusters?  (See "Dedup semantics"
below.)
**Effort:** 2-3 days + 1-2 days of running both pipelines for
verification.

### Phase 4 — decommission polling services

Once the DB pipeline has been running cleanly for a week or two:

- `WD_DECODE_VIA_DB=1` and `WD_UPLOAD_VIA_DB=1` become defaults.
- Disable `wd-decode@*` and `wd-post@*`; remove their unit
  templates from the installer.
- Prune `/var/spool/wsprdaemon/posting/` from the spool layout.
- Update sigmond's per-service CPU-affinity drop-ins (the
  individual band services no longer exist; affinity now lives
  inside `wd-ka9q-record`'s pool config).
- Bump `wsprdaemon-client` major version.

**Risk:** low — by this point both paths have been observed to
produce equivalent spot counts, and the rollback is a single env
flag flip.
**Effort:** 1 day.

## Dedup semantics — confirmed 2026-05-12

**Rule:** dedup is **(a) only** — within-band same-callsign in the
same cycle, keep the spot with the best SNR.

Three things this rule deliberately does NOT do:

(b) **Cross-band same-callsign** — `K9XX` heard on 40 m AND 20 m in
the same cycle stays as two distinct rows uploaded to WSPRnet.  These
are independent propagation reports and WSPRnet wants both.

**Multi-receiver same-band** — on hosts running multiple antennas /
receivers, the same callsign may be decoded on the same band twice
in the same cycle from different `radiod_id`s.  Every row goes into
the local `wspr.spots` DB tagged with its `radiod_id` so that the
**wsprdaemon-server** upload path (via hs-uploader) gets the full
multi-receiver picture — operators downstream care which antenna
heard what.  At **WSPRnet** upload time, however, `wd-upload-wsprnet`
groups by `(time, callsign, band)` **ignoring `radiod_id`**, picks
the best SNR across all receivers, and ships only one row.

(c) **Type-2 + type-3 from the same station** — `K9XX` and
`<K9XX>` in the same cycle stay as separate rows.  Treating them
as duplicates is a downstream interpretation problem; we keep them
distinct locally.

**Phase 3 implication:** the SQL dedup query is exactly:

```sql
WITH ranked AS (
  SELECT *,
         ROW_NUMBER() OVER (
           PARTITION BY time, callsign, band
           ORDER BY snr_db DESC, frequency_hz ASC
         ) AS rk
  FROM wspr_spots
  WHERE time = :cycle AND uploaded_at IS NULL
)
SELECT * FROM ranked WHERE rk = 1;
```

`PARTITION BY time, callsign, band` (no `radiod_id`) is the
load-bearing part — it merges across receivers.  Ties on `snr_db`
resolved by `MIN(frequency_hz)` for stability.

## Decode concurrency / CPU policy

`wsprd -d 3` (deep decode) can take up to 30 s on a busy slot.  At
cycle end, all 13 bands close their WAVs within the same second; if
all 13 wsprd processes spawn concurrently and saturate the host,
late-arriving spots miss the upload window.

Current per-service model already mitigates this implicitly — each
band's cgroup has CPU shares set by sigmond's affinity drop-ins.

New model needs to replicate this in-process:

- Subprocess pool with `max_concurrent = N` (default = number of
  bands, configurable).
- Per-spawn CPU pinning via `taskset` or `sched_setaffinity` —
  config table maps band → CPU set, sigmond's existing affinity
  policy as input.
- Decode-timeout watchdog: if a `wsprd` exceeds (cycle_len -
  upload_settle_time), kill it and log; the next cycle's WAV is
  more important than waiting on the runaway.

## Compatibility / migration mechanics

- New `wsprdaemon-client` versions detect old per-band service
  units and refuse to install over them unless `--allow-legacy`
  is passed.  The post-uninstall hook of the new package disables
  + masks the old templates.
- Sigmond's wsprdaemon-client client adapter (`inventory --json`)
  reports a `pipeline_version` field so `smd status` can flag
  hosts still on the v1 chain.

## Open questions for review

1. ~~Dedup semantics~~ — **decided 2026-05-12**: rule (a) only;
   see "Dedup semantics" section.
2. **FST4 / WSPR-15 modes** — same architecture works, but the
   spawn timing differs (15-min cycle ≠ 2-min cycle).  Need to
   confirm the decode-pool worker correctly handles both cadences
   when bands within one cycle have different periods.
3. **CPU affinity input** — current sigmond drop-ins are
   per-band-service; new model needs a config representation
   inside `wd-ka9q-record`'s env file.  Default proposal:
   `[decode_pool] cpu_map = { "20" = "4-5", "40" = "6-7", ... }`
   in the existing wsprdaemon-client TOML.  Sigmond's affinity
   drop-ins for the old per-band services translate 1:1.
4. **Sigmond `smd watch wspr` source** — currently reads the
   partial-report file written by `wd-upload-wsprnet`.  We keep
   writing that file in Phase 3 so the verb keeps working
   unchanged.  Worth a follow-up to make it read from
   `wspr.spots` directly once we trust the DB-source path.

## Tracking

A topic branch per phase (`pipeline-v2/phase-1`, etc.).  Each phase
PRs into `main` of `wsprdaemon-client`.  This doc updates with each
merge to reflect "as-built" vs "planned."
