"""get_text_blocks and translate_pdf: reading a page's text as blocks, fitting each
translation into its block's place, taking the old text out and drawing the new, and
the checks that refuse what can't be done exactly."""
from __future__ import annotations

import ctypes
import hashlib
import io
import math
import time

import pikepdf
import pypdfium2 as pdfium
import pytest
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas

from paperboy import engine as E
from paperboy.engine import translate as T
from paperboy.engine.redact_content import Unsure
from paperboy.engine.render import _open
from paperboy.engine.stamp import _visible_box
from paperboy.errors import PdfUserError

Name = pikepdf.Name
PARAGRAPH = ["Thank you for choosing our hotel. Breakfast is served every",
             "morning from seven until ten in the main restaurant on the",
             "ground floor, and the pool is open all day."]
GERMAN = ("Vielen Dank, dass Sie sich für unser Hotel entschieden haben. Das Frühstück wird "
          "jeden Morgen von sieben bis zehn Uhr im Hauptrestaurant im Erdgeschoss serviert, "
          "und der Pool ist den ganzen Tag geöffnet.")


def make(path, *pages, size=(595, 842), prepare=None):
    """A PDF drawn with reportlab: one page per function, each given the canvas;
    *prepare(pdf)* can change it with pikepdf before it is saved."""
    c = canvas.Canvas(str(path), pagesize=size)
    for draw in pages:
        draw(c)
        c.showPage()
    c.save()
    if prepare:
        with pikepdf.open(path, allow_overwriting_input=True) as pdf:
            prepare(pdf)
            pdf.save(path)
    return path


def raw(path, *contents, size=(612, 792), prepare=None):
    """A PDF with one page per content stream, Helvetica as /F1."""
    pdf = pikepdf.new()
    font = pdf.make_indirect(pikepdf.Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                                BaseFont=Name.Helvetica))
    for content in contents:
        page = pdf.add_blank_page(page_size=size)
        page.obj.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
        page.obj.Contents = pdf.make_stream(content)
    if prepare:
        prepare(pdf)
    pdf.save(path, fix_metadata_version=False)
    return path


def guide(c):
    c.setFont("Helvetica-Bold", 20)
    c.drawCentredString(297.5, 780, "Welcome Guide")
    c.setFont("Helvetica", 11)
    for n, line in enumerate(PARAGRAPH):
        c.drawString(72, 740 - n * 14, line)
    for n, line in enumerate(["Reception", "12 High Street", "London"]):
        c.drawString(72, 680 - n * 14, line)
    c.drawString(72, 600, "Check-out")
    c.drawString(300, 600, "11:00")
    c.drawString(72, 585, "Wi-Fi password")
    c.drawString(300, 585, "sunny-days")
    c.setFillColorRGB(0.6, 0, 0)
    c.drawString(72, 550, "• Quiet hours start at 22:00")
    c.drawString(72, 536, "• No smoking in the rooms")
    c.setFillColorRGB(0, 0, 0)
    c.drawString(72, 500, "1,250.00")


def blocks(path, settings, **kw) -> dict:
    return E.get_text_blocks(path, settings, **kw)


def by_text(found: dict) -> dict[str, str]:
    return {b["text"]: b["id"] for b in found["blocks"]}


def text_of(path, page=0, password=None) -> str:
    with pikepdf.open(path, password=password or "") as pdf:
        buf = io.BytesIO()
        pdf.save(buf)
    doc = pdfium.PdfDocument(buf.getvalue())
    try:
        return doc[page].get_textpage().get_text_range().replace("\r\n", "\n")
    finally:
        doc.close()


def flat(text: str) -> str:
    return " ".join(text.split())


def translate(path, settings, translations, out=None, **kw):
    found = blocks(path, settings, password=kw.get("password"))
    ids = by_text(found)
    given = [{"block": ids.get(old, old), "text": new} for old, new in translations.items()]
    out = out or path.with_name("out.pdf")
    return E.translate_pdf(path, out, settings, found["digest"], given, **kw), out


def page_of(path, index=0) -> T._Page:
    with pikepdf.open(path) as pdf:
        visible = _visible_box(pdf.pages[index])
    doc = _open(path.read_bytes())
    try:
        return T._read_page(doc, index, visible, time.monotonic() + 60, obstacles=True)
    finally:
        doc.close()


def inked(path, box, page=0, scale=2.0) -> bool:
    """Whether anything is drawn in *box* (x0, y0, x1, y1 in points)."""
    doc = pdfium.PdfDocument(str(path))
    try:
        image = doc[page].render(scale=scale).to_pil().convert("L")
        height = doc[page].get_height()
    finally:
        doc.close()
    x0, y0, x1, y1 = box
    crop = image.crop((int(x0 * scale), int((height - y1) * scale),
                       int(x1 * scale), int((height - y0) * scale)))
    return crop.getextrema()[0] < 200


# --- reading blocks ---------------------------------------------------------------------


def test_a_page_is_read_as_blocks(tmp_path, settings):
    src = make(tmp_path / "in.pdf", guide)
    found = blocks(src, settings)
    texts = [b["text"] for b in found["blocks"]]
    assert texts == [
        "Welcome Guide", " ".join(PARAGRAPH), "Reception\n12 High Street\nLondon",
        "Check-out\nWi-Fi password", "11:00 sunny-days", "• Quiet hours start at 22:00",
        "• No smoking in the rooms"]    # the amount has no letters: not listed
    assert [b["id"] for b in found["blocks"]] == [f"p1-b{n}" for n in range(1, 8)]
    assert [b["lines"] for b in found["blocks"]] == [1, 3, 3, 2, 2, 1, 1]
    assert found["digest"] == hashlib.sha256(src.read_bytes()).hexdigest()
    assert found["page_count"] == 1 and found["pages_read"] == [1]
    assert found["next_page"] is None
    assert found["pages_without_text"] == [] and found["pages_with_invisible_text"] == []


def test_long_documents_come_in_parts(tmp_path, settings, monkeypatch):
    src = make(tmp_path / "in.pdf", guide, guide, guide)
    monkeypatch.setattr(T, "MAX_REPLY_TEXT", 700)
    first = blocks(src, settings)
    assert first["pages_read"] == [1, 2] and first["next_page"] == 3
    rest = blocks(src, settings, pages="3-")
    assert rest["pages_read"] == [3] and rest["next_page"] is None
    assert rest["blocks"][0]["id"] == "p3-b1"


