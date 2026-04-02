#!/usr/bin/env python3
"""wd-ka9q-record — KA9Q recording daemon for wsprdaemon v4.

Uses ka9q-python RadiodControl to dynamically create WSPR/FST4W channels
on radiod, then receives RTP multicast using a single select() loop —
one receive thread for ALL bands.  No per-band threads, no numpy in the
hot path.

Thread count: 3 (main + receive + health-monitor) regardless of band count.
Hot path per packet: 12-byte header strip + bytearray.extend(payload).

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

import os
import select as _select
import signal
import socket
import struct
import sys
import threading
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from ka9q import RadiodControl
from ka9q.discovery import ChannelInfo

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

WINDOW_SEC     = 60
EXPECTED_BYTES = SAMPLE_RATE * WINDOW_SEC * CHANNELS * 4  # float32 = 4 bytes
DROP_TIMEOUT   = 10.0   # seconds without packets before channel recreation


# ── WAV writer ────────────────────────────────────────────────────────────────
def _write_wav(path: Path, raw: bytes, sample_rate: int, channels: int) -> None:
    """Write an IEEE_FLOAT (AudioFormat=3) wav file from raw float32 bytes."""
    data_size = len(raw)
    with open(str(path), 'wb') as f:
        f.write(b'RIFF')
        f.write(struct.pack('<I', 36 + data_size))
        f.write(b'WAVE')
        f.write(b'fmt ')
        f.write(struct.pack('<I', 16))
        f.write(struct.pack('<H', 3))                            # IEEE_FLOAT
        f.write(struct.pack('<H', channels))
        f.write(struct.pack('<I', sample_rate))
        f.write(struct.pack('<I', sample_rate * channels * 4))   # ByteRate
        f.write(struct.pack('<H', channels * 4))                 # BlockAlign
        f.write(struct.pack('<H', 32))                           # BitsPerSample
        f.write(b'data')
        f.write(struct.pack('<I', data_size))
        f.write(raw)


# ── Per-band state ────────────────────────────────────────────────────────────
@dataclass
class BandState:
    band:        str
    freq_hz:     int
    channel:     ChannelInfo
    sock:        socket.socket
    out_dir:     Path
    buf:         bytearray = field(default_factory=bytearray)
    win_start:   Optional[float] = None   # Unix time of current window start
    last_pkt_t:  float = 0.0              # for health monitoring


def _open_socket(channel: ChannelInfo) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, 'SO_REUSEPORT'):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    sock.bind(('0.0.0.0', channel.port))
    mreq = struct.pack('=4s4s',
                       socket.inet_aton(channel.multicast_address),
                       socket.inet_aton('0.0.0.0'))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.setblocking(False)
    return sock


def _flush(bs: BandState, start_utc: float) -> None:
    """Write one minute wav file for the given band."""
    raw = bytes(bs.buf)
    bs.buf = bytearray()

    if not raw:
        return

    # Enforce exact byte count — zero-pad front if short, truncate if long
    if len(raw) < EXPECTED_BYTES:
        deficit = EXPECTED_BYTES - len(raw)
        log.warning('[%s] short window: %d/%d bytes, zero-padding front',
                    bs.band, len(raw), EXPECTED_BYTES)
        raw = bytes(deficit) + raw
    elif len(raw) > EXPECTED_BYTES:
        log.warning('[%s] long window: %d/%d bytes, truncating',
                    bs.band, len(raw), EXPECTED_BYTES)
        raw = raw[:EXPECTED_BYTES]

    dt   = datetime.fromtimestamp(start_utc, tz=timezone.utc)
    name = dt.strftime(f'%Y%m%d_%H%M%S_{bs.freq_hz}{FILE_SUFFIX}.wav')
    path = bs.out_dir / name
    _write_wav(path, raw, SAMPLE_RATE, CHANNELS)
    log.info('[%s] wrote %s (%d samples / 60 s)',
             bs.band, name, len(raw) // (CHANNELS * 4))


# ── Receive loop (single thread, all bands) ───────────────────────────────────
def _receive_loop(states: List[BandState], states_lock: threading.Lock,
                  running: threading.Event) -> None:
    """Single select() loop demuxing all band sockets."""

    while running.is_set():
        with states_lock:
            sock_to_state: Dict[socket.socket, BandState] = {
                bs.sock: bs for bs in states
            }

        if not sock_to_state:
            time.sleep(0.1)
            continue

        try:
            readable, _, _ = _select.select(list(sock_to_state.keys()), [], [], 1.0)
        except (ValueError, OSError):
            # A socket was closed (during health restore); rebuild next iteration
            continue

        now = time.time()
        win = (int(now) // WINDOW_SEC) * WINDOW_SEC

        for sock in readable:
            try:
                data = sock.recv(65536)
            except OSError:
                continue

            if len(data) < 12:
                continue
            version = (data[0] >> 6) & 0x03
            if version != 2:
                continue
            csrc_count = data[0] & 0x0F
            hdr_len    = 12 + csrc_count * 4
            payload    = data[hdr_len:]
            if not payload:
                continue

            bs = sock_to_state.get(sock)
            if bs is None:
                continue

            bs.last_pkt_t = now

            # Minute-window management
            if bs.win_start is None:
                bs.win_start = win + WINDOW_SEC  # align to next full minute
                continue

            if now < bs.win_start:
                continue  # pre-window, discard

            if win > bs.win_start:
                _flush(bs, bs.win_start)
                bs.win_start = win

            bs.buf.extend(payload)


# ── Health monitor (one thread, all bands) ────────────────────────────────────
def _health_monitor(states: List[BandState], states_lock: threading.Lock,
                    control: RadiodControl, running: threading.Event) -> None:
    """Recreate stale channels when no packets arrive for DROP_TIMEOUT seconds."""
    while running.is_set():
        time.sleep(2.0)
        now = time.time()

        with states_lock:
            for bs in states:
                if bs.last_pkt_t == 0.0:
                    continue  # never received — give startup time
                if now - bs.last_pkt_t < DROP_TIMEOUT:
                    continue

                log.warning('[%s] no packets for %.0fs — recreating channel',
                            bs.band, now - bs.last_pkt_t)
                try:
                    # Close old socket
                    try:
                        bs.sock.close()
                    except OSError:
                        pass

                    # Ask radiod to (re)create the channel
                    channel = control.ensure_channel(
                        frequency_hz=bs.freq_hz,
                        preset=PRESET,
                        sample_rate=SAMPLE_RATE,
                        agc_enable=0,
                        gain=GAIN,
                    )
                    bs.channel   = channel
                    bs.sock      = _open_socket(channel)
                    bs.last_pkt_t = now  # reset timer
                    log.info('[%s] channel restored at %s:%d',
                             bs.band, channel.multicast_address, channel.port)
                except Exception as exc:
                    log.error('[%s] channel restore failed: %s', bs.band, exc)
                    bs.last_pkt_t = now  # avoid hammering


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

    running     = threading.Event()
    states_lock = threading.Lock()
    states: List[BandState] = []

    def _stop(sig, _frame):
        log.info('received signal %d, shutting down', sig)
        running.clear()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT,  _stop)

    running.set()

    with RadiodControl(RADIOD_STATUS) as control:
        # Create channels and open sockets for all bands
        for band in BANDS:
            freq_hz = BAND_FREQ_HZ.get(band)
            if freq_hz is None:
                log.warning('unknown band %r — skipping', band)
                continue

            try:
                channel = control.ensure_channel(
                    frequency_hz=freq_hz,
                    preset=PRESET,
                    sample_rate=SAMPLE_RATE,
                    agc_enable=0,
                    gain=GAIN,
                )
            except Exception as exc:
                log.error('[%s] ensure_channel failed: %s — skipping', band, exc)
                continue

            out_dir = RECORDING_DIR / band
            out_dir.mkdir(parents=True, exist_ok=True)

            sock = _open_socket(channel)
            bs   = BandState(band=band, freq_hz=freq_hz,
                             channel=channel, sock=sock, out_dir=out_dir)
            states.append(bs)
            log.info('[%s] channel ready: %s:%d (SSRC %d)',
                     band, channel.multicast_address, channel.port, channel.ssrc)

        if not states:
            log.error('no bands configured, exiting')
            return 1

        # Start receive and health-monitor threads
        rx_thread = threading.Thread(
            target=_receive_loop,
            args=(states, states_lock, running),
            daemon=True, name='rx-loop')
        hm_thread = threading.Thread(
            target=_health_monitor,
            args=(states, states_lock, control, running),
            daemon=True, name='health-monitor')
        rx_thread.start()
        hm_thread.start()

        # Block main thread until SIGTERM/SIGINT
        while running.is_set():
            time.sleep(0.5)

        log.info('stopping — flushing %d band(s)', len(states))
        rx_thread.join(timeout=3.0)
        hm_thread.join(timeout=3.0)

        with states_lock:
            for bs in states:
                if bs.win_start is not None and bs.buf:
                    _flush(bs, bs.win_start)
                try:
                    bs.sock.close()
                except OSError:
                    pass

    log.info('exiting')
    return 0


if __name__ == '__main__':
    sys.exit(main())
