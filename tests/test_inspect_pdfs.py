"""inspect_pdfs: a read-only report on many PDFs, each file on its own."""
from __future__ import annotations

import os

import pikepdf
import pytest
from reportlab.pdfgen import canvas

import paperboy.server as S
from paperboy.engine import survey
from paperboy.errors import PdfUserError

from conftest import _make_pdf


@pytest.fixture(autouse=True)
def sandbox(settings, monkeypatch):
    monkeypatch.setattr(S, "SETTINGS", settings)
    return settings


def _lock(path, password="secret"):
    plain = _make_pdf(path.with_name("." + path.name), 2)  # hidden: left out of folders
    with pikepdf.open(plain) as pdf:
        pdf.save(path, encryption=pikepdf.Encryption(user=password, owner=password, R=6))
    return path


def _scripted(path):
    """Runs a script when opened, and its author is in the metadata."""
    _make_pdf(path, 1)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.Root.OpenAction = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Action, S=pikepdf.Name.JavaScript, JS=pikepdf.String("x()")))
        pdf.docinfo["/Author"] = "Jo Bloggs"
        pdf.docinfo["/Title"] = "Plan\x07 B"
        pdf.save(path)
    return path


def _form(path):
    """A text field and an empty signature field."""
    c = canvas.Canvas(str(path))
    c.acroForm.textfield(name="name", x=72, y=700, width=200, height=20)
    c.showPage()
    c.save()
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        sig = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Widget, FT=pikepdf.Name.Sig,
            T=pikepdf.String("signed"), Rect=[72, 600, 272, 640], P=page.obj))
        page.Annots.append(sig)
        pdf.Root.AcroForm.Fields.append(sig)
        pdf.save(path)
    return path


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "inbox"
    (d / "old").mkdir(parents=True)
    _make_pdf(d / "plain.pdf", 3)
    _scripted(d / "scripted.pdf")
    _form(d / "form.pdf")
    _lock(d / "locked.pdf")
    (d / "broken.pdf").write_bytes(b"%PDF-1.7\nnot really")
    _make_pdf(d / "old" / "archive.pdf", 4)
    (d / "notes.txt").write_text("not a PDF")
    return d


def _by_name(res):
    return {os.path.basename(p["path"]): p for p in res["pdfs"]}


