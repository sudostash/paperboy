"""Command-line entry point.

Default behavior (no subcommand) runs the MCP server over stdio, so MCP clients
configured with the bare command keep working. Subcommands add the one-command
install experience.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import CLI_COMMAND, DISPLAY_NAME, SERVER_NAME, __version__
from . import installer as I


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=CLI_COMMAND,
        description=f"{DISPLAY_NAME} — an MCP server for PDF tools.",
    )
    p.add_argument("--version", action="version", version=f"{CLI_COMMAND} {__version__}")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("serve", help="Run the MCP server over stdio (default).")

    def add_target(sp):
        sp.add_argument("client", nargs="?", default=None,
                        help="claude-desktop | claude-code | cursor | windsurf | all")
        sp.add_argument("--name", default=SERVER_NAME,
                        help=f"Server name/key in the client config (default: {SERVER_NAME})")

    inst = sub.add_parser("install", help="Write this server into an MCP client's config.")
    add_target(inst)
    inst.add_argument("--root", default=None,
                      help="Set PAPERBOY_ROOT to confine file access to this directory.")
    inst.add_argument("--uvx", action="store_true",
                      help="Configure the client to launch via `uvx` instead of this interpreter.")
    inst.add_argument("--command", default=None,
                      help="Override the launch command entirely (advanced).")
    inst.add_argument("--force", action="store_true", help="Overwrite an existing entry.")

    un = sub.add_parser("uninstall", help="Remove this server from an MCP client's config.")
    add_target(un)

    sub.add_parser("list-clients", help="Show known MCP clients and whether they are detected.")

    cfg = sub.add_parser("config", help="Print a copy-pasteable config snippet.")
    cfg.add_argument("--name", default=SERVER_NAME)
    cfg.add_argument("--root", default=None)
    cfg.add_argument("--command", default=None)
    cfg.add_argument("--no-uvx", dest="uvx", action="store_false",
                     help="Emit the local-interpreter command instead of the uvx form.")
    return p


def _targets(client: str | None) -> list[str]:
    known = [c["client"] for c in I.list_clients()]
    if client in (None, "all"):
        return known
    if client not in known:
        print(f"Unknown client {client!r}. Known: {', '.join(known)}, all", file=sys.stderr)
        raise SystemExit(2)
    return [client]


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)

    if args.cmd in (None, "serve"):
        from .server import serve
        serve()
        return

    if args.cmd == "list-clients":
        for c in I.list_clients():
            mark = "detected" if c["detected"] else "not found"
            where = c["path"] or "(via `claude` CLI)"
            print(f"{c['client']:<16} {mark:<10} {where}")
        return

    if args.cmd == "config":
        print(I.config_snippet(name=args.name, use_uvx=args.uvx,
                               command=args.command, root=args.root))
        return

    if args.cmd == "install":
        if args.client in (None, "all"):
            print("Specify a client: claude-desktop | claude-code | cursor | windsurf | all",
                  file=sys.stderr)
            raise SystemExit(2)
        ok = True
        for target in _targets(args.client):
            try:
                res = I.install(target, name=args.name, use_uvx=args.uvx,
                                command=args.command, root=args.root, force=args.force)
                print(json.dumps(res, indent=2))
            except RuntimeError as exc:
                ok = False
                print(f"[{target}] {exc}", file=sys.stderr)
        if args.root is None:
            print("\nTip: pass --root <dir> to confine file access to one folder.",
                  file=sys.stderr)
        raise SystemExit(0 if ok else 1)

    if args.cmd == "uninstall":
        ok = True
        for target in _targets(args.client):
            try:
                print(json.dumps(I.uninstall(target, name=args.name), indent=2))
            except RuntimeError as exc:
                ok = False
                print(f"[{target}] {exc}", file=sys.stderr)
        raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
