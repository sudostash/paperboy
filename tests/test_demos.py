"""Tests for the tutorial demo harness in ``demos/``.

Scenario tool calls run against the real server over stdio. Browser tests
(viewer playback, video recording) need the ``[demo]`` extra plus Chromium and
skip cleanly without them.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp import types as mcp_types
from mcp.types import ToolAnnotations
from PIL import Image
from pypdf import PdfReader

DEMOS = Path(__file__).resolve().parent.parent / "demos"
sys.path.insert(0, str(DEMOS))

import demo_pdfs  # noqa: E402
import record  # noqa: E402
from scenarios import GROUPS, PROMO, SCENARIOS  # noqa: E402
from test_server_stdio import EXPECTED_TOOLS  # noqa: E402

STEP_TYPES = {"tools", "say", "call"}


# --------------------------------------------------------------------------- #
# demo_pdfs
# --------------------------------------------------------------------------- #


def test_build_report_has_expected_pages_text_and_metadata(tmp_path):
    path = demo_pdfs.build_report(tmp_path / "report.pdf")

    reader = PdfReader(str(path))
    assert len(reader.pages) == len(demo_pdfs.REPORT_PAGES)
    assert reader.metadata.title == "Q3 Field Report"
    for page, (title, _, bullets) in zip(reader.pages, demo_pdfs.REPORT_PAGES, strict=True):
        text = page.extract_text()
        assert title in text
        assert bullets[0].split()[0] in text


def test_build_report_is_deterministic_in_content(tmp_path):
    a = demo_pdfs.build_report(tmp_path / "a.pdf")
    b = demo_pdfs.build_report(tmp_path / "b.pdf")
    text = [[p.extract_text() for p in PdfReader(str(f)).pages] for f in (a, b)]
    assert text[0] == text[1]


def test_build_writes_every_named_file(tmp_path):
    demo_pdfs.build(list(demo_pdfs.BUILDERS), tmp_path)
    for name in demo_pdfs.BUILDERS:
        assert (tmp_path / name).is_file()


def test_build_quote_is_damaged_but_recoverable(tmp_path):
    import pikepdf

    path = demo_pdfs.build_quote(tmp_path / "quote.pdf")

    with pikepdf.open(path) as pdf:  # qpdf rebuilds the index, warning as it does
        assert len(pdf.pages) == 1
        assert any("damaged" in w for w in pdf.get_warnings())
        assert b"Quote Q-1187" in pdf.pages[0].Contents.read_bytes()


def test_build_receipts_are_photos(tmp_path):
    for build in (demo_pdfs.build_receipt_taxi, demo_pdfs.build_receipt_hotel):
        with Image.open(build(tmp_path / f"{build.__name__}.jpg")) as im:
            assert im.format == "JPEG"
            assert im.size == (600, 800)


def test_build_trip_notes_is_plain_text(tmp_path):
    text = demo_pdfs.build_trip_notes(tmp_path / "notes.txt").read_text(encoding="utf-8")
    assert text.startswith("Expense claim")
    assert "EUR 128.90" in text


def test_build_rejects_unknown_file(tmp_path):
    with pytest.raises(KeyError):
        demo_pdfs.build(["missing.pdf"], tmp_path)


# --------------------------------------------------------------------------- #
# scenarios: static shape, so a typo fails here rather than mid-recording
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("slug", sorted(SCENARIOS))
def test_scenario_is_well_formed(slug):
    s = SCENARIOS[slug]
    assert {"title", "subtitle", "files", "prompt", "steps"} <= s.keys()
    assert set(s["files"]) <= demo_pdfs.BUILDERS.keys()
    assert s["steps"], "a scenario needs at least one step"
    for step in s["steps"]:
        assert step["type"] in STEP_TYPES
        if step["type"] == "call":
            assert step["tool"] in EXPECTED_TOOLS
            assert isinstance(step["args"], dict)
            assert set(step.get("show", {})) <= {"before", "after", "password", "highlight",
                                                 "review", "layout"}
            views = [k for k in ("show", "showSchema", "showImages", "showTable",
                                 "showGrid", "showMarkdown") if step.get(k)]
            assert len(views) <= 1, f"pick one stage view, not {views}"
        if step["type"] == "say":
            assert step["text"].strip()


def test_every_tool_appears_in_a_scenario():
    # Each tool has a tutorial video showing it in use.
    used = {step["tool"] for s in SCENARIOS.values() for step in s["steps"]
            if step["type"] == "call"}
    used |= {sub["tool"] for s in SCENARIOS.values() for step in s["steps"]
             if step.get("tool") == "run_pipeline" for sub in step["args"]["steps"]}
    assert EXPECTED_TOOLS - used == set()


@pytest.mark.parametrize("slug", sorted(SCENARIOS))
def test_tutorials_cover_every_scenario(slug):
    # docs/tutorials.md quotes each scenario's request and names its video.
    doc = (DEMOS.parent / "docs" / "tutorials.md").read_text(encoding="utf-8")
    flat = re.sub(r"\s*\n>?\s*", " ", doc)  # quotes wrap over "> " lines
    assert f"Video: [`{slug}.webm`]({record.RELEASE_URL}{slug}.webm)" in doc
    assert f'*"{SCENARIOS[slug]["prompt"]}"*' in flat


def test_every_scenario_is_in_exactly_one_gallery_group():
    grouped = [slug for _, slugs in GROUPS for slug in slugs]
    assert sorted(grouped) == sorted(SCENARIOS)


def test_promo_is_well_formed():
    assert {"name", "tagline", "dateline", "hook", "ask", "clients", "playback",
            "features", "trust", "install", "url"} <= PROMO.keys()
    assert PROMO["playback"] >= 1  # the viewer never plays slower than recorded
    assert PROMO["features"]
    for item in PROMO["features"]:
        assert item["slug"] in SCENARIOS
        assert item["headline"].strip() and item["detail"].strip()
    assert len({item["slug"] for item in PROMO["features"]}) == len(PROMO["features"])
    for item in PROMO["trust"]:
        assert item["title"].strip() and item["detail"].strip()
    assert PROMO["install"][0] == "pip install paperboy-mcp"


# --------------------------------------------------------------------------- #
# record.py helpers
# --------------------------------------------------------------------------- #


def _result(structured=None, text=None):
    content = [SimpleNamespace(text=text)] if text is not None else []
    return SimpleNamespace(structuredContent=structured, content=content)


@pytest.mark.parametrize(("result", "expected"), [
    (_result(structured={"result": {"a": 1}}), {"a": 1}),
    (SimpleNamespace(structured_content={"pages": 2}, content=[]), {"pages": 2}),
    (_result(structured={"pages": 3}), {"pages": 3}),
    (_result(text='{"pages": 3}'), {"pages": 3}),
    (_result(text="plain words"), "plain words"),
    (_result(), ""),
])
def test_payload_unwraps_each_result_shape(result, expected):
    assert record._payload(result) == expected


def test_hint_reads_annotation_by_alias():
    ann = ToolAnnotations.model_validate({"title": "T", "readOnlyHint": True})
    assert record._hint(ann, "readOnlyHint", "read_only_hint") is True
    assert record._hint(ann, "title", "title") == "T"


def test_hint_is_none_without_annotations():
    assert record._hint(None, "readOnlyHint", "read_only_hint") is None


# --------------------------------------------------------------------------- #
# run_scenario: real server over stdio
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("slug", sorted(SCENARIOS))
async def test_scenario_runs_against_real_server(slug, tmp_path):
    types_a_password = any(step.get("answers") for step in SCENARIOS[slug]["steps"])
    if types_a_password and not hasattr(mcp_types, "UrlElicitationCapability"):
        pytest.skip("the client opens the password page through URL-mode elicitation")
    if slug == "ocr" and not shutil.which("tesseract"):
        pytest.skip("OCR needs Tesseract installed")
    transcript = await record.run_scenario(slug, SCENARIOS[slug], out=tmp_path)

    files = tmp_path / slug / "files"
    for step in transcript["steps"]:
        if step["type"] == "call":
            assert step["isError"] == bool(step.get("expectError")), step
            assert step["title"]
            assert step["schema"]["type"] == "object"  # every tool publishes outputSchema
            if step["isError"]:  # a refusal shown on purpose: its message
                assert isinstance(step["result"], str) and step["result"]
                continue
            assert set(step["result"]) == set(step["schema"]["properties"])
            for name in step["images"]:  # saved exactly as the tool returned them
                with Image.open(tmp_path / slug / name) as im:
                    assert im.format == "PNG"
            if step.get("showImages"):
                assert step["images"] or step["result"].get("files") \
                    or step["result"].get("images")  # render_pages / extract_images
            for key in ("before", "after"):
                if key in step.get("show", {}):
                    assert (files / step["show"][key]).is_file()
        if step["type"] == "tools":
            assert {t["name"] for t in step["tools"]} == EXPECTED_TOOLS
    saved = json.loads((tmp_path / slug / "script.json").read_text())
    assert saved == transcript


SMALL_PROMO = dict(PROMO, features=[{"slug": "interleave", "headline": "Combines scans",
                                    "detail": "Fronts and backs, in order."}])


async def test_run_promo_writes_its_script_with_the_real_tool_count(tmp_path):
    script = await record.run_promo(tmp_path, SMALL_PROMO)

    assert script["tools"] == len(EXPECTED_TOOLS)
    assert json.loads((tmp_path / "promo" / "script.json").read_text()) == script
    assert (tmp_path / "interleave" / "script.json").is_file()  # the scene it plays


async def test_run_promo_starts_clean(tmp_path):
    stale = tmp_path / "promo" / "raw" / "old.webm"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")

    await record.run_promo(tmp_path, dict(SMALL_PROMO, features=[]))

    assert not stale.exists()


def test_write_catalog_lists_what_can_be_watched_or_replayed(tmp_path):
    (tmp_path / "videos").mkdir()
    (tmp_path / "videos" / "search.webm").write_bytes(b"v")
    (tmp_path / "videos" / record.PROMO_VIDEO).write_bytes(b"v")
    (tmp_path / "ocr").mkdir()
    (tmp_path / "ocr" / "script.json").write_text("{}")

    path = record.write_catalog(tmp_path)

    text = path.read_text()
    assert path == tmp_path / "catalog.js"
    assert text.startswith("// Written by demos/record.py")
    catalog = json.loads(text.split("window.PAPERBOY_DEMOS = ", 1)[1].rstrip().rstrip(";"))
    assert [g["name"] for g in catalog["groups"]] == [name for name, _ in GROUPS]
    demos = {d["slug"]: d for g in catalog["groups"] for d in g["demos"]}
    assert demos.keys() == SCENARIOS.keys()
    assert demos["search"] == {"slug": "search", "title": SCENARIOS["search"]["title"],
                               "subtitle": SCENARIOS["search"]["subtitle"],
                               "video": True, "live": False}
    assert demos["ocr"]["live"] and not demos["ocr"]["video"]
    assert catalog["promo"]["video"] and not catalog["promo"]["live"]
    assert catalog["promo"]["file"] == record.PROMO_VIDEO


async def test_prepare_all_runs_every_scenario_and_skips_what_cannot_run(
        tmp_path, monkeypatch, capsys):
    ran = []

    async def fake_run(slug, scenario, out):
        if slug == "ocr":
            (out / slug).mkdir(parents=True)  # a half-made demo is cleaned away
            raise RuntimeError("OCR needs Tesseract")
        ran.append(slug)

    monkeypatch.setattr(record, "run_scenario", fake_run)

    skipped = await record.prepare_all(tmp_path)

    assert skipped == ["ocr"]
    assert ran == [slug for slug in SCENARIOS if slug != "ocr"]
    assert not (tmp_path / "ocr").exists()
    assert "skipped ocr: OCR needs Tesseract" in capsys.readouterr().out
    # The product video's script, without running its scenes a second time.
    script = json.loads((tmp_path / "promo" / "script.json").read_text())
    assert script["tools"] == len(EXPECTED_TOOLS)


async def test_run_scenario_hides_local_paths(tmp_path):
    transcript = await record.run_scenario(
        "tool-annotations", SCENARIOS["tool-annotations"], out=tmp_path)

    text = json.dumps(transcript)
    assert str(tmp_path) not in text
    assert record.DISPLAY_ROOT in text


async def test_run_scenario_records_tool_hints(tmp_path):
    transcript = await record.run_scenario(
        "tool-annotations", SCENARIOS["tool-annotations"], out=tmp_path)

    calls = {s["tool"]: s for s in transcript["steps"] if s["type"] == "call"}
    assert calls["pdf_info"]["readOnly"] is True
    assert calls["rotate_pages"]["readOnly"] is False


async def test_run_scenario_starts_from_a_clean_workspace(tmp_path):
    stale = tmp_path / "s" / "files" / "stale.pdf"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")
    scenario = {"title": "t", "subtitle": "", "files": ["report.pdf"], "prompt": "p",
                "steps": [{"type": "say", "text": "hi"}]}

    await record.run_scenario("s", scenario, out=tmp_path)

    assert not stale.exists()
    assert (tmp_path / "s" / "files" / "report.pdf").is_file()


async def test_run_scenario_fails_loudly_when_a_tool_errors(tmp_path):
    scenario = {"title": "t", "subtitle": "", "files": [], "prompt": "p",
                "steps": [{"type": "call", "tool": "pdf_info",
                           "args": {"input_path": "missing.pdf"}}]}

    with pytest.raises(RuntimeError, match="pdf_info failed"):
        await record.run_scenario("broken", scenario, out=tmp_path)


async def test_run_scenario_can_show_an_expected_refusal(tmp_path):
    scenario = {"title": "t", "subtitle": "", "files": ["lease.pdf"], "prompt": "p",
                "steps": [{"type": "call", "tool": "delete_pages", "expectError": True,
                           "args": {"input_path": "lease.pdf", "pages": "1"}}]}

    transcript = await record.run_scenario("refused", scenario, out=tmp_path)

    assert transcript["steps"][0]["isError"]
    assert "owner password" in transcript["steps"][0]["result"]


async def test_run_scenario_fails_when_an_expected_refusal_doesnt_happen(tmp_path):
    scenario = {"title": "t", "subtitle": "", "files": ["report.pdf"], "prompt": "p",
                "steps": [{"type": "call", "tool": "pdf_info", "expectError": True,
                           "args": {"input_path": "report.pdf"}}]}

    with pytest.raises(RuntimeError, match="expected to be refused"):
        await record.run_scenario("allowed", scenario, out=tmp_path)


async def test_run_scenario_stops_at_the_first_failed_step(tmp_path):
    scenario = {"title": "t", "subtitle": "", "files": ["report.pdf"], "prompt": "p",
                "steps": [
                    {"type": "call", "tool": "pdf_info", "args": {"input_path": "missing.pdf"}},
                    {"type": "call", "tool": "rotate_pages",
                     "args": {"input_path": "report.pdf", "degrees": 90}},
                ]}

    with pytest.raises(RuntimeError):
        await record.run_scenario("halt", scenario, out=tmp_path)

    assert not (tmp_path / "halt" / "files" / "report_rotated.pdf").exists()


async def test_run_scenario_cannot_escape_its_workspace(tmp_path):
    scenario = {"title": "t", "subtitle": "", "files": [], "prompt": "p",
                "steps": [{"type": "call", "tool": "pdf_info",
                           "args": {"input_path": "/etc/hosts"}}]}

    with pytest.raises(RuntimeError, match="outside the allowed root"):
        await record.run_scenario("escape", scenario, out=tmp_path)


# --------------------------------------------------------------------------- #
# _serve: localhost server for the viewer
# --------------------------------------------------------------------------- #


@pytest.fixture
def served(tmp_path):
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "script.json").write_text('{"ok": true}')
    server = record._serve(tmp_path)
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _status(url: str) -> int:
    try:
        with urllib.request.urlopen(url) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_serve_binds_to_localhost_only(tmp_path):
    server = record._serve(tmp_path)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.shutdown()


def test_serve_serves_viewer_and_output(served):
    assert _status(f"{served}/viewer/index.html") == 200
    with urllib.request.urlopen(f"{served}/out/demo/script.json") as resp:
        assert json.loads(resp.read()) == {"ok": True}


@pytest.mark.parametrize("path", [
    "/record.py",                    # demo sources are not served
    "/scenarios.py",
    "/",
    "/out/../record.py",             # traversal out of the output dir
    "/viewer/../record.py",
    "/out/%2e%2e/record.py",
    "/out/%252e%252e/record.py",     # double-encoded
])
def test_serve_refuses_anything_else(served, path):
    assert _status(f"{served}{path}") == 404


# --------------------------------------------------------------------------- #
# CLI: main() and preview()
# --------------------------------------------------------------------------- #


def test_main_lists_scenarios(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["record.py", "--list"])
    record.main()
    out = capsys.readouterr().out
    for slug, s in SCENARIOS.items():
        assert slug in out and s["title"] in out


def test_main_rejects_unknown_scenario(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["record.py", "nope"])
    with pytest.raises(SystemExit) as exc:
        record.main()
    assert exc.value.code == 2
    assert "unknown scenario(s): nope" in capsys.readouterr().err


@pytest.mark.parametrize("slugs", [[], ["tool-annotations", "tool-annotations"]])
def test_main_preview_needs_exactly_one_scenario(monkeypatch, slugs):
    monkeypatch.setattr(sys, "argv", ["record.py", "--preview", *slugs])
    with pytest.raises(SystemExit):
        record.main()


def test_main_records_all_scenarios_by_default(monkeypatch):
    calls = []

    async def fake_record(slugs, headed):
        calls.append((slugs, headed))

    monkeypatch.setattr(record, "record", fake_record)
    monkeypatch.setattr(sys, "argv", ["record.py", "--headed"])
    record.main()
    assert calls == [(list(SCENARIOS), True)]


def test_main_records_the_promo(monkeypatch):
    calls = []

    async def fake_record(slugs, headed, promo=False):
        calls.append((slugs, headed, promo))

    monkeypatch.setattr(record, "record", fake_record)
    monkeypatch.setattr(sys, "argv", ["record.py", "--promo"])
    record.main()
    assert calls == [([], False, True)]


def test_main_previews_the_promo(monkeypatch):
    seen = []
    monkeypatch.setattr(record, "preview", seen.append)
    monkeypatch.setattr(sys, "argv", ["record.py", "--promo", "--preview"])
    record.main()
    assert seen == [None]


def test_main_promo_takes_no_scenarios(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["record.py", "--promo", "tool-annotations"])
    with pytest.raises(SystemExit):
        record.main()
    assert "--promo takes no scenarios" in capsys.readouterr().err


def test_main_opens_the_gallery(monkeypatch):
    seen = []
    monkeypatch.setattr(record, "gallery", lambda: seen.append(True))
    monkeypatch.setattr(sys, "argv", ["record.py", "--gallery"])
    record.main()
    assert seen == [True]


@pytest.mark.parametrize("extra", [["tool-annotations"], ["--promo"], ["--preview"]])
def test_main_gallery_takes_no_other_options(monkeypatch, capsys, extra):
    monkeypatch.setattr(sys, "argv", ["record.py", "--gallery", *extra])
    with pytest.raises(SystemExit):
        record.main()
    assert "--gallery takes no other options" in capsys.readouterr().err


def test_gallery_prepares_demos_and_opens_the_browser(monkeypatch, tmp_path):
    prepared, opened = [], []

    async def fake_prepare(out):
        prepared.append(out)
        return []

    monkeypatch.setattr(record, "prepare_all", fake_prepare)
    monkeypatch.setattr(record.webbrowser, "open", opened.append)

    def interrupt():
        assert _status(opened[0]) == 200
        assert _status(opened[0].replace("viewer/index.html", "out/catalog.js")) == 200
        raise KeyboardInterrupt

    monkeypatch.setattr(record, "_wait_forever", interrupt)

    record.gallery(out=tmp_path)

    assert prepared == [tmp_path]
    assert opened[0].endswith("/viewer/index.html")
    with pytest.raises(OSError):
        urllib.request.urlopen(opened[0], timeout=2)


def test_main_dispatches_preview(monkeypatch):
    seen = []
    monkeypatch.setattr(record, "preview", seen.append)
    monkeypatch.setattr(sys, "argv", ["record.py", "--preview", "tool-annotations"])
    record.main()
    assert seen == ["tool-annotations"]


def test_preview_opens_browser_and_shuts_down_on_ctrl_c(monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr(record.webbrowser, "open", opened.append)

    def interrupt():
        # The page must be reachable while the preview is "running".
        assert _status(opened[0]) == 200
        raise KeyboardInterrupt

    monkeypatch.setattr(record, "_wait_forever", interrupt)

    record.preview("tool-annotations", out=tmp_path)

    assert opened and opened[0].endswith("/viewer/index.html?s=tool-annotations")
    assert (tmp_path / "tool-annotations" / "script.json").is_file()
    with pytest.raises(OSError):  # server is shut down afterwards
        urllib.request.urlopen(opened[0].split("?")[0], timeout=2)


def test_preview_of_the_promo_opens_the_promo_page(monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr(record.webbrowser, "open", opened.append)
    real = record.run_promo
    monkeypatch.setattr(record, "run_promo", lambda out: real(out, SMALL_PROMO))

    def interrupt():
        assert _status(opened[0]) == 200
        raise KeyboardInterrupt

    monkeypatch.setattr(record, "_wait_forever", interrupt)

    record.preview(None, out=tmp_path)

    assert opened[0].endswith("/viewer/promo.html")
    assert (tmp_path / "promo" / "script.json").is_file()


# --------------------------------------------------------------------------- #
# Browser: viewer playback and video recording (need the [demo] extra)
# --------------------------------------------------------------------------- #


@pytest.fixture
async def browser():
    pw_api = pytest.importorskip("playwright.async_api")
    async with pw_api.async_playwright() as pw:
        try:
            b = await pw.chromium.launch()
        except Exception as exc:  # browser binary not installed
            pytest.skip(f"Chromium unavailable: {exc}")
        yield b
        await b.close()


async def _stage_text(browser, url: str) -> str:
    page = await browser.new_page()
    await page.goto(url)
    await page.wait_for_selector(".placeholder code", timeout=10_000)
    text = await page.inner_text("#stage")
    await page.close()
    return text


@pytest.fixture
def copied_viewer(tmp_path):
    """The viewer beside an out/ folder of its own, as it sits in demos/."""
    shutil.copytree(DEMOS / "viewer", tmp_path / "viewer")
    (tmp_path / "out").mkdir()
    return tmp_path


async def _open(browser, url: str):
    page = await browser.new_page(viewport=record.VIEWPORT)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    await page.goto(url)
    page.errors = errors
    return page


async def test_viewer_explains_live_replay_needs_http(browser, copied_viewer):
    url = (copied_viewer / "viewer" / "index.html").as_uri() + "?s=search"
    text = await _stage_text(browser, url)
    assert "served over HTTP" in text
    assert "--gallery" in text


async def test_gallery_from_a_file_offers_the_videos(browser, copied_viewer):
    videos = copied_viewer / "out" / "videos"
    videos.mkdir()
    (videos / "search.webm").write_bytes(b"v")
    (copied_viewer / "out" / "search").mkdir()
    (copied_viewer / "out" / "search" / "script.json").write_text("{}")
    record.write_catalog(copied_viewer / "out")

    page = await _open(browser, (copied_viewer / "viewer" / "index.html").as_uri())
    try:
        assert not page.errors
        assert await page.is_hidden("#player")
        assert await page.locator(".card").count() == len(SCENARIOS)
        # Opened as a file: videos only, no live replay, and a note saying how.
        assert await page.get_by_role("button", name="Watch video: Search a PDF").count() == 1
        assert await page.locator("a.secondary").count() == 0
        assert "--gallery" in await page.inner_text(".note")
    finally:
        await page.close()


async def test_gallery_without_recordings_says_how_to_make_them(browser, copied_viewer):
    page = await _open(browser, (copied_viewer / "viewer" / "index.html").as_uri())
    try:
        assert not page.errors
        text = await page.inner_text("#gallery")
        assert "No demos recorded yet" in text
        assert "python demos/record.py --gallery" in text
    finally:
        await page.close()


async def test_gallery_lists_every_demo_by_group(browser, tmp_path):
    for slug in ("search", "redaction"):
        (tmp_path / slug).mkdir()
        (tmp_path / slug / "script.json").write_text("{}")
    (tmp_path / "videos").mkdir()
    (tmp_path / "videos" / "search.webm").write_bytes(b"v")
    record.write_catalog(tmp_path)
    server = record._serve(tmp_path)
    page = await _open(browser, f"http://127.0.0.1:{server.server_address[1]}/viewer/index.html")
    try:
        assert not page.errors
        assert await page.inner_text("#title") == "Demos"
        assert await page.locator(".group h2").all_inner_texts() == [n for n, _ in GROUPS]
        assert await page.locator(".card").count() == len(SCENARIOS)
        assert await page.locator(".card h3").all_inner_texts() == [
            SCENARIOS[slug]["title"] for _, slugs in GROUPS for slug in slugs]
        # Each card offers what exists: a video, a live replay, or neither yet.
        live = page.get_by_role("link", name="Play live: Redact for real")
        assert await live.get_attribute("href") == "index.html?s=redaction"
        assert await page.get_by_role("button", name="Watch video: Search a PDF").count() == 1
        assert await page.get_by_role("button", name="Watch video: Redact for real").count() == 0
        assert await page.locator(".card .missing").count() == len(SCENARIOS) - 2
        assert await page.is_hidden("#back")
    finally:
        await page.close()
        server.shutdown()


async def test_gallery_plays_a_video_in_a_dialog(browser, tmp_path):
    await record.record(["list-pdfs"], headed=False, out=tmp_path, speed=40)
    server = record._serve(tmp_path)
    page = await _open(browser, f"http://127.0.0.1:{server.server_address[1]}/viewer/index.html")
    try:
        watch = page.get_by_role("button", name="Watch video: See which PDFs you have")
        await watch.click()
        assert await page.is_visible("#video-dialog")
        assert await page.inner_text("#video-title") == SCENARIOS["list-pdfs"]["title"]
        assert (await page.get_attribute("#video", "src")).endswith("/videos/list-pdfs.webm")
        await page.wait_for_function("document.getElementById('video').readyState >= 1")
        await page.keyboard.press("Escape")
        assert await page.is_hidden("#video-dialog")
        # Stopped, not playing on behind the page (the close event follows the Escape).
        await page.wait_for_function("!document.getElementById('video').hasAttribute('src')")
        assert await page.evaluate("document.activeElement.getAttribute('aria-label')") == \
            "Watch video: See which PDFs you have"
        await watch.click()
        await page.click("#video-close")
        assert await page.is_hidden("#video-dialog")
        assert not page.errors
    finally:
        await page.close()
        server.shutdown()


async def test_a_playing_demo_links_back_to_the_gallery(browser, tmp_path):
    await record.run_scenario("list-pdfs", SCENARIOS["list-pdfs"], out=tmp_path)
    record.write_catalog(tmp_path)
    server = record._serve(tmp_path)
    base = f"http://127.0.0.1:{server.server_address[1]}/viewer/index.html"
    page = await _open(browser, f"{base}?s=list-pdfs&speed=40")
    try:
        assert await page.is_visible("#back")
        await page.click("#back")
        await page.wait_for_selector("#gallery .card")
        assert page.url == base
        # Recordings and the product video hide it.
        await page.goto(f"{base}?s=list-pdfs&speed=40&embed=1")
        await page.wait_for_function("window.__demoDone || window.__demoError")
        assert await page.is_hidden("#back")
        assert not page.errors
    finally:
        await page.close()
        server.shutdown()


async def test_viewer_explains_unrecorded_scenario(browser, served):
    text = await _stage_text(browser, f"{served}/viewer/index.html?s=nope")
    assert "no recorded transcript" in text


@pytest.mark.parametrize("slug", sorted(SCENARIOS))
async def test_viewer_plays_scenario_to_completion(browser, tmp_path, slug):
    transcript = await record.run_scenario(slug, SCENARIOS[slug], out=tmp_path)
    server = record._serve(tmp_path)
    page = await browser.new_page(viewport=record.VIEWPORT)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    approvals: list[str] = []
    await page.expose_function("__sawApproval", lambda text: approvals.append(text))
    await page.add_init_script("""
        window.__mostFlagged = 0;
        window.__mostReview = 0;
        new MutationObserver(() => {
          const bar = document.querySelector('.approve span');
          if (bar && !bar.dataset.seen) {
            bar.dataset.seen = 1;
            window.__sawApproval(bar.textContent);
          }
          window.__mostFlagged = Math.max(window.__mostFlagged,
                                          document.querySelectorAll('canvas.flagged').length);
          window.__mostReview = Math.max(window.__mostReview,
                                         document.querySelectorAll('canvas.review').length);
        }).observe(document, {subtree: true, childList: true});
    """)
    try:
        port = server.server_address[1]
        await page.goto(f"http://127.0.0.1:{port}/viewer/index.html?s={slug}&speed=40")
        await page.wait_for_function("window.__demoDone || window.__demoError",
                                     timeout=60_000)

        assert not errors
        assert await page.evaluate("window.__demoError || null") is None
        assert await page.inner_text("#title") == transcript["title"]
        assert await page.inner_text(".msg.user") == transcript["prompt"]
        calls = [s for s in transcript["steps"] if s["type"] == "call"]
        assert await page.locator(".call").count() == len(calls)
        assert await page.locator(".call pre.result").count() == len(calls)
        writes = [s["title"] for s in calls if not s["readOnly"]]
        assert approvals == [f"Allow Paperboy to run “{t}”?" for t in writes]
        if calls and calls[-1].get("show"):
            assert await page.locator("#stage canvas").count() > 0
        highlighted = [len(c["result"][c["show"]["highlight"]]) for c in calls
                       if (c.get("show") or {}).get("highlight")]
        assert await page.evaluate("window.__mostFlagged") == max(highlighted, default=0)
        reviewed = [len(c["result"][c["show"]["review"]]) for c in calls
                    if (c.get("show") or {}).get("review")]
        assert await page.evaluate("window.__mostReview") == max(reviewed, default=0)
        if calls and calls[-1].get("showImages"):
            last = calls[-1]
            saved = last["result"].get("files") or last["result"].get("images") or []
            expected = len(last["images"]) or len(saved)
            imgs = page.locator("#stage img")
            assert await imgs.count() == expected
            assert all(w > 0 for w in await imgs.evaluate_all(
                "els => els.map(e => e.naturalWidth)"))  # every image actually loaded
        if calls and calls[-1].get("showTable"):
            table = calls[-1]["result"][calls[-1]["showTable"]]
            assert await page.locator(".schema-table tr").count() == len(table)
        shown = [c for c in calls if any(c.get(k) for k in (
            "show", "showSchema", "showImages", "showTable", "showGrid", "showMarkdown"))]
        if shown and shown[-1].get("showMarkdown"):  # still on stage
            text = shown[-1]["result"][shown[-1]["showMarkdown"]]
            assert await page.locator(".markdown .md-line").count() == len(text.split("\n"))
            assert await page.locator(".md-heading").count() == sum(
                line.startswith("#") for line in text.split("\n"))
        if calls and calls[-1].get("showSchema"):
            fields = calls[-1]["schema"]["properties"]
            rows = page.locator(".schema-table tr td.field")
            assert await rows.all_inner_texts() == list(fields)
            assert await page.locator(".schema-table .value.null").count() == sum(
                1 for name in fields if calls[-1]["result"][name] is None)
    finally:
        await page.close()
        server.shutdown()


async def test_record_writes_a_webm_video(browser, tmp_path):
    videos = await record.record(["tool-annotations"], headed=False, out=tmp_path, speed=40)

    assert videos == [tmp_path / "videos" / "tool-annotations.webm"]
    data = videos[0].read_bytes()
    assert data[:4] == b"\x1a\x45\xdf\xa3"  # EBML header: a WebM container
    assert len(data) > 10_000
    assert not (tmp_path / "tool-annotations" / "raw").exists()
    assert '"video": true' in (tmp_path / "catalog.js").read_text()  # the gallery lists it


async def test_record_fails_when_the_viewer_errors(browser, tmp_path, monkeypatch):
    async def broken_transcript(slug, scenario, out):
        (out / slug).mkdir(parents=True, exist_ok=True)
        (out / slug / "script.json").write_text('{"title": "t", "steps": null}')

    monkeypatch.setattr(record, "run_scenario", broken_transcript)

    with pytest.raises(RuntimeError, match="viewer errors"):
        await record.record(["tool-annotations"], headed=False, out=tmp_path, speed=40)
    assert not (tmp_path / "videos" / "tool-annotations.webm").exists()


async def _play_promo(browser, out: Path) -> tuple:
    server = record._serve(out)
    page = await browser.new_page(viewport=record.VIEWPORT)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    headlines: list[str] = []
    await page.expose_function("__sawHeadline", lambda text: headlines.append(text))
    await page.add_init_script("""
        new MutationObserver(() => {
          for (const h of document.querySelectorAll('.feature h2:not([data-seen])')) {
            h.dataset.seen = 1;
            window.__sawHeadline(h.textContent);
          }
        }).observe(document, {subtree: true, childList: true});
    """)
    try:
        port = server.server_address[1]
        await page.goto(f"http://127.0.0.1:{port}/viewer/promo.html?speed=40")
        await page.wait_for_function("window.__demoDone || window.__demoError",
                                     timeout=60_000)
        failed = await page.evaluate("window.__demoError || null")
        text = await page.inner_text("#reel")
    finally:
        await page.close()
        server.shutdown()
    return failed, errors, headlines, text


async def test_promo_plays_to_completion(browser, tmp_path):
    script = await record.run_promo(tmp_path, SMALL_PROMO)

    failed, errors, headlines, text = await _play_promo(browser, tmp_path)

    assert failed is None and not errors
    assert headlines == [item["headline"] for item in script["features"]]
    # It ends on the install commands and the sign-off.
    for command in script["install"]:
        assert f"$ {command}" in text
    assert script["url"] in text


async def test_promo_reports_a_scene_that_cannot_play(browser, tmp_path):
    await record.run_promo(tmp_path, dict(SMALL_PROMO, features=[]))
    script = json.loads((tmp_path / "promo" / "script.json").read_text())
    script["features"] = [{"slug": "never-recorded", "headline": "h", "detail": "d"}]
    (tmp_path / "promo" / "script.json").write_text(json.dumps(script))

    failed, _, _, text = await _play_promo(browser, tmp_path)

    assert "never-recorded" in failed
    assert "Could not play the product video" in text


async def test_promo_reports_a_missing_script(browser, tmp_path):
    failed, _, _, _ = await _play_promo(browser, tmp_path)
    assert "no recorded promo script" in failed


async def test_record_writes_the_promo_video(browser, tmp_path, monkeypatch):
    real = record.run_promo
    monkeypatch.setattr(record, "run_promo", lambda out: real(out, SMALL_PROMO))

    videos = await record.record([], headed=False, out=tmp_path, speed=40, promo=True)

    assert videos == [tmp_path / "videos" / record.PROMO_VIDEO]
    assert videos[0].read_bytes()[:4] == b"\x1a\x45\xdf\xa3"
    assert not (tmp_path / "promo" / "raw").exists()


async def test_record_fails_when_the_promo_cannot_play(browser, tmp_path, monkeypatch):
    async def no_scenes(out):
        (out / "promo").mkdir(parents=True, exist_ok=True)
        (out / "promo" / "script.json").write_text(json.dumps(dict(
            PROMO, tools=1, features=[{"slug": "never-recorded", "headline": "h",
                                       "detail": "d"}])))

    monkeypatch.setattr(record, "run_promo", no_scenes)

    with pytest.raises(RuntimeError, match="promo: viewer errors"):
        await record.record([], headed=False, out=tmp_path, speed=40, promo=True)