def test_a_folder_is_reported_file_by_file_with_totals(folder):
    before = {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    res = S.inspect_pdfs(folder=str(folder))
    assert res["folder"] == str(folder)
    pdfs = _by_name(res)
    assert sorted(pdfs) == ["broken.pdf", "form.pdf", "locked.pdf", "plain.pdf",
                            "scripted.pdf"]  # not old/, hidden files or notes.txt
    assert [p["path"] for p in res["pdfs"]] == sorted(p["path"] for p in res["pdfs"])
    assert (res["files"], res["inspected"], res["needs_password"], res["failed"],
            res["not_run"], res["stopped"]) == (5, 3, 1, 1, 0, None)
    assert res["pages"] == 3 + 1 + 1
    assert (res["encrypted"], res["unhealthy"], res["active_content"],
            res["suspicious"], res["with_forms"]) == (1, 1, 0, 1, 1)
    assert res["with_hidden_data"] >= 1

    plain = pdfs["plain.pdf"]
    assert plain["status"] == "inspected" and plain["error"] is None
    assert (plain["pages"], plain["encrypted"], plain["healthy"], plain["problems"],
            plain["verdict"], plain["risks"], plain["form_fields"],
            plain["signature_fields"], plain["attachments"]) == (
        3, False, True, [], "no_active_content_found", [], 0, 0, 0)
    assert plain["size_bytes"] == (folder / "plain.pdf").stat().st_size
    assert plain["pdf_version"]

    scripted = pdfs["scripted.pdf"]
    assert scripted["verdict"] == "suspicious"           # a script run on open
    assert "javascript_on_open" in scripted["risks"]
    assert {"metadata_fields", "javascript"} <= set(scripted["hidden_data"])
    assert scripted["title"] == "Plan  B"                  # control characters dropped

    form = pdfs["form.pdf"]
    assert (form["form_fields"], form["signature_fields"]) == (2, 1)

    locked = pdfs["locked.pdf"]
    assert locked["status"] == "needs_password" and locked["encrypted"] is True
    assert "password protected" in locked["error"] and locked["pages"] is None

    broken = pdfs["broken.pdf"]
    assert broken["status"] == "failed" and broken["healthy"] is False
    assert broken["error"].startswith("Could not open PDF")
    assert all(p.read_bytes() == data for p, data in before.items())  # nothing changed


def test_recursive_includes_subfolders(folder):
    res = S.inspect_pdfs(folder=str(folder), recursive=True)
    assert "archive.pdf" in _by_name(res) and res["files"] == 6


@pytest.mark.filterwarnings("ignore:A password was provided")
def test_one_password_opens_the_files_that_need_it(folder, tmp_path):
    _lock(folder / "other.pdf", "different")
    res = S.inspect_pdfs(folder=str(folder), password="secret")
    pdfs = _by_name(res)
    assert pdfs["locked.pdf"]["status"] == "inspected"
    assert (pdfs["locked.pdf"]["pages"], pdfs["locked.pdf"]["healthy"]) == (2, True)
    assert pdfs["plain.pdf"]["status"] == "inspected"     # tried only where needed
    other = pdfs["other.pdf"]
    assert other["status"] == "needs_password"
    assert other["error"] == "The password given doesn't open it"
    assert res["needs_password"] == 1 and res["encrypted"] == 2


def test_input_paths_are_reported_from_the_folder_they_share(folder):
    res = S.inspect_pdfs(input_paths=[str(folder / "old" / "archive.pdf"),
                                      str(folder / "plain.pdf")])
    assert res["folder"] == str(folder) and res["inspected"] == 2
    assert [os.path.basename(p["path"]) for p in res["pdfs"]] == ["archive.pdf",
                                                                    "plain.pdf"]


@pytest.mark.parametrize("kwargs, message", [
    ({}, "Give either folder or input_paths"),
    ({"input_paths": []}, "input_paths is empty"),
    ({"input_paths": ["missing.pdf"]}, "not found|does not exist|No such"),
])
def test_bad_calls_are_refused(folder, kwargs, message):
    if kwargs.get("input_paths"):
        kwargs = {"input_paths": [str(folder / p) for p in kwargs["input_paths"]]}
    with pytest.raises(S.ToolError, match=message):
        S.inspect_pdfs(**kwargs)


def test_a_folder_without_pdfs_says_so(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(S.ToolError, match=r"No PDFs in .*recursive=true"):
        S.inspect_pdfs(folder=str(tmp_path / "empty"))
    with pytest.raises(S.ToolError, match=r"No PDFs in [^(]*$"):
        S.inspect_pdfs(folder=str(tmp_path / "empty"), recursive=True)


def test_too_many_files_are_refused(folder, monkeypatch):
    monkeypatch.setattr(S, "INSPECT_MAX_FILES", 2)
    with pytest.raises(S.ToolError, match="covers at most 2"):
        S.inspect_pdfs(folder=str(folder))
    with pytest.raises(S.ToolError, match="input_paths can name at most 2 PDFs"):
        S.inspect_pdfs(input_paths=[str(folder / n) for n in
                                    ("plain.pdf", "form.pdf", "scripted.pdf")])


def test_a_report_stops_starting_files_after_its_time_limit(folder, monkeypatch):
    monkeypatch.setattr(S, "INSPECT_TIME_LIMIT_S", 0)
    res = S.inspect_pdfs(folder=str(folder))
    assert res["stopped"] == "The report took over 0 minutes"
    assert (res["files"], res["not_run"]) == (5, 4)
    assert [p["status"] for p in res["pdfs"]].count("not_run") == 4
    unrun = next(p for p in res["pdfs"] if p["status"] == "not_run")
    assert unrun["error"] is None and unrun["pages"] is None


def test_a_file_that_goes_missing_or_breaks_the_engine_is_reported(folder, monkeypatch,
                                                                    caplog):
    real = S._survey

    def flaky(src, *args):
        if src.name == "plain.pdf":
            raise RuntimeError("boom")
        return real(src, *args)
    monkeypatch.setattr(S, "_survey", flaky)
    (folder / "form.pdf").unlink()   # listed, then gone: checked again before reading
    monkeypatch.setattr(S, "_inspect_files", lambda *a: (folder, sorted(
        [folder / "form.pdf", folder / "plain.pdf", folder / "scripted.pdf"])))
    res = S.inspect_pdfs(folder=str(folder))
    pdfs = _by_name(res)
    assert pdfs["form.pdf"]["status"] == "failed"
    assert pdfs["plain.pdf"]["error"] == ("Internal error; details were logged by the "
                                          "server")
    assert "boom" in caplog.text
    assert pdfs["scripted.pdf"]["status"] == "inspected"
    assert res["failed"] == 2


def test_progress_is_reported_after_each_file(folder):
    seen = []
    token = S._PROGRESS.set(lambda *a: seen.append(a))
    try:
        S.inspect_pdfs(input_paths=[str(folder / "plain.pdf"), str(folder / "form.pdf")])
    finally:
        S._PROGRESS.reset(token)
    assert seen == [(0, 2, "1 of 2: “plain.pdf”"),
                    (1, 2, "2 of 2: “form.pdf”"), (2, 2, "Done")]


def test_a_typed_password_serves_only_the_files_it_was_typed_for(folder):
    token = S._BATCH_BOUND.set(S._batch_key([folder / "plain.pdf"]))
    try:
        with pytest.raises(S.ToolError, match="changed after the password was typed"):
            S.inspect_pdfs(folder=str(folder))
        res = S.inspect_pdfs(input_paths=[str(folder / "plain.pdf")])
    finally:
        S._BATCH_BOUND.reset(token)
    assert res["inspected"] == 1


# --- the engine's one-file survey ----------------------------------------------------------


def test_a_survey_keeps_what_it_learnt_before_a_failure(tmp_path, settings, monkeypatch):
    path = _make_pdf(tmp_path / "big.pdf", 3)

    def refuse(*args, **kwargs):
        raise PdfUserError("Document has 3 pages, over the 2 page limit")
    monkeypatch.setattr(survey, "scan_pdf", refuse)
    entry = survey.survey_pdf(path, settings)
    assert entry["status"] == "failed" and "page limit" in entry["error"]
    assert (entry["pages"], entry["healthy"], entry["verdict"]) == (3, True, None)


def test_a_survey_shortens_long_problems_and_keeps_the_first_few(tmp_path, settings,
                                                                monkeypatch):
    path = _make_pdf(tmp_path / "odd.pdf", 1)
    monkeypatch.setattr(survey, "check_pdf", lambda *a: {
        "healthy": False, "problems": ["x" * 1000] + [f"p{i}" for i in range(9)]})
    entry = survey.survey_pdf(path, settings)
    assert entry["healthy"] is False and len(entry["problems"]) == survey.MAX_PROBLEMS
    assert len(entry["problems"][0]) == survey.MAX_TEXT
    assert entry["problems"][0].endswith("...") and entry["problems"][1] == "p0"


def test_problems_name_the_file_not_its_path(tmp_path, settings, monkeypatch):
    path = _make_pdf(tmp_path / "odd.pdf", 1)
    monkeypatch.setattr(survey, "check_pdf", lambda *a: {
        "healthy": False, "problems": [f"WARNING: {path}: file is damaged"]})
    assert survey.survey_pdf(path, settings)["problems"] == [
        "WARNING: odd.pdf: file is damaged"]


def test_an_encrypted_file_that_opens_without_a_password_is_read_without_one(
        tmp_path, settings):
    plain = _make_pdf(tmp_path / "p.pdf", 2)
    path = tmp_path / "restricted.pdf"
    with pikepdf.open(plain) as pdf:   # owner password only: opens for anyone
        pdf.save(path, encryption=pikepdf.Encryption(user="", owner="boss", R=6))
    entry = survey.survey_pdf(path, settings, password="not-it")
    assert entry["status"] == "inspected" and entry["encrypted"] is True
    assert entry["pages"] == 2


def test_antivirus_says_how_clamav_went_never_silently_clean(tmp_path, settings,
                                                              monkeypatch):
    import dataclasses
    path = _make_pdf(tmp_path / "a.pdf", 1)
    without = dataclasses.replace(settings, clamscan_path=None)
    assert survey.survey_pdf(path, without)["antivirus"] is None        # not asked for
    assert survey.survey_pdf(path, without, antivirus=True)["antivirus"] == "unavailable"
    real = survey.scan_pdf
    for infected, word in ((False, "clean"), (True, "infected"), (None, "error")):
        def scan(*args, infected=infected):
            res = real(*args)
            res["antivirus"].update(available=True, infected=infected)
            return res
        monkeypatch.setattr(survey, "scan_pdf", scan)
        assert survey.survey_pdf(path, settings, antivirus=True)["antivirus"] == word


def test_an_engine_surprise_keeps_what_was_learnt_before_it(tmp_path, settings,
                                                            monkeypatch, caplog):
    path = _make_pdf(tmp_path / "odd.pdf", 2)

    def boom(*args):
        raise KeyError("/Fields")
    monkeypatch.setattr(survey, "list_form_fields", boom)
    entry = survey.survey_pdf(path, settings)
    assert entry["status"] == "failed" and entry["error"].startswith("Internal error")
    assert (entry["pages"], entry["healthy"], entry["verdict"]) == (
        2, True, "no_active_content_found")
    assert "Unexpected error inspecting a PDF" in caplog.text


def test_a_locked_file_the_password_opens_but_cant_read_is_failed_not_locked(
        tmp_path, settings, monkeypatch):
    path = _lock(tmp_path / "locked.pdf")
    real = survey.pdf_info

    def info(path, settings, password=None):
        if password:
            raise PdfUserError("Could not open PDF: xref not found")
        return real(path, settings)
    monkeypatch.setattr(survey, "pdf_info", info)
    entry = survey.survey_pdf(path, settings, password="secret")
    assert entry["status"] == "failed" and entry["healthy"] is False
    assert entry["error"] == "Could not open PDF: xref not found"


def test_a_long_error_is_shortened(tmp_path):
    entry = survey.blank(tmp_path / "x.pdf", "failed", "e\x07" * 400)
    assert len(entry["error"]) == survey.MAX_TEXT and "\x07" not in entry["error"]
