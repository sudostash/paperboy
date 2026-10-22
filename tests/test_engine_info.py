"""Inspection: pdf_info, check_pdf, extract_text, get_metadata."""
from __future__ import annotations

import dataclasses
import subprocess

import pikepdf
import pytest

from paperboy import engine as E
from paperboy.errors import PdfUserError

from conftest import HAS_QPDF

# --- pdf_info --------------------------------------------------------------


def test_pdf_info_reports_document_facts(doc, settings):
    info = E.pdf_info(doc, settings)
    assert info["path"] == str(doc)
    assert info["size_bytes"] == doc.stat().st_size
    assert info["pages"] == 5
    assert info["is_encrypted"] is False
    assert info["pdf_version"].startswith("1.")
    assert info["linearized"] is False
    assert info["first_page_size_pt"] == pytest.approx([595.3, 841.9], abs=0.5)  # A4
    assert isinstance(info["metadata"], dict)
    assert info["permissions"] is None


def test_pdf_info_on_encrypted_without_password_needs_one(make_encrypted, settings):
    info = E.pdf_info(make_encrypted(), settings)
    assert info["is_encrypted"] is True
    assert info["pages"] is None
    assert "Password required" in info["note"]


def test_pdf_info_with_password_reports_permissions(make_encrypted, settings):
    locked = make_encrypted(password="pw", owner="owner",
                            allow=pikepdf.Permissions(print_highres=False,
                                                      print_lowres=False))
    info = E.pdf_info(locked, settings, "pw")
    assert info["pages"] == 3
    assert info["is_encrypted"] is True
    assert info["permissions"]["print_highres"] is False
    assert info["permissions"]["extract"] is True


def test_pdf_info_wrong_password(make_encrypted, settings):
    with pytest.raises(PdfUserError, match="Incorrect password"):
        E.pdf_info(make_encrypted(), settings, "nope")


def test_pdf_info_rejects_non_pdf(tmp_path, settings):
    junk = tmp_path / "junk.pdf"
    junk.write_text("not a pdf")
    with pytest.raises(PdfUserError, match="Could not open PDF"):
        E.pdf_info(junk, settings)


def test_pdf_info_reports_linearized_files(doc, tmp_path, settings):
    out = tmp_path / "lin.pdf"
    with pikepdf.open(doc) as pdf:
        pdf.save(out, linearize=True)
    assert E.pdf_info(out, settings)["linearized"] is True


def test_pdf_info_includes_metadata(doc, tmp_path, settings):
    out = tmp_path / "meta.pdf"
    E.set_metadata(doc, out, {"title": "Hello"}, settings)
    assert "Hello" in E.pdf_info(out, settings)["metadata"].values()


def test_pdf_info_enforces_nothing_beyond_reading(make_pdf, settings):
    # A zero-page document is unusual but valid; no first-page size is reported.
    src = make_pdf("one.pdf", 1)
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        del pdf.pages[0]
        pdf.save(src)
    info = E.pdf_info(src, settings)
    assert info["pages"] == 0
    assert info["first_page_size_pt"] is None


# --- check_pdf ---------------------------------------------------------------


def test_check_pdf_fallback_on_healthy_file(doc, settings):
    res = E.check_pdf(doc, dataclasses.replace(settings, qpdf_path=None))
    assert res == {"path": str(doc), "healthy": True, "checker": "pikepdf", "problems": [],
                   "raw": None}


def test_check_pdf_fallback_on_unreadable_file(tmp_path, settings):
    junk = tmp_path / "junk.pdf"
    junk.write_text("garbage")
    with pytest.raises(PdfUserError, match="Could not open PDF"):
        E.check_pdf(junk, dataclasses.replace(settings, qpdf_path=None))


def test_check_pdf_fallback_needs_password(make_encrypted, settings):
    with pytest.raises(PdfUserError, match="supply its password"):
        E.check_pdf(make_encrypted(), dataclasses.replace(settings, qpdf_path=None))


