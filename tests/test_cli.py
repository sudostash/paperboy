"""Command-line entry point: subcommand dispatch, output, and exit codes."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from paperboy import __version__, cli
from paperboy import installer as I

SRC = str(Path(__file__).resolve().parent.parent / "src")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData/Roaming"))
    monkeypatch.setattr(I.shutil, "which", lambda name: None)  # no `claude` CLI
    return tmp_path


def _run(argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    return exc.value.code


def test_version(capsys):
    assert _run(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"paperboy {__version__}"


@pytest.mark.parametrize("argv", [[], ["serve"]])
def test_default_and_serve_start_the_server(monkeypatch, argv):
    import paperboy.server as server
    started = []
    monkeypatch.setattr(server, "serve", lambda: started.append(True))
    cli.main(argv)
    assert started == [True]


def test_list_clients(home, capsys):
    (home / ".cursor").mkdir()
    (home / ".cursor/mcp.json").write_text("{}")
    cli.main(["list-clients"])
    lines = {line.split()[0]: line for line in capsys.readouterr().out.splitlines()}
    assert set(lines) == {"claude-desktop", "cursor", "windsurf", "claude-code"}
    assert "detected" in lines["cursor"] and "not found" not in lines["cursor"]
    assert "not found" in lines["windsurf"]
    assert "(via `claude` CLI)" in lines["claude-code"]


def test_config_prints_uvx_snippet_by_default(capsys):
    cli.main(["config", "--root", "/data", "--name", "pdf"])
    snippet = json.loads(capsys.readouterr().out)
    entry = snippet["mcpServers"]["pdf"]
    assert entry["command"] == "uvx"
    assert entry["env"] == {"PAPERBOY_ROOT": "/data"}


def test_config_no_uvx(capsys):
    cli.main(["config", "--no-uvx"])
    entry = json.loads(capsys.readouterr().out)["mcpServers"]["paperboy"]
    assert entry == {"command": sys.executable, "args": ["-m", "paperboy"]}


def test_install_requires_a_client(home, capsys):
    assert _run(["install"]) == 2
    assert "Specify a client" in capsys.readouterr().err


def test_install_one_client(home, capsys):
    assert _run(["install", "cursor", "--root", str(home / "docs")]) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["action"] == "installed"
    assert "Tip:" not in out.err
    cfg = json.loads((home / ".cursor/mcp.json").read_text())
    assert cfg["mcpServers"]["paperboy"]["env"]["PAPERBOY_ROOT"] == str(home / "docs")


def test_install_without_root_suggests_one(home, capsys):
    assert _run(["install", "cursor"]) == 0
    assert "pass --root" in capsys.readouterr().err


def test_install_passes_options_through(home):
    assert _run(["install", "windsurf", "--name", "pdf", "--command", "/opt/pb"]) == 0
    cfg = json.loads((home / ".codeium/windsurf/mcp_config.json").read_text())
    assert cfg["mcpServers"]["pdf"] == {"command": "/opt/pb", "args": []}


def test_install_uvx_flag(home):
    _run(["install", "cursor", "--uvx"])
    assert json.loads((home / ".cursor/mcp.json").read_text())[
        "mcpServers"]["paperboy"]["command"] == "uvx"


def test_install_all_targets_every_detected_client(home, capsys):
    # README: `paperboy install all --root ~/Documents   # every detected client`
    (home / ".cursor").mkdir()
    (home / ".cursor/mcp.json").write_text("{}")
    assert _run(["install", "all", "--root", str(home)]) == 0
    assert "paperboy" in json.loads((home / ".cursor/mcp.json").read_text())["mcpServers"]
    assert not (home / ".codeium").exists()           # not detected: left alone
    assert json.loads(capsys.readouterr().out)["client"] == "cursor"


def test_install_all_with_nothing_detected(home, capsys):
    assert _run(["install", "all"]) == 1
    assert "No MCP clients detected" in capsys.readouterr().err


def test_install_unknown_client(home, capsys):
    assert _run(["install", "notepad"]) == 2
    assert "Unknown client 'notepad'" in capsys.readouterr().err


def test_install_failure_exits_nonzero(home, capsys):
    _run(["install", "cursor"])
    assert _run(["install", "cursor"]) == 1  # already installed, no --force
    assert "[cursor] " in capsys.readouterr().err


def test_install_force(home):
    _run(["install", "cursor"])
    assert _run(["install", "cursor", "--force", "--command", "/x"]) == 0


def test_uninstall_one_client(home, capsys):
    _run(["install", "cursor"])
    capsys.readouterr()
    assert _run(["uninstall", "cursor"]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "removed"


def test_uninstall_all_reports_each_client(home, capsys):
    # claude-code fails (no CLI); the JSON clients are no-ops, so overall exit is 1.
    assert _run(["uninstall"]) == 1
    out = capsys.readouterr()
    assert out.out.count('"action": "noop"') == 3
    assert "[claude-code]" in out.err


def test_uninstall_unknown_client(home):
    assert _run(["uninstall", "notepad"]) == 2


def test_python_dash_m_entry_point():
    env = dict(os.environ, PYTHONPATH=SRC)
    proc = subprocess.run([sys.executable, "-m", "paperboy", "--version"],
                          capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 0
    assert proc.stdout.strip() == f"paperboy {__version__}"
