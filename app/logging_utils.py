from __future__ import annotations

import json
import logging
import os
from logging.handlers import RotatingFileHandler
from typing import Any

from app.config import get_settings

LOGGER_NAME = "f5api_converter"


def _safe_json_loads(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return None


def pretty_payload(data: Any, max_len: int = 4000) -> str:
    if data is None:
        return "null"
    if isinstance(data, (dict, list)):
        text = json.dumps(data, ensure_ascii=False, indent=2)
    elif isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
        try:
            text = raw.decode("utf-8")
        except Exception:
            text = repr(raw[:512])
    else:
        text = str(data)
    if len(text) > max_len:
        return text[:max_len] + "\n...<truncated>"
    return text


def mask_auth(headers: dict[str, str]) -> dict[str, str]:
    masked = dict(headers)
    for k in list(masked.keys()):
        if k.lower() == "authorization":
            value = masked[k]
            if value.startswith("Bearer "):
                token = value[7:]
                masked[k] = "Bearer " + (token[:6] + "***" if token else "***")
            else:
                masked[k] = "***"
    return masked


def maybe_parse_json_bytes(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8")
    except Exception:
        return None
    return _safe_json_loads(text)


def setup_logger() -> logging.Logger:
    settings = get_settings()
    logger = logging.getLogger(LOGGER_NAME)
    logger.propagate = False
    logger.setLevel(logging.DEBUG if settings.log_debug else logging.INFO)

    if logger.handlers:
        return logger

    log_path = os.path.join(os.getcwd(), settings.log_file_name)
    handler = RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger
