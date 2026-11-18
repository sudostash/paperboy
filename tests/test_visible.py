"""visible: telling whether text a tool drew can be seen (the cases the tools' own tests
don't reach)."""
from __future__ import annotations

import time

import pikepdf
import pypdfium2 as pdfium

from paperboy.engine import visible as V
from paperboy.engine.redact_content import Budget


def test_a_form_without_resources_is_made_opaque():
    pdf = pikepdf.new()
    form = pdf.make_stream(b"", Type=pikepdf.Name.XObject, Subtype=pikepdf.Name.Form)
    V.make_opaque(pdf, form)
    assert float(form.Resources.ExtGState.Pv.ca) == 1.0


def test_nothing_to_check_shows():
    budget = Budget(time.monotonic() + 60)
    assert V.shown(None, [], 0, [], budget)
    assert V.shown(None, [], 0, [None], budget)   # a character with no ink


def test_characters_without_an_outline_have_no_ink(tmp_path, monkeypatch):
    pdf = pikepdf.new()
    page = pdf.add_blank_page()
    page.obj.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=pikepdf.Dictionary(
        Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
        BaseFont=pikepdf.Name.Helvetica)))
    page.obj.Contents = pdf.make_stream(b"BT /F1 12 Tf 72 700 Td (Hi) Tj ET")
    path = tmp_path / "in.pdf"
    pdf.save(path)
    doc = pdfium.PdfDocument(str(path))
    try:
        found = V.glyphs(doc, 0)
        assert [chr(u) for u, *_ in found] == ["H", "i"]
        assert all(ink is not None and ink[4] >= ink[3] - ink[1] for *_, ink in found)
        monkeypatch.setattr(V.pdfium_raw, "FPDFText_GetCharBox", lambda *a: False)
        assert all(ink is None for *_, ink in V.glyphs(doc, 0))

        def flat(raw, i, left, right, bottom, top):
            left.value = right.value = bottom.value = top.value = 1.0
            return True
        monkeypatch.setattr(V.pdfium_raw, "FPDFText_GetCharBox", flat)
        assert all(ink is None for *_, ink in V.glyphs(doc, 0))
    finally:
        doc.close()
