"""Blank-page detection.

A page is blank when it has no text object and (almost) no ink against its
background; "nearly blank" pages have a little ink and should be looked at
(view_pages) before deleting; anything else is content.
"""
from __future__ import annotations

import dataclasses
import random
import threading
import time

import pikepdf
import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from paperboy import engine as E
from paperboy.engine import render
from paperboy.errors import PdfUserError

W, H = A4


def _white(c):
    pass


def _dust(c):
    rnd = random.Random(1)
    c.setFillGray(0.3)
    for _ in range(25):
        c.circle(rnd.uniform(0, W), rnd.uniform(0, H), 0.6, fill=1, stroke=0)


def _page_number_text(c):
    c.setFont("Helvetica", 10)
    c.drawString(W / 2, 30, "3")


def _cream_paper(c):
    c.setFillColorRGB(0.95, 0.92, 0.85)
    c.rect(0, 0, W, H, fill=1, stroke=0)


def _body_text(c):
    c.setFont("Helvetica", 12)
    for i in range(30):
        c.drawString(72, 750 - 16 * i, "A line of body text " * 4)


def _small_mark(c):  # like a scanned page number: ink, but no text layer
    c.setFillGray(0)
    c.rect(W / 2 - 20, 20, 40, 14, fill=1, stroke=0)


def _solid_dark(c):
    c.setFillGray(0.1)
    c.rect(0, 0, W, H, fill=1, stroke=0)


def _invisible_text(c):  # an OCR text layer over an otherwise empty page
    t = c.beginText(72, 700)
    t.setTextRenderMode(3)
    t.textLine("Recognised text")
    c.drawText(t)


def _whitespace_text(c):  # text objects that draw nothing visible
    c.setFont("Helvetica", 12)
    c.drawString(72, 700, "     ")


def _form_field(c):  # a filled-in form field is the page's only content
    c.acroForm.textfield(name="name", value="John Smith", x=72, y=700, width=200, height=20,
                         borderWidth=0)


def _faint_lines(c):  # ruled lines in 0.85 grey, like a blank notebook page
    c.setStrokeGray(0.85)
    for y in range(80, 780, 24):
        c.line(72, y, W - 72, y)


PAGES = [_white, _dust, _page_number_text, _cream_paper, _body_text, _small_mark,
         _solid_dark, _invisible_text]
BLANK = [1, 4, 7]          # white, cream paper, a uniform dark page
NEARLY_BLANK = [2, 6]      # dust, a small scanned mark: look before deleting


def _build(path, painters=PAGES, size=A4):
    c = canvas.Canvas(str(path), pagesize=size)
    for paint in painters:
        paint(c)
        c.showPage()
    c.save()
    return path


@pytest.fixture
def sample(tmp_path):
    return _build(tmp_path / "scan.pdf")


def test_finds_the_blank_pages(sample, settings):
    res = E.find_blank_pages(sample, settings)
    assert res["path"] == str(sample)
    assert res["total_pages"] == res["pages_checked"] == len(PAGES)
    assert res["complete"] is True
    assert res["blank_pages"] == BLANK
    assert res["nearly_blank_pages"] == NEARLY_BLANK
    assert res["ink_threshold_percent"] == 0.005
    verdicts = {d["page"]: d["verdict"] for d in res["details"]}
    assert verdicts == {1: "blank", 2: "nearly_blank", 4: "blank", 6: "nearly_blank",
                        7: "blank"}
    for d in res["details"]:
        assert (d["ink_percent"] <= 0.005) is (d["verdict"] == "blank")
        assert d["ink_percent"] <= 0.5


@pytest.mark.parametrize(("painter", "verdict"), [
    (_white, "blank"), (_cream_paper, "blank"), (_solid_dark, "blank"),
    (_dust, "nearly_blank"), (_small_mark, "nearly_blank"),
    (_faint_lines, "content"),        # light marks still count (low contrast cut-off)
    (_page_number_text, "content"),   # any text object is content
    (_invisible_text, "content"),     # including an OCR layer
    (_whitespace_text, "content"),    # and whitespace-only text: errs toward keeping
    (_form_field, "content"),         # form fields are rendered, not skipped
    (_body_text, "content"),
], ids=lambda v: getattr(v, "__name__", str(v)))
def test_each_kind_of_page(tmp_path, settings, painter, verdict):
    res = E.find_blank_pages(_build(tmp_path / "one.pdf", [painter]), settings)
    found = ("blank" if res["blank_pages"] == [1]
             else "nearly_blank" if res["nearly_blank_pages"] == [1] else "content")
    assert found == verdict


def test_threshold_decides_borderline_marks(tmp_path, settings):
    doc = _build(tmp_path / "mark.pdf", [_small_mark])
    assert E.find_blank_pages(doc, settings)["blank_pages"] == []
    assert E.find_blank_pages(doc, settings, ink_threshold_percent=0.5)["blank_pages"] == [1]
    # Zero tolerates no ink at all: dust is then only nearly blank.
    dust = _build(tmp_path / "dust.pdf", [_dust])
    res = E.find_blank_pages(dust, settings, ink_threshold_percent=0)
    assert res["blank_pages"] == [] and res["nearly_blank_pages"] == [1]