def test_one_page_is_always_read_even_past_the_limit(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(T, "MAX_REPLY_TEXT", 10)
    found = blocks(make(tmp_path / "in.pdf", guide, guide), settings)
    assert found["pages_read"] == [1] and found["next_page"] == 2


def test_scans_and_blank_pages_have_no_text(tmp_path, settings):
    src = make(tmp_path / "in.pdf", guide, lambda c: None)
    assert blocks(src, settings)["pages_without_text"] == [2]


@pytest.mark.skipif(not hasattr(T.pdfium_raw, "FPDFText_GetTextObject"),
                    reason="this PDFium can't tell (the next test covers it)")
def test_invisible_text_is_in_no_block(tmp_path, settings):
    src = raw(tmp_path / "in.pdf", b"BT /F1 12 Tf 3 Tr 72 700 Td (Hidden words) Tj ET "
                                   b"BT /F1 12 Tf 0 Tr 72 600 Td (Shown words) Tj ET")
    found = blocks(src, settings)
    assert [b["text"] for b in found["blocks"]] == ["Shown words"]
    assert found["pages_with_invisible_text"] == [1]


def test_old_pdfium_lists_invisible_text_and_the_removal_refuses_it(tmp_path, settings,
                                                                    monkeypatch):
    monkeypatch.delattr(T.pdfium_raw, "FPDFText_GetTextObject", raising=False)
    src = raw(tmp_path / "in.pdf", b"BT /F1 12 Tf 3 Tr 72 700 Td (Hidden words) Tj ET")
    assert [b["text"] for b in blocks(src, settings)["blocks"]] == ["Hidden words"]
    with pytest.raises(PdfUserError, match="invisible: it is the searchable layer"):
        translate(src, settings, {"Hidden words": "Versteckte Wörter"})


def test_outlined_text_is_refused(tmp_path, settings):
    src = raw(tmp_path / "in.pdf", b"BT /F1 12 Tf 1 Tr 72 700 Td (Outlined words) Tj ET")
    with pytest.raises(PdfUserError, match="drawn in outline"):
        translate(src, settings, {"Outlined words": "Umrandete Wörter"})


def test_text_off_the_page_and_text_of_no_size_are_in_no_block(tmp_path, settings):
    src = raw(tmp_path / "in.pdf", b"BT /F1 12 Tf 72 900 Td (Above the page) Tj ET "
                                   b"BT /F1 0 Tf 72 600 Td (No size) Tj ET "
                                   b"BT /F1 12 Tf 72 500 Td (On the page) Tj ET")
    assert [b["text"] for b in blocks(src, settings)["blocks"]] == ["On the page"]


def test_flattened_text_is_in_no_block(tmp_path, settings):
    src = raw(tmp_path / "in.pdf", b"BT /F1 12 Tf 1 0 0 0 72 600 Tm (Squashed) Tj ET "
                                   b"BT /F1 12 Tf 72 500 Td (Upright) Tj ET")
    assert [b["text"] for b in blocks(src, settings)["blocks"]] == ["Upright"]


def test_a_footnote_mark_stays_on_its_line(tmp_path, settings):
    src = raw(tmp_path / "in.pdf",
              b"BT /F1 12 Tf 72 700 Td (Deliveries grew) Tj 7 Tf 3 Ts (1) Tj "
              b"12 Tf 0 Ts ( this year.) Tj ET")
    assert [b["text"] for b in blocks(src, settings)["blocks"]] == [
        "Deliveries grew1 this year."]


def test_soft_hyphens_and_symbols_read_as_they_show(tmp_path):
    line = T._Line((1, 0, 1.0), (1, 0, 0, 1), 10.0, 1.0, (0, 0, 0, 255), 0, 10, 0,
                   False)
    line.parts = ["d", "a", "­", "m", "\x07", "­", " "]
    line.finish()
    assert line.text == "dam-"


def test_blocks_split_where_the_text_stops_going_on(tmp_path, settings):
    def page(c):
        c.setFont("Helvetica", 11)
        c.drawString(72, 700, "Plain line")
        c.setFont("Helvetica-Bold", 11)
        c.drawString(72, 686, "Bold line")           # weight differs
        c.setFont("Helvetica", 16)
        c.drawString(72, 668, "Big line")            # size differs
        c.setFont("Helvetica", 11)
        c.drawString(72, 600, "Far below")           # too far below
        c.drawString(72, 586, "Next one")
        c.drawString(72, 568, "Spaced away")         # spacing changes
        c.drawString(400, 554, "Off to the side")    # not under the block
        c.drawString(72, 520, "List intro")
        c.drawString(72, 506, "1. First item")       # a list marker
        c.drawString(72, 470, "Close")
        c.drawString(72, 467, "Too close")           # less than half a line below
    texts = [b["text"] for b in blocks(make(tmp_path / "in.pdf", page), settings)["blocks"]]
    assert texts == ["Plain line", "Bold line", "Big line", "Far below\nNext one",
                     "Spaced away", "Off to the side", "List intro", "1. First item",
                     "Close", "Too close"]


def test_a_colour_change_starts_a_block_and_long_blocks_split(tmp_path, settings,
                                                              monkeypatch):
    def page(c):
        c.setFont("Helvetica", 11)
        c.drawString(72, 700, "black words")
        c.setFillColorRGB(1, 0, 0)
        c.drawString(72, 686, "red words")
        c.drawString(72, 672, "more red words")
    monkeypatch.setattr(T, "MAX_BLOCK_TEXT", 12)
    texts = [b["text"] for b in blocks(make(tmp_path / "in.pdf", page), settings)["blocks"]]
    assert texts == ["black words", "red words", "more red words"]


def test_alignment_is_read_from_the_lines(tmp_path, settings):
    def page(c):
        c.setFont("Helvetica", 11)
        for n, text in enumerate(["Right one", "Right number two"]):
            c.drawRightString(500, 700 - n * 14, text)
        for n, text in enumerate(["Centred", "Centred line two"]):
            c.drawCentredString(200, 600 - n * 14, text)
        for n, text in enumerate(["Ragged start", "and ragged end"]):
            c.drawString(72 + n * 30, 500 - n * 14, text)
    page_ = page_of(make(tmp_path / "in.pdf", page))
    assert [b.align for b in page_.listed] == ["right", "center", "left"]


def test_text_at_an_angle_is_read_along_its_line(tmp_path, settings):
    def page(c):
        c.saveState()
        c.translate(150, 150)
        c.rotate(45)
        c.setFont("Helvetica-Bold", 60)
        c.drawString(0, 0, "CONFIDENTIAL")
        c.restoreState()
        c.saveState()
        c.translate(560, 100)
        c.rotate(90)
        c.setFont("Helvetica", 11)
        c.drawString(0, 0, "Printed sideways")
        c.restoreState()
    texts = [b["text"] for b in blocks(make(tmp_path / "in.pdf", page), settings)["blocks"]]
    assert texts == ["CONFIDENTIAL", "Printed sideways"]


def test_reading_too_much_is_refused(tmp_path, settings, monkeypatch):
    src = make(tmp_path / "in.pdf", guide)
    monkeypatch.setattr(T, "MAX_PAGE_TEXT", 50)
    with pytest.raises(PdfUserError, match="more than 50 characters"):
        blocks(src, settings)
    monkeypatch.setattr(T, "MAX_PAGE_TEXT", 100_000)
    monkeypatch.setattr(T, "TRANSLATE_TIME_LIMIT_S", -1)
    with pytest.raises(PdfUserError, match="took longer than"):
        blocks(src, settings)


def test_pages_with_too_much_content_are_refused(tmp_path, settings, monkeypatch):
    src = make(tmp_path / "in.pdf", guide, guide)
    monkeypatch.setattr(T, "MAX_PAGE_CONTENT", 10)
    with pytest.raises(PdfUserError, match="Pages 1, 2 have too much content"):
        blocks(src, settings)
    monkeypatch.setattr(T, "MAX_PAGE_CONTENT", 0)
    src = make(tmp_path / "many.pdf", *[guide] * 25)
    with pytest.raises(PdfUserError, match="20 …"):
        blocks(src, settings)


def test_copying_must_be_allowed(tmp_path, settings):
    src = make(tmp_path / "plain.pdf", guide)
    locked = tmp_path / "in.pdf"
    with pikepdf.open(src) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(
            user="", owner="owner", R=6, allow=pikepdf.Permissions(extract=False)))
    with pytest.raises(PdfUserError, match="copying"):
        blocks(locked, settings)


