"""Tests for the tutorial demo harness in ``demos/``.

Scenario tool calls run against the real server over stdio. Browser tests
(viewer playback, video recording) need the ``[demo]`` extra plus Chromium and
skip cleanly without them.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.types import ToolAnnotations
from pypdf import PdfReader

DEMOS = Path(__file__).resolve().parent.parent / "demos"
sys.path.insert(0, str(DEMOS))

import demo_pdfs  # noqa: E402
import record  # noqa: E402
from scenarios import SCENARIOS  # noqa: E402
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
            assert set(step.get("show", {})) <= {"before", "after", "password"}
            assert not (step.get("show") and step.get("showSchema")), "pick one stage view"
        if step["type"] == "say":
            assert step["text"].strip()


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
    transcript = await record.run_scenario(slug, SCENARIOS[slug], out=tmp_path)

    files = tmp_path / slug / "files"
    for step in transcript["steps"]:
        if step["type"] == "call":
            assert not step["isError"], step
            assert step["title"]
            assert step["schema"]["type"] == "object"  # every tool publishes outputSchema
            assert set(step["result"]) == set(step["schema"]["properties"])
            for key in ("before", "after"):
                if key in step.get("show", {}):
                    assert (files / step["show"][key]).is_file()
        if step["type"] == "tools":
            assert {t["name"] for t in step["tools"]} == EXPECTED_TOOLS
    saved = json.loads((tmp_path / slug / "script.json").read_text())
    assert saved == transcript


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


async def test_viewer_explains_file_url(browser):
    text = await _stage_text(browser, (DEMOS / "viewer" / "index.html").as_uri())
    assert "served over HTTP" in text
    assert "--preview" in text


async def test_viewer_explains_missing_scenario(browser, served):
    text = await _stage_text(browser, f"{served}/viewer/index.html")
    assert "No scenario selected" in text


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
        new MutationObserver(() => {
          const bar = document.querySelector('.approve span');
          if (bar && !bar.dataset.seen) {
            bar.dataset.seen = 1;
            window.__sawApproval(bar.textContent);
          }
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


async def test_record_fails_when_the_viewer_errors(browser, tmp_path, monkeypatch):
    async def broken_transcript(slug, scenario, out):
        (out / slug).mkdir(parents=True, exist_ok=True)
        (out / slug / "script.json").write_text('{"title": "t", "steps": null}')

    monkeypatch.setattr(record, "run_scenario", broken_transcript)

    with pytest.raises(RuntimeError, match="viewer errors"):
        await record.record(["tool-annotations"], headed=False, out=tmp_path, speed=40)
    assert not (tmp_path / "videos" / "tool-annotations.webm").exists()
