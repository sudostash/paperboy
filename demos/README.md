# Demo videos

Short, reproducible tutorial videos for each Paperboy feature, for guides and docs.

## How it works

1. `demo_pdfs.py` builds deterministic sample PDFs.
2. `record.py` runs a scenario's tool calls against the **real server over MCP stdio**,
   confined to a fresh workspace (`PAPERBOY_ROOT`), and saves the transcript to
   `out/<scenario>/script.json`. Workspace paths are shown as `~/Documents`.
3. `viewer/` replays the transcript: the prompt types out, each tool call appears with its
   read-only / asks-first badge (and an approval prompt for tools that write), and the stage
   renders before/after pages with pdf.js.
4. Playwright records the viewer at 1280×720 to `out/videos/<scenario>.webm`.

Nothing in a video is mocked: if a tool's behavior changes, re-recording reflects it.
`tests/test_demos.py` covers the harness: sample PDFs, scenario shape, every scenario's tool
calls against the real server, the localhost server's path restrictions, the CLI, and (with the
`[demo]` extra and Chromium) full viewer playback and video recording. The browser tests skip
without Chromium; the `demos` CI job installs it so they always run there.

## Usage

```bash
pip install -e ".[demo]"
playwright install chromium

python demos/record.py --list               # available scenarios
python demos/record.py                      # record all
python demos/record.py tool-annotations     # record one
python demos/record.py tool-annotations --headed
python demos/record.py --preview tool-annotations   # play in your browser, no video
python demos/record.py --promo              # the product video
python demos/record.py --promo --preview    # play it in your browser
```

Don't open `viewer/index.html` directly: browsers block its scripts on `file://`, and it needs a
recorded transcript plus a `?s=<scenario>` parameter. `--preview` sets all of that up. To just
watch a finished recording, open `out/videos/<scenario>.webm` in a browser.

The viewer loads pdf.js from cdn.jsdelivr.net, so recording needs network access.
`demos/out/` is gitignored; videos are generated locally, not committed.

## The product video

`python demos/record.py --promo` records `out/videos/paperboy-promo.webm`, a short
marketing video in the style of a newspaper front page: title cards (`viewer/promo.html`)
around a reel of real scenarios, each played back faster in the demo viewer. Its copy and
the scenarios it shows are `PROMO` in `scenarios.py`; the tool count is read from the
server when it's recorded. Keep every claim in `PROMO` true of the server.

To share it where WebM isn't accepted, convert it, e.g.
`ffmpeg -i paperboy-promo.webm -c:v libx264 -crf 18 -pix_fmt yuv420p paperboy-promo.mp4`.

## Adding a scenario

Add an entry to `SCENARIOS` in `scenarios.py` (see the docstring for step types). If it needs a
new sample file, add a builder to `BUILDERS` in `demo_pdfs.py`. Then add a guide to
[docs/tutorials.md](../docs/tutorials.md) that quotes its prompt and names its video;
`tests/test_demos.py` checks that every scenario has one.
