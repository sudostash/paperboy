"""edit_text: finding what to change, fitting the new text, taking the old out and
drawing the new in its place, the checks, and the redactor's in-place drawing."""
from __future__ import annotations

import io
import math
import time

import pikepdf
import pypdfium2 as pdfium
import pytest
from reportlab.pdfbase import pdfmetrics

from paperboy import engine as E
from paperboy.engine import edittext as T
from paperboy.engine import redact_content as RC
from paperboy.errors import PdfUserError

Name = pikepdf.Name
SENTENCE = b"BT /F1 12 Tf 72 700 Td (Payment is due on 30 November 2026 by bank transfer.) Tj ET"
DATE = [{"find": "30 November 2026", "replace": "15 December 2026"}]


def font(pdf, base="Helvetica"):
    return pdf.make_indirect(pikepdf.Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                                BaseFont=Name(f"/{base}")))


def build(path, *contents, fonts=None, edit=None, size=(612, 792)):
    """A PDF with one page per content stream, Helvetica as /F1 (and *fonts*: name ->
    base font); *edit(pdf)* can change it before saving."""
    pdf = pikepdf.new()
    res = pikepdf.Dictionary(Font=pikepdf.Dictionary(
        {"/F1": font(pdf), **{k: font(pdf, v) for k, v in (fonts or {}).items()}}))
    for content in contents:
        page = pdf.add_blank_page(page_size=size)
        page.obj.Resources = res
        page.obj.Contents = pdf.make_stream(content)
    if edit:
        edit(pdf)
    pdf.save(path, fix_metadata_version=False)
    return path


def text_of(path, page=0, password=None) -> str:
    with pikepdf.open(path, password=password or "") as pdf:
        buf = io.BytesIO()
        pdf.save(buf)
    doc = pdfium.PdfDocument(buf.getvalue())
    try:
        return doc[page].get_textpage().get_text_range().replace("\r\n", "\n")
    finally:
        doc.close()


def box_of(path, text, page=0) -> tuple:
    """The box of *text* on the page (union of its characters' boxes)."""
    doc = pdfium.PdfDocument(str(path))
    try:
        textpage = doc[page].get_textpage()
        searcher = textpage.search(text, match_case=True)
        start, count = searcher.get_next()
        boxes = [textpage.get_charbox(i) for i in range(start, start + count)]
        return (min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes))
    finally:
        doc.close()


def content_of(path, page=0) -> bytes:
    """Everything a page draws: its content and its forms', decoded."""
    with pikepdf.open(path) as pdf:
        obj = pdf.pages[page].obj
        data = b"".join(s.read_bytes() for s in (
            obj.Contents if isinstance(obj.Contents, pikepdf.Array) else [obj.Contents]))
        xobjects = obj.Resources.get("/XObject", {})
        return data + b"".join(xobjects[k].read_bytes() for k in xobjects.keys())


def inked(path, text, page=0) -> int:
    """Dark pixels where *text* is drawn on the page."""
    left, bottom, right, top = box_of(path, text, page)
    doc = pdfium.PdfDocument(str(path))
    try:
        height = doc[page].get_height()
        picture = doc[page].render(scale=2).to_pil().convert("L")
    finally:
        doc.close()
    area = picture.crop((int(left * 2), int((height - top) * 2), int(right * 2) + 1,
                         int((height - bottom) * 2) + 1))
    return sum(area.histogram()[:128])


def edit(tmp_path, settings, *contents, edits=DATE, fonts=None, prepare=None, **kw):
    src = build(tmp_path / "in.pdf", *contents, fonts=fonts, edit=prepare)
    out = tmp_path / "out.pdf"
    return E.edit_text(src, out, settings, edits, **kw), out


# --- the edit itself --------------------------------------------------------------------


def test_replaces_text_in_its_place_and_in_reading_order(tmp_path, settings):
    r, out = edit(tmp_path, settings, SENTENCE)
    assert text_of(out) == "Payment is due on 15 December 2026 by bank transfer."
    assert r["change_count"] == 1 and r["pages_edited"] == [1]
    change = r["changes"][0]
    assert change == {"page": 1, "find": "30 November 2026", "old": "30 November 2026",
                      "new": "15 December 2026", "font": "Helvetica", "same_font": True,
                      "original_font": "Helvetica", "size": 12.0, "squeezed": None,
                      "line_moved": change["line_moved"]}
    # "November" and "December" are as wide in Helvetica: the line stays put.
    assert change["line_moved"] is None
    after = box_of(out, "by bank")[0] - box_of(out, "15 December 2026")[2]
    original = box_of(tmp_path / "in.pdf", "by bank")[0] - \
        box_of(tmp_path / "in.pdf", "30 November 2026")[2]
    assert after == pytest.approx(original, abs=0.1)
    assert r["originals_kept"] == 0 and r["password_protected"] is False
    # The old characters are gone from the file, not covered.
    with pikepdf.open(out) as pdf:
        data = b"".join(pdf.pages[0].Contents.read_bytes() for _ in [0])
    assert b"November" not in data
    # Drawn where the old text started, on its baseline.
    left, bottom, _, _ = box_of(out, "15 December 2026")
    before = box_of(tmp_path / "in.pdf", "30 November 2026")
    assert abs(left - before[0]) < 1.0 and abs(bottom - before[1]) < 1.0


def test_every_occurrence_on_the_chosen_pages(tmp_path, settings):
    line = b"BT /F1 12 Tf 72 700 Td (Ref ABC-1 and ABC-1 again) Tj ET"
    r, out = edit(tmp_path, settings, line, line, edits=[
        {"find": "ABC-1", "replace": "XYZ-9"}], pages="2")
    assert r["change_count"] == 2 and r["pages_edited"] == [2]
    assert text_of(out, 0) == "Ref ABC-1 and ABC-1 again"
    assert text_of(out, 1) == "Ref XYZ-9 and XYZ-9 again"


def test_match_case_and_whole_word(tmp_path, settings):
    line = b"BT /F1 12 Tf 72 700 Td (Smith met SMITH and Smithson) Tj ET"
    r, out = edit(tmp_path, settings, line, edits=[{"find": "Smith", "replace": "Jones"}],
                  whole_word=True)
    assert r["change_count"] == 1
    assert text_of(out) == "Jones met SMITH and Smithson"
    r, out = edit(tmp_path, settings, line, edits=[{"find": "smith", "replace": "Jones"}],
                  match_case=False, whole_word=True)
    assert r["change_count"] == 2


