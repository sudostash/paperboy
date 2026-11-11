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
    python demos/record.py --promo             # the product video
    python demos/record.py --promo --preview
    python demos/record.py --gallery           # browse every demo in your browser
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
from scenarios import GROUPS, PROMO, SCENARIOS  # noqa: E402

PROMO_VIDEO = "paperboy-promo.webm"


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


def _server(workspace: Path) -> StdioServerParameters:
    """Launch this checkout's server over stdio, confined to *workspace*."""
    env = dict(os.environ, PAPERBOY_ROOT=str(workspace),
               PYTHONPATH=str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    return StdioServerParameters(command=sys.executable, args=["-m", "paperboy"],
                                 env=env, cwd=str(workspace))


async def run_scenario(slug: str, scenario: dict, out: Path = OUT) -> dict:
    """Execute the scenario's steps against the server; return the transcript."""
    workspace = out / slug / "files"
    shutil.rmtree(workspace.parent, ignore_errors=True)
    workspace.mkdir(parents=True)
    build(scenario["files"], workspace)

    params = _server(workspace)
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
                # A step can show a refusal on purpose (expectError); any other is a bug.
                if step["isError"] != bool(step.get("expectError")):
                    failure = (f"{slug}: {step['tool']} failed: {step['result']}"
                               if step["isError"] else
                               f"{slug}: {step['tool']} was expected to be refused")
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


async def run_promo(out: Path = OUT, promo: dict = PROMO, scenes: bool = True) -> dict:
    """Run the scenarios the product video shows (unless *scenes* is false: they have
    just been run), and write its script with the server's real tool count."""
    workspace = out / "promo"
    shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir(parents=True)
    async with stdio_client(_server(workspace)) as (r, w), ClientSession(r, w) as session:
        await session.initialize()
        tools = len((await session.list_tools()).tools)
    for item in promo["features"] if scenes else []:
        await run_scenario(item["slug"], SCENARIOS[item["slug"]], out)
    script = dict(promo, tools=tools)
    (workspace / "script.json").write_text(json.dumps(script, indent=2))
    return script


def write_catalog(out: Path = OUT) -> Path:
    """Write ``<out>/catalog.js``: the demos the gallery (viewer/index.html) lists, and
    which of them have a video to watch or a transcript to replay live.

    A script rather than JSON, so the gallery can load it from a file:// page too."""
    videos = out / "videos"

    def entry(slug: str) -> dict:
        return {"slug": slug, "title": SCENARIOS[slug]["title"],
                "subtitle": SCENARIOS[slug]["subtitle"],
                "video": (videos / f"{slug}.webm").is_file(),
                "live": (out / slug / "script.json").is_file()}

    catalog = {
        "promo": {"title": "Product video",
                  "subtitle": f"{PROMO['tagline']}: a short tour of what it does.",
                  "file": PROMO_VIDEO, "video": (videos / PROMO_VIDEO).is_file(),
                  "live": (out / "promo" / "script.json").is_file()},
        "groups": [{"name": name, "demos": [entry(slug) for slug in slugs]}
                   for name, slugs in GROUPS],
    }
    out.mkdir(parents=True, exist_ok=True)
    path = out / "catalog.js"
    path.write_text("// Written by demos/record.py: the demos the gallery lists.\n"
                    f"window.PAPERBOY_DEMOS = {json.dumps(catalog, indent=2)};\n")
    return path


async def prepare_all(out: Path = OUT) -> list[str]:
    """Run every scenario, so each can be replayed live, and the product video's script.
    Returns the scenarios that couldn't run here (say, without Tesseract), which the
    gallery then offers only as videos."""
    skipped = []
    for slug in SCENARIOS:
        try:
            await run_scenario(slug, SCENARIOS[slug], out)
        except Exception as exc:  # noqa: BLE001 - one demo mustn't stop the rest
            shutil.rmtree(out / slug, ignore_errors=True)
            skipped.append(slug)
            print(f"skipped {slug}: {exc}")
    await run_promo(out, scenes=False)
    return skipped


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


async def _capture(browser, url: str, raw: Path, target: Path, name: str,
                   timeout: int = 180_000) -> None:
    """Record the page at *url* until it signals done, and save the video to *target*."""
    context = await browser.new_context(
        viewport=VIEWPORT, device_scale_factor=1,
        record_video_dir=str(raw), record_video_size=VIEWPORT,
    )
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    await page.goto(url)
    await page.wait_for_function("window.__demoDone || window.__demoError", timeout=timeout)
    failed = await page.evaluate("window.__demoError || null")
    await context.close()
    if errors or failed:
        raise RuntimeError(f"{name}: viewer errors: {errors or [failed]}")
    await page.video.save_as(str(target))
    shutil.rmtree(raw, ignore_errors=True)
    print(f"recorded {target}")


async def record(slugs: list[str], headed: bool, out: Path = OUT, speed: int = 1,
                 promo: bool = False) -> list[Path]:
    """Record each scenario to ``<out>/videos/<slug>.webm``, and with *promo* the product
    video to ``<out>/videos/paperboy-promo.webm``; return the video paths.

    ``speed`` > 1 plays the viewer faster (for tests); recordings use 1.
    """
    from playwright.async_api import async_playwright  # optional: the [demo] extra

    videos = out / "videos"
    videos.mkdir(parents=True, exist_ok=True)
    server = _serve(out)
    viewer = f"http://127.0.0.1:{server.server_address[1]}/viewer"
    recorded = []
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=not headed)
            for slug in slugs:
                await run_scenario(slug, SCENARIOS[slug], out)
                target = videos / f"{slug}.webm"
                await _capture(browser, f"{viewer}/index.html?s={slug}&speed={speed}&embed=1",
                               out / slug / "raw", target, slug)
                recorded.append(target)
            if promo:
                await run_promo(out)
                target = videos / PROMO_VIDEO
                await _capture(browser, f"{viewer}/promo.html?speed={speed}",
                               out / "promo" / "raw", target, "promo", timeout=600_000)
                recorded.append(target)
            await browser.close()
    finally:
        server.shutdown()
        write_catalog(out)  # list whatever was recorded, even if a later one failed
    return recorded