@pytest.mark.skipif(not HAS_QPDF, reason="qpdf CLI not installed")
def test_check_pdf_with_qpdf_on_healthy_file(doc, settings):
    res = E.check_pdf(doc, settings)
    assert res["checker"] == "qpdf"
    assert res["healthy"] is True
    assert res["problems"] == []


@pytest.mark.skipif(not HAS_QPDF, reason="qpdf CLI not installed")
def test_check_pdf_with_qpdf_reports_damage(doc, tmp_path, settings):
    damaged = tmp_path / "damaged.pdf"
    data = doc.read_bytes()
    damaged.write_bytes(data.replace(b"xref", b"xxxx", 1))
    res = E.check_pdf(damaged, settings)
    assert res["checker"] == "qpdf"
    assert res["healthy"] is False  # recoverable, but damaged
    assert all(p.startswith(("WARNING", "qpdf:")) for p in res["problems"])
    assert any("xref" in p for p in res["problems"])


@pytest.mark.skipif(not HAS_QPDF, reason="qpdf CLI not installed")
def test_check_pdf_with_qpdf_and_password_never_echoes_it(make_encrypted, settings):
    res = E.check_pdf(make_encrypted(password="hunter2"), settings, "hunter2")
    assert res["healthy"] is True
    assert res["problems"] == []
    assert "hunter2" not in str(res)
    assert "User password = [redacted]" in res["raw"]


@pytest.mark.skipif(not HAS_QPDF, reason="qpdf CLI not installed")
def test_check_pdf_with_qpdf_wrong_password(make_encrypted, settings):
    res = E.check_pdf(make_encrypted(password="pw"), settings, "nope")
    assert res["healthy"] is False
    assert any("invalid password" in p for p in res["problems"])


