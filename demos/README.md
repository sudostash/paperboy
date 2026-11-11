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

python demos/record.py --gallery            # browse every demo in your browser
python demos/record.py --list               # available scenarios
python demos/record.py                      # record all
python demos/record.py tool-annotations     # record one
python demos/record.py tool-annotations --headed
python demos/record.py --preview tool-annotations   # play in your browser, no video
python demos/record.py --promo              # the product video
python demos/record.py --promo --preview    # play it in your browser
```

### Browsing the demos

`viewer/index.html` without a scenario is a gallery of every demo, grouped like
[docs/tutorials.md](../docs/tutorials.md), with the product video first. Each card has
**Watch video** (the recording, played on the page) and **Play live** (the transcript replayed
in the viewer), for whichever exist; a playing demo links back with **All demos**.

- `python demos/record.py --gallery` runs every scenario against the real server (under a
  minute; any that can't run here, say without Tesseract, are listed as skipped), then
  serves the gallery and opens it in your browser, with both buttons on every card.
- After recording, you can also just open `viewer/index.html` from your file manager. Browsers
  don't run the live replay on a `file://` page, so it offers the videos only.

The gallery's list is `out/catalog.js`, which `record.py` rewrites whenever it records,
previews or opens the gallery.

The viewer loads pdf.js from cdn.jsdelivr.net, so recording needs network access.

## Publishing the videos

`demos/out/` is gitignored: committed videos would grow the repository by ~90 MB every time
they're re-recorded, and ship in the source package. They're published as assets of the
[`demo-videos` release](https://github.com/sudostash/paperboy/releases/tag/demo-videos) instead, which `docs/tutorials.md` and the README link to.
After re-recording, replace them (this needs push access and the GitHub CLI):

```bash
cd demos/out/videos
ffmpeg -y -i paperboy-promo.webm -c:v libx264 -crf 18 -pix_fmt yuv420p \
  -movflags +faststart paperboy-promo.mp4      # MP4 for sharing where WebM isn't accepted
gh release upload demo-videos *.webm paperboy-promo.mp4 --clobber
```

The README's product-video poster is `docs/media/product-video.jpg`, a frame of the
video with a play button.

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