def test_right_and_center_alignment(tmp_path, settings):
    amount = (b"BT /F1 11 Tf 72 650 Td (Total) Tj ET "
              b"BT /F1 11 Tf 480 650 Td ($1,200.00) Tj ET "
              b"BT /F1 14 Tf 250 600 Td (Heading) Tj ET")
    src = build(tmp_path / "in.pdf", amount)
    out = tmp_path / "out.pdf"
    E.edit_text(src, out, settings, [
        {"find": "$1,200.00", "replace": "$12,400.00", "align": "right"},
        {"find": "Heading", "replace": "Title", "align": "center"}])
    old, new = box_of(src, "$1,200.00"), box_of(out, "$12,400.00")
    assert abs(old[2] - new[2]) < 1.0 and new[0] < old[0]
    old, new = box_of(src, "Heading"), box_of(out, "Title")
    assert abs((old[0] + old[2]) / 2 - (new[0] + new[2]) / 2) < 1.0


def test_pictures_and_drawings_under_the_text_stay(tmp_path, settings):
    content = (b"q 0.8 0.9 1 rg 60 690 300 30 re f Q "
               b"q 0 0 1 RG 72 698 m 300 698 l S Q " + SENTENCE)
    r, out = edit(tmp_path, settings, content)
    with pikepdf.open(out) as pdf:
        ops = [str(op) for _, op in pikepdf.parse_content_stream(pdf.pages[0])]
    assert ops.count("re") == 1 and ops.count("S") == 1 and ops.count("f") == 1
    assert "15 December 2026" in text_of(out)


def test_colour_size_and_angle_are_kept(tmp_path, settings):
    turned = (b"q 0.8 0 0 rg 1 0 0 1 300 300 cm 0.866 0.5 -0.5 0.866 0 0 cm "
              b"BT /F1 18 Tf 0 0 Td (Draft copy) Tj ET Q")
    r, out = edit(tmp_path, settings, turned, edits=[{"find": "Draft", "replace": "Final"}])
    assert r["changes"][0]["size"] == 18.0
    doc = pdfium.PdfDocument(str(out))
    try:
        textpage = doc[0].get_textpage()
        start, _ = textpage.search("Final", match_case=True).get_next()
        raw = textpage.raw
        # PDFium measures angles clockwise: 30 degrees anticlockwise is 330.
        assert math.isclose(pdfium.raw.FPDFText_GetCharAngle(raw, start),
                            2 * math.pi - math.radians(30), abs_tol=0.02)
        (_, matrix, size, colour, *_) = T._style(raw, start)
        assert size == 18.0 and colour[:3] == (204, 0, 0)
    finally:
        doc.close()
    assert "Final copy" in text_of(out)


def test_bold_serif_text_gets_the_matching_standard_font(tmp_path, settings):
    content = b"BT /F2 12 Tf 72 700 Td (Name: Alice) Tj ET"
    r, _ = edit(tmp_path, settings, content, fonts={"/F2": "Times-Bold"},
                edits=[{"find": "Alice", "replace": "Bob"}])
    assert r["changes"][0]["font"] == "Times-Bold"
    assert r["changes"][0]["original_font"] == "Times-Bold"


def test_text_beyond_latin_is_drawn_in_dejavu(tmp_path, settings):
    r, out = edit(tmp_path, settings, SENTENCE, edits=[
        {"find": "bank transfer", "replace": "переводом"}])
    assert r["changes"][0]["font"] == "PaperboySans"
    assert "переводом" in text_of(out)


def test_characters_no_font_has_are_refused(tmp_path, settings):
    with pytest.raises(PdfUserError, match="no available font can show"):
        edit(tmp_path, settings, SENTENCE, edits=[{"find": "bank", "replace": "銀行"}])
    assert not (tmp_path / "out.pdf").exists()


def test_empty_replacement_removes_the_text(tmp_path, settings):
    r, out = edit(tmp_path, settings, SENTENCE, edits=[
        {"find": "30 November 2026 ", "replace": ""}])
    assert r["changes"][0]["font"] is not None
    assert "November" not in text_of(out)
    assert text_of(out).startswith("Payment is due on")


# "then more" is drawn on its own, so it can't be moved along.
APART = b"BT /F1 12 Tf 72 700 Td (Due 30 Nov) Tj ET BT /F1 12 Tf 138 700 Td (then more) Tj ET"


def test_longer_text_moves_the_rest_of_its_line(tmp_path, settings):
    r, out = edit(tmp_path, settings, SENTENCE, edits=[
        {"find": "30 November 2026", "replace": "the thirtieth of November 2026"}])
    assert r["changes"][0]["line_moved"] > 50 and r["changes"][0]["squeezed"] is None
    assert text_of(out) == "Payment is due on the thirtieth of November 2026 by bank transfer."
    assert box_of(out, "November 2026")[2] < box_of(out, "by bank")[0]


def test_a_line_that_cant_move_is_squeezed_instead(tmp_path, settings):
    r, out = edit(tmp_path, settings, APART, edits=[{"find": "30 Nov", "replace": "30 Nov."}])
    squeezed = r["changes"][0]["squeezed"]
    assert squeezed is not None and T.MIN_SQUEEZE <= squeezed < 1
    assert r["changes"][0]["line_moved"] is None
    assert box_of(out, "30 Nov.")[2] <= box_of(out, "then")[0]


def test_text_that_cant_fit_is_refused(tmp_path, settings):
    with pytest.raises(PdfUserError, match="too long.*room for about"):
        edit(tmp_path, settings, APART, edits=[
            {"find": "30 Nov", "replace": "the thirtieth of November"}])
    assert not (tmp_path / "out.pdf").exists()


def test_a_line_moved_past_the_page_edge_is_refused(tmp_path, settings):
    line = b"BT /F1 12 Tf 72 700 Td (Due 30 Nov and then a lot more words to the edge) Tj ET"
    with pytest.raises(PdfUserError, match="pushes the rest of\\s+the line"):
        edit(tmp_path, settings, line, edits=[
            {"find": "30 Nov", "replace": "x" * 30}, {"find": "and", "replace": "x" * 30}])


def test_shorter_text_pulls_the_rest_of_its_line_back(tmp_path, settings):
    r, out = edit(tmp_path, settings, SENTENCE, edits=[
        {"find": "30 November 2026", "replace": "1 May"}])
    moved = r["changes"][0]["line_moved"]
    width = pdfmetrics.stringWidth
    assert moved == pytest.approx(width("1 May", "Helvetica", 12) -
                                  width("30 November 2026", "Helvetica", 12), abs=0.01)
    assert box_of(out, "by bank")[0] == pytest.approx(
        box_of(tmp_path / "in.pdf", "by bank")[0] + moved, abs=0.05)


def test_several_edits_on_one_line_move_it_together(tmp_path, settings):
    line = b"BT /F1 12 Tf 72 700 Td (From 3 May to 9 June, room 4.) Tj ET"
    r, out = edit(tmp_path, settings, line, edits=[
        {"find": "3 May", "replace": "13 September"}, {"find": "9 June", "replace": "1 Oct"},
        {"find": "4", "replace": "42"}])
    assert text_of(out) == "From 13 September to 1 Oct, room 42."
    assert [c["line_moved"] is not None for c in r["changes"]] == [True, True, True]


