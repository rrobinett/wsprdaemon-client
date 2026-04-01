"""Per-daemon logging for wsprdaemon v4.

Each daemon gets its own log file in /var/log/wsprdaemon/.
When a log file exceeds MAX_LOG_BYTES (default 1 MB), it is
truncated to the first 25% of lines — preserving startup context
while preventing unbounded growth.

Usage:
    from wdlib.logger import get_logger
    log = get_logger('wd-decode@KA9Q_0-80')
    log.info("Decode started")
"""

import logging
import os
from pathlib import Path

from wdlib.paths import LOG_DIR

MAX_LOG_BYTES = 1_048_576   # 1 MB
KEEP_FRACTION = 0.25        # Keep first 25% of lines on truncation


class TruncatingFileHandler(logging.FileHandler):
    """A file handler that truncates to first 25% of lines when oversized."""

    def __init__(self, filename, max_bytes=MAX_LOG_BYTES,
                 keep_fraction=KEEP_FRACTION, **kwargs):
        self.max_bytes = max_bytes
        self.keep_fraction = keep_fraction
        super().__init__(filename, **kwargs)

    def emit(self, record):
        super().emit(record)
        try:
            if os.path.getsize(self.baseFilename) > self.max_bytes:
                self._truncate()
        except OSError:
            pass

    def _truncate(self):
        """Keep only the first 25% of lines."""
        try:
            with open(self.baseFilename, 'r') as f:
                lines = f.readlines()
            keep_count = max(1, int(len(lines) * self.keep_fraction))
            kept = lines[:keep_count]
            kept.append(f'--- log truncated: kept {keep_count}/{len(lines)} lines ---\n')
            with open(self.baseFilename, 'w') as f:
                f.writelines(kept)
        except OSError:
            pass


def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """Get a named logger that writes to /var/log/wsprdaemon/<name>.log.

    Also logs to stderr so journald captures it when running under systemd.
    """
    logger = logging.getLogger(f'wsprdaemon.{name}')
    if logger.handlers:
        return logger  # Already configured

    logger.setLevel(level)
    formatter = logging.Formatter(
        '%(asctime)s %(levelname)-7s %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%S'
    )

    # File handler with truncation
    log_path = LOG_DIR / f'{name}.log'
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = TruncatingFileHandler(str(log_path))
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    except OSError:
        pass  # Fall through to stderr only

    # Stderr handler (captured by journald under systemd)
    sh = logging.StreamHandler()
    sh.setFormatter(formatter)
    logger.addHandler(sh)

    return logger


def set_verbosity(logger: logging.Logger, level: int):
    """Change verbosity dynamically (used by USR1/USR2 signal handlers)."""
    logger.setLevel(level)
    for handler in logger.handlers:
        handler.setLevel(level)
