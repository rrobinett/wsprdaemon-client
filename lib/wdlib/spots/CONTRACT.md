# wspr.spots row contract

Pipeline-v2 (see `docs/PIPELINE-V2-DESIGN.md`) defines this row shape
as the contract between the decoders that wsprdaemon-client runs
(`wsprd`, `jt9`, and `wsprd -FST4`) and the canonical local sink
(`/var/lib/sigmond/sink.db`, table `wspr.spots`, written via
`sigmond.hamsci_ch.Writer`).

The same shape is what `wd-upload-wsprnet` will query in Phase 3.
Upstream wsprdaemon-server's ClickHouse `wspr.spots` table is a
superset; the hs-uploader translates locally-written rows to that
shape at sync time.

## Fields

See `row.py`.  Every field is mandatory except `uploaded_at`
(populated by the uploader on successful POST to WSPRnet) and
`schema_version` (defaults to the module-level constant).

## Conventions

- **`radiod_id`** is the **receiver identifier**.  On multi-RX hosts
  (multiple antennas / multiple RX888s / KiwiSDR alongside RX888),
  each radiod instance gets its own `radiod_id` and every spot
  carries the `radiod_id` of the receiver that decoded it.  This
  matters because the local DB keeps every spot — including
  duplicates of the same callsign decoded on the same band by
  different receivers — so the wsprdaemon-server upload path can
  ship the full multi-receiver picture upstream.  At WSPRnet upload
  time, `wd-upload-wsprnet` groups by `(time, callsign, band)`
  **ignoring `radiod_id`** and picks the best-SNR row to ship.
  See `docs/PIPELINE-V2-DESIGN.md`, "Dedup semantics".
- **`time`** is the slot start in UTC, second resolution.  Producers
  must pass a tz-aware UTC datetime (or a tz-naive datetime that is
  *known* to be UTC — `row_to_dict` tags it Z).
- **`frequency_hz`** is the absolute receive frequency, not a
  band-relative offset.  Parsers compute it as
  `int(round(freq_mhz * 1_000_000))`.
- **`drift_hz_per_s`** is Hz/second.  wsprd reports Hz/minute;
  parsers divide by 60 at the parse boundary so consumers don't
  need to know the source.
- **`mode`** is the wsprdaemon mode key — `"W2"`, `"W15"`, `"F2"`,
  `"F5"`, `"FST4"` — *not* the spelled-out band/mode string.
- **`band`** is the wsprdaemon band token (`"20"`, `"40"`,
  `"FST4"`, etc.) — what the caller knows from its per-band
  invocation context.
- **`grid`** is empty (`""`) when the spot has no transmitted
  locator (type-2 compound-callsign spot like `W4UK/P`).
- **`callsign`** preserves the on-the-wire form, including the
  `<…>` brackets for type-3 hashed-call spots.
- **`type_2_3`** is the wsprd type-2/3 column — `1`/`2`/`3` in
  practice — preserved as-is from the decoder.

## Adding a new parser

1. Write parser in `wdlib/spots/parsers/<source>.py`.
2. Parser takes one line + slot-context kwargs, returns
   `Optional[Row]`.  Must not raise.
3. Add a fixture under `tests/fixtures/spots/<source>_*.txt`
   (golden capture, not synthetic).
4. Add a test class in `tests/test_spots_<source>_parser.py`
   following the pattern of `test_spots_wsprd_parser.py`:
   per-case unit tests + a fixture-full-pass test.
5. Export the parser from `wdlib/spots/parsers/__init__.py`.

## Versioning

Schema changes bump `SCHEMA_VERSION` in `row.py`.  The on-disk
sink keeps `schema_version` per row, so the hs-uploader can
translate per-row at sync time.  Producers always write the current
version; readers must tolerate older versions during a rollout
window.
