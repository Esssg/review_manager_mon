from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock


DEFAULT_LOG_FILE = "logs/server.log.txt"
DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 5

_LOCK = Lock()


def configure_server_logging(log_file: str | Path | None = None) -> Path:
    # 서버가 오래 켜져 있어도 로그 파일 하나가 무한히 커지지 않도록 회전 로그를 사용합니다.
    path = _resolve_log_file(log_file)
    max_bytes = _read_int_env("SERVER_LOG_MAX_BYTES", DEFAULT_LOG_MAX_BYTES)
    backup_count = _read_int_env("SERVER_LOG_BACKUP_COUNT", DEFAULT_LOG_BACKUP_COUNT)

    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = _get_or_create_file_handler(path, max_bytes, backup_count)

        for logger_name in ("review_manager_mon", "uvicorn.error", "uvicorn.access"):
            logger = logging.getLogger(logger_name)
            if handler not in logger.handlers:
                logger.addHandler(handler)
            logger.setLevel(logging.INFO)

    return path


def _resolve_log_file(log_file: str | Path | None) -> Path:
    configured = log_file or os.environ.get("SERVER_LOG_FILE") or DEFAULT_LOG_FILE
    path = Path(configured)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def _get_or_create_file_handler(
    path: Path,
    max_bytes: int,
    backup_count: int,
) -> RotatingFileHandler:
    resolved_path = str(path.resolve())
    for logger_name in ("review_manager_mon", "uvicorn.error", "uvicorn.access"):
        for handler in logging.getLogger(logger_name).handlers:
            if isinstance(handler, RotatingFileHandler) and handler.baseFilename == resolved_path:
                return handler

    handler = RotatingFileHandler(
        resolved_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )
    return handler


def _read_int_env(name: str, default: int) -> int:
    value = os.environ.get(name)
    if not value:
        return default

    try:
        return int(value)
    except ValueError:
        return default
