"""Every tool numbers pages alike, even in a file qpdf and PDFium repair differently: PDFium
reads qpdf's copy of a file qpdf had to repair, never its own idea of it."""
from __future__ import annotations

from pathlib import Path

import pikepdf
import pypdfium2 as pdfium

from paperboy import engine as E
from paperboy.engine import render
from paperboy.engine.base import open_pdf, pdf_problems, repaired, warnings_of
from paperboy.engine.render import pdfium_data


def damaged(path):
    """Three pages, ONE, TWO and THREE, where TWO's page dictionary holds an integer too
    large for qpdf, which drops that page; PDFium reads all three."""
    font = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R 4 0 R 5 0 R] /Count 3 >>"]
    for n, huge in ((0, b""), (1, b"/Huge 123456789012345678901234567890123456789 "),
                    (2, b"")):
        objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] " + huge +
                    b"/Resources << /Font << /F1 9 0 R >> >> /Contents %d 0 R >>" % (6 + n))
    for word in (b"ONE", b"TWO", b"THREE"):
        content = b"BT /F1 24 Tf 72 700 Td (" + word + b") Tj ET"
        objs.append(b"<< /Length %d >>\nstream\n" % len(content) + content +
                    b"\nendstream")
    objs.append(font)
    out, offsets = b"%PDF-1.7\n", []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1, xref)
    path.write_bytes(out)
    return path


def pages_of(data: bytes) -> int:
    doc = pdfium.PdfDocument(data)
    try:
        return len(doc)
    finally:
        doc.close()


def test_pdfium_sees_the_pages_qpdf_does(tmp_path, settings):
    # qpdf 12 drops the broken page and PDFium doesn't: PDFium reads qpdf's copy. qpdf
    # 11 keeps it as an empty page, so both count 3: PDFium reads the file itself.
    src = damaged(tmp_path / "in.pdf")
    data = src.read_bytes()
    with open_pdf(data, settings) as pdf:
        assert repaired(pdf) and warnings_of(pdf) == warnings_of(pdf) != []
        given = pdfium_data(pdf, data)
        assert pages_of(given) == len(pdf.pages)
        assert (given is data) == (len(pdf.pages) == 3)
    clean = tmp_path / "clean.pdf"
    with pikepdf.open(src) as pdf:
        pdf.save(clean)
    with open_pdf(clean, settings) as pdf:    # PDFium sees its pages: read as it is
        assert pdfium_data(pdf, clean.read_bytes()) == clean.read_bytes()


def test_a_file_pdfium_cant_open_is_read_as_qpdf_reads_it(tmp_path, settings,
                                                          monkeypatch):
    src = damaged(tmp_path / "in.pdf")
    data = src.read_bytes()

    def broken(*args):
        raise pdfium.PdfiumError("no")
    with open_pdf(data, settings) as pdf:
        monkeypatch.setattr(render.pdfium, "PdfDocument", broken)
        assert pdfium_data(pdf, data) != data


def test_search_numbers_pages_as_the_other_tools_do(tmp_path, settings):
    src = damaged(tmp_path / "in.pdf")
    total = E.pdf_info(src, settings)["pages"]
    found = E.search_text(src, "THREE", settings)
    assert [m["page"] for m in found["matches"]] == [total]   # the last page, either way


def test_rendering_numbers_pages_as_the_other_tools_do(tmp_path, settings):
    src = damaged(tmp_path / "in.pdf")
    out = tmp_path / "damaged"
    out.mkdir()
    r = E.render_pages(src, out, settings, dpi=36, pages="1")
    total = E.pdf_info(src, settings)["pages"]
    last = E.render_pages(src, out, settings, dpi=36, pages=str(total))
    clean = tmp_path / "clean.pdf"    # THREE alone, to compare the last page with
    pdf = pikepdf.open(src)
    pdf.pages[-1].obj.Contents.read_bytes()
    alone = pikepdf.new()
    alone.pages.append(pdf.pages[-1])
    alone.save(clean)
    pdf.close()
    out2 = tmp_path / "clean"
    out2.mkdir()
    want = E.render_pages(clean, out2, settings, dpi=36)
    assert len(r["files"]) == 1
    assert Path(last["files"][0]).read_bytes() == Path(want["files"][0]).read_bytes()


def test_a_damaged_stream_alone_isnt_saved_again(tmp_path, settings):
    pdf = pikepdf.new()
    page = pdf.add_blank_page()
    page.obj.Contents = pdf.make_stream(b"not deflate data", Filter=pikepdf.Name.FlateDecode)
    src = tmp_path / "in.pdf"
    pdf.save(src)
    data = src.read_bytes()
    with open_pdf(data, settings) as pdf:
        try:
            pdf.pages[0].Contents.read_bytes()   # qpdf warns about the stream now
        except pikepdf.PdfError:
            pass
        assert warnings_of(pdf)                  # damaged, but not its page tree
        assert pdfium_data(pdf, data) is data


def test_every_reader_sees_the_warnings_from_opening(tmp_path, settings):
    src = damaged(tmp_path / "in.pdf")
    with open_pdf(src, settings) as pdf:
        first = warnings_of(pdf)
        assert first and all(w in pdf_problems(pdf) for w in first[:5])
        assert warnings_of(pdf)[:len(first)] == first
