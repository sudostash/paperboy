"""Runtime configuration, read from environment variables.

All limits exist to keep the server safe to expose to an AI agent: a confined
filesystem root, caps on file size and page count, and subprocess timeouts.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")
    return value


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    """Server configuration. Build with :meth:`from_env`."""

    root: Path | None = None
    max_file_bytes: int = 200 * 1024 * 1024
    max_pages: int = 5000
    soffice_timeout: int = 180
    soffice_path: str | None = None
    qpdf_path: str | None = None
    default_overwrite: bool = False
    require_password_references: bool = False   # refuse plain passwords in tool arguments
    clamscan_path: str | None = None
    clamscan_timeout: int = 300

    @classmethod
    def from_env(cls) -> Settings:
        root_raw = os.environ.get("PAPERBOY_ROOT")
        root = Path(root_raw).expanduser().resolve() if root_raw else None
        return cls(
            root=root,
            max_file_bytes=_int_env("PAPERBOY_MAX_FILE_MB", 200) * 1024 * 1024,
            max_pages=_int_env("PAPERBOY_MAX_PAGES", 5000),
            soffice_timeout=_int_env("PAPERBOY_SOFFICE_TIMEOUT", 180),
            soffice_path=os.environ.get("PAPERBOY_SOFFICE") or shutil.which("soffice")
            or shutil.which("libreoffice"),
            qpdf_path=os.environ.get("PAPERBOY_QPDF") or shutil.which("qpdf"),
            default_overwrite=_bool_env("PAPERBOY_ALLOW_OVERWRITE", False),
            require_password_references=_bool_env("PAPERBOY_REQUIRE_PASSWORD_REFERENCES",
                                                  False),
            clamscan_path=os.environ.get("PAPERBOY_CLAMSCAN") or shutil.which("clamscan"),
            clamscan_timeout=_int_env("PAPERBOY_CLAMSCAN_TIMEOUT", 300),
        )