@pytest.mark.parametrize("threshold", [-0.1, 5.1])
def test_threshold_out_of_range(sample, settings, threshold):
    with pytest.raises(PdfUserError, match="ink_threshold_percent must be between 0 and 5"):
        E.find_blank_pages(sample, settings, ink_threshold_percent=threshold)


def test_checks_only_selected_pages(sample, settings):
    res = E.find_blank_pages(sample, settings, pages="3-5")
    assert res["pages_checked"] == 3
    assert res["blank_pages"] == [4] and res["nearly_blank_pages"] == []


def test_no_blank_pages(tmp_path, settings):
    res = E.find_blank_pages(_build(tmp_path / "full.pdf", [_body_text] * 3), settings)
    assert res["blank_pages"] == res["nearly_blank_pages"] == res["details"] == []


def test_empty_document(tmp_path, settings):
    empty = tmp_path / "empty.pdf"
    pikepdf.new().save(empty)
    res = E.find_blank_pages(empty, settings)
    assert res == {"path": str(empty), "total_pages": 0, "pages_checked": 0, "complete": True,
                   "blank_pages": [], "nearly_blank_pages": [], "details": [],
                   "ink_threshold_percent": 0.005}


def test_rendering_is_bounded_for_huge_pages(tmp_path, settings, monkeypatch):
    sizes = []
    real = render._render

    def spy(doc, index, scale, **kwargs):
        image = real(doc, index, scale, **kwargs)
        sizes.append(image.size)
        return image

    monkeypatch.setattr(render, "_render", spy)
    huge = _build(tmp_path / "huge.pdf", [_white], size=(14400, 14400))  # 200 x 200 in
    start = time.monotonic()
    assert E.find_blank_pages(huge, settings)["blank_pages"] == [1]
    assert max(sizes[0]) <= 1000
    assert time.monotonic() - start < 5


def test_encrypted_input_needs_its_password(sample, tmp_path, settings):
    locked = tmp_path / "locked.pdf"
    with pikepdf.open(sample) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(
            user="u", owner="o", R=6, allow=pikepdf.Permissions(extract=False)))
    with pytest.raises(PdfUserError, match="supply its password"):
        E.find_blank_pages(locked, settings)
    # Analysing pages isn't copying them: the open password is enough.
    assert E.find_blank_pages(locked, settings, password="u")["blank_pages"] == BLANK


def test_page_cap(sample, settings):
    with pytest.raises(PdfUserError, match="page limit"):
        E.find_blank_pages(sample, dataclasses.replace(settings, max_pages=3))


def test_pdfium_calls_hold_the_lock(sample, settings, monkeypatch):
    held = []
    real_render = render.pdfium.PdfPage.render
    real_text = render.pdfium.PdfPage.get_objects

    def spy_render(self, *args, **kwargs):
        held.append(render._PDFIUM_LOCK.locked())
        return real_render(self, *args, **kwargs)

    def spy_text(self, *args, **kwargs):
        held.append(render._PDFIUM_LOCK.locked())
        return real_text(self, *args, **kwargs)

    monkeypatch.setattr(render.pdfium.PdfPage, "render", spy_render)
    monkeypatch.setattr(render.pdfium.PdfPage, "get_objects", spy_text)
    E.find_blank_pages(sample, settings)
    assert held and all(held)


def test_concurrent_calls_agree(sample, settings):
    results, errors = [], []

    def work():
        try:
            results.append(E.find_blank_pages(sample, settings)["blank_pages"])
        except Exception as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and results == [BLANK] * 6


def test_text_probe_failure_is_a_user_error(sample, settings, monkeypatch):
    def broken(self, *args, **kwargs):
        raise render.pdfium.PdfiumError("Failed to read page objects")

    monkeypatch.setattr(render.pdfium.PdfPage, "get_objects", broken)
    with pytest.raises(PdfUserError, match="Could not read page 1"):
        E.find_blank_pages(sample, settings)


# --- review findings: cost bounds ------------------------------------------------------


def _many_spaces(path, count):
    """One compressed page of `count` space characters: tiny file, huge text index."""
    import zlib
    content = b"BT /F1 12 Tf 72 700 Td " + b"( ) Tj " * count + b"ET"
    pdf = pikepdf.new()
    font = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica))
    stream = pikepdf.Stream(pdf, zlib.compress(content))
    stream.Filter = pikepdf.Name.FlateDecode
    pdf.add_blank_page(page_size=(612, 792))
    pdf.pages[0].Contents = pdf.make_indirect(stream)
    pdf.pages[0].Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    pdf.save(path)
    return path


def test_huge_whitespace_text_is_cheap(tmp_path, settings):
    # Building PDFium's text index for this took ~40 s; probing for a text object doesn't.
    doc = _many_spaces(tmp_path / "spaces.pdf", 200_000)
    start = time.monotonic()
    res = E.find_blank_pages(doc, settings)
    assert time.monotonic() - start < 3
    assert res["blank_pages"] == [] and res["nearly_blank_pages"] == []  # text = content


def test_time_limit_returns_a_partial_result(sample, settings, monkeypatch):
    monkeypatch.setattr(render, "BLANK_TIME_LIMIT_S", -1)  # always over the limit
    res = E.find_blank_pages(sample, settings)
    assert res["complete"] is False
    assert res["pages_checked"] == 1           # at least one page is always checked
    assert res["blank_pages"] == [1]
