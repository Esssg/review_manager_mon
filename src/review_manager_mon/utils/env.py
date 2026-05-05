from __future__ import annotations

import os
from pathlib import Path


_DOTENV_LOADED = False


def load_dotenv(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_dotenv_line(line)
        if parsed is None:
            continue

        name, value = parsed
        # 셸이나 배포 환경에서 이미 주입한 값은 로컬 .env 값으로 덮어쓰지 않습니다.
        os.environ.setdefault(name, value)


def _ensure_dotenv_loaded() -> None:
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return

    load_dotenv()
    _DOTENV_LOADED = True


def _parse_dotenv_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None

    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].strip()

    name, separator, value = stripped.partition("=")
    if not separator:
        return None

    name = name.strip()
    value = value.strip()
    if not name:
        return None

    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]

    return name, value


def require_env(name: str) -> str:
    _ensure_dotenv_loaded()
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def optional_int_env(name: str, default: int) -> int:
    _ensure_dotenv_loaded()
    value = os.environ.get(name)
    if not value:
        return default

    parsed = int(value)
    if parsed < 1:
        raise RuntimeError(f"{name} must be a positive integer")
    return parsed


def optional_bool_env(name: str, default: bool) -> bool:
    _ensure_dotenv_loaded()
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes"}
