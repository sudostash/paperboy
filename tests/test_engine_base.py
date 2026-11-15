"""Shared engine helpers: open_pdf, is_encrypted, copy_docinfo."""
from __future__ import annotations

import dataclasses
import subprocess

import pikepdf
import pytest

from paperboy.engine.base import (
    MAX_PROBLEMS,
    copy_docinfo,
    is_encrypted,
    open_pdf,
    open_with_password,
    pdf_problems,
    problems_reading,
    run_qpdf,
)
from paperboy.errors import PdfUserError

from conftest import HAS_QPDF


def _spy_close(monkeypatch) -> list:
    closed = []
    real = pikepdf.Pdf.close
    monkeypatch.setattr(pikepdf.Pdf, "close", lambda self: (closed.append(self), real(self)))
    return closed


def test_open_pdf_yields_document_and_closes_it(doc, settings, monkeypatch):
    closed = _spy_close(monkeypatch)
    with open_pdf(doc, settings) as pdf:
        assert len(pdf.pages) == 5
        assert closed == []
    assert closed == [pdf]


def test_open_pdf_closes_even_when_the_body_fails(doc, settings, monkeypatch):
    closed = _spy_close(monkeypatch)
    with pytest.raises(RuntimeError):
        with open_pdf(doc, settings):
            raise RuntimeError("boom")
    assert len(closed) == 1


def test_open_pdf_closes_when_over_the_page_cap(doc, settings, monkeypatch):
    closed = _spy_close(monkeypatch)
    with pytest.raises(PdfUserError):
        with open_pdf(doc, dataclasses.replace(settings, max_pages=1)):
            pass
    assert len(closed) == 1


def test_open_pdf_with_correct_password(make_encrypted, settings):
    with open_pdf(make_encrypted(password="s3cret"), settings, "s3cret") as pdf:
        assert len(pdf.pages) == 3


def test_open_pdf_without_password_asks_for_one(make_encrypted, settings):
    with pytest.raises(PdfUserError, match="supply its password"):
        with open_pdf(make_encrypted(), settings):
            pass


def test_open_pdf_with_wrong_password(make_encrypted, settings):
    with pytest.raises(PdfUserError, match="Incorrect password"):
        with open_pdf(make_encrypted(password="right"), settings, "wrong"):
            pass


def test_open_pdf_rejects_non_pdf(tmp_path, settings):
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"this is not a pdf at all")
    with pytest.raises(PdfUserError, match="Could not open PDF"):
        with open_pdf(junk, settings):
            pass


def test_open_pdf_enforces_page_cap(doc, settings):
    capped = dataclasses.replace(settings, max_pages=4)
    with pytest.raises(PdfUserError, match="5 pages, over the 4 page limit"):
        with open_pdf(doc, capped):
            pass


def test_open_pdf_allows_exactly_the_cap(doc, settings):
    with open_pdf(doc, dataclasses.replace(settings, max_pages=5)) as pdf:
        assert len(pdf.pages) == 5


def test_is_encrypted(doc, make_encrypted):
    assert is_encrypted(doc) is False
    assert is_encrypted(make_encrypted()) is True


def test_is_encrypted_with_owner_password_only(make_pdf, tmp_path):
    # Empty user password: opens without a password but is still encrypted.
    src = make_pdf("p.pdf", 1)
    out = tmp_path / "owner-only.pdf"
    with pikepdf.open(src) as pdf:
        pdf.save(out, encryption=pikepdf.Encryption(user="", owner="boss", R=6))
    assert is_encrypted(out) is True


def test_copy_docinfo_carries_metadata(doc, tmp_path):
    with pikepdf.open(doc) as src:
        with src.open_metadata() as m:
            m["dc:title"] = "Carried"
        src.save(tmp_path / "titled.pdf")
    with pikepdf.open(tmp_path / "titled.pdf") as src, pikepdf.new() as dst:
        copy_docinfo(src, dst)
        with dst.open_metadata() as m:
            assert m["dc:title"] == "Carried"


def _info_only(doc, tmp_path, **entries):
    """*doc* with document info entries and no XMP, as most PDFs come."""
    out = tmp_path / "info.pdf"
    with pikepdf.open(doc) as pdf:
        if "/Metadata" in pdf.Root:
            del pdf.Root.Metadata
        for key, value in entries.items():
            pdf.docinfo[f"/{key}"] = value
        pdf.save(out)
    return out


def test_copy_docinfo_carries_info_only_metadata(doc, tmp_path):
    src_path = _info_only(doc, tmp_path, Title="Q3 Report", Author="Ann Lee",
                          CreationDate="D:20260101120000Z", Trapped=pikepdf.Name.False_,
                          Custom="not a standard entry")
    with pikepdf.open(src_path) as src, pikepdf.new() as dst:
        copy_docinfo(src, dst)
        assert str(dst.docinfo["/Title"]) == "Q3 Report"
        assert str(dst.docinfo["/Author"]) == "Ann Lee"
        assert str(dst.docinfo["/CreationDate"]) == "D:20260101120000Z"
        assert dst.docinfo["/Trapped"] == pikepdf.Name.False_
        assert "/Custom" not in dst.docinfo  # only the standard entries
        assert "/Metadata" not in dst.Root  # no XMP made up from nothing


def test_copy_docinfo_lets_xmp_win_and_skips_odd_values(doc, tmp_path):
    src_path = _info_only(doc, tmp_path, Title="Old title", Subject="From info")
    with pikepdf.open(src_path, allow_overwriting_input=True) as pdf:
        with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False) as m:
            m["dc:title"] = "XMP title"
        pdf.docinfo["/Keywords"] = pikepdf.Array([1, 2])  # not a string: skipped
        pdf.save(src_path)
    with pikepdf.open(src_path) as src, pikepdf.new() as dst:
        copy_docinfo(src, dst)
        assert str(dst.docinfo["/Title"]) == "XMP title"
        assert str(dst.docinfo["/Subject"]) == "From info"
        assert "/Keywords" not in dst.docinfo


