"""Write this server into MCP client config files (the one-command install).

Supported clients share the same ``mcpServers`` config shape:

    {"mcpServers": {"<name>": {"command": "...", "args": [...], "env": {...}}}}

Claude Code is handled through its own ``claude mcp add`` CLI when present.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import CLI_COMMAND, DIST_NAME, SERVER_NAME


@dataclass(frozen=True)
class Client:
    key: str
    label: str
    # Path to the JSON config per platform; None where unsupported.
    paths: dict[str, Path]
    uses_cli: bool = False  # Claude Code: configured via its own CLI instead of JSON

    def config_path(self) -> Path | None:
        return self.paths.get(sys.platform)


def _home() -> Path:
    return Path.home()


def _clients() -> dict[str, Client]:
    home = _home()
    appdata = Path(os.environ.get("APPDATA", home / "AppData/Roaming"))
    return {
        "claude-desktop": Client(
            "claude-desktop", "Claude Desktop",
            {
                "darwin": home / "Library/Application Support/Claude/claude_desktop_config.json",
                "win32": appdata / "Claude/claude_desktop_config.json",
                "linux": home / ".config/Claude/claude_desktop_config.json",
            },
        ),
        "cursor": Client(
            "cursor", "Cursor",
            {p: home / ".cursor/mcp.json" for p in ("darwin", "win32", "linux")},
        ),
        "windsurf": Client(
            "windsurf", "Windsurf",
            {p: home / ".codeium/windsurf/mcp_config.json"
             for p in ("darwin", "win32", "linux")},
        ),
        "claude-code": Client(
            "claude-code", "Claude Code", {}, uses_cli=True,
        ),
    }


def server_command(use_uvx: bool, command: str | None, root: str | None) -> dict:
    """Build the config entry describing how a client should launch this server."""
    if command:
        cmd, args = command, []
    elif use_uvx:
        cmd, args = "uvx", ["--from", DIST_NAME, CLI_COMMAND]
    else:
        # The exact interpreter this package is installed in — most robust for pip.
        cmd, args = sys.executable, ["-m", "paperboy"]
    entry: dict = {"command": cmd, "args": args}
    if root:
        entry["env"] = {"PAPERBOY_ROOT": str(Path(root).expanduser())}
    return entry


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{path} contains invalid JSON; fix or move it first: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        raise RuntimeError(
            f"{path} is not an MCP config (expected an object with an \"mcpServers\" "
            "object); fix or move it first"
        )
    return data


def _backup(path: Path) -> Path | None:
    if path.exists():
        bak = path.with_suffix(path.suffix + f".bak.{int(time.time())}")
        shutil.copy2(path, bak)
        return bak
    return None


def list_clients() -> list[dict]:
    """Report each known client, whether it is detected, and its config path."""
    out = []
    for c in _clients().values():
        if c.uses_cli:
            detected = shutil.which("claude") is not None
            out.append({"client": c.key, "label": c.label, "method": "cli",
                        "detected": detected, "path": None})
        else:
            p = c.config_path()
            out.append({"client": c.key, "label": c.label, "method": "json",
                        "detected": bool(p and p.exists()), "path": str(p) if p else None})
    return out


def install(
    client_key: str,
    *,
    name: str = SERVER_NAME,
    use_uvx: bool = False,
    command: str | None = None,
    root: str | None = None,
    force: bool = False,
) -> dict:
    clients = _clients()
    if client_key not in clients:
        raise RuntimeError(f"Unknown client {client_key!r}. Known: {', '.join(clients)}")
    client = clients[client_key]
    entry = server_command(use_uvx, command, root)

    if client.uses_cli:
        return _install_claude_code(name, entry, force)

    path = client.config_path()
    if path is None:
        raise RuntimeError(f"{client.label} is not supported on this platform ({sys.platform})")
    data = _read_json(path)
    servers = data.setdefault("mcpServers", {})
    if name in servers and not force:
        raise RuntimeError(
            f"{client.label} already has an MCP server named {name!r}. "
            "Pass force=True to overwrite, or choose another --name."
        )
    backup = _backup(path)
    servers[name] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {"client": client.key, "action": "installed", "name": name,
            "path": str(path), "backup": str(backup) if backup else None, "entry": entry}


def _install_claude_code(name: str, entry: dict, force: bool) -> dict:
    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError(
            "The `claude` CLI was not found. Install Claude Code, or run:\n"
            f"  claude mcp add {name} -- {entry['command']} {' '.join(entry['args'])}"
        )
    cmd = [claude, "mcp", "add", name]
    if force:
        cmd += ["--force"]
    for k, v in (entry.get("env") or {}).items():
        cmd += ["-e", f"{k}={v}"]
    cmd += ["--", entry["command"], *entry["args"]]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"`claude mcp add` failed: {(proc.stderr or proc.stdout).strip()}")
    return {"client": "claude-code", "action": "installed", "name": name,
            "method": "claude mcp add", "output": proc.stdout.strip()}


def uninstall(client_key: str, *, name: str = SERVER_NAME) -> dict:
    clients = _clients()
    if client_key not in clients:
        raise RuntimeError(f"Unknown client {client_key!r}. Known: {', '.join(clients)}")
    client = clients[client_key]

    if client.uses_cli:
        claude = shutil.which("claude")
        if not claude:
            raise RuntimeError("The `claude` CLI was not found.")
        proc = subprocess.run([claude, "mcp", "remove", name], capture_output=True, text=True)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            raise RuntimeError(f"`claude mcp remove` failed: {detail}")
        return {"client": "claude-code", "action": "removed", "name": name}

    path = client.config_path()
    if not path or not path.exists():
        return {"client": client.key, "action": "noop", "reason": "no config file", "name": name}
    data = _read_json(path)
    servers = data.get("mcpServers", {})
    if name not in servers:
        return {"client": client.key, "action": "noop", "reason": "not installed", "name": name}
    backup = _backup(path)
    del servers[name]
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {"client": client.key, "action": "removed", "name": name,
            "path": str(path), "backup": str(backup) if backup else None}


def config_snippet(*, name: str = SERVER_NAME, use_uvx: bool = True,
                   command: str | None = None, root: str | None = None) -> str:
    """Return a copy-pasteable mcpServers JSON snippet for any client."""
    entry = server_command(use_uvx, command, root)
    return json.dumps({"mcpServers": {name: entry}}, indent=2)
