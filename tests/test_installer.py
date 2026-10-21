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


# --- per-client config locations ------------------------------------------------


@pytest.mark.parametrize(("platform", "relative"), [
    ("darwin", "Library/Application Support/Claude/claude_desktop_config.json"),
    ("linux", ".config/Claude/claude_desktop_config.json"),
    ("win32", "AppData/Roaming/Claude/claude_desktop_config.json"),
])
def test_claude_desktop_config_path_per_platform(sandbox_home, monkeypatch, platform, relative):
    monkeypatch.setattr(sys, "platform", platform)
    res = I.install("claude-desktop")
    assert res["path"] == str(sandbox_home / relative)
    assert (sandbox_home / relative).is_file()


def test_windsurf_config_path(sandbox_home):
    res = I.install("windsurf")
    assert res["path"] == str(sandbox_home / ".codeium/windsurf/mcp_config.json")


def test_unsupported_platform(sandbox_home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "sunos5")
    with pytest.raises(RuntimeError, match="not supported on this platform"):
        I.install("claude-desktop")


def test_list_clients_reports_detection(sandbox_home, monkeypatch):
    monkeypatch.setattr(I.shutil, "which", lambda name: None)
    before = {c["client"]: c for c in I.list_clients()}
    assert not before["cursor"]["detected"]
    assert before["claude-code"] == {"client": "claude-code", "label": "Claude Code",
                                     "method": "cli", "detected": False, "path": None}
    I.install("cursor")
    assert {c["client"]: c for c in I.list_clients()}["cursor"]["detected"]


# --- launch command ------------------------------------------------------------------


def test_server_command_variants(sandbox_home):
    assert I.server_command(False, None, None) == {
        "command": sys.executable, "args": ["-m", "paperboy"]}
    assert I.server_command(True, None, None) == {
        "command": "uvx", "args": ["--from", "paperboy-mcp", "paperboy"]}
    assert I.server_command(True, "/opt/pb", None) == {"command": "/opt/pb", "args": []}


def test_root_is_expanded(sandbox_home):
    entry = I.server_command(False, None, "~/Docs")
    assert entry["env"] == {"PAPERBOY_ROOT": str(sandbox_home / "Docs")}


# --- writing JSON configs safely ----------------------------------------------------------


def _cursor(home):
    return home / ".cursor/mcp.json"


def test_fresh_install_has_no_backup(sandbox_home):
    assert I.install("cursor")["backup"] is None


def test_install_preserves_unrelated_config(sandbox_home):
    path = _cursor(sandbox_home)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"theme": "dark", "mcpServers": {"other": {"command": "x"}}}))
    res = I.install("cursor")
    cfg = json.loads(path.read_text())
    assert cfg["theme"] == "dark"
    assert cfg["mcpServers"]["other"] == {"command": "x"}
    assert "paperboy" in cfg["mcpServers"]
    backup = json.loads(open(res["backup"]).read())
    assert "paperboy" not in backup["mcpServers"]  # backup is the pre-change file


def test_empty_config_file_is_treated_as_empty(sandbox_home):
    path = _cursor(sandbox_home)
    path.parent.mkdir(parents=True)
    path.write_text("")
    I.install("cursor")
    assert "paperboy" in json.loads(path.read_text())["mcpServers"]


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '"text"', '{"mcpServers": []}'])
def test_unusable_config_is_left_untouched(sandbox_home, content):
    path = _cursor(sandbox_home)
    path.parent.mkdir(parents=True)
    path.write_text(content)
    with pytest.raises(RuntimeError, match="fix or move it first"):
        I.install("cursor")
    assert path.read_text() == content
    assert list(path.parent.iterdir()) == [path]  # no backup or partial write


def test_force_replaces_existing_entry(sandbox_home):
    I.install("cursor")
    I.install("cursor", command="/opt/new", force=True)
    cfg = json.loads(_cursor(sandbox_home).read_text())
    assert cfg["mcpServers"]["paperboy"]["command"] == "/opt/new"