def test_copy_docinfo_without_any_metadata(doc, tmp_path):
    with pikepdf.open(doc) as src, pikepdf.new() as dst:
        if "/Info" in src.trailer:
            del src.trailer.Info
        copy_docinfo(src, dst)
        assert "/Title" not in dst.docinfo


def test_copy_docinfo_never_raises_on_info(doc):
    class BrokenDocinfo:
        Root = pikepdf.Dictionary()

        @property
        def docinfo(self):
            raise RuntimeError("boom")

    with pikepdf.open(doc) as src:
        src.docinfo["/Title"] = "T"
        copy_docinfo(src, BrokenDocinfo())  # best-effort: swallowed


def test_open_gives_a_trailer_without_size_one(doc, tmp_path):
    data = doc.read_bytes()
    cut = tmp_path / "cut.pdf"
    cut.write_bytes(data[:data.rindex(b"\nxref") + 1] + b"xref\n")  # ends mid-table
    try:
        pdf = open_with_password(str(cut), None)
    except pikepdf.PdfError:
        pytest.skip("this qpdf can't rebuild a file without a trailer (qpdf < 12)")
    with pdf:
        assert "/Size" in pdf.trailer
        pdf.save(tmp_path / "saved.pdf")
    with pikepdf.open(tmp_path / "saved.pdf") as saved:
        assert saved.get_warnings() == []


class _Checked:
    """Stands in for a pikepdf.Pdf: the warnings from opening, and a syntax check."""

    def __init__(self, warnings, problems, renamed=True):
        self._warnings = warnings
        if renamed:
            self.check_pdf_syntax = lambda: problems
        else:  # pikepdf < 9
            self.check = lambda: problems

    def get_warnings(self):
        return self._warnings


@pytest.mark.parametrize("renamed", [True, False])
def test_pdf_problems_lists_warnings_then_syntax_problems(renamed):
    pdf = _Checked(["file is damaged"], ["object 3: bad stream"], renamed)
    assert pdf_problems(pdf) == ["file is damaged", "object 3: bad stream"]


def test_pdf_problems_is_bounded():
    pdf = _Checked(["w" * 900] * (MAX_PROBLEMS + 50), ["late"])
    found = pdf_problems(pdf)
    assert len(found) == MAX_PROBLEMS
    assert all(len(problem) == 500 for problem in found)


def test_problems_reading(doc, tmp_path):
    assert problems_reading(doc, None) == []
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"%PDF-1.4\n garbage")
    assert len(problems_reading(junk, None)) == 1  # won't open: that is the problem


def test_copy_docinfo_never_raises(doc):
    class Broken:
        def open_metadata(self, *a, **k):
            raise RuntimeError("boom")

    with pikepdf.open(doc) as src:
        copy_docinfo(src, Broken())  # best-effort: swallowed


# --- run_qpdf ------------------------------------------------------------------


def _capture_run(monkeypatch) -> list[dict]:
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_run_qpdf_sends_password_on_stdin_not_argv(settings, monkeypatch):
    calls = _capture_run(monkeypatch)
    run_qpdf(dataclasses.replace(settings, qpdf_path="qpdf"), ["--check", "f.pdf"], "s3cret")
    (call,) = calls
    assert call["cmd"] == ["qpdf", "--password-file=-", "--check", "f.pdf"]
    assert call["input"] == b"s3cret"
    assert not any("s3cret" in part for part in call["cmd"])
    assert call["timeout"] == 120


def test_run_qpdf_without_password(settings, monkeypatch):
    calls = _capture_run(monkeypatch)
    run_qpdf(dataclasses.replace(settings, qpdf_path="qpdf"), ["--check", "f.pdf"])
    assert calls[0]["cmd"] == ["qpdf", "--check", "f.pdf"]


def test_run_qpdf_timeout_is_a_user_error(settings, monkeypatch):
    def hang(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", hang)
    with pytest.raises(PdfUserError, match="qpdf timed out after 5s"):
        run_qpdf(dataclasses.replace(settings, qpdf_path="qpdf"), ["x"], timeout=5)


@pytest.mark.skipif(not HAS_QPDF, reason="qpdf CLI not installed")
def test_run_qpdf_password_works_with_real_qpdf(make_encrypted, settings):
    locked = make_encrypted(password="pw")
    assert run_qpdf(settings, ["--check", str(locked)], "pw").returncode == 0
    assert run_qpdf(settings, ["--check", str(locked)], "bad").returncode == 2


@pytest.mark.parametrize("password", ["a\nb", "a\rb"])
def test_run_qpdf_rejects_multiline_passwords(settings, monkeypatch, password):
    calls = _capture_run(monkeypatch)
    with pytest.raises(PdfUserError, match="line break"):
        run_qpdf(dataclasses.replace(settings, qpdf_path="qpdf"), ["x"], password)
    assert calls == []


def test_run_qpdf_decodes_output_safely(settings, monkeypatch):
    def run(cmd, **kwargs):
        assert kwargs["input"] == "pässword".encode()
        return subprocess.CompletedProcess(cmd, 0, b"caf\xe9\r\n", b"oops \xff")

    monkeypatch.setattr(subprocess, "run", run)
    proc = run_qpdf(dataclasses.replace(settings, qpdf_path="qpdf"), ["x"], "pässword")
    assert proc.stdout == "caf�\r\n"  # raw bytes decoded, \r kept as-is
    assert proc.stderr == "oops �"