# --- translating ------------------------------------------------------------------------


def test_a_page_is_translated_in_place(tmp_path, settings):
    src = make(tmp_path / "in.pdf", guide)
    r, out = translate(src, settings, {
        "Welcome Guide": "Willkommen",
        " ".join(PARAGRAPH): GERMAN,
        "Reception\n12 High Street\nLondon": "Rezeption\n12 High Street\nLondon",
        "Check-out\nWi-Fi password": "Abreise\nWLAN-Passwort",
        "• Quiet hours start at 22:00": "• Nachtruhe ab 22:00 Uhr",
    }, language="de")
    text = flat(text_of(out))
    assert "Welcome" not in text and "Breakfast" not in text and "Reception" not in text
    for kept in ("11:00", "sunny-days", "No smoking in the rooms", "1,250.00"):
        assert kept in text
    assert flat(GERMAN) in text and "Abreise WLAN-Passwort" in text
    assert r["blocks_translated"] == 5 and r["pages_translated"] == [1]
    assert r["fonts"] == ["Helvetica", "Helvetica-Bold"]
    assert r["language"] == "de" and r["new_text_untagged"] is False
    assert r["output"] == str(out) and r["originals_kept"] == 0
    assert [s["block"] for s in r["shrunk"]] == [by_text(blocks(src, settings))[
        " ".join(PARAGRAPH)]]
    with pikepdf.open(out) as pdf:
        assert str(pdf.Root.Lang) == "de"


def test_the_translation_keeps_the_block_colour_and_reading_order(tmp_path, settings):
    src = make(tmp_path / "in.pdf", guide)
    _, out = translate(src, settings, {"• Quiet hours start at 22:00": "• Nachtruhe"})
    text = flat(text_of(out))
    assert text.index("sunny-days") < text.index("Nachtruhe") < text.index("No smoking")
    doc = pdfium.PdfDocument(str(out))
    try:
        textpage = doc[0].get_textpage()
        index = textpage.get_text_range().index("Nachtruhe")
        rgba = [ctypes.c_uint() for _ in range(4)]
        pdfium.raw.FPDFText_GetFillColor(textpage.raw, index, *rgba)
        assert [v.value for v in rgba[:3]] == [153, 0, 0]
    finally:
        doc.close()


def test_greek_and_cyrillic_are_drawn_in_dejavu(tmp_path, settings):
    src = make(tmp_path / "in.pdf", guide)
    r, out = translate(src, settings, {"Welcome Guide": "Καλώς ήρθατε",
                                       "Check-out\nWi-Fi password": "Выезд\nПароль"})
    assert r["fonts"] == ["PaperboySans", "PaperboySans-Bold"]
    text = flat(text_of(out))
    assert "Καλώς ήρθατε" in text and "Выезд Пароль" in text


@pytest.mark.parametrize(("text", "message"), [
    ("你好", "no available font can show"),
    ("مرحبا", "right-to-left"),
    ("नमस्ते", "letters join or reorder"),
    ("Hi 😀", "beyond U\\+FFFF"),
    ("Hi\x07", "control or invalid"),
    ("  \n ", "is empty"),
    ("x" * 8001, "at most 8,000"),
    (5, "must be text"),
])
def test_translations_that_cant_be_drawn_are_refused(tmp_path, settings, text, message):
    src = make(tmp_path / "in.pdf", guide)
    with pytest.raises(PdfUserError, match=message):
        translate(src, settings, {"Welcome Guide": text})


@pytest.mark.parametrize(("translations", "message"), [
    ("p1-b1", "Give 1 to 2,000"),
    ([], "Give 1 to 2,000"),
    (["p1-b1"], "must be {\"block\""),
    ([{"block": "p1-b1"}], "must be {\"block\""),
    ([{"block": 1, "text": "x"}], "must be {\"block\""),
    ([{"block": "b1", "text": "x"}], "an id from get_text_blocks"),
    ([{"block": "p1-b1", "text": "x"}, {"block": "p1-b1", "text": "y"}], "twice"),
])
def test_bad_translations_are_refused(tmp_path, settings, translations, message):
    src = make(tmp_path / "in.pdf", guide)
    digest = blocks(src, settings)["digest"]
    with pytest.raises(PdfUserError, match=message):
        E.translate_pdf(src, tmp_path / "out.pdf", settings, digest, translations)