# --- uninstall -----------------------------------------------------------------------------


def test_uninstall_without_config_file(sandbox_home):
    res = I.uninstall("cursor")
    assert res["action"] == "noop" and res["reason"] == "no config file"


def test_uninstall_keeps_other_servers_and_backs_up(sandbox_home):
    I.install("cursor")
    I.install("cursor", name="other")
    res = I.uninstall("cursor")
    cfg = json.loads(_cursor(sandbox_home).read_text())
    assert list(cfg["mcpServers"]) == ["other"]
    assert res["backup"]


def test_uninstall_unknown_client(sandbox_home):
    with pytest.raises(RuntimeError, match="Unknown client"):
        I.uninstall("notaclient")


def test_uninstall_unusable_config(sandbox_home):
    path = _cursor(sandbox_home)
    path.parent.mkdir(parents=True)
    path.write_text("[]")
    with pytest.raises(RuntimeError, match="fix or move it first"):
        I.uninstall("cursor")


# --- Claude Code via its CLI (a recording stand-in for `claude`) -------------------------


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    def make(exit_code=0, stdout="Added", stderr=""):
        bin_dir = tmp_path / "fakebin"
        bin_dir.mkdir(exist_ok=True)
        script = bin_dir / "claude"
        script.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "$@" > "{bin_dir}/args.txt"\n'
            f'echo "{stdout}"\necho "{stderr}" >&2\nexit {exit_code}\n')
        script.chmod(0o755)
        monkeypatch.setenv("PATH", str(bin_dir))
        return lambda: (bin_dir / "args.txt").read_text().splitlines()
    return make


def test_claude_code_install_runs_mcp_add(sandbox_home, fake_claude):
    args = fake_claude()
    res = I.install("claude-code", root=str(sandbox_home / "docs"), force=True)
    assert res["action"] == "installed" and res["method"] == "claude mcp add"
    assert args() == ["mcp", "add", "paperboy", "--force",
                      "-e", f"PAPERBOY_ROOT={sandbox_home / 'docs'}",
                      "--", sys.executable, "-m", "paperboy"]


def test_claude_code_install_failure(sandbox_home, fake_claude):
    fake_claude(exit_code=1, stdout="", stderr="already exists")
    with pytest.raises(RuntimeError, match="claude mcp add` failed: already exists"):
        I.install("claude-code")


def test_claude_code_missing_cli_explains_manual_command(sandbox_home, monkeypatch):
    monkeypatch.setenv("PATH", str(sandbox_home / "empty"))
    with pytest.raises(RuntimeError) as exc:
        I.install("claude-code", use_uvx=True)
    assert "claude mcp add paperboy -- uvx --from paperboy-mcp paperboy" in str(exc.value)


def test_claude_code_uninstall(sandbox_home, fake_claude):
    args = fake_claude()
    assert I.uninstall("claude-code", name="pb")["action"] == "removed"
    assert args() == ["mcp", "remove", "pb"]


def test_claude_code_uninstall_failure(sandbox_home, fake_claude):
    fake_claude(exit_code=1, stdout="", stderr="not found")
    with pytest.raises(RuntimeError, match="claude mcp remove` failed: not found"):
        I.uninstall("claude-code")


def test_claude_code_uninstall_missing_cli(sandbox_home, monkeypatch):
    monkeypatch.setenv("PATH", str(sandbox_home / "empty"))
    with pytest.raises(RuntimeError, match="`claude` CLI was not found"):
        I.uninstall("claude-code")


def test_config_snippet_shape(sandbox_home):
    snip = json.loads(I.config_snippet(name="pdf", use_uvx=False, command=None, root=None))
    assert snip == {"mcpServers": {"pdf": {"command": sys.executable, "args": ["-m", "paperboy"]}}}
