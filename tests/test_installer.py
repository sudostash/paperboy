"""Tests for the self-installer (config writing)."""
from __future__ import annotations

import json
import sys

import pytest

from paperboy import installer as I


@pytest.fixture
def sandbox_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData/Roaming"))
    return tmp_path


def test_list_clients(sandbox_home):
    keys = {c["client"] for c in I.list_clients()}
    assert keys == {"claude-desktop", "cursor", "windsurf", "claude-code"}


def test_install_cursor_default_command(sandbox_home):
    res = I.install("cursor", root=str(sandbox_home / "docs"))
    cfg = json.loads((sandbox_home / ".cursor/mcp.json").read_text())
    entry = cfg["mcpServers"]["paperboy"]
    assert entry["command"] == sys.executable
    assert entry["args"] == ["-m", "paperboy"]
    assert entry["env"]["PAPERBOY_ROOT"].endswith("docs")
    assert res["action"] == "installed"


def test_install_idempotency_guard(sandbox_home):
    I.install("cursor")
    with pytest.raises(RuntimeError, match="already has"):
        I.install("cursor")


def test_force_overwrite_and_merge(sandbox_home):
    I.install("cursor", name="paperboy")
    res = I.install("cursor", name="pdftools", use_uvx=True, force=True)
    cfg = json.loads((sandbox_home / ".cursor/mcp.json").read_text())
    assert cfg["mcpServers"]["pdftools"]["command"] == "uvx"
    assert "paperboy" in cfg["mcpServers"]  # existing entry preserved
    assert res["backup"] is not None


def test_uninstall(sandbox_home):
    I.install("cursor", name="paperboy")
    assert I.uninstall("cursor", name="paperboy")["action"] == "removed"
    assert I.uninstall("cursor", name="paperboy")["action"] == "noop"


def test_unknown_client(sandbox_home):
    with pytest.raises(RuntimeError, match="Unknown client"):
        I.install("notaclient")


def test_config_snippet(sandbox_home):
    snip = json.loads(I.config_snippet(root="/tmp/x", use_uvx=True))
    assert snip["mcpServers"]["paperboy"]["command"] == "uvx"