def test_line_breaks_and_spaces_in_a_translation_are_tidied():
    assert T._translation("\r\n  Eins  \t zwei\r\ndrei \n", "p1-b1") == "Eins zwei\ndrei"


@pytest.mark.parametrize("language", ["deutsch!", 5, "d"])
def test_a_bad_language_is_refused(tmp_path, settings, language):
    src = make(tmp_path / "in.pdf", guide)
    with pytest.raises(PdfUserError, match="language tag"):
        translate(src, settings, {"Welcome Guide": "Willkommen"}, language=language)


def test_the_digest_must_be_this_files(tmp_path, settings):
    src = make(tmp_path / "in.pdf", guide)
    for digest in ("0" * 64, None):
        with pytest.raises(PdfUserError, match="isn't the one get_text_blocks read"):
            E.translate_pdf(src, tmp_path / "out.pdf", settings, digest,
                            [{"block": "p1-b1", "text": "Willkommen"}])
    assert not (tmp_path / "out.pdf").exists()


def test_block_ids_must_exist(tmp_path, settings):
    src = make(tmp_path / "in.pdf", guide)
    with pytest.raises(PdfUserError, match="Page 1 has no block p1-b99"):
        translate(src, settings, {"p1-b99": "x"})
    with pytest.raises(PdfUserError, match="The PDF has 1 pages"):
        translate(src, settings, {"p2-b1": "x"})


def test_a_translation_with_no_room_is_refused(tmp_path, settings):
    def page(c):
        c.setFont("Helvetica", 11)
        c.drawString(72, 700, "Name")
        c.drawString(150, 700, "value")
        c.rect(72, 690, 200, 3, fill=1)       # no room below either
    src = make(tmp_path / "in.pdf", page)
    with pytest.raises(PdfUserError, match="no room for their translations, even at 60%"):
        translate(src, settings, {"Name": "Vollständiger Name des Antragstellers"})


def test_many_blocks_without_room_are_listed_briefly(tmp_path, settings):
    def page(c):
        c.setFont("Helvetica", 11)
        for n in range(22):
            c.drawString(72, 760 - n * 30, f"Item{n}")
            c.drawString(150, 760 - n * 30, "value")
            c.rect(72, 753 - n * 30, 200, 3, fill=1)
    src = make(tmp_path / "in.pdf", page)
    found = blocks(src, settings)
    given = [{"block": b["id"], "text": "Ein viel zu langer Eintrag"}
             for b in found["blocks"] if b["text"].startswith("Item")]
    with pytest.raises(PdfUserError, match="p1-b39 … have no room"):
        E.translate_pdf(src, tmp_path / "out.pdf", settings, found["digest"], given)


def test_a_one_line_block_runs_on_into_free_space(tmp_path, settings):
    def page(c):
        c.setFont("Helvetica", 11)
        c.drawString(72, 700, "Name")
        c.drawString(300, 700, "Jane")
    src = make(tmp_path / "in.pdf", page)
    r, out = translate(src, settings, {"Name": "Vollständiger Name"})
    assert r["shrunk"] == []
    assert "Vollständiger Name" in text_of(out)


def test_neighbours_both_translated_share_the_space_between(tmp_path, settings):
    def page(c):
        c.setFont("Helvetica", 11)
        c.drawString(72, 700, "Left")
        c.drawRightString(200, 700, "Right")
    src = make(tmp_path / "in.pdf", page)
    r, out = translate(src, settings, {"Left": "Links links", "Right": "Rechts rechts"})
    text = flat(text_of(out))
    assert "Links links" in text and "Rechts rechts" in text


def test_text_across_a_block_stops_it_running_on(tmp_path, settings):
    def page(c):
        c.setFont("Helvetica", 11)
        c.drawString(72, 700, "Label")
        c.setFont("Helvetica", 6)
        c.drawString(80, 703, "note")    # smaller, over the label: a block of its own
    page_ = page_of(make(tmp_path / "in.pdf", page))
    label = page_.listed[0]
    assert T._room(label, page_, set()) == (label.x0, label.x1)


def test_pictures_beside_a_block_stop_it_running_on(tmp_path, settings):
    def page(c):
        c.setFont("Helvetica", 11)
        c.drawString(100, 700, "Label")
        c.rect(140, 695, 60, 14, fill=1)     # a box after it
        c.rect(40, 695, 50, 14, fill=1)      # and before it
        c.rect(110, 690, 5, 30, fill=1)      # and one across it
        c.drawString(100, 600, "Other")
        c.rect(30, 590, 400, 30, fill=0)     # a frame round it: no obstacle
    page_ = page_of(make(tmp_path / "in.pdf", page))
    label, other = page_.listed
    assert T._room(label, page_, set()) == (label.x0, label.x1)
    left, right = T._room(other, page_, set())
    assert left < other.x0 - 50 and right > other.x1 + 300


def test_paths_are_measured_part_by_part(tmp_path, settings):
    def page(c):
        c.setFont("Helvetica", 11)
        c.drawString(100, 700, "Label")
        p = c.beginPath()            # one path: a box after the label, one far below
        p.rect(140, 695, 60, 14)
        p.rect(60, 100, 400, 20)
        c.drawPath(p, fill=1)
    page_ = page_of(make(tmp_path / "in.pdf", page))
    label = page_.listed[0]
    assert T._room(label, page_, set())[1] < 140


def test_paths_that_cant_be_read_part_by_part_use_their_bounds(tmp_path, settings,
                                                               monkeypatch):
    def page(c):
        c.setFont("Helvetica", 11)
        c.drawString(100, 700, "Label")
        c.rect(140, 695, 60, 14, fill=1)
    src = make(tmp_path / "in.pdf", page)
    monkeypatch.setattr(T.pdfium_raw, "FPDFPageObj_GetMatrix", lambda obj, m: False)
    assert T._room(page_of(src).listed[0], page_of(src), set())[1] < 140
    monkeypatch.undo()
    monkeypatch.setattr(T.pdfium_raw, "FPDFPathSegment_GetPoint", lambda s, x, y: False)
    assert T._room(page_of(src).listed[0], page_of(src), set())[1] < 140


