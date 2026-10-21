"""Optimization, repair, and flattening."""
from __future__ import annotations

import dataclasses
import subprocess

import pikepdf
import pytest
from reportlab.pdfgen import canvas

from paperboy import engine as E
from paperboy.errors import DependencyError, PdfUserError

from conftest import HAS_QPDF, page_labels


def _form_pdf(path):
    """A one-page PDF with a filled text field (an AcroForm widget annotation)."""
    c = canvas.Canvas(str(path))
    c.drawString(72, 760, "FORM")
    c.acroForm.textfield(name="name", value="Ada", x=72, y=700, width=200, height=20)
    c.showPage()
    c.save()
    return path


def _annotation_count(path, password=""):
    with pikepdf.open(path, password=password) as pdf:
        return sum(len(p.obj.get("/Annots", [])) for p in pdf.pages)


# --- optimize --------------------------------------------------------------------


def test_optimize_reports_sizes_and_keeps_content(doc, tmp_path, settings):
    out = tmp_path / "o.pdf"
    res = E.optimize_pdf(doc, out, settings)
    assert res["output"] == str(out)
    assert res["pages"] == 5
    assert res["size_before"] == doc.stat().st_size
    assert res["size_after"] == out.stat().st_size
    assert res["saved_bytes"] == res["size_before"] - res["size_after"]
    assert res["linearized"] is True
    assert page_labels(out) == page_labels(doc)


def test_optimize_linearizes(doc, tmp_path, settings):
    out = tmp_path / "o.pdf"
    E.optimize_pdf(doc, out, settings)
    with pikepdf.open(out) as pdf:
        assert pdf.is_linearized


def test_optimize_without_linearize(doc, tmp_path, settings):
    out = tmp_path / "o.pdf"
    res = E.optimize_pdf(doc, out, settings, linearize=False, compress=False)
    assert res["linearized"] is False
    with pikepdf.open(out) as pdf:
        assert not pdf.is_linearized


def test_optimize_shrinks_uncompressed_input(make_pdf, tmp_path, settings):
    bloated = tmp_path / "bloated.pdf"
    with pikepdf.open(make_pdf("src.pdf", 20)) as pdf:
        pdf.save(bloated, compress_streams=False,
                 object_stream_mode=pikepdf.ObjectStreamMode.disable)
    res = E.optimize_pdf(bloated, tmp_path / "o.pdf", settings, linearize=False)
    assert res["saved_bytes"] > 0


def test_optimize_encrypted_needs_password(make_encrypted, tmp_path, settings):
    with pytest.raises(PdfUserError, match="supply its password"):
        E.optimize_pdf(make_encrypted(), tmp_path / "o.pdf", settings)


# --- repair ----------------------------------------------------------------------


def test_repair_rebuilds_broken_xref(doc, tmp_path, settings):
    damaged = tmp_path / "damaged.pdf"
    damaged.write_bytes(doc.read_bytes().replace(b"xref", b"xxxx", 1))
    out = tmp_path / "fixed.pdf"
    res = E.repair_pdf(damaged, out, settings)
    assert res == {"output": str(out), "pages": 5}
    assert page_labels(out) == page_labels(doc)


def test_repair_gives_up_on_garbage(tmp_path, settings):
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"%PDF-1.4\n garbage with no objects")
    with pytest.raises(PdfUserError, match="too damaged"):
        E.repair_pdf(junk, tmp_path / "fixed.pdf", settings)


@pytest.mark.parametrize(("password", "message"), [
    (None, "supply its password"), ("bad", "Incorrect password")])
def test_repair_encrypted(make_encrypted, tmp_path, settings, password, message):
    with pytest.raises(PdfUserError, match=message):
        E.repair_pdf(make_encrypted(), tmp_path / "f.pdf", settings, password)


def test_repair_enforces_page_cap(doc, tmp_path, settings):
    with pytest.raises(PdfUserError, match="page limit"):
        E.repair_pdf(doc, tmp_path / "f.pdf", dataclasses.replace(settings, max_pages=2))
    assert not (tmp_path / "f.pdf").exists()


# --- flatten ---------------------------------------------------------------------


def test_flatten_requires_qpdf(doc, tmp_path, settings):
    with pytest.raises(DependencyError, match="needs the qpdf"):
        E.flatten_pdf(doc, tmp_path / "f.pdf", dataclasses.replace(settings, qpdf_path=None))


@pytest.mark.skipif(not HAS_QPDF, reason="qpdf CLI not installed")
def test_flatten_burns_form_fields_into_the_page(tmp_path, settings):
    form = _form_pdf(tmp_path / "form.pdf")
    assert _annotation_count(form) == 1
    out = tmp_path / "flat.pdf"
    res = E.flatten_pdf(form, out, settings)
    assert res["output"] == str(out)
    assert res["pages"] == 1
    assert _annotation_count(out) == 0
    assert "Ada" in page_labels(out)[0]


@pytest.mark.skipif(not HAS_QPDF, reason="qpdf CLI not installed")
def test_flatten_encrypted_input_with_password(tmp_path, settings):
    form = _form_pdf(tmp_path / "form.pdf")
    locked = tmp_path / "locked.pdf"
    with pikepdf.open(form) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(user="pw", owner="pw", R=6))
    out = tmp_path / "flat.pdf"
    assert E.flatten_pdf(locked, out, settings, "pw")["pages"] == 1


@pytest.mark.skipif(not HAS_QPDF, reason="qpdf CLI not installed")
def test_flatten_reports_qpdf_failure(tmp_path, settings):
    junk = tmp_path / "junk.pdf"
    junk.write_text("not a pdf")
    with pytest.raises(PdfUserError, match="Flatten failed"):
        E.flatten_pdf(junk, tmp_path / "f.pdf", settings)


def test_flatten_reports_warnings(doc, tmp_path, settings, monkeypatch):
    out = tmp_path / "f.pdf"

    def warn(cmd, **kwargs):
        out.write_bytes(doc.read_bytes())  # qpdf wrote output but warned
        return subprocess.CompletedProcess(cmd, 3, "", "WARNING: something")

    monkeypatch.setattr(subprocess, "run", warn)
    res = E.flatten_pdf(doc, out, dataclasses.replace(settings, qpdf_path="qpdf"))
    assert res["warnings"] is True
    assert res["pages"] == 5


def test_flatten_timeout_is_a_user_error(doc, tmp_path, settings, monkeypatch):
    def hang(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", hang)
    with pytest.raises(PdfUserError, match="timed out"):
        E.flatten_pdf(doc, tmp_path / "f.pdf", dataclasses.replace(settings, qpdf_path="q"))
