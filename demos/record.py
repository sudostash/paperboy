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
import functools
import json
import os
import shutil
import sys
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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


def _payload(result) -> object:
    structured = getattr(result, "structuredContent", None)
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
    async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
        await session.initialize()
        for step in scenario["steps"]:
            step = dict(step)
            if step["type"] == "tools":
                listed = await session.list_tools()
                step["tools"] = [{
                    "name": t.name,
                    "title": _hint(t.annotations, "title", "title") or t.name,
                    "readOnly": bool(_hint(t.annotations, "readOnlyHint", "read_only_hint")),
                } for t in listed.tools]
            elif step["type"] == "call":
                result = await session.call_tool(step["tool"], step["args"])
                step["isError"] = bool(getattr(result, "isError", False))
                step["result"] = _payload(result)
                tool = next(t for t in (await session.list_tools()).tools
                            if t.name == step["tool"])
                step["readOnly"] = bool(_hint(tool.annotations, "readOnlyHint",
                                              "read_only_hint"))
                step["title"] = _hint(tool.annotations, "title", "title") or step["tool"]
                if step["isError"]:
                    raise RuntimeError(f"{slug}: {step['tool']} failed: {step['result']}")
            steps.append(step)

    transcript = {k: v for k, v in scenario.items() if k != "steps"}
    transcript.update(slug=slug, steps=steps, root=DISPLAY_ROOT)
    # Show tidy, machine-independent paths in the video.
    text = json.dumps(transcript, indent=2).replace(str(workspace), DISPLAY_ROOT)
    (out / slug / "script.json").write_text(text)
    return json.loads(text)


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass


def _serve() -> ThreadingHTTPServer:
    handler = functools.partial(_QuietHandler, directory=str(DEMOS))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


async def record(slugs: list[str], headed: bool) -> None:
    from playwright.async_api import async_playwright  # optional: the [demo] extra

    videos = OUT / "videos"
    videos.mkdir(parents=True, exist_ok=True)
    server = _serve()
    port = server.server_address[1]
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=not headed)
            for slug in slugs:
                await run_scenario(slug, SCENARIOS[slug])
                raw = OUT / slug / "raw"
                context = await browser.new_context(
                    viewport=VIEWPORT, device_scale_factor=1,
                    record_video_dir=str(raw), record_video_size=VIEWPORT,
                )
                page = await context.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
                await page.goto(f"http://127.0.0.1:{port}/viewer/index.html?s={slug}")
                await page.wait_for_function("window.__demoDone || window.__demoError",
                                             timeout=180_000)
                await context.close()
                if errors:
                    raise RuntimeError(f"{slug}: viewer errors: {errors}")
                target = videos / f"{slug}.webm"
                await page.video.save_as(str(target))
                shutil.rmtree(raw, ignore_errors=True)
                print(f"recorded {target.relative_to(DEMOS.parent)}")
            await browser.close()
    finally:
        server.shutdown()


def preview(slug: str) -> None:
    """Run the scenario, then serve the viewer and open it in the default browser."""
    asyncio.run(run_scenario(slug, SCENARIOS[slug]))
    server = _serve()
    url = f"http://127.0.0.1:{server.server_address[1]}/viewer/index.html?s={slug}"
    print(f"Playing {slug} at {url} (reload to replay, Ctrl+C to stop)")
    webbrowser.open(url)
    try:
        threading.Event().wait()
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