def test_pages_with_too_many_drawings_get_no_extra_room(tmp_path, settings, monkeypatch):
    def page(c):
        c.setFont("Helvetica", 11)
        c.drawString(100, 700, "Label")
        c.rect(400, 100, 10, 10)
    src = make(tmp_path / "in.pdf", page)
    for name in ("MAX_OBJECTS", "MAX_SEGMENTS"):
        with monkeypatch.context() as m:
            m.setattr(T, name, 0)
            page_ = page_of(src)
            label = page_.listed[0]
            assert page_.things is None and page_.obstacles(label.key, label.matrix) is None
            assert T._room(label, page_, set()) == (label.x0, label.x1)
            assert T._floor(label, page_, label.x0, label.x1) == label.bottom


def test_a_paragraph_grows_into_free_space_below(tmp_path, settings):
    def page(c):
        c.setFont("Helvetica", 11)
        for n, line in enumerate(PARAGRAPH):
            c.drawString(72, 740 - n * 14, line)
        c.drawString(72, 500, "Far below")
    src = make(tmp_path / "in.pdf", page)
    r, out = translate(src, settings, {" ".join(PARAGRAPH): GERMAN})
    assert r["shrunk"] == []
    assert not inked(out, (72, 520, 500, 640))     # the gap above "Far below" stays


def test_growing_stops_above_drawings_and_form_fields(tmp_path, settings):
    def page(c):
        c.setFont("Helvetica", 11)
        for n, line in enumerate(PARAGRAPH):
            c.drawString(72, 740 - n * 14, line)
        c.rect(72, 690, 300, 10, fill=1)                  # just below the paragraph
        c.rect(50, 50, 500, 760, fill=0)                  # a frame round the page
    src = make(tmp_path / "in.pdf", page)
    page_ = page_of(src)
    block = page_.listed[0]
    assert T._floor(block, page_, block.x0, block.x1) == pytest.approx(block.bottom)
    assert T._fit(block, GERMAN, page_, {block.number}) and block.scale < 1

    def form(pdf):
        widget = pdf.make_indirect(pikepdf.Dictionary(
            Type=Name.Annot, Subtype=Name.Widget, Rect=[72, 680, 300, 700], FT=Name.Tx,
            T="field"))
        link = pdf.make_indirect(pikepdf.Dictionary(Type=Name.Annot, Subtype=Name.Link,
                                                    Rect=[72, 600, 300, 640]))
        pdf.pages[0].obj.Annots = pdf.make_indirect(pikepdf.Array(
            [widget, link, pikepdf.Dictionary(Subtype=Name.Text, Rect=[1, 2, 3])]))
        pdf.Root.AcroForm = pikepdf.Dictionary(Fields=[widget])
    src = make(tmp_path / "field.pdf", lambda c: [c.setFont("Helvetica", 11)] + [
        c.drawString(72, 740 - n * 14, line) for n, line in enumerate(PARAGRAPH)],
        prepare=form)
    with pikepdf.open(src) as pdf:
        assert T._annotation_boxes(pdf.pages[0]) == [(72.0, 680.0, 300.0, 700.0)]
    r, _ = translate(src, settings, {" ".join(PARAGRAPH): GERMAN})
    assert r["shrunk"] and "form fields" in r["not_translated"]


def test_a_paragraph_keeps_its_indents(tmp_path, settings):
    def page(c):
        c.setFont("Helvetica", 11)
        c.drawString(72, 700, "• Delivery is free on every order over fifty pounds,")
        c.drawString(82, 686, "and returns are free within thirty days of the date.")
        c.drawString(72, 600, "end")
    src = make(tmp_path / "in.pdf", page)
    page_ = page_of(src)
    block = page_.listed[0]
    assert block.wraps
    assert T._fit(block, "• Die Lieferung ist ab fünfzig Pfund kostenlos, und "
                         "Rücksendungen sind dreißig Tage lang frei.", page_, {block.number})
    assert [round(x) for x, *_ in block.placed] == [72, 82]


def test_a_paragraph_starting_part_way_along_a_line(tmp_path, settings):
    # "Ref:" then a paragraph that starts after it: the paragraph's first line ends
    # where it did, its later lines use the full width.
    def page(c):
        c.setFont("Helvetica", 11)
        c.drawString(72, 700, "Opening words of the paragraph")
        c.drawString(300, 700, "Ref: 12")
        c.drawString(72, 686, "carry on across the whole width of the column here and")
        c.drawString(72, 672, "end on the last line of it.")
    src = make(tmp_path / "in.pdf", page)
    page_ = page_of(src)
    block = next(b for b in page_.listed if b.text.startswith("Opening"))
    assert T._next_beside(block, block.lines[0], page_) < 300
    assert T._next_beside(block, block.lines[1], page_) == math.inf
    r, out = translate(src, settings, {block.text: "Einleitende Worte des Absatzes gehen "
                                                   "weiter über die ganze Breite der Spalte "
                                                   "und enden auf der letzten Zeile."})
    assert "Ref: 12" in text_of(out)


def test_centred_and_right_aligned_blocks_stay_so(tmp_path, settings):
    def page(c):
        c.setFont("Helvetica", 11)
        c.drawCentredString(297.5, 760, "A centred title")
        for n, text in enumerate(["Right one", "Right number two"]):
            c.drawRightString(500, 700 - n * 14, text)
    src = make(tmp_path / "in.pdf", page)
    page_ = page_of(src)
    title, right = page_.listed
    assert T._fit(title, "Ein zentrierter Titel", page_, {title.number, right.number})
    x, _, _, drawn = title.placed[0]
    assert x + drawn / 2 == pytest.approx(297.5, abs=0.5)
    assert T._fit(right, "Rechts\nRechts Nummer zwei", page_, {title.number, right.number})
    assert all(x + drawn == pytest.approx(500, abs=0.5) for x, _, _, drawn in right.placed)


def test_translations_that_would_overlap_are_refused(tmp_path, settings):
    def page(c):
        c.setFont("Helvetica", 11)
        c.drawString(72, 700, "Short")
        c.drawString(160, 700, "Next")
    src = make(tmp_path / "in.pdf", page)
    page_ = page_of(src)
    one, two = page_.listed
    one.placed, one.drawn_size = [(72, 700, "x", 100)], 11
    two.placed, two.drawn_size = [(160, 700, "y", 20)], 11
    with pytest.raises(PdfUserError, match="would run into other text"):
        T._clashes(page_, [one])
    with pytest.raises(PdfUserError, match="would run into other text on page 1"):
        T._clashes(page_, [two, one])
    one.placed = [(72, 700, "x", 30)]
    T._clashes(page_, [one, two])


