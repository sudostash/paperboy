"""Record tutorial videos of Paperboy features.

For each scenario this:
  1. builds fresh sample PDFs in ``demos/out/<slug>/files``;
  2. runs the scenario's tool calls against the real server over MCP stdio
     (the same path Claude Desktop uses), confined to that workspace;
  3. plays the transcript back in ``viewer/index.html`` and records it with
     Playwright to ``demos/out/videos/<slug>.webm``.

Usage:
    pip install -e ".[demo]" && playwright install chromium
    python demos/record.py                     # every scenario
    python demos/record.py tool-annotations    # just one
    python demos/record.py --list
    python demos/record.py --preview tool-annotations   # play in your browser, no video
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import functools
import json
import os
import posixpath
import shutil
import sys
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

DEMOS = Path(__file__).resolve().parent
SRC = DEMOS.parent / "src"
OUT = DEMOS / "out"
VIEWPORT = {"width": 1280, "height": 720}
DISPLAY_ROOT = "~/Documents"

sys.path.insert(0, str(DEMOS))
from demo_pdfs import build  # noqa: E402
from scenarios import SCENARIOS  # noqa: E402


def _field(obj, snake: str, camel: str, default=None):
    """Read a result field under its mcp 2.x (snake_case) or 1.x (camelCase) name."""
    return getattr(obj, snake, getattr(obj, camel, default))


def _payload(result) -> object:
    structured = _field(result, "structured_content", "structuredContent")
    if isinstance(structured, dict):
        return structured.get("result", structured)
    text = result.content[0].text if result.content else ""
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


def _hint(ann, alias: str, field: str):
    if ann is None:
        return None
    return ann.model_dump(by_alias=True).get(alias, getattr(ann, field, None))


async def run_scenario(slug: str, scenario: dict, out: Path = OUT) -> dict:
    """Execute the scenario's steps against the server; return the transcript."""
    workspace = out / slug / "files"
    shutil.rmtree(workspace.parent, ignore_errors=True)
    workspace.mkdir(parents=True)
    build(scenario["files"], workspace)

    env = dict(os.environ, PAPERBOY_ROOT=str(workspace),
               PYTHONPATH=str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    params = StdioServerParameters(command=sys.executable, args=["-m", "paperboy"],
                                   env=env, cwd=str(workspace))
    steps = []
    failure = None
    pending: list[dict] = []   # what the user types on the page Paperboy opens
    asked: list[dict] = []     # what it asked: shown as a dialog (never the password)

    async def answer(context, params):
        # The client opens Paperboy's local page (URL-mode elicitation); the user types
        # the password there, so it reaches Paperboy without passing through the client.
        import http.client
        import urllib.parse

        import anyio
        from mcp import types
        reply = pending.pop(0)
        parts = urllib.parse.urlsplit(params.url)

        def post():
            conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
            conn.request("POST", parts.path, urllib.parse.urlencode(reply),
                         {"Content-Type": "application/x-www-form-urlencoded"})
            conn.getresponse().read()
            conn.close()
        await anyio.to_thread.run_sync(post)
        asked.append({"message": params.message, "host": parts.netloc,
                      "new": "confirm" in reply})
        return types.ElicitResult(action="accept")

    async with stdio_client(params) as (r, w), \
            ClientSession(r, w, elicitation_callback=answer) as session:
        await session.initialize()
        for step in scenario["steps"]:
            if failure:
                break
            step = dict(step)
            if step["type"] == "tools":
                listed = await session.list_tools()
                step["tools"] = [{
                    "name": t.name,
                    "title": _hint(t.annotations, "title", "title") or t.name,
                    "readOnly": bool(_hint(t.annotations, "readOnlyHint", "read_only_hint")),
                } for t in listed.tools]
            elif step["type"] == "call":
                pending[:] = step.pop("answers", [])
                asked.clear()
                result = await session.call_tool(step["tool"], step["args"])
                step["asked"] = list(asked)
                step["isError"] = bool(_field(result, "is_error", "isError", False))
                step["result"] = _payload(result)
                tool = next(t for t in (await session.list_tools()).tools
                            if t.name == step["tool"])
                step["readOnly"] = bool(_hint(tool.annotations, "readOnlyHint",
                                              "read_only_hint"))
                step["title"] = _hint(tool.annotations, "title", "title") or step["tool"]
                step["schema"] = _field(tool, "output_schema", "outputSchema")
                # Images the tool returned (view_pages): saved so the viewer can show
                # exactly what the assistant received.
                step["images"] = []
                for k, item in enumerate(c for c in result.content if c.type == "image"):
                    name = f"images/step{len(steps)}-{k + 1}.png"
                    (out / slug / name).parent.mkdir(exist_ok=True)
                    (out / slug / name).write_bytes(base64.b64decode(item.data))
                    step["images"].append(name)
                if step["isError"]:
                    failure = f"{slug}: {step['tool']} failed: {step['result']}"
            steps.append(step)
    # Raised outside the session so it isn't wrapped in the client's ExceptionGroup.
    if failure:
        raise RuntimeError(failure)

    transcript = {k: v for k, v in scenario.items() if k != "steps"}
    transcript.update(slug=slug, steps=steps, root=DISPLAY_ROOT)
    # Show tidy, machine-independent paths in the video.
    text = json.dumps(transcript, indent=2).replace(str(workspace), DISPLAY_ROOT)
    (out / slug / "script.json").write_text(text)
    return json.loads(text)


class _DemoHandler(SimpleHTTPRequestHandler):
    """Serve only ``/viewer/`` (from the repo) and ``/out/`` (from *out*); 404 otherwise."""

    def __init__(self, *args, out: Path, **kwargs):
        self.out = out
        super().__init__(*args, directory=str(DEMOS), **kwargs)

    def translate_path(self, path: str) -> str:
        # Normalize before routing so "/viewer/../x" cannot reach outside the two trees.
        route = posixpath.normpath(unquote(urlsplit(path).path))
        if route.startswith("/out/"):
            self.directory = str(self.out)
            return super().translate_path(route[len("/out"):])
        if route.startswith("/viewer/"):
            return super().translate_path(route)
        return ""  # send_head() answers 404 for an empty path

    def log_message(self, *args) -> None:
        pass


def _serve(out: Path = OUT) -> ThreadingHTTPServer:
    """Start a localhost-only server for the viewer on a free port."""
    handler = functools.partial(_DemoHandler, out=out)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


async def record(slugs: list[str], headed: bool, out: Path = OUT, speed: int = 1) -> list[Path]:
    """Record each scenario to ``<out>/videos/<slug>.webm``; return the video paths.

    ``speed`` > 1 plays the viewer faster (for tests); recordings use 1.
    """
    from playwright.async_api import async_playwright  # optional: the [demo] extra

    videos = out / "videos"
    videos.mkdir(parents=True, exist_ok=True)
    server = _serve(out)
    port = server.server_address[1]
    recorded = []
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=not headed)
            for slug in slugs:
                await run_scenario(slug, SCENARIOS[slug], out)
                raw = out / slug / "raw"
                context = await browser.new_context(
                    viewport=VIEWPORT, device_scale_factor=1,
                    record_video_dir=str(raw), record_video_size=VIEWPORT,
                )
                page = await context.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
                await page.goto(f"http://127.0.0.1:{port}/viewer/index.html?s={slug}&speed={speed}")
                await page.wait_for_function("window.__demoDone || window.__demoError",
                                             timeout=180_000)
                await context.close()
                if errors:
                    raise RuntimeError(f"{slug}: viewer errors: {errors}")
                target = videos / f"{slug}.webm"
                await page.video.save_as(str(target))
                shutil.rmtree(raw, ignore_errors=True)
                recorded.append(target)
                print(f"recorded {target}")
            await browser.close()
    finally:
        server.shutdown()
    return recorded


