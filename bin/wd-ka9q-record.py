#!/usr/bin/env python3
"""wd-ka9q-record — KA9Q recording daemon for wsprdaemon v4.

Uses ka9q-python (RadiodControl + ManagedStream) to dynamically create
WSPR/FST4W channels on radiod and write 1-minute wav files into the spool
directory.  Channels are created at startup and removed on SIGTERM.

Environment variables (set by systemd EnvironmentFile):
    WD_RADIOD_STATUS    Radiod status/control DNS  (e.g. k3lr-hf.local)
    WD_RECEIVER_NAME    Receiver name              (e.g. KA9Q_0)
    WD_RECEIVER_TYPE    ka9q | ka9q_wwv
    WD_BANDS            Space-separated band list  (e.g. "630 80 40 20")
    WD_RECORDING_DIR    Spool root for this receiver
    WD_GAIN_DB          RF output gain in dB       (default: 60)

Wav file format: IEEE_FLOAT (AudioFormat=3), 32-bit float.
  WSPR/FST4W: mono,   12000 Hz  → <timestamp>_<freq>_usr.wav
  WWV IQ:     stereo, 24000 Hz  → <timestamp>_<freq>_IQ.wav  (interleaved I/Q)

Every file contains exactly SAMPLE_RATE * 60 samples per channel.
Files shorter than expected (startup jitter) are zero-padded at the front;
files longer are truncated.  The timestamp in the filename is the UTC
second at which the minute window began.
"""

import math
import os
import signal
import struct
import sys
import threading
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import numpy as np
from ka9q import RadiodControl, ManagedStream

# wdlib is in /opt/wsprdaemon/lib (installed) or relative to this script
_HERE = Path(__file__).resolve().parent
for _p in ['/opt/wsprdaemon/lib', str(_HERE.parent / 'lib')]:
    if Path(_p).exists() and _p not in sys.path:
        sys.path.insert(0, _p)

from wdlib.envgen import BAND_FREQ_HZ  # noqa: E402

log = logging.getLogger('wd-ka9q-record')

# ── Configuration from environment ──────────────────────────────────────────
RADIOD_STATUS = os.environ['WD_RADIOD_STATUS']
RX_NAME       = os.environ['WD_RECEIVER_NAME']
RX_TYPE       = os.environ.get('WD_RECEIVER_TYPE', 'ka9q')
BANDS         = os.environ['WD_BANDS'].split()
RECORDING_DIR = Path(os.environ['WD_RECORDING_DIR'])
GAIN_DB       = float(os.environ.get('WD_GAIN_DB', '60'))

# WSPR/FST4W uses 12 kHz USB mono; WWV IQ uses 24 kHz stereo
if RX_TYPE == 'ka9q_wwv':
    PRESET       = 'iq'
    SAMPLE_RATE  = 24000
    CHANNELS     = 2        # stereo: interleaved I/Q
    GAIN         = 0.0
    FILE_SUFFIX  = '_IQ'
else:
    PRESET       = 'usb'
    SAMPLE_RATE  = 12000
    CHANNELS     = 1        # mono: USB audio
    GAIN         = GAIN_DB
    FILE_SUFFIX  = '_usr'

WINDOW_SEC      = 60
# Exact float32 values expected in every completed minute file
EXPECTED_FLOATS = SAMPLE_RATE * WINDOW_SEC * CHANNELS


# ── IEEE_FLOAT WAV writer ────────────────────────────────────────────────────
def write_ieee_float_wav(path: Path, data: np.ndarray,
                         sample_rate: int, channels: int) -> None:
    """Write a IEEE_FLOAT (AudioFormat=3) wav file.

    Python's wave module only supports PCM (AudioFormat=1).
    wsprd/sox expect AudioFormat=3 to correctly interpret float32 samples.
    """
    samples = data.astype(np.float32)
    raw = samples.tobytes()          # little-endian float32
    data_size = len(raw)

    with open(str(path), 'wb') as f:
        # RIFF header
        f.write(b'RIFF')
        f.write(struct.pack('<I', 36 + data_size))
        f.write(b'WAVE')
        # fmt chunk
        f.write(b'fmt ')
        f.write(struct.pack('<I', 16))                          # chunk size
        f.write(struct.pack('<H', 3))                           # IEEE_FLOAT
        f.write(struct.pack('<H', channels))
        f.write(struct.pack('<I', sample_rate))
        f.write(struct.pack('<I', sample_rate * channels * 4))  # ByteRate
        f.write(struct.pack('<H', channels * 4))                # BlockAlign
        f.write(struct.pack('<H', 32))                          # BitsPerSample
        # data chunk
        f.write(b'data')
        f.write(struct.pack('<I', data_size))
        f.write(raw)