def test_text_drawn_over_a_block_is_refused(tmp_path, settings):
    src = raw(tmp_path / "in.pdf",
              b"BT /F1 12 Tf 72 700 Td (Words under a stamp) Tj ET "
              b"BT /F1 12 Tf 0 1 -1 0 108 698 Tm (X) Tj ET")   # a stamp across it
    with pytest.raises(PdfUserError, match="has other text drawn over it"):
        translate(src, settings, {"Words under a stamp": "Wörter unter einem Stempel"})


def test_text_at_an_angle_is_translated(tmp_path, settings):
    def page(c):
        c.setFont("Helvetica", 11)
        c.drawString(250, 280, "Body text")    # across the watermark's band
        c.saveState()
        c.translate(150, 150)
        c.rotate(45)
        c.setFillColorRGB(1, 0.5, 0.5, alpha=0.5)
        c.setFont("Helvetica-Bold", 60)
        c.drawString(0, 0, "CONFIDENTIAL")
        c.restoreState()
    src = make(tmp_path / "in.pdf", page)
    r, out = translate(src, settings, {"CONFIDENTIAL": "VERTRAULICH", "Body text": "Text"})
    text = flat(text_of(out))
    assert "VERTRAULICH" in text and "CONFIDENTIAL" not in text and "Text" in text


def test_a_rotated_page_is_translated(tmp_path, settings):
    src = make(tmp_path / "in.pdf", guide,
               prepare=lambda pdf: setattr(pdf.pages[0].obj, "Rotate", 90))
    r, out = translate(src, settings, {"Welcome Guide": "Willkommen"})
    assert "Willkommen" in text_of(out)


def test_tagged_text_keeps_its_tag(tmp_path, settings):
    def tag(pdf):
        pdf.Root.StructTreeRoot = pdf.make_indirect(pikepdf.Dictionary(
            Type=Name.StructTreeRoot))
    src = raw(tmp_path / "in.pdf",
              b"/P <</MCID 0>> BDC BT /F1 12 Tf 72 700 Td (Tagged words) Tj ET EMC",
              prepare=tag)
    r, out = translate(src, settings, {"Tagged words": "Markierte Wörter"})
    assert r["new_text_untagged"] is False
    with pikepdf.open(out) as pdf:
        ops = [str(op) for _, op in pikepdf.parse_content_stream(pdf.pages[0])]
    assert ops.index("BDC") < ops.index("Do") < ops.index("EMC")


def test_other_places_with_text_are_reported(tmp_path, settings):
    def prepare(pdf):
        pdf.docinfo["/Title"] = "Welcome Guide"
        bookmark = pdf.make_indirect(pikepdf.Dictionary(Title="Welcome"))
        pdf.Root.Outlines = pdf.make_indirect(pikepdf.Dictionary(First=bookmark,
                                                                 Last=bookmark))
        pdf.Root.Metadata = pdf.make_stream(b"<x:xmpmeta/>")
        note = pikepdf.Dictionary(Type=Name.Annot, Subtype=Name.Text, Rect=[0, 0, 9, 9],
                                  Contents="A comment")
        pdf.pages[0].obj.Annots = pdf.make_indirect(pikepdf.Array([note]))
    src = make(tmp_path / "in.pdf", guide, prepare=prepare)
    r, _ = translate(src, settings, {"Welcome Guide": "Willkommen"})
    assert r["not_translated"] == ["document info", "XMP metadata", "bookmarks",
                                   "comments"]
    plain = make(tmp_path / "plain.pdf", guide,
                 prepare=lambda pdf: pdf.pages[0].obj.__setitem__("/Annots", pikepdf.Array(
                     [pikepdf.Dictionary(Subtype=Name.Text, Contents=" ")])))
    r, _ = translate(plain, settings, {"Welcome Guide": "Willkommen"},
                     out=tmp_path / "plain_out.pdf")
    assert r["not_translated"] == ["document info"]   # reportlab's "untitled"


def test_signed_input_is_refused(tmp_path, settings):
    src = make(tmp_path / "in.pdf", guide, prepare=lambda pdf: setattr(
        pdf.Root, "Perms", pikepdf.Dictionary(DocMDP=pikepdf.Dictionary())))
    with pytest.raises(PdfUserError, match="digitally signed"):
        translate(src, settings, {"Welcome Guide": "Willkommen"})


def test_reader_usage_rights_and_thumbnails_are_dropped(tmp_path, settings):
    def prepare(pdf):
        pdf.Root.Perms = pikepdf.Dictionary(UR3=pdf.make_indirect(pikepdf.Dictionary(
            Type=Name.Sig, ByteRange=[0, 1, 2, 3])))
        pdf.pages[0].obj.Thumb = pdf.make_stream(b"old picture")
    src = make(tmp_path / "in.pdf", guide, prepare=prepare)
    _, out = translate(src, settings, {"Welcome Guide": "Willkommen"})
    with pikepdf.open(out) as pdf:
        assert "/Perms" not in pdf.Root and "/Thumb" not in pdf.pages[0].obj


def test_encrypted_input_stays_protected(tmp_path, settings):
    src = make(tmp_path / "plain.pdf", guide)
    locked = tmp_path / "in.pdf"
    with pikepdf.open(src) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(user="pw", owner="pw", R=6))
    r, out = translate(locked, settings, {"Welcome Guide": "Willkommen"}, password="pw")
    assert r["password_protected"] is True
    assert "Willkommen" in text_of(out, password="pw")


def test_forbidden_changes_need_the_owner_password(tmp_path, settings):
    src = make(tmp_path / "plain.pdf", guide)
    locked = tmp_path / "in.pdf"
    with pikepdf.open(src) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(
            user="", owner="owner", R=6, allow=pikepdf.Permissions(modify_other=False)))
    with pytest.raises(PdfUserError, match="does not allow changing"):
        translate(locked, settings, {"Welcome Guide": "Willkommen"})


def test_content_the_removal_cant_read_exactly_is_refused(tmp_path, settings, monkeypatch):
    src = make(tmp_path / "in.pdf", guide)

    def unsure(*args):
        raise Unsure("a font can't be read")
    monkeypatch.setattr(T, "_remove", unsure)
    with pytest.raises(PdfUserError, match="can't be translated exactly: a font can't"):
        translate(src, settings, {"Welcome Guide": "Willkommen"})


