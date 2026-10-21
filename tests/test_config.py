"""Settings loaded from environment variables."""
from __future__ import annotations

from pathlib import Path

import pytest

from paperboy.config import Settings

ENV_VARS = [
    "PAPERBOY_ROOT", "PAPERBOY_MAX_FILE_MB", "PAPERBOY_MAX_PAGES",
    "PAPERBOY_SOFFICE_TIMEOUT", "PAPERBOY_SOFFICE", "PAPERBOY_QPDF",
    "PAPERBOY_ALLOW_OVERWRITE",
]


@pytest.fixture
def clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_defaults_without_environment(clean_env):
    clean_env.setattr("shutil.which", lambda _: None)
    s = Settings.from_env()
    assert s.root is None
    assert s.max_file_bytes == 200 * 1024 * 1024
    assert s.max_pages == 5000
    assert s.soffice_timeout == 180
    assert s.soffice_path is None
    assert s.qpdf_path is None
    assert s.default_overwrite is False


def test_root_is_expanded_and_resolved(clean_env, tmp_path):
    clean_env.setenv("HOME", str(tmp_path))
    (tmp_path / "docs").mkdir()
    clean_env.setenv("PAPERBOY_ROOT", "~/docs/../docs")
    assert Settings.from_env().root == (tmp_path / "docs").resolve()


def test_root_symlink_is_resolved_to_its_target(clean_env, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    clean_env.setenv("PAPERBOY_ROOT", str(link))
    assert Settings.from_env().root == real.resolve()


def test_numeric_limits_are_read(clean_env):
    clean_env.setenv("PAPERBOY_MAX_FILE_MB", "5")
    clean_env.setenv("PAPERBOY_MAX_PAGES", "12")
    clean_env.setenv("PAPERBOY_SOFFICE_TIMEOUT", "30")
    s = Settings.from_env()
    assert s.max_file_bytes == 5 * 1024 * 1024
    assert s.max_pages == 12
    assert s.soffice_timeout == 30


def test_empty_numeric_value_falls_back_to_default(clean_env):
    clean_env.setenv("PAPERBOY_MAX_PAGES", "")
    assert Settings.from_env().max_pages == 5000


@pytest.mark.parametrize("value", ["abc", "1.5", "10MB"])
def test_non_integer_limit_is_rejected(clean_env, value):
    clean_env.setenv("PAPERBOY_MAX_PAGES", value)
    with pytest.raises(ValueError, match="PAPERBOY_MAX_PAGES must be an integer"):
        Settings.from_env()


@pytest.mark.parametrize("value", ["0", "-1"])
def test_non_positive_limit_is_rejected(clean_env, value):
    clean_env.setenv("PAPERBOY_MAX_FILE_MB", value)
    with pytest.raises(ValueError, match="must be positive"):
        Settings.from_env()


@pytest.mark.parametrize(("value", "expected"), [
    ("1", True), ("true", True), ("YES", True), (" on ", True),
    ("0", False), ("false", False), ("no", False), ("", False), ("maybe", False),
])
def test_allow_overwrite_flag(clean_env, value, expected):
    clean_env.setenv("PAPERBOY_ALLOW_OVERWRITE", value)
    assert Settings.from_env().default_overwrite is expected


def test_explicit_binary_paths_take_precedence(clean_env):
    clean_env.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    clean_env.setenv("PAPERBOY_SOFFICE", "/opt/lo/soffice")
    clean_env.setenv("PAPERBOY_QPDF", "/opt/qpdf")
    s = Settings.from_env()
    assert s.soffice_path == "/opt/lo/soffice"
    assert s.qpdf_path == "/opt/qpdf"


def test_binaries_are_discovered_on_path(clean_env):
    found = {"libreoffice": "/usr/bin/libreoffice", "qpdf": "/usr/bin/qpdf"}
    clean_env.setattr("shutil.which", found.get)
    s = Settings.from_env()
    assert s.soffice_path == "/usr/bin/libreoffice"  # falls back from soffice
    assert s.qpdf_path == "/usr/bin/qpdf"


def test_settings_are_immutable():
    s = Settings(root=Path("/tmp"))
    with pytest.raises(AttributeError):
        s.max_pages = 1  # type: ignore[misc]
