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
`tests/test_demos.py` runs every scenario's tool calls in CI so they cannot silently break.

## Usage

```bash
pip install -e ".[demo]"
playwright install chromium

python demos/record.py --list               # available scenarios
python demos/record.py                      # record all
python demos/record.py tool-annotations     # record one
python demos/record.py tool-annotations --headed
python demos/record.py --preview tool-annotations   # play in your browser, no video
```

Don't open `viewer/index.html` directly: browsers block its scripts on `file://`, and it needs a
recorded transcript plus a `?s=<scenario>` parameter. `--preview` sets all of that up. To just
watch a finished recording, open `out/videos/<scenario>.webm` in a browser.

The viewer loads pdf.js from cdn.jsdelivr.net, so recording needs network access.
`demos/out/` is gitignored; videos are generated locally, not committed.

## Adding a scenario

Add an entry to `SCENARIOS` in `scenarios.py` (see the docstring for step types). If it needs a
new sample file, add a builder to `BUILDERS` in `demo_pdfs.py`.