def test_a_removal_that_changes_other_text_is_refused(tmp_path, settings, monkeypatch):
    src = make(tmp_path / "in.pdf", guide)
    monkeypatch.setattr(T, "_verify", lambda *args: "text outside the areas changed")
    with pytest.raises(PdfUserError, match="text outside the translated blocks"):
        translate(src, settings, {"Welcome Guide": "Willkommen"})
    assert not (tmp_path / "out.pdf").exists()


def test_translations_that_cant_be_read_back_are_refused(tmp_path, settings, monkeypatch):
    src = make(tmp_path / "in.pdf", guide)
    real = T._confirm

    def wrong(index, before, after, blocks_, wanted, budget):
        return real(index, before, after, blocks_, {k: v + "!" for k, v in wanted.items()},
                    budget)
    monkeypatch.setattr(T, "_confirm", wrong)
    with pytest.raises(PdfUserError, match="block p1-b1 on page 1 couldn't be confirmed"):
        translate(src, settings, {"Welcome Guide": "Willkommen"})


def test_extra_text_on_the_page_fails_the_read_back(tmp_path, settings):
    src = make(tmp_path / "in.pdf", guide)
    page_ = page_of(src)
    block = page_.listed[0]
    block.placed, block.drawn_size = [(200, 780, "Hi", 30)], 20
    before = [(ord("Z"), 50.0, 50.0)]
    budget = page_.budget
    T._confirm(0, before, [(ord("Z"), 50.2, 50.1), (ord("H"), 205, 785),
                           (ord("i"), 215, 785)], [block], {block.id: "Hi"}, budget)
    with pytest.raises(PdfUserError, match="translations on page 1 couldn't be confirmed"):
        T._confirm(0, before, [(ord("H"), 205, 785), (ord("i"), 215, 785),
                               (ord("Q"), 400, 400)], [block], {block.id: "Hi"}, budget)


def test_translating_takes_a_time_limit(tmp_path, settings, monkeypatch):
    src = make(tmp_path / "in.pdf", guide)
    found = blocks(src, settings)
    monkeypatch.setattr(T, "TRANSLATE_TIME_LIMIT_S", -1)
    with pytest.raises(PdfUserError, match="took longer than"):
        E.translate_pdf(src, tmp_path / "out.pdf", settings, found["digest"],
                        [{"block": "p1-b1", "text": "Willkommen"}])


def test_a_drawing_across_a_blocks_last_line_leaves_no_room_below(tmp_path, settings):
    def page(c):
        c.setFont("Helvetica", 11)
        c.drawString(72, 700, "Label")
        c.rect(100, 650, 20, 52, fill=0)    # from inside the line to below it
    page_ = page_of(make(tmp_path / "in.pdf", page))
    label = page_.listed[0]
    assert T._floor(label, page_, label.x0, label.x1 + 50) == label.bottom


def test_pictures_and_lone_moves_are_measured(tmp_path, settings, monkeypatch):
    from PIL import Image
    picture = tmp_path / "p.png"
    Image.new("RGB", (4, 4), "red").save(picture)

    def page(c):
        c.setFont("Helvetica", 11)
        c.drawString(72, 700, "Label")
        c.drawImage(str(picture), 200, 690, 30, 30)
    src = make(tmp_path / "in.pdf", page,
               prepare=lambda pdf: pdf.pages[0].contents_add(
                   pdf.make_stream(b"300 300 m 400 400 m 450 450 l S")))
    things = page_of(src).things
    assert (300.0, 300.0, 300.0, 300.0) not in things     # a move alone draws nothing
    assert any(t[0] == pytest.approx(200) and t[2] == pytest.approx(230) for t in things)
    monkeypatch.setattr(T.pdfium_raw, "FPDFPageObj_GetBounds", lambda obj, *edges: False)
    assert not any(t[0] == pytest.approx(200) for t in page_of(src).things)


def test_a_drawing_with_impossible_bounds_is_left_out(monkeypatch):
    def bounds(obj, *edges):
        for e in edges:
            e.value = math.inf
        return True
    monkeypatch.setattr(T.pdfium_raw, "FPDFPage_CountObjects", lambda raw: 1)
    monkeypatch.setattr(T.pdfium_raw, "FPDFPage_GetObject", lambda raw, k: object())
    monkeypatch.setattr(T.pdfium_raw, "FPDFPageObj_GetType", lambda obj: 3)   # an image
    monkeypatch.setattr(T.pdfium_raw, "FPDFPageObj_GetBounds", bounds)
    assert T._things(None, T.Budget(time.monotonic() + 60)) == []


def test_words_wider_than_their_line_dont_wrap():
    assert T._wrap("Supercalifragilistic", "Helvetica", 12, [20], 200) is None
    assert T._wrap("a Supercalifragilistic", "Helvetica", 12, [40], 20) is None
    width = pdfmetrics.stringWidth
    assert T._wrap("one two\nthree", "Helvetica", 10, [100], 100) == [
        ("one two", width("one two", "Helvetica", 10)), ("three", width("three", "Helvetica", 10))]


def test_a_character_drawn_flat_is_in_no_block(tmp_path, settings, monkeypatch):
    src = raw(tmp_path / "in.pdf", b"BT /F1 12 Tf 72 700 Td (Ab) Tj ET")
    real = T._style

    def flat_first(raw_, i):
        style = real(raw_, i)
        return (style[0], (1.0, 0.0, 0.0, 0.0), *style[2:]) if i == 0 else style
    monkeypatch.setattr(T, "_style", flat_first)
    assert [b.text for b in page_of(src).listed] == ["b"]


def test_too_many_drawings_still_translate(tmp_path, settings, monkeypatch):
    src = make(tmp_path / "in.pdf", guide)
    monkeypatch.setattr(T, "MAX_OBJECTS", 0)
    r, out = translate(src, settings, {"Welcome Guide": "Willkommen"})
    assert "Willkommen" in text_of(out)


def test_only_the_block_under_a_stamp_is_refused(tmp_path, settings):
    src = raw(tmp_path / "in.pdf",
              b"BT /F1 12 Tf 72 700 Td (First words here) Tj ET "
              b"BT /F1 12 Tf 72 600 Td (Words under a stamp) Tj ET "
              b"BT /F1 12 Tf 0 1 -1 0 108 598 Tm (X) Tj ET")
    with pytest.raises(PdfUserError, match="Block p1-b2 on page 1 has other text"):
        translate(src, settings, {"First words here": "Erste Wörter hier",
                                  "Words under a stamp": "Wörter unter einem Stempel"})


