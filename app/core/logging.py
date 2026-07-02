from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(
    debug: bool = False,
    *,
    log_file: str | Path | None = None,
) -> Path:
    level = logging.DEBUG if debug else logging.INFO
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(level)

    fmt = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d | %(levelname)s | %(threadName)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(fmt)
    root.addHandler(console)

    log_path = (
        Path(log_file)
        if log_file is not None
        else (Path.cwd() / "var" / "logs" / "tgccm.log")
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=20 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # Keep Telethon actionable without drowning in wire-level debug noise.
    logging.getLogger("telethon").setLevel(logging.DEBUG if debug else logging.INFO)
    logging.getLogger("asyncio").setLevel(logging.DEBUG if debug else logging.INFO)

    logging.getLogger(__name__).info(
        "Logging configured: level=%s file=%s",
        logging.getLevelName(level),
        str(log_path),
    )
    return log_path