def _wait_forever() -> None:
    threading.Event().wait()


def preview(slug: str, out: Path = OUT) -> None:
    """Run the scenario, then serve the viewer and open it in the default browser."""
    asyncio.run(run_scenario(slug, SCENARIOS[slug], out))
    server = _serve(out)
    url = f"http://127.0.0.1:{server.server_address[1]}/viewer/index.html?s={slug}"
    print(f"Playing {slug} at {url} (reload to replay, Ctrl+C to stop)")
    webbrowser.open(url)
    try:
        _wait_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("slugs", nargs="*", help="scenarios to record (default: all)")
    parser.add_argument("--headed", action="store_true", help="show the browser")
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    parser.add_argument("--preview", action="store_true",
                        help="play one scenario in your browser instead of recording")
    args = parser.parse_args()
    if args.list:
        for slug, s in SCENARIOS.items():
            print(f"{slug:24} {s['title']}")
        return
    unknown = set(args.slugs) - SCENARIOS.keys()
    if unknown:
        parser.error(f"unknown scenario(s): {', '.join(sorted(unknown))}")
    if args.preview:
        if len(args.slugs) != 1:
            parser.error("--preview takes exactly one scenario")
        preview(args.slugs[0])
        return
    asyncio.run(record(args.slugs or list(SCENARIOS), args.headed))


if __name__ == "__main__":
    main()