def test_a_long_word_is_shrunk_until_it_fits(tmp_path, settings):
    def page(c):
        c.setFont("Helvetica", 11)
        c.drawString(72, 700, "Name")
        c.drawString(150, 700, "value")
        c.rect(72, 690, 200, 3, fill=1)
    r, out = translate(make(tmp_path / "in.pdf", page), settings,
                       {"Name": "Antragstellername"})
    assert r["shrunk"][0]["scale"] < 1 and "Antragstellername" in text_of(out)


# --- after review: what can't be seen, other text, and limits -----------------------------


def test_a_translation_cut_off_by_a_clip_is_refused(tmp_path, settings):
    src = raw(tmp_path / "in.pdf",
              b"q 50 700 30 20 re W n BT /F1 12 Tf 52 705 Td (Hi) Tj ET Q")
    with pytest.raises(PdfUserError, match="would be partly hidden"):
        translate(src, settings, {"Hi": "Guten Tag allerseits"})
    assert not (tmp_path / "out.pdf").exists()


def test_a_translation_covered_by_a_later_drawing_is_refused(tmp_path, settings):
    # A white box drawn after the text, over all of it (a cover-up, not a redaction):
    # it holds the whole block, as a background would, so only the picture shows it.
    src = raw(tmp_path / "in.pdf", b"BT /F1 12 Tf 72 700 Td (Hi) Tj ET "
                                   b"1 1 1 rg 40 690 460 30 re f")
    with pytest.raises(PdfUserError, match="would be partly hidden"):
        translate(src, settings, {"Hi": "Hallo zusammen"})


def test_text_sized_by_its_matrix_is_still_in_the_way(tmp_path, settings):
    # "Price" is 12 pt by its text matrix, "Hi" by its font size: one direction, so the
    # translation of "Hi" stops before "Price" (and wraps below) instead of covering it.
    src = raw(tmp_path / "in.pdf",
              b"BT /F1 12 Tf 52 705 Td (Hi) Tj ET "
              b"BT /F1 1 Tf 12 0 0 12 120 705 Tm (Price) Tj ET")
    found = blocks(src, settings)
    assert [b["text"] for b in found["blocks"]] == ["Hi", "Price"]
    r, out = translate(src, settings, {"Hi": "Guten Tag zusammen, Freunde"})
    page_ = page_of(src)
    hi, price = page_.listed
    assert T._room(hi, page_, {hi.number})[1] < price.x0
    assert "Price" in text_of(out)


def test_a_colour_change_splits_a_line_and_transparent_text_is_hidden(tmp_path, settings):
    def page(c):
        c.setFont("Helvetica", 11)
        text = c.beginText(72, 700)
        text.textOut("Pay now ")
        text.setFillColorRGB(1, 1, 1)
        text.textOut("SECRET WORDS ")
        text.setFillColorRGB(0, 0, 0)
        text.textOut("thanks")
        c.drawText(text)
        c.setFillAlpha(0)
        c.drawString(72, 600, "Transparent words")
    found = blocks(make(tmp_path / "in.pdf", page), settings)
    assert [b["text"] for b in found["blocks"]] == ["Pay now", "SECRET WORDS", "thanks"]
    assert found["pages_with_invisible_text"] == [1]


def test_white_text_on_white_cant_be_translated(tmp_path, settings):
    def page(c):
        c.setFont("Helvetica", 11)
        c.setFillColorRGB(1, 1, 1)
        c.drawString(72, 700, "Hidden words")
    src = make(tmp_path / "in.pdf", page)
    with pytest.raises(PdfUserError, match="would be partly hidden"):
        translate(src, settings, {"Hidden words": "Versteckte Wörter"})


def test_every_kind_of_space_and_line_break_is_tidied():
    assert T._translation("a\u3000b\u00a0c\u2028d\u2029e", "p1-b1") == "a b c\nd\ne"


def test_too_much_translation_at_once_is_refused(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(T, "MAX_TRANSLATED", 5)
    with pytest.raises(PdfUserError, match="more than 5 characters"):
        T._check([{"block": "p1-b1", "text": "Willkommen"}])


def test_drawings_are_measured_in_a_few_directions_at_most(tmp_path, settings,
                                                          monkeypatch):
    src = make(tmp_path / "in.pdf", guide)
    page_ = page_of(src)
    monkeypatch.setattr(T, "MAX_FRAMES", 1)
    first, second = (1.0, 0.0, 1.0), (0.0, 1.0, 1.0)
    assert page_.obstacles(first, (1, 0, 0, 1)) is not None
    assert page_.obstacles(second, (0, 1, -1, 0)) is None


def test_tall_drawings_are_kept_apart_and_still_found(tmp_path, settings, monkeypatch):
    rows = T._Rows([(0, 0, 10, 5000), (0, 10, 10, 20)], T.Budget(time.monotonic() + 60))
    assert rows.tall == [(0, 0, 10, 5000)]
    assert rows.between(0, 30) == [(0, 0, 10, 5000), (0, 10, 10, 20)]
    monkeypatch.setattr(T._Rows, "MAX_ROWS", -1)    # every drawing is "tall"
    test_growing_stops_above_drawings_and_form_fields(tmp_path, settings)


def test_cells_find_boxes_at_points_and_near_boxes():
    cells = T._Cells()
    cells.add((0, 0, 10, 10), "small")
    cells.add((0, 0, 10_000, 10), "wide")
    cells.add((0, math.inf, 1, 1), "nowhere")
    assert {o for _, o in cells.at(5, 5)} == {"small", "wide"}
    assert cells.at(math.nan, 5) == [] and cells.at(500, 500) == []
    assert {o for _, o in cells.near((0, 0, 20_000, 20))} == {"small", "wide"}
    assert {o for _, o in cells.near((100, 100, 110, 110))} == {"wide"}


def test_many_neighbours_are_checked_within_the_time_limit(tmp_path, settings):
    content = b" ".join(b"BT /F1 1 Tf %d 400 Td (x) Tj ET" % (10 + 3 * k) for k in range(1100))
    src = raw(tmp_path / "in.pdf", content + b" BT /F1 12 Tf 10 700 Td (Hi) Tj ET",
              size=(3400, 792))
    page_ = page_of(src)
    hi = page_.listed[0]
    hi.placed, hi.drawn_size = [(10, 405, "Hi", 3300)], 1
    T._clashes(page_, [hi])
    page_.budget.deadline = 0
    with pytest.raises(PdfUserError, match="took too long"):
        T._clashes(page_, [hi])
