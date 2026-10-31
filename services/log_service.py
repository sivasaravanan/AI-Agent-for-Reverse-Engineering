"""Structured logging setup (file + console). Messages are redacted."""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from security.secret_scanner import redact


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def configure_logging(log_dir: str | Path, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("ase")
    if logger.handlers:
        return logger
    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(Path(log_dir) / "app.log", maxBytes=1_000_000,
                                                  backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        pass
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    logger.addFilter(_RedactingFilter())
    return logger