def test_the_page_edge_limits_room(tmp_path, settings):
    line = b"BT /F1 12 Tf 500 700 Td (End here) Tj ET"
    with pytest.raises(PdfUserError, match="too long"):
        edit(tmp_path, settings, line,
             edits=[{"find": "here", "replace": "somewhere very far away"}])


def test_text_on_one_line_only(tmp_path, settings):
    two_lines = b"BT /F1 12 Tf 72 700 Td (Payment is due) Tj 0 -14 Td (on Friday) Tj ET"
    with pytest.raises(PdfUserError, match="more than\\s+one line"):
        edit(tmp_path, settings, two_lines, edits=[{"find": "due on", "replace": "on"}])


def test_overlapping_edits_are_refused(tmp_path, settings):
    with pytest.raises(PdfUserError, match="Edits 1 and 2 change the same text"):
        edit(tmp_path, settings, SENTENCE, edits=[
            {"find": "30 November", "replace": "1 December"},
            {"find": "November 2026", "replace": "December 2027"}])


def test_edits_not_found_are_named(tmp_path, settings):
    with pytest.raises(PdfUserError, match="Edits 2 \\(\"nowhere\"\\) weren't found"):
        edit(tmp_path, settings, SENTENCE, edits=[*DATE, {"find": "nowhere", "replace": "x"}])


def test_pages_without_text_are_mentioned(tmp_path, settings):
    with pytest.raises(PdfUserError, match="Pages 2 have no text"):
        edit(tmp_path, settings, SENTENCE, b"0 0 1 1 re f",
             edits=[{"find": "nowhere", "replace": "x"}])


def test_invisible_ocr_text_is_refused(tmp_path, settings):
    ocr = b"BT 3 Tr /F1 12 Tf 72 700 Td (Scanned words) Tj ET"
    with pytest.raises(PdfUserError, match="invisible"):
        edit(tmp_path, settings, ocr, edits=[{"find": "Scanned", "replace": "Typed"}])