@pytest.mark.skipif(not HAS_QPDF, reason="qpdf CLI not installed")
def test_check_pdf_with_qpdf_on_truncated_file(doc, tmp_path, settings):
    truncated = tmp_path / "trunc.pdf"
    data = doc.read_bytes()
    truncated.write_bytes(data[: len(data) // 2])
    res = E.check_pdf(truncated, settings)
    assert res["healthy"] is False
    assert res["problems"]


def test_check_pdf_qpdf_timeout_is_a_user_error(doc, settings, monkeypatch):
    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout"))

    monkeypatch.setattr(subprocess, "run", hang)
    with pytest.raises(PdfUserError, match="timed out"):
        E.check_pdf(doc, dataclasses.replace(settings, qpdf_path="/usr/bin/qpdf"))


# --- extract_text ------------------------------------------------------------


def test_extract_text_all_pages(doc, settings):
    res = E.extract_text(doc, settings)
    assert res["pages_read"] == 5
    assert res["truncated"] is False
    for n in range(1, 6):
        assert f"--- Page {n} ---" in res["text"]
        assert f"PAGE-{n}" in res["text"]


def test_extract_text_selected_pages_in_order(doc, settings):
    res = E.extract_text(doc, settings, pages="4,2")
    assert res["pages_read"] == 2
    assert res["text"].index("PAGE-4") < res["text"].index("PAGE-2")
    assert "PAGE-1" not in res["text"]


def test_extract_text_truncates(doc, settings):
    res = E.extract_text(doc, settings, max_chars=20)
    assert res["truncated"] is True
    assert res["text"].endswith("[truncated]")
    assert len(res["text"]) == 20 + len("\n[truncated]")


def test_extract_text_encrypted(make_encrypted, settings):
    locked = make_encrypted(password="pw")
    with pytest.raises(PdfUserError, match="supply its password"):
        E.extract_text(locked, settings)
    with pytest.raises(PdfUserError, match="Incorrect password"):
        E.extract_text(locked, settings, password="bad")
    assert "PAGE-1" in E.extract_text(locked, settings, password="pw")["text"]


def test_extract_text_rejects_non_pdf(tmp_path, settings):
    junk = tmp_path / "junk.pdf"
    junk.write_text("garbage")
    with pytest.raises(PdfUserError, match="Could not read PDF"):
        E.extract_text(junk, settings)


def test_extract_text_enforces_page_cap(doc, settings):
    with pytest.raises(PdfUserError, match="over the 2 limit"):
        E.extract_text(doc, dataclasses.replace(settings, max_pages=2))


def test_extract_text_bad_page_spec(doc, settings):
    with pytest.raises(PdfUserError, match="out of range"):
        E.extract_text(doc, settings, pages="9")


# --- get_metadata --------------------------------------------------------------


def test_get_metadata_returns_known_fields_only(doc, tmp_path, settings):
    out = tmp_path / "m.pdf"
    E.set_metadata(doc, out, {"title": "T", "author": "Ann, Bob", "keywords": "a b"}, settings)
    res = E.get_metadata(out, settings)
    assert res["path"] == str(out)
    assert res["metadata"]["title"] == "T"
    assert res["metadata"]["author"] == "Ann, Bob"   # list joined back
    assert res["metadata"]["keywords"] == "a b"


def test_get_metadata_reads_reportlab_defaults_from_docinfo(doc, settings):
    # The fixture has no XMP; its fields live only in the classic Info dictionary.
    meta = E.get_metadata(doc, settings)["metadata"]
    assert meta["title"] == "untitled"
    assert meta["author"] == "anonymous"
    assert "keywords" not in meta  # empty values are skipped


def test_get_metadata_encrypted(make_encrypted, settings):
    with pytest.raises(PdfUserError):
        E.get_metadata(make_encrypted(), settings)
    assert E.get_metadata(make_encrypted("b.pdf", password="x"), settings, "x")["path"]


def test_pdf_info_survives_unreadable_metadata(doc, settings, monkeypatch):
    def broken(self, *a, **k):
        raise RuntimeError("bad XMP")

    monkeypatch.setattr(pikepdf.Pdf, "open_metadata", broken)
    info = E.pdf_info(doc, settings)
    assert info["metadata"] == {}
    assert info["pages"] == 5


def test_check_pdf_fallback_reports_unparseable_pages(doc, settings, monkeypatch):
    from contextlib import contextmanager

    from paperboy.engine import info as info_module

    class BrokenPages:
        def __iter__(self):
            raise pikepdf.PdfError("page tree is corrupt")

    @contextmanager
    def fake_open(*args, **kwargs):
        yield type("Doc", (), {"pages": BrokenPages()})()

    monkeypatch.setattr(info_module, "open_pdf", fake_open)
    res = E.check_pdf(doc, dataclasses.replace(settings, qpdf_path=None))
    assert res["healthy"] is False
    assert res["problems"] == ["page tree is corrupt"]


# --- review findings: qpdf output handling ---------------------------------------


def _fake_qpdf(monkeypatch, stdout: bytes, code: int = 0):
    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, code, stdout, b"")

    monkeypatch.setattr(subprocess, "run", run)


def test_check_pdf_keeps_qpdf_error_lines(doc, settings, monkeypatch):
    _fake_qpdf(monkeypatch, (
        b"checking doc.pdf\nPDF Version: 1.3\nFile is not encrypted\nFile is not linearized\n"
        b"WARNING: doc.pdf (offset 9): stream error\n"
        b"ERROR: page 1: content stream (content stream object 4 0): errors while decoding\n"
        b"qpdf: errors detected\n"), code=2)
    res = E.check_pdf(doc, dataclasses.replace(settings, qpdf_path="qpdf"))
    assert res["problems"] == [
        "WARNING: doc.pdf (offset 9): stream error",
        "ERROR: page 1: content stream (content stream object 4 0): errors while decoding",
        "qpdf: errors detected",
    ]


@pytest.mark.parametrize("secret", [b"hunter2", b"qz\rxv7", b"qz\nxv7", b"qzx\xe9", b"qz\r\nxv7"])
def test_check_pdf_never_returns_any_part_of_a_recovered_password(doc, settings, monkeypatch,
                                                                  secret):
    _fake_qpdf(monkeypatch, (
        b"checking doc.pdf\nPDF Version: 1.6\nR = 4\nP = -4\n"
        b"User password = " + secret + b"\nSupplied password is owner password\n"
        b"extract for accessibility: allowed\nFile is not linearized\n"
        b"No syntax or stream encoding errors found; the file may still contain\n"
        b"errors that qpdf cannot detect\n"))
    res = E.check_pdf(doc, dataclasses.replace(settings, qpdf_path="qpdf"), "owner")
    text = str(res)
    for fragment in secret.replace(b"\r", b"\n").split(b"\n"):
        if fragment:
            assert fragment.decode("utf-8", "replace") not in text
    assert res["healthy"] is True
    assert res["problems"] == []
    assert "User password = [redacted]" in res["raw"]


@pytest.mark.skipif(not HAS_QPDF, reason="qpdf CLI not installed")
def test_check_pdf_with_owner_password_hides_recovered_user_password(make_encrypted, tmp_path,
                                                                     settings):
    src = make_encrypted("plain-src.pdf", password="x")
    rc4 = tmp_path / "rc4.pdf"
    with pikepdf.open(src, password="x") as pdf:
        pdf.save(rc4, encryption=pikepdf.Encryption(user="us\rer2", owner="boss", R=4,
                                                    aes=False, metadata=False))
    res = E.check_pdf(rc4, settings, "boss")
    assert "er2" not in str(res)
    assert "User password = [redacted]" in res["raw"]


# --- metadata from the classic Info dictionary (no XMP) --------------------------


def _docinfo_only(tmp_path, **fields):
    from reportlab.pdfgen import canvas
    path = tmp_path / "docinfo.pdf"
    c = canvas.Canvas(str(path))
    c.setTitle(fields.get("title", ""))
    c.setAuthor(fields.get("author", ""))
    c.setSubject(fields.get("subject", ""))
    c.setKeywords(fields.get("keywords", ""))
    c.setCreator(fields.get("creator", ""))
    c.drawString(72, 720, "x")
    c.showPage()
    c.save()
    with pikepdf.open(path) as pdf:
        assert "/Metadata" not in pdf.Root  # really no XMP
    return path


def test_get_metadata_falls_back_to_docinfo(tmp_path, settings):
    path = _docinfo_only(tmp_path, title="Q3 Report", author="Ann Lee", subject="Sales",
                         keywords="q3, sales", creator="Writer")
    meta = E.get_metadata(path, settings)["metadata"]
    assert meta.pop("producer").startswith("ReportLab")  # set by reportlab itself
    assert meta == {"title": "Q3 Report", "author": "Ann Lee", "subject": "Sales",
                    "keywords": "q3, sales", "creator": "Writer"}


def test_docinfo_unicode_is_decoded(tmp_path, settings):
    path = _docinfo_only(tmp_path, title="Résumé — 履歴書")
    assert E.get_metadata(path, settings)["metadata"]["title"] == "Résumé — 履歴書"


def test_xmp_wins_over_docinfo(tmp_path, settings):
    path = _docinfo_only(tmp_path, title="Old title", author="Old author")
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False) as m:
            m["dc:title"] = "New title"
        pdf.save(path)
    meta = E.get_metadata(path, settings)["metadata"]
    assert meta["title"] == "New title"     # from XMP
    assert meta["author"] == "Old author"   # only in DocInfo


def test_pdf_info_reports_the_same_metadata_as_get_metadata(tmp_path, settings):
    path = _docinfo_only(tmp_path, title="Q3 Report", author="Ann Lee")
    assert E.pdf_info(path, settings)["metadata"] == E.get_metadata(path, settings)["metadata"]


def test_metadata_reading_does_not_modify_the_file(tmp_path, settings):
    path = _docinfo_only(tmp_path, title="Keep")
    before = path.read_bytes()
    E.get_metadata(path, settings)
    E.pdf_info(path, settings)
    assert path.read_bytes() == before


def test_empty_xmp_value_does_not_hide_docinfo(tmp_path, settings):
    path = _docinfo_only(tmp_path)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False) as m:
            m["dc:title"] = ""
        pdf.docinfo["/Title"] = "From DocInfo"
        pdf.save(path)
    meta = E.get_metadata(path, settings)["metadata"]
    assert meta["title"] == "From DocInfo"
    assert "None" not in meta.values()