def _wait_forever() -> None:
    threading.Event().wait()


def preview(slug: str | None, out: Path = OUT) -> None:
    """Run the scenario (or, for None, the product video's scenarios), then serve the
    viewer and open it in the default browser."""
    if slug is None:
        asyncio.run(run_promo(out))
        page, slug = "promo.html", "the product video"
    else:
        asyncio.run(run_scenario(slug, SCENARIOS[slug], out))
        page = f"index.html?s={slug}"
    write_catalog(out)  # so "All demos" lists what's here
    server = _serve(out)
    url = f"http://127.0.0.1:{server.server_address[1]}/viewer/{page}"
    print(f"Playing {slug} at {url} (reload to replay, Ctrl+C to stop)")
    webbrowser.open(url)
    try:
        _wait_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()


def gallery(out: Path = OUT) -> None:
    """Prepare every demo, then open the gallery in the default browser."""
    print("Preparing every demo against the real server (under a minute)...")
    asyncio.run(prepare_all(out))
    write_catalog(out)
    server = _serve(out)
    url = f"http://127.0.0.1:{server.server_address[1]}/viewer/index.html"
    print(f"Gallery at {url} (Ctrl+C to stop)")
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
    parser.add_argument("--promo", action="store_true",
                        help="the product video: a reel of scenarios between title cards")
    parser.add_argument("--gallery", action="store_true",
                        help="prepare every demo and browse them in your browser")
    args = parser.parse_args()
    if args.list:
        for slug, s in SCENARIOS.items():
            print(f"{slug:24} {s['title']}")
        return
    unknown = set(args.slugs) - SCENARIOS.keys()
    if unknown:
        parser.error(f"unknown scenario(s): {', '.join(sorted(unknown))}")
    if args.gallery:
        if args.slugs or args.promo or args.preview:
            parser.error("--gallery takes no other options")
        gallery()
        return
    if args.promo:
        if args.slugs:
            parser.error("--promo takes no scenarios")
        if args.preview:
            preview(None)
        else:
            asyncio.run(record([], args.headed, promo=True))
        return
    if args.preview:
        if len(args.slugs) != 1:
            parser.error("--preview takes exactly one scenario")
        preview(args.slugs[0])
        return
    asyncio.run(record(args.slugs or list(SCENARIOS), args.headed))


if __name__ == "__main__":
    main()