def test_too_many_changes(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(T, "MAX_CHANGES", 1)
    line = b"BT /F1 12 Tf 72 700 Td (ab ab) Tj ET"
    with pytest.raises(PdfUserError, match="more than 1 times"):
        edit(tmp_path, settings, line, edits=[{"find": "ab", "replace": "cd"}])


def test_too_much_content_is_refused(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(T, "MAX_PAGE_CONTENT", 10)
    with pytest.raises(PdfUserError, match="too much content to edit"):
        edit(tmp_path, settings, SENTENCE)


def test_searching_too_long(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(T, "EDIT_TIME_LIMIT_S", -1)
    with pytest.raises(PdfUserError, match="took longer"):
        edit(tmp_path, settings, SENTENCE)


def test_text_drawn_flat_is_refused(tmp_path, settings):
    flat = b"q 1 0 0 0 0 0 cm BT /F1 12 Tf 72 700 Td (Flat) Tj ET Q " + SENTENCE
    with pytest.raises(PdfUserError, match="(flattened to nothing|can't be located)"):
        edit(tmp_path, settings, flat, edits=[{"find": "Flat", "replace": "Tall"}])


def test_upside_down_text(tmp_path, settings):
    flipped = b"BT /F1 12 Tf -1 0 0 -1 400 500 Tm (Payment is due on 30 November 2026) Tj ET"
    _, out = edit(tmp_path, settings, flipped)
    assert "15 December 2026" in text_of(out)


def test_a_match_over_pieces_of_text(tmp_path, settings):
    pieces = b"BT /F1 12 Tf 72 700 Td (Due) Tj 40 0 Td (date) Tj 60 0 Td (later) Tj ET"
    r, out = edit(tmp_path, settings, pieces, edits=[{"find": "Due date", "replace": "Day"}])
    assert r["changes"][0]["old"] == "Due date"
    assert text_of(out).startswith("Day")


def test_style_when_pdfium_cant_say(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(T.pdfium_raw, "FPDFText_GetMatrix", lambda *a: 0)
    monkeypatch.setattr(T.pdfium_raw, "FPDFText_GetFillColor", lambda *a: 0)
    r, out = edit(tmp_path, settings, b"q 1 0 0 rg " + SENTENCE + b" Q")
    assert "15 December 2026" in text_of(out)


def test_a_match_that_cant_be_placed(tmp_path, settings, monkeypatch):
    real = T._chars
    monkeypatch.setattr(T, "_chars", lambda textpage, union=False: [
        (u, (math.nan,) * 4, g) for u, _, g in real(textpage, union)])
    with pytest.raises(PdfUserError, match="can't be located"):
        edit(tmp_path, settings, SENTENCE)


def test_a_picture_under_the_text_stays(tmp_path, settings):
    def prepare(pdf):
        picture = pdf.make_stream(b"\x80" * 4, Type=Name.XObject, Subtype=Name.Image,
                                  Width=2, Height=2, ColorSpace=Name.DeviceGray,
                                  BitsPerComponent=8)
        pdf.pages[0].obj.Resources.XObject = pikepdf.Dictionary(Im=picture)
    content = (b"q 300 30 0 0 60 690 cm /Im Do Q "
               b"q 10 0 0 10 200 698 cm BI /W 1 /H 1 /CS /G /BPC 8 ID \x00 EI Q " + SENTENCE)
    _, out = edit(tmp_path, settings, content, prepare=prepare)
    with pikepdf.open(out) as pdf:
        assert "/Im" in pdf.pages[0].Resources.XObject
        ops = [str(op) for _, op in pikepdf.parse_content_stream(pdf.pages[0])]
    assert ops.count("INLINE IMAGE") == 1 and ops.count("Do") == 2   # picture, new text
    assert "15 December 2026" in text_of(out)


def test_a_form_holding_only_the_text_is_kept_and_edited(tmp_path, settings):
    def prepare(pdf):
        form = pdf.make_stream(b"BT /F1 12 Tf 0 0 Td (30 November 2026) Tj ET",
                               Type=Name.XObject, Subtype=Name.Form, BBox=[0, 0, 90, 9],
                               Resources=pikepdf.Dictionary(Font=pdf.pages[0].Resources.Font))
        pdf.pages[0].obj.Resources.XObject = pikepdf.Dictionary(Fm=form)
    content = b"BT /F1 12 Tf 72 700 Td (Due:) Tj ET q 1 0 0 1 110 700 cm /Fm Do Q"
    _, out = edit(tmp_path, settings, content, prepare=prepare)
    assert text_of(out) == "Due: 15 December 2026"


# --- what the document is ---------------------------------------------------------------


def test_signed_input_is_refused(tmp_path, settings):
    def sign(pdf):
        pdf.Root.Perms = pikepdf.Dictionary(DocMDP=pikepdf.Dictionary())
    with pytest.raises(PdfUserError, match="digitally signed"):
        edit(tmp_path, settings, SENTENCE, prepare=sign)


def test_reader_usage_rights_and_thumbnails_are_dropped(tmp_path, settings):
    def prepare(pdf):
        pdf.Root.Perms = pikepdf.Dictionary(UR3=pdf.make_indirect(pikepdf.Dictionary(
            Type=Name.Sig, ByteRange=[0, 1, 2, 3])))
        pdf.pages[0].obj.Thumb = pdf.make_stream(b"old picture")
    _, out = edit(tmp_path, settings, SENTENCE, prepare=prepare)
    with pikepdf.open(out) as pdf:
        assert "/Perms" not in pdf.Root and "/Thumb" not in pdf.pages[0].obj


def test_encrypted_input_stays_protected(tmp_path, settings):
    src = build(tmp_path / "plain.pdf", SENTENCE)
    locked = tmp_path / "in.pdf"
    with pikepdf.open(src) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(user="pw", owner="pw", R=6))
    out = tmp_path / "out.pdf"
    r = E.edit_text(locked, out, settings, DATE, password="pw")
    assert r["password_protected"] is True
    assert "15 December 2026" in text_of(out, password="pw")


def test_forbidden_changes_need_the_owner_password(tmp_path, settings):
    src = build(tmp_path / "plain.pdf", SENTENCE)
    locked = tmp_path / "in.pdf"
    with pikepdf.open(src) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(
            user="", owner="owner", R=6, allow=pikepdf.Permissions(modify_other=False)))
    with pytest.raises(PdfUserError, match="does not allow changing"):
        E.edit_text(locked, tmp_path / "out.pdf", settings, DATE)


def test_text_in_a_form_is_edited_and_other_pages_keep_theirs(tmp_path, settings):
    def prepare(pdf):
        form = pdf.make_stream(SENTENCE, Type=Name.XObject, Subtype=Name.Form,
                               BBox=[0, 0, 612, 792])
        for page in pdf.pages:
            page.obj.Resources = pikepdf.Dictionary(
                Font=page.obj.Resources.Font, XObject=pikepdf.Dictionary(Fm=form))
    r, out = edit(tmp_path, settings, b"q /Fm Do Q", b"q /Fm Do Q", prepare=prepare,
                  pages="1")
    # The text was in the form: the new text follows it, once it has been drawn.
    assert text_of(out, 0) == "Payment is due on by bank transfer. 15 December 2026"
    assert "30 November 2026" in text_of(out, 1)
    assert r["originals_kept"] == 1


def test_rotated_page(tmp_path, settings):
    def prepare(pdf):
        pdf.pages[0].obj.Rotate = 90
    _, out = edit(tmp_path, settings, SENTENCE, prepare=prepare)
    assert text_of(out) == "Payment is due on 15 December 2026 by bank transfer."


def test_tagged_text_keeps_its_tag(tmp_path, settings):
    tagged = b"/P <</MCID 0>> BDC " + SENTENCE + b" EMC"
    _, out = edit(tmp_path, settings, tagged)
    with pikepdf.open(out) as pdf:
        ops = [str(op) for _, op in pikepdf.parse_content_stream(pdf.pages[0])]
    assert ops.index("BDC") < ops.index("Do") < ops.index("EMC")


def test_marked_content_inside_the_text_object_isnt_split(tmp_path, settings):
    inner = (b"BT /F1 12 Tf 72 700 Td /Span <</MCID 0>> BDC (Payment is due on 30 November "
             b"2026 by bank transfer.) Tj EMC ET")
    _, out = edit(tmp_path, settings, inner)
    with pikepdf.open(out) as pdf:
        ops = [str(op) for _, op in pikepdf.parse_content_stream(pdf.pages[0])]
    assert ops.count("BT") == 1 and ops.index("ET") < ops.index("Do")
    assert "15 December 2026" in text_of(out)


def test_text_after_a_quote_operator_and_kerning(tmp_path, settings):
    content = (b"BT /F1 12 Tf 14 TL 72 720 Td (First line: ) Tj "
               b"[(Due ) -50 (30 November 2026) 20 ( today)] TJ "
               b"(Pay on 30 November 2026 please) ' "
               b"1 0 (And 30 November 2026 too) \" ET")
    r, out = edit(tmp_path, settings, content)
    assert r["change_count"] == 3
    assert text_of(out) == ("First line: Due 15 December 2026 today\n"
                            "Pay on 15 December 2026 please\nAnd 15 December 2026 too")
    assert b"November" not in content_of(out)


def test_new_text_resets_the_inherited_text_state(tmp_path, settings):
    spaced = b"BT 3 Tc 2 Tw 80 Tz /F1 12 Tf 72 700 Td (Code ABC here) Tj ET"
    _, out = edit(tmp_path, settings, spaced, edits=[{"find": "ABC", "replace": "XYZ"}])
    with pikepdf.open(out) as pdf:
        xobjects = pdf.pages[0].Resources.XObject
        forms = [xobjects[k] for k in xobjects.keys()]
        assert forms[-1].read_bytes().startswith(b"0 Tc 0 Tw 100 Tz 0 Ts 0 Tr")


# --- the checks -------------------------------------------------------------------------


def test_text_that_cant_be_followed_exactly_is_refused(tmp_path, settings, monkeypatch):
    def unsure(*args, **kwargs):
        raise RC.Unsure("a font whose widths aren't known")
    monkeypatch.setattr(RC.Redactor, "page", unsure)
    with pytest.raises(PdfUserError, match="can't be edited exactly: a font whose"):
        edit(tmp_path, settings, SENTENCE)


def test_a_failed_removal_check_is_refused(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(T, "_unchanged", lambda *a: False)
    with pytest.raises(PdfUserError, match="would have changed"):
        edit(tmp_path, settings, SENTENCE)
    assert not (tmp_path / "out.pdf").exists()


def test_new_text_that_cant_be_found_is_refused(tmp_path, settings, monkeypatch):
    counts = iter([0, 0])
    monkeypatch.setattr(T, "_count", lambda *a: next(counts))
    with pytest.raises(PdfUserError, match="couldn't be confirmed"):
        edit(tmp_path, settings, SENTENCE)
    assert not (tmp_path / "out.pdf").exists()


@pytest.mark.parametrize("edits, message", [
    ([], "Give 1 to"),
    ("x", "Give 1 to"),
    ([{"find": "a"}], "must be"),
    ([{"find": "a", "replace": "b", "colour": "red"}], "must be"),
    (["a"], "must be"),
    ([{"find": "a", "replace": "b", "align": "justify"}], "align must be"),
    ([{"find": "", "replace": "b"}], "Edit 1's find"),
    ([{"find": "a", "replace": 3}], "at most"),
    ([{"find": "a", "replace": "x" * 501}], "at most"),
    ([{"find": "a", "replace": "two\nlines"}], "one line"),
    ([{"find": "a", "replace": "tab\there"}], "control"),
    ([{"find": "a", "replace": "\U0001f600"}], "beyond"),
    ([{"find": "a", "replace": "שלום"}], "right-to-left"),
])
def test_bad_edits(edits, message):
    with pytest.raises(PdfUserError, match=message):
        T._check_edits(edits)


def test_too_many_edits():
    with pytest.raises(PdfUserError, match="Give 1 to 50"):
        T._check_edits([{"find": "a", "replace": "b"}] * 51)


@pytest.mark.parametrize("name, flags, weight, font", [
    ("ABCDEF+Calibri", 32, 400, "Helvetica"),
    ("Arial-BoldMT", 32, 0, "Helvetica-Bold"),
    ("TimesNewRomanPS-ItalicMT", 32, 0, "Times-Italic"),
    ("Georgia", 32 | 64, 700, "Times-BoldItalic"),
    ("Consolas", 0, 0, "Courier"),
    ("Unknown", 1, 0, "Courier"),
    ("Unknown", 2, 0, "Times-Roman"),
    ("Unknown", 2 | (1 << 18), 0, "Times-Bold"),
    ("MicrosoftSansSerif", 2, 0, "Helvetica"),
    ("Unknown", 0, 0, "Helvetica"),
])
def test_closest_standard_font(name, flags, weight, font):
    assert T._font_for(T._SUBSET.sub("", name), flags, weight, "text") == font


@pytest.mark.parametrize("name, font", [
    ("Courier-Bold", "PaperboyMono"), ("Arial-BoldItalic", "PaperboySans-Bold"),
    ("Arial-Italic", "PaperboySans-Oblique"), ("Times-Roman", "PaperboySans")])
def test_closest_unicode_font(name, font):
    assert T._font_for(name, 0, 0, "Ωμέγα") == font


# --- the redactor's in-place drawing ----------------------------------------------------


def test_along_finds_the_distance_along_any_line():
    assert RC._along((1, 0, 0, 1, 15, 5), (1, 0, 0, 1, 10, 5)) == 5
    assert RC._along((0, 2, -2, 0, 3, 14), (0, 2, -2, 0, 3, 10)) == 2
    assert RC._along((0, 0, 0, 0, 1, 1), (0, 0, 0, 0, 0, 0)) is None


def test_a_line_that_cant_be_found_again_is_drawn_after_its_text(tmp_path, settings,
                                                                 monkeypatch):
    monkeypatch.setattr(RC, "_along", lambda tm, tlm: None)
    _, out = edit(tmp_path, settings, SENTENCE)
    with pikepdf.open(out) as pdf:
        ops = [str(op) for _, op in pikepdf.parse_content_stream(pdf.pages[0])]
    assert ops.count("BT") == 1 and ops.index("ET") < ops.index("Do")


def test_no_way_back_to_the_page_draws_at_the_end(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(RC, "_invert", lambda m: None)
    _, out = edit(tmp_path, settings, SENTENCE)
    with pikepdf.open(out) as pdf:
        ops = [str(op) for _, op in pikepdf.parse_content_stream(pdf.pages[0])]
    assert ops[-3:] == ["q", "Do", "Q"]
    assert text_of(out).endswith("15 December 2026")


def test_clipping_text_isnt_split(tmp_path, settings):
    clip = (b"BT 7 Tr /F1 12 Tf 72 600 Td (Clip) Tj 0 Tr 0 100 Td "
            b"(Payment is due on 30 November 2026 by bank transfer.) Tj ET")
    _, out = edit(tmp_path, settings, clip)
    with pikepdf.open(out) as pdf:
        ops = [str(op) for _, op in pikepdf.parse_content_stream(pdf.pages[0])]
    # The clip takes effect at ET: the new text is drawn at the page's end, outside it.
    assert ops.count("BT") == 1 and ops[-3:] == ["q", "Do", "Q"]
    assert inked(out, "15 December 2026") > 100


# What redaction made of these before edit_text existed (the Redactor of PR #38): it
# must make exactly the same now, when nothing is drawn.
GOLDEN = [
    (b"BT /F1 12 Tf 14 TL 72 720 Td [(Due ) -50 (30 November 2026) 20 ( today)] TJ "
     b"(Pay on 30 November 2026) ' 2 1 (30 November 2026 again) \" ET",
     b"q\nBT\n/F1 12 Tf\n14 TL\n72 720 Td\n[ (Due ) -50 (30 Novemb) -6095 ] TJ\nT*\n"
     b"[ (Pay o) -9338 ] TJ\n2 Tw\n1 Tc\nT*\n[ (30 N) -10949.333 ] TJ\nET\nQ\n", 49),
    (b"q 1 0 0 rg 60 690 300 30 re f Q BT 3 Tc 2 Tw 80 Tz /F1 12 Tf 72 700 Td "
     b"(Code 30 November 2026 here) Tj ET q 0.5 0 0 0.5 0 0 cm BT /F1 24 Tf 144 1300 Td "
     b"(30 November 2026) Tj ET Q", "a character is partly under an area", None),
    (b"/P <</MCID 0>> BDC BT 7 Tr /F1 12 Tf 72 600 Td (Clip) Tj 0 Tr 0 100 Td "
     b"(Payment 30 November 2026 by) Tj ET EMC",
     b"q\n/P << /MCID 0 >> BDC\nBT\n7 Tr\n/F1 12 Tf\n72 600 Td\n(Clip) Tj\n0 Tr\n"
     b"0 100 Td\n[ (Paym) -11506 ] TJ\nET\nEMC\nQ\n", 23),
]


@pytest.mark.parametrize("content, made, removed", GOLDEN)
def test_redaction_without_drawing_is_unchanged(content, made, removed):
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    page.obj.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font(pdf)))
    page.obj.Contents = pdf.make_stream(content)
    areas = [(160.0, 690.0, 260.0, 730.0), (100.0, 600.0, 300.0, 712.0),
             (140.0, 640.0, 330.0, 668.0)]
    redactor = RC.Redactor(pdf, areas, RC.Budget(math.inf))
    if isinstance(made, str):
        with pytest.raises(RC.Unsure, match=made):
            redactor.page(page.obj, page.obj.Resources, "black")
        return
    redactor.page(page.obj, page.obj.Resources, "black")

    def written(data: bytes) -> bytes:   # as this pikepdf writes it (old ones use hex)
        return pikepdf.unparse_content_stream(pikepdf.parse_content_stream(
            pdf.make_stream(data)))
    assert written(page.obj.Contents.read_bytes()) == written(made + RC.boxes(areas, "black"))
    assert redactor.stats["text_removed"] == removed


# --- the page's own font ----------------------------------------------------------------


def dejavu_letter(path):
    """A letter set in DejaVu Sans, embedded as a subset (as word processors do)."""
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    from paperboy.engine.markdown_pdf import FONTS

    pdfmetrics.registerFont(TTFont("TestDejaVu", str(FONTS / "DejaVuSans.ttf")))
    c = canvas.Canvas(str(path), pagesize=(595, 842))
    c.setFont("TestDejaVu", 11)
    c.drawString(60, 720, "Dear Jordan Lee, your start date is 3 March 2027.")
    c.drawString(60, 704, "Your salary is 42,000 a year.")
    c.save()
    return path


def test_new_text_is_drawn_in_the_pages_own_font(tmp_path, settings):
    src = dejavu_letter(tmp_path / "in.pdf")
    out = tmp_path / "out.pdf"
    r = E.edit_text(src, out, settings, [{"find": "3 March", "replace": "4 March"},
                                         {"find": "Lee", "replace": "Leeds"}])
    assert [(c["font"], c["same_font"]) for c in r["changes"]] == [
        ("DejaVuSans", True), ("DejaVuSans", True)]
    assert text_of(out).startswith("Dear Jordan Leeds, your start date is 4 March 2027.")
    with pikepdf.open(out) as pdf:
        page = pdf.pages[0]
        original = [page.Resources.Font[k].objgen for k in page.Resources.Font.keys()]
        xobjects = page.Resources.XObject
        forms = [xobjects[k] for k in xobjects.keys()
                 if "/Font" in xobjects[k].get("/Resources", {})]
        assert forms and all(f.Resources.Font.Pf.objgen in original for f in forms)


def test_a_character_the_page_never_draws_falls_back(tmp_path, settings):
    src = dejavu_letter(tmp_path / "in.pdf")
    r = E.edit_text(src, tmp_path / "out.pdf", settings, [{"find": "3 March",
                                                             "replace": "5 March"}])
    assert r["changes"][0]["same_font"] is False
    assert r["changes"][0]["font"] == "Helvetica"


def to_unicode(pdf, body: bytes):
    return pdf.make_stream(b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap "
                           + body + b" endcmap end end")


def unicode_of(font, used):
    return T._UnicodeMap(font, RC.Budget(math.inf)).lookup(used, RC.Budget(math.inf))


def test_unicode_maps_are_read():
    pdf = pikepdf.new()
    font = pikepdf.Dictionary(Subtype=Name.TrueType, ToUnicode=to_unicode(pdf, (
        b"endbfrange 2 beginbfchar <01> <0041> <02> <D800> endbfchar "
        b"2 beginbfrange <10> <12> <0061> <20> <21> [<0058> <0059>] endbfrange "
        b"1 beginbfchar <3> <0042> endbfchar")))
    used = {b"\x01", b"\x02", b"\x11", b"\x21", b"\x30", b"\x13", b"\x00\x10"}
    assert unicode_of(font, used) == {b"\x01": "A", b"\x11": "b", b"\x21": "Y",
                                         b"\x30": "B"}


def test_damaged_unicode_ranges_are_skipped():
    pdf = pikepdf.new()
    font = pikepdf.Dictionary(Subtype=Name.TrueType, ToUnicode=to_unicode(pdf, (
        b"4 beginbfrange [ <01> <0041> <20> <21> ] <30> <31> <0061> <0100> <0101> <0041> "
        b"endbfrange")))
    # "[ <01> <0041>" has no bounds, "<20> <21> ]" no target, and no two-byte codes are
    # used: only the third range counts.
    assert unicode_of(font, {b"\x01", b"\x20", b"\x31"}) == {b"\x31": "b"}


def test_unicode_maps_listing_too_few_targets():
    pdf = pikepdf.new()
    font = pikepdf.Dictionary(Subtype=Name.Type0, ToUnicode=to_unicode(
        pdf, b"1 beginbfrange <0010> <0012> [<0061>] endbfrange"))
    assert unicode_of(font, {b"\x00\x10", b"\x00\x12"}) == {b"\x00\x10": "a"}


def test_unicode_without_a_map():
    winansi = pikepdf.Dictionary(Subtype=Name.TrueType, Encoding=Name.WinAnsiEncoding)
    assert unicode_of(winansi, {b"A", b"\x80", b"\x81"}) == {b"A": "A", b"\x80": "€"}
    assert unicode_of(pikepdf.Dictionary(Subtype=Name.Type1), {b"A"}) == {}


def test_unicode_map_too_big_or_unreadable(monkeypatch):
    pdf = pikepdf.new()
    font = pikepdf.Dictionary(Subtype=Name.TrueType, ToUnicode=to_unicode(
        pdf, b"1 beginbfchar <01> <0041> endbfchar"))
    monkeypatch.setattr(T, "MAX_CMAP", 10)
    assert unicode_of(font, {b"\x01"}) == {}


def a_change(size=10.0, centres=((65, 10.0, 10.0),)):   # "A"
    change = T._Change()
    change.size, change.centres = size, list(centres)
    return change


def font_glyphs(font, codes=(b"A", b"B"), x=10.0, y=10.0):
    return [(("o", 1), font, code, x + 6 * k, y, x + 6 * k + 3, y)
            for k, code in enumerate(codes)]


def filed(glyphs):
    return T._Glyphs(glyphs)


@pytest.mark.parametrize("why", ["far", "type3", "vertical", "mixed", "lying", "unsure"])
def test_the_pages_font_isnt_used_when_it_cant_be_trusted(why, monkeypatch):
    pdf = pikepdf.new()
    font = pikepdf.Dictionary(Subtype=Name.TrueType, Encoding=Name.WinAnsiEncoding)
    glyphs = font_glyphs(font)
    change = a_change()
    if why == "far":
        change = a_change(centres=[(ord("A"), 100.0, 100.0)])
    elif why == "type3":
        font.Subtype = Name.Type3
    elif why == "vertical":
        font = pikepdf.Dictionary(Subtype=Name.Type0, Encoding=Name("/Identity-V"))
        glyphs = font_glyphs(font)
    elif why == "mixed":
        change = a_change(centres=[(ord("A"), 10.0, 10.0), (ord("B"), 16.0, 10.0)])
        glyphs[1] = (("o", 2), *glyphs[1][1:])
    elif why == "lying":
        change = a_change(centres=[(ord("Z"), 10.0, 10.0)])
    else:
        def unsure(*a):
            raise RC.Unsure("a font has no widths")
        monkeypatch.setattr(T, "Font", unsure)
    assert T._own_font(change, filed(glyphs), "AB", {}, {}, RC.Budget(math.inf)) is None
    del pdf


def test_the_pages_font_is_used_when_it_has_every_character():
    font = pikepdf.Dictionary(Subtype=Name.TrueType, Encoding=Name.WinAnsiEncoding,
                              FirstChar=65, Widths=[600, 700])
    glyphs = filed(font_glyphs(font) + [(("o", 1), font, b"A", math.nan, 0.0, 0.0, 0.0)])
    own = T._own_font(a_change(), glyphs, "BA", {}, {}, RC.Budget(math.inf))
    assert own[1] == b"BA" and own[2] == pytest.approx(13.0)
    assert T._own_font(a_change(), glyphs, "", {}, {}, RC.Budget(math.inf)) is None
    assert T._own_font(a_change(), filed([]), "A", {}, {}, RC.Budget(math.inf)) is None


def test_glyphs_are_found_near_and_not_far():
    glyphs = filed([(("o", 1), None, b"A", 10.0, 10.0, 12.0, 10.0),
                    (("o", 1), None, b"B", 13.0, 10.0, 15.0, 10.0)])
    assert glyphs.near(12.0, 10.0, 5.0)[2] == b"B"
    assert glyphs.near(12.0, 10.0, 1.5)[2] == b"B"
    assert glyphs.near(60.0, 10.0, 1000.0) is None   # looked for only REACH cells away


def test_a_page_that_cant_be_read_uses_standard_fonts(tmp_path, settings, monkeypatch):
    def unsure(*a, **k):
        raise RC.Unsure("it has too much content")
    monkeypatch.setattr(T, "parse", unsure)
    src = dejavu_letter(tmp_path / "in.pdf")
    r = E.edit_text(src, tmp_path / "out.pdf", settings, [{"find": "3 March",
                                                             "replace": "4 March"}])
    assert r["changes"][0]["same_font"] is False


def test_a_line_moves_only_when_the_old_texts_end_is_known(tmp_path, settings):
    # Text in a form isn't read by the survey: its end isn't known exactly.
    def prepare(pdf):
        form = pdf.make_stream(SENTENCE, Type=Name.XObject, Subtype=Name.Form,
                               BBox=[0, 0, 612, 792],
                               Resources=pikepdf.Dictionary(Font=pdf.pages[0].Resources.Font))
        pdf.pages[0].obj.Resources.XObject = pikepdf.Dictionary(Fm=form)
    r, out = edit(tmp_path, settings, b"q /Fm Do Q", prepare=prepare,
                  edits=[{"find": "30 November 2026", "replace": "1 May"}])
    assert r["changes"][0]["line_moved"] is None
    assert "1 May" in text_of(out)


def test_characters_on_an_areas_edge_may_go_either_way():
    areas = [(10.0, 0.0, 20.0, 10.0)]
    before = [(ord("a"), 5.0, 5.0), (ord("b"), 15.0, 5.0), (ord("c"), 20.5, 5.0),
              (ord("d"), 30.0, 5.0)]
    budget = RC.Budget(math.inf)
    assert T._unchanged(before, [before[0], before[3]], areas, [], 0, budget)
    assert T._unchanged(before, [before[0], before[2], before[3]], areas, [], 0, budget)
    assert not T._unchanged(before, [before[0]], areas, [], 0, budget)
    assert not T._unchanged(before, before, areas, [], 0, budget)


# --- review: bounds, render modes, lines across operators, tags, the old text elsewhere ---


def test_a_hostile_unicode_map_is_read_in_bounded_time():
    pdf = pikepdf.new()
    unclosed = to_unicode(pdf, b"40000 beginbfrange " + b"<00> <01> [" * 40000 + b" endbfrange")
    font = pikepdf.Dictionary(Subtype=Name.TrueType, ToUnicode=unclosed)
    started = time.perf_counter()
    assert unicode_of(font, {b"\x00", b"\x01"}) == {}
    assert time.perf_counter() - started < 2


def test_overlapping_ranges_settle_each_code_once():
    pdf = pikepdf.new()
    ranges = b"".join(b"<0000> <ffff> <%04x> " % k for k in range(5000))
    font = pikepdf.Dictionary(Subtype=Name.Type0, ToUnicode=to_unicode(
        pdf, b"5000 beginbfrange " + ranges + b"endbfrange"))
    used = {k.to_bytes(2, "big") for k in range(0x41, 0x41 + 20000)}
    started = time.perf_counter()
    found = unicode_of(font, used)
    assert time.perf_counter() - started < 3
    # The last range holding a code wins: it starts at 0x1387 (4999).
    assert found[b"\x00\x41"] == chr(0x1387 + 0x41)


def test_a_map_with_too_many_tokens_is_not_used(monkeypatch):
    monkeypatch.setattr(T, "MAX_MAP_TOKENS", 3)
    pdf = pikepdf.new()
    font = pikepdf.Dictionary(Subtype=Name.TrueType, ToUnicode=to_unicode(
        pdf, b"2 beginbfchar <01> <0041> <02> <0042> endbfchar"))
    assert unicode_of(font, {b"\x01", b"\x02"}) == {}


def test_fonts_too_large_to_follow_are_not_used(monkeypatch):
    monkeypatch.setattr(RC, "MAX_FONT_BYTES", 10)
    pdf = pikepdf.new()
    font = pikepdf.Dictionary(Subtype=Name.TrueType, ToUnicode=to_unicode(
        pdf, b"1 beginbfchar <41> <0041> endbfchar"))
    glyphs = filed(font_glyphs(font))
    assert T._own_font(a_change(), glyphs, "A", {}, {}, RC.Budget(math.inf)) is None


def test_characters_stacked_on_one_spot_are_filed_only_so_far():
    glyphs = filed([(("o", 1), None, b"A", 10.0, 10.0, 12.0, 10.0)] * 1000)
    assert sum(map(len, glyphs.cells.values())) == T.MAX_CELL


def test_a_page_is_projected_once_however_many_matches(tmp_path, settings, monkeypatch):
    line = b"BT /F1 2 Tf 20 700 Td (" + b"ab cd " * 80 + b") Tj ET"   # all on the page
    calls = []
    real = T._turned
    monkeypatch.setattr(T, "_turned", lambda *a: calls.append(1) or real(*a))
    r, _ = edit(tmp_path, settings, line, edits=[{"find": "ab", "replace": "AB"}])
    assert r["change_count"] == 80
    characters = 80 * 4
    # Once per character for the line index (4 corners), plus a little per match.
    assert len(calls) < 4 * characters + 80 * 30


@pytest.mark.parametrize("mode", [1, 2])
def test_outlined_text_is_refused(tmp_path, settings, mode):
    outlined = (b"1 1 1 rg 0 0 0 RG " + f"{mode} Tr ".encode() + SENTENCE)
    with pytest.raises(PdfUserError, match="drawn in outline"):
        edit(tmp_path, settings, outlined)
    assert not (tmp_path / "out.pdf").exists()


def test_the_new_text_can_be_seen(tmp_path, settings):
    _, out = edit(tmp_path, settings, b"0.1 0.2 0.6 rg " + SENTENCE)
    assert inked(out, "15 December 2026") > 100


def test_a_line_moves_across_its_text_operators(tmp_path, settings):
    two = b"BT /F1 12 Tf 72 700 Td [(Hello World again)] TJ [( Foo Bar end)] TJ ET"
    r, out = edit(tmp_path, settings, two, edits=[{"find": "World", "replace": "Worlds"},
                                                 {"find": "Bar", "replace": "Bars"}])
    assert [c["line_moved"] is not None for c in r["changes"]] == [True, True]
    assert text_of(out) == "Hello Worlds again Foo Bars end"
    assert box_of(out, "Worlds")[2] < box_of(out, "again")[0]
    assert box_of(out, "Bars")[2] < box_of(out, "end")[0]


def test_text_ending_its_operator_still_moves_its_line(tmp_path, settings):
    runs = b"BT /F1 12 Tf 72 700 Td (Dear ) Tj (Smith) Tj (, thanks for coming.) Tj ET"
    r, out = edit(tmp_path, settings, runs, edits=[{"find": "Smith", "replace": "Smithson"}])
    assert r["changes"][0]["line_moved"] > 0
    assert text_of(out) == "Dear Smithson, thanks for coming."
    assert box_of(out, "Smithson")[2] <= box_of(out, ", thanks")[0] + 0.5


def test_text_ending_a_line_waits_for_the_text_object_to_end(tmp_path, settings):
    lines = b"BT /F1 12 Tf 14 TL 72 700 Td (Due 30 November 2026) Tj T* (Next line) Tj ET"
    r, out = edit(tmp_path, settings, lines)
    assert r["changes"][0]["line_moved"] is None
    assert text_of(out) == "Due \nNext line\n15 December 2026" or \
        "15 December 2026" in text_of(out).splitlines()[-1]


def test_marked_content_closed_inside_the_text_object_isnt_split(tmp_path, settings):
    crossing = (b"/P <</MCID 0>> BDC BT /F1 12 Tf 72 700 Td (Start ) Tj EMC "
                b"/Span <</MCID 1>> BDC (30 November 2026 more) Tj ET EMC")
    _, out = edit(tmp_path, settings, crossing)
    with pikepdf.open(out) as pdf:
        ops = [str(op) for _, op in pikepdf.parse_content_stream(pdf.pages[0])]
    assert ops.count("BT") == 1


@pytest.mark.parametrize("inside, untagged", [(False, True), (True, False)])
def test_new_text_outside_the_tags_is_reported(tmp_path, settings, inside, untagged):
    def tag(pdf):
        pdf.Root.StructTreeRoot = pdf.make_indirect(pikepdf.Dictionary(
            Type=Name.StructTreeRoot))
    content = (b"BT /F1 12 Tf 72 700 Td /Span <</MCID 0>> BDC (Payment is due on 30 "
               b"November 2026 by bank transfer.) Tj EMC ET")
    if inside:
        content = b"/P <</MCID 0>> BDC " + SENTENCE + b" EMC"
    r, _ = edit(tmp_path, settings, content, prepare=tag)
    assert r["new_text_untagged"] is untagged


def test_the_old_text_elsewhere_is_reported(tmp_path, settings):
    def prepare(pdf):
        pdf.docinfo["/Title"] = "Invoice due 30 November 2026"
        bookmark = pdf.make_indirect(pikepdf.Dictionary(Title="Due 30 november 2026"))
        pdf.Root.Outlines = pdf.make_indirect(pikepdf.Dictionary(First=bookmark,
                                                                 Last=bookmark))
        pdf.Root.Metadata = pdf.make_stream(b"<x:xmpmeta>30 November 2026</x:xmpmeta>")
        if unreadable:
            pdf.Root.Metadata.Filter = Name.DCTDecode
        note = pikepdf.Dictionary(Type=Name.Annot, Subtype=Name.Text, Rect=[0, 0, 9, 9],
                                  Contents="Pay by 30 November 2026")
        parent = pdf.make_indirect(pikepdf.Dictionary(T="due", V="30 November 2026"))
        field = pdf.make_indirect(pikepdf.Dictionary(
            Type=Name.Annot, Subtype=Name.Widget, Rect=[0, 0, 9, 9], Parent=parent))
        parent.Kids = pikepdf.Array([field])
        pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array([parent]))
        pdf.pages[0].obj.Annots = pikepdf.Array([note, field, 7])
    unreadable = False
    r, _ = edit(tmp_path, settings, SENTENCE, prepare=prepare)
    # Bookmarks differ in case: found only when not matching case.
    assert r["old_text_elsewhere"] == ["document info", "XMP metadata", "comments",
                                       "form fields"]
    r, _ = edit(tmp_path, settings, SENTENCE, prepare=prepare, match_case=False)
    assert "bookmarks" in r["old_text_elsewhere"]
    r, _ = edit(tmp_path, settings, SENTENCE)
    assert r["old_text_elsewhere"] == []
    unreadable = True   # XMP that can't be decoded isn't looked in
    r, _ = edit(tmp_path, settings, SENTENCE, prepare=prepare)
    assert "XMP metadata" not in r["old_text_elsewhere"]


def test_looking_elsewhere_is_bounded(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(T, "MAX_LOOKED", 1)
    def prepare(pdf):
        notes = [pikepdf.Dictionary(Type=Name.Annot, Subtype=Name.Text, Rect=[0, 0, 9, 9],
                                    Contents=text) for text in ("x", "30 November 2026")]
        first = pdf.make_indirect(pikepdf.Dictionary(Title="x"))
        first.Next = pdf.make_indirect(pikepdf.Dictionary(Title="30 November 2026"))
        first.First = first   # a loop: looked at once
        pdf.Root.Outlines = pdf.make_indirect(pikepdf.Dictionary(First=first))
        for page in pdf.pages:
            page.obj.Annots = pikepdf.Array(notes)
    r, _ = edit(tmp_path, settings, SENTENCE, SENTENCE, prepare=prepare)
    assert r["old_text_elsewhere"] == []


def test_timeouts_name_what_was_being_done():
    with pytest.raises(PdfUserError, match="Editing took too long"):
        RC.Budget(-1, action="Editing").check()
    with pytest.raises(PdfUserError, match="Redacting took too long"):
        RC.Budget(-1).check()