# ── Per-band wav writer ──────────────────────────────────────────────────────
class BandWriter:
    """Accumulates samples for one band and flushes 1-minute wav files."""

    def __init__(self, band: str, freq_hz: int, out_dir: Path, file_suffix: str):
        self.band        = band
        self.freq_hz     = freq_hz
        self.out_dir     = out_dir
        self.file_suffix = file_suffix
        self.out_dir.mkdir(parents=True, exist_ok=True)

        self._lock     = threading.Lock()
        self._chunks: List[np.ndarray] = []
        self._win_start: Optional[float] = None

    def on_samples(self, samples, quality) -> None:
        """ManagedStream callback — ~267 ms cadence (10 packets × 320 samples)."""
        now = time.time()
        win = math.floor(now / WINDOW_SEC) * WINDOW_SEC

        # Convert to float32 numpy array, handling both real and complex IQ
        if np.iscomplexobj(samples):
            # IQ: interleave real (I) and imaginary (Q) → stereo float32
            flat = np.empty(len(samples) * 2, dtype=np.float32)
            flat[0::2] = samples.real.astype(np.float32)
            flat[1::2] = samples.imag.astype(np.float32)
            chunk = flat
        else:
            chunk = samples.astype(np.float32)

        with self._lock:
            if self._win_start is None:
                self._win_start = win + WINDOW_SEC   # align to next full minute
                return

            if now < self._win_start:
                return   # pre-window, discard

            if win > self._win_start:
                self._flush(self._win_start)
                self._win_start = win

            self._chunks.append(chunk)

    def _flush(self, start_utc: float) -> None:
        if not self._chunks:
            return
        data = np.concatenate(self._chunks)
        self._chunks = []

        # Enforce exact sample count — pad front with zeros if short (startup
        # jitter), truncate if long (rare clock slip).
        if len(data) < EXPECTED_FLOATS:
            deficit = EXPECTED_FLOATS - len(data)
            log.warning('[%s] short window: %d/%d floats, zero-padding front',
                        self.band, len(data), EXPECTED_FLOATS)
            data = np.concatenate([np.zeros(deficit, dtype=np.float32), data])
        elif len(data) > EXPECTED_FLOATS:
            log.warning('[%s] long window: %d/%d floats, truncating',
                        self.band, len(data), EXPECTED_FLOATS)
            data = data[:EXPECTED_FLOATS]

        dt   = datetime.fromtimestamp(start_utc, tz=timezone.utc)
        name = dt.strftime(f'%Y%m%d_%H%M%S_{self.freq_hz}{self.file_suffix}.wav')
        path = self.out_dir / name

        write_ieee_float_wav(path, data, SAMPLE_RATE, CHANNELS)
        log.info('[%s] wrote %s (%d samples / %.1f s)',
                 self.band, name, len(data) // CHANNELS, len(data) / CHANNELS / SAMPLE_RATE)

    def flush_final(self) -> None:
        with self._lock:
            if self._win_start is not None:
                self._flush(self._win_start)


# ── Main ─────────────────────────────────────────────────────────────────────
def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)sZ %(name)s %(levelname)s %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%S',
    )
    log.info('starting: rx=%s status=%s type=%s bands=%s',
             RX_NAME, RADIOD_STATUS, RX_TYPE, BANDS)

    RECORDING_DIR.mkdir(parents=True, exist_ok=True)

    streams: List[ManagedStream] = []
    writers: List[BandWriter]    = []
    stop_event = threading.Event()

    def _stop(sig, _frame):
        log.info('received signal %d, shutting down', sig)
        stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT,  _stop)

    with RadiodControl(RADIOD_STATUS) as control:
        for band in BANDS:
            freq_hz = BAND_FREQ_HZ.get(band)
            if freq_hz is None:
                log.warning('unknown band %r — skipping', band)
                continue

            writer = BandWriter(band, freq_hz, RECORDING_DIR / band, FILE_SUFFIX)
            writers.append(writer)

            stream = ManagedStream(
                control=control,
                frequency_hz=freq_hz,
                preset=PRESET,
                sample_rate=SAMPLE_RATE,
                gain=GAIN,
                on_samples=writer.on_samples,
                on_stream_dropped=lambda reason, b=band:
                    log.warning('[%s] stream dropped: %s', b, reason),
                on_stream_restored=lambda ch, b=band:
                    log.info('[%s] stream restored at %.4f MHz',
                             b, ch.frequency / 1e6),
            )
            stream.start()
            streams.append(stream)
            log.info('[%s] stream started at %d Hz (preset=%s channels=%d)',
                     band, freq_hz, PRESET, CHANNELS)

        stop_event.wait()

        log.info('stopping %d stream(s)', len(streams))
        for s in streams:
            s.stop()
        for w in writers:
            w.flush_final()

    log.info('channels removed, exiting')
    return 0


if __name__ == '__main__':
    sys.exit(main())
