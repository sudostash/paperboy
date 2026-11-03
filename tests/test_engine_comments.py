"""list_comments / add_comments: comments that look right everywhere and read back."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pikepdf
import pypdfium2 as pdfium
import pytest
from pikepdf import Dictionary, Name
from test_permissions import NO_ANNOTATIONS, OPEN, _edit, _lock, _signed, _usage_rights

from paperboy import engine as E
from paperboy.engine import comments as C
from paperboy.errors import PdfUserError


def _add(src, settings, *comments, out=None, **kw):
    out = out or src.with_name(f"{src.stem}_c.pdf")
    return E.add_comments(src, out, settings, list(comments), **kw), out


def _listed(path, settings, **kw):
    return E.list_comments(path, settings, **kw)["comments"]


def _pixel(path, page, x, y, scale=1.0):
    """The colour shown at PDF point (x, y) of an unrotated page."""
    doc = pdfium.PdfDocument(str(path))  # (pypdfium2 4.30 has no "with")
    try:
        image = doc[page].render(scale=scale, may_draw_forms=True).to_pil().convert("RGB")
    finally:
        doc.close()
    return image.getpixel((round(x * scale), round(image.height - y * scale)))


# --- adding ---------------------------------------------------------------------------


def test_text_markup_over_found_text_reads_back(doc, settings):
    res, out = _add(doc, settings,
                    {"type": "highlight", "text": "page-3", "comment": "Check this"},
                    {"type": "underline", "text": "PAGE-1", "page": 1},
                    {"type": "strikeout", "text": "PAGE-2", "color": "#00ff00"},
                    {"type": "squiggly", "text": "PAGE-4", "match_case": True},
                    author="Sam")
    assert res["added"] == 4 and res["output"] == str(out)
    assert [(c["type"], c["added"]) for c in res["comments"]] == [
        ("highlight", [{"id": "p3-1", "page": 3}]), ("underline", [{"id": "p1-1", "page": 1}]),
        ("strikeout", [{"id": "p2-1", "page": 2}]), ("squiggly", [{"id": "p4-1", "page": 4}])]
    listed = {c["id"]: c for c in _listed(out, settings)}
    high = listed["p3-1"]
    assert (high["type"], high["author"], high["comment"], high["marked_text"],
            high["color"]) == ("highlight", "Sam", "Check this", "PAGE-3", "#ffd400")
    assert listed["p2-1"]["color"] == "#00ff00" and listed["p4-1"]["marked_text"] == "PAGE-4"
    assert datetime.fromisoformat(high["created"]).tzinfo is not None
    assert high["created"] == high["modified"]


def test_every_place_the_text_is_found(make_pdf, settings):
    pdf = make_pdf("d.pdf", 3)
    res, out = _add(pdf, settings, {"type": "highlight", "text": "PAGE"})
    assert [a["page"] for a in res["comments"][0]["added"]] == [1, 2, 3]
    res, _ = _add(pdf, settings, {"type": "highlight", "text": "PAGE", "page": 2})
    assert [a["page"] for a in res["comments"][0]["added"]] == [2]


def test_matches_per_comment_are_limited(make_pdf, settings, monkeypatch):
    monkeypatch.setattr(C, "MAX_MATCHES", 2)
    res, _ = _add(make_pdf("d.pdf", 4), settings, {"type": "underline", "text": "PAGE"})
    assert [a["page"] for a in res["comments"][0]["added"]] == [1, 2]


def test_highlights_are_drawn_beneath_the_text(doc, settings):
    _, out = _add(doc, settings, {"type": "highlight", "rects": [[60, 700, 200, 740]],
                                  "page": 1})
    r, g, b = _pixel(out, 0, 150, 730)  # beside the text, inside the highlight
    assert r > 200 and g > 180 and b < 80
    assert _pixel(out, 0, 300, 730) == (255, 255, 255)
    with pikepdf.open(out) as pdf:
        annot = pdf.pages[0].Annots[0]
        assert list(annot.QuadPoints) == [60, 740, 200, 740, 60, 700, 200, 700]
        assert annot.AP.N.Resources.ExtGState.GS0.BM == Name.Multiply


@pytest.mark.parametrize("kind", ["underline", "strikeout", "squiggly"])
def test_lines_are_drawn(kind, doc, settings):
    _, out = _add(doc, settings, {"type": kind, "rects": [[300, 400, 500, 440]], "page": 1})
    marked = [_pixel(out, 0, 400, y) for y in range(398, 442)]
    assert any(p != (255, 255, 255) for p in marked)


def test_notes_beside_text_or_at_a_point(doc, settings):
    res, out = _add(doc, settings,
                    {"type": "note", "text": "PAGE", "comment": "first page only"},
                    {"type": "note", "page": 2, "at": [300, 500], "comment": "here",
                     "color": "blue"})
    assert [c["added"] for c in res["comments"]] == [[{"id": "p1-1", "page": 1}],
                                                    [{"id": "p2-1", "page": 2}]]
    notes = _listed(out, settings)
    assert notes[1]["rect"] == [300, 480, 320, 500] and notes[1]["color"] == "#1f4fd1"
    assert notes[0]["rect"][2] <= 72  # just left of the text
    with pikepdf.open(out) as pdf:
        assert pdf.pages[1].Annots[0].F == 4 | 8 | 16  # stays upright and full size
    assert _pixel(out, 1, 305, 483)[2] > 180  # the blue icon is drawn


def test_text_boxes_are_drawn_and_read_back(doc, settings):
    res, out = _add(doc, settings, {"type": "text_box", "page": 1,
                                    "rect": [300, 600, 500, 650], "text": "Needs a date",
                                    "comment": "Review", "font_size": 14,
                                    "color": "red"}, author="Ann")
    (box,) = _listed(out, settings)
    assert (box["type"], box["comment"], box["subject"], box["author"]) == \
        ("text_box", "Needs a date", "Review", "Ann")
    with pikepdf.open(out) as pdf:
        annot = pdf.pages[0].Annots[0]
        assert str(annot.DA) == "/Helv 14 Tf 0.8 0 0 rg" and "/Rotate" not in annot
    assert _pixel(out, 0, 400, 625) == (255, 255, 255)        # filled
    assert _pixel(out, 0, 300.5, 625)[0] > 150                 # red border


def test_text_boxes_without_a_border(doc, settings):
    _, out = _add(doc, settings, {"type": "text_box", "page": 1, "border": False,
                                  "rect": [300, 600, 500, 650], "text": "x"})
    assert _pixel(out, 0, 300.5, 625) == (255, 255, 255)


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_text_boxes_are_upright_on_rotated_pages(rotation, doc, settings):
    rotated = _edit(doc, lambda pdf: setattr(pdf.pages[0], "Rotate", rotation))
    _, out = _add(rotated, settings, {"type": "text_box", "page": 1,
                                      "rect": [300, 400, 360, 600], "text": "Upright"})
    with pikepdf.open(out) as pdf:
        annot = pdf.pages[0].Annots[0]
        assert annot.Rotate == rotation
        expected = {90: [0, 1, -1, 0, 0, 0], 180: [-1, 0, 0, -1, 0, 0],
                    270: [0, -1, 1, 0, 0, 0]}[rotation]
        assert [float(v) for v in annot.AP.N.Matrix] == expected
        width, height = (float(v) for v in list(annot.AP.N.BBox)[2:])
    assert (width, height) == ((200, 60) if rotation in (90, 270) else (60, 200))


def test_text_that_does_not_fit_is_refused(doc, settings):
    with pytest.raises(PdfUserError, match="doesn't fit in its text box"):
        _add(doc, settings, {"type": "text_box", "page": 1, "rect": [300, 600, 340, 610],
                             "text": "far too much text for so small a box"})
    with pytest.raises(PdfUserError, match="doesn't fit"):  # one unbreakable word
        _add(doc, settings, {"type": "text_box", "page": 1, "rect": [300, 600, 340, 700],
                             "text": "Supercalifragilistic"})


def test_replies_answer_a_comment(doc, settings):
    res, out = _add(doc, settings, {"type": "highlight", "text": "PAGE-2"})
    parent = res["comments"][0]["added"][0]["id"]
    res, out2 = _add(out, settings, {"type": "reply", "to": parent, "comment": "Agreed"},
                     out=doc.with_name("r.pdf"), author="Lee")
    assert res["comments"][0]["added"] == [{"id": "p2-2", "page": 2}]
    high, reply = _listed(out2, settings)
    assert reply["reply_to"] == parent and reply["type"] == "note"
    assert reply["rect"] == high["rect"] and reply["author"] == "Lee"


def test_a_reply_to_a_comment_written_inside_the_page_list(doc, settings):
    def inline(pdf):
        pdf.pages[0].obj.Annots = pikepdf.Array([Dictionary(
            Type=Name.Annot, Subtype=Name.Square, Rect=[10, 10, 50, 50])])
    _edit(doc, inline)
    _, out = _add(doc, settings, {"type": "reply", "to": "p1-1", "comment": "ok"})
    square, reply = _listed(out, settings)
    assert reply["reply_to"] == "p1-1" and square["type"] == "square"


@pytest.mark.parametrize("target", ["p1-9", "p9-1", "p1-2", "p1-3"])
def test_replies_need_a_comment_to_answer(target, doc, settings):
    def annots(pdf):
        link = Dictionary(Type=Name.Annot, Subtype=Name.Link, Rect=[0, 0, 1, 1])
        pdf.pages[0].obj.Annots = pikepdf.Array([
            Dictionary(Type=Name.Annot, Subtype=Name.Square, Rect=[0, 0, 1, 1]), link, 5])
    _edit(doc, annots)
    with pytest.raises(PdfUserError, match=f"Comment 1: there is no comment {target}"):
        _add(doc, settings, {"type": "reply", "to": target, "comment": "?"})


def test_a_shared_list_of_annotations_stays_with_the_other_page(doc, settings):
    def share(pdf):
        shared = pdf.make_indirect(pikepdf.Array([pdf.make_indirect(Dictionary(
            Type=Name.Annot, Subtype=Name.Square, Rect=[0, 0, 10, 10]))]))
        pdf.pages[0].obj.Annots = shared
        pdf.pages[1].obj.Annots = shared
    _edit(doc, share)
    _, out = _add(doc, settings, {"type": "note", "page": 1, "at": [100, 100],
                                  "comment": "only here"})
    assert [c["page"] for c in _listed(out, settings)] == [1, 1, 2]


def test_text_not_found_writes_nothing(doc, settings):
    with pytest.raises(PdfUserError, match="Comment 2: 'nowhere' isn't found in the PDF"):
        _add(doc, settings, {"type": "highlight", "text": "PAGE-1"},
             {"type": "highlight", "text": "nowhere"})
    with pytest.raises(PdfUserError, match="isn't found on page 2"):
        _add(doc, settings, {"type": "note", "text": "PAGE-1", "page": 2, "comment": "x"})
    assert not doc.with_name("doc_c.pdf").exists()


def test_pages_too_large_to_search_are_passed_over(doc, settings, monkeypatch):
    monkeypatch.setattr(C, "MAX_PAGE_CONTENT", 1)
    with pytest.raises(PdfUserError, match="isn't found"):
        _add(doc, settings, {"type": "highlight", "text": "PAGE-1"})


def test_finding_text_has_a_time_limit(doc, settings, monkeypatch):
    monkeypatch.setattr(C, "FIND_TIME_LIMIT_S", -1)
    with pytest.raises(PdfUserError, match="finding 'PAGE-1' took too long; give its page"):
        _add(doc, settings, {"type": "highlight", "text": "PAGE-1"})


# --- what may be added ----------------------------------------------------------------


@pytest.mark.parametrize(("comment", "message"), [
    ("x", "must be an object"),
    ({"type": "circle"}, "type must be one of"),
    ({"type": "note", "rects": []}, "a note doesn't take rects"),
    ({"type": "highlight", "page": 0, "text": "a"}, "page must be 1 to 5"),
    ({"type": "highlight", "page": True, "text": "a"}, "page must be 1 to 5"),
    ({"type": "highlight", "text": "a", "comment": 5}, "comment must be text"),
    ({"type": "highlight", "text": "a", "comment": "x" * 10_001}, "comment must be text"),
    ({"type": "highlight", "text": "a", "whole_word": "yes"}, "whole_word must be true"),
    ({"type": "highlight", "text": "a", "color": 5}, "color must be #rrggbb"),
    ({"type": "highlight", "text": "a", "color": "mauve"}, "color must be #rrggbb"),
    ({"type": "highlight"}, "needs text to find, or rects"),
    ({"type": "highlight", "text": "a", "rects": [], "page": 1}, "needs text to find"),
    ({"type": "highlight", "rects": [[0, 0, 1, 1]]}, "rects need the page"),
    ({"type": "highlight", "rects": [], "page": 1}, "rects must be a list of 1 to 50"),
    ({"type": "highlight", "rects": [[0, 0, 1]], "page": 1}, "each rect must be"),
    ({"type": "highlight", "rects": [[0, 0, 1, True]], "page": 1}, "each rect must be"),
    ({"type": "highlight", "rects": [[5, 0, 1, 1]], "page": 1}, "left < right"),
    ({"type": "highlight", "rects": [[0, 0, 1, 1e9]], "page": 1}, "within the page"),
    ({"type": "highlight", "text": "  "}, "query"),
    ({"type": "note", "at": [1, 1], "page": 1}, "a note needs its comment"),
    ({"type": "note", "comment": "x"}, "a note needs text to find"),
    ({"type": "note", "comment": "x", "at": [1, 1]}, "at needs the page"),
    ({"type": "note", "comment": "x", "at": [1], "page": 1}, "at must be"),
    ({"type": "note", "comment": "x", "at": [1, 1e9], "page": 1}, "at must be"),
    ({"type": "text_box", "rect": [0, 0, 1, 1], "text": "x"}, "needs its page and rect"),
    ({"type": "text_box", "page": 1, "rect": [0, 0, 9, 9]}, "needs its text"),
    ({"type": "text_box", "page": 1, "rect": [0, 0, 9, 9], "text": " "}, "needs its text"),
    ({"type": "text_box", "page": 1, "rect": [0, 0, 9, 9], "text": "東京"},
     "can only use Latin characters"),
    ({"type": "text_box", "page": 1, "rect": [0, 0, 9, 9], "text": "x", "font_size": 3},
     "font_size must be 4 to 72"),
    ({"type": "text_box", "page": 1, "rect": [0, 0, 9, 9], "text": "x",
      "font_size": False}, "font_size must be 4 to 72"),
    ({"type": "text_box", "page": 1, "rect": "x", "text": "x"}, "rect must be"),
    ({"type": "reply", "to": "p1-1"}, "a reply needs its comment"),
    ({"type": "reply", "to": "1", "comment": "x"}, "to must be the id of a comment"),
])
def test_comments_are_checked(comment, message, doc, settings):
    with pytest.raises(PdfUserError, match=f"Comment 1: .*{message}"):
        _add(doc, settings, comment)


@pytest.mark.parametrize(("comments", "kw", "message"), [
    ([], {}, "comments must be a list of 1 to 100"),
    ([{"type": "note"}] * 101, {}, "comments must be a list of 1 to 100"),
    ([{"type": "note"}], {"author": "x" * 201}, "author must be text"),
    ([{"type": "note"}], {"author": 5}, "author must be text"),
])
def test_calls_are_checked(comments, kw, message, doc, settings):
    with pytest.raises(PdfUserError, match=message):
        E.add_comments(doc, doc.with_name("o.pdf"), settings, comments, **kw)


def test_a_text_box_may_break_lines(doc, settings):
    _, out = _add(doc, settings, {"type": "text_box", "page": 1,
                                  "rect": [300, 500, 500, 650], "text": "one\ntwo"})
    assert _listed(out, settings)[0]["comment"] == "one\ntwo"


# --- protection and signatures --------------------------------------------------------


def test_changing_comments_needs_the_owner_password_when_forbidden(doc, settings, tmp_path):
    locked = _lock(doc, tmp_path / "l.pdf", NO_ANNOTATIONS)
    note = {"type": "note", "page": 1, "at": [1, 100], "comment": "x"}
    with pytest.raises(PdfUserError, match="does not allow adding comments"):
        _add(locked, settings, note, password="user")
    res, out = _add(locked, settings, note, password="owner")
    assert res["password_protected"] is True
    with pikepdf.open(out, password="owner") as pdf:
        assert pdf.is_encrypted and not pdf.allow.modify_annotation


def test_comments_on_a_protected_pdf_stay_protected(doc, settings, tmp_path):
    locked = _lock(doc, tmp_path / "l.pdf", OPEN)
    res, out = _add(locked, settings, {"type": "highlight", "text": "PAGE-1"},
                    password="user")
    assert res["password_protected"] is True
    assert _listed(out, settings, password="user")[0]["marked_text"] == "PAGE-1"


def test_signed_pdfs_are_refused(doc, settings):
    _edit(doc, _signed)
    with pytest.raises(PdfUserError, match="digitally signed; adding comments"):
        _add(doc, settings, {"type": "note", "page": 1, "at": [1, 100], "comment": "x"})


def test_reader_usage_rights_are_dropped(doc, settings):
    _edit(doc, _usage_rights)
    _, out = _add(doc, settings, {"type": "note", "page": 1, "at": [1, 100], "comment": "x"})
    with pikepdf.open(out) as pdf:
        assert "/Perms" not in pdf.Root


# --- listing --------------------------------------------------------------------------


def _with_annots(path, *annots, page=0):
    def add(pdf):
        pdf.pages[page].obj.Annots = pikepdf.Array([
            pdf.make_indirect(a) if isinstance(a, Dictionary) else a for a in annots])
    return _edit(path, add)


@pytest.mark.filterwarnings("ignore:This document has 1 form widget")  # the stray one
def test_only_comments_are_listed(doc, settings):
    _with_annots(doc,
                 Dictionary(Type=Name.Annot, Subtype=Name.Link, Rect=[0, 0, 1, 1]),
                 Dictionary(Type=Name.Annot, Subtype=Name.Widget, Rect=[0, 0, 1, 1]),
                 Dictionary(Type=Name.Annot, Subtype=Name.Ink, Rect=[0, 0, 1, 1],
                            C=[0.5], T=pikepdf.String("A" * 300)),
                 Dictionary(Type=Name.Annot, Subtype=Name.Stamp, Rect=[1, 1, 0, 0],
                            C=[0, 0, 0, 1], Contents=pikepdf.String("x" * 6000)),
                 Dictionary(Type=Name.Annot, Subtype=Name.Circle, Rect=["a", 1, 2, 3],
                            C=[1, 2]), 7)
    _edit(doc, lambda pdf: pdf.pages[0].obj.Annots.append(Dictionary(  # written inline
        Type=Name.Annot, Subtype=Name.Square, Rect=[0, 0, 1, 1], C=[Name.X, 0, 0])))
    ink, stamp, circle, square = _listed(doc, settings)
    assert (square["id"], square["color"]) == ("p1-7", None)
    assert (ink["id"], ink["type"], ink["color"], len(ink["author"])) == \
        ("p1-3", "ink", "#808080", 200)
    assert ink["author"].endswith("…")
    assert (stamp["id"], stamp["color"], stamp["rect"]) == ("p1-4", "#000000", [0, 0, 1, 1])
    assert len(stamp["comment"]) == 5000
    assert (circle["rect"], circle["color"]) == (None, None)


def test_listing_pages_and_limits(make_pdf, settings, monkeypatch):
    pdf = make_pdf("d.pdf", 3)
    for page in range(3):
        _with_annots(pdf, Dictionary(Type=Name.Annot, Subtype=Name.Text,
                                     Rect=[0, 0, 1, 1]), page=page)
    assert [c["page"] for c in _listed(pdf, settings, pages="2-3")] == [2, 3]
    monkeypatch.setattr(C, "MAX_LISTED", 2)
    res = E.list_comments(pdf, settings)
    assert res["count"] == 2 and res["truncated"] is True
    monkeypatch.setattr(C, "MAX_SCANNED", 2)
    with pytest.raises(PdfUserError, match="over 2 annotations; list fewer pages"):
        E.list_comments(pdf, settings)


def test_comment_text_is_withheld_when_copying_is_forbidden(doc, settings, tmp_path):
    _, out = _add(doc, settings, {"type": "highlight", "text": "PAGE-1", "comment": "why"},
                  author="Sam")
    locked = _lock(out, tmp_path / "l.pdf", OPEN._replace(extract=False))
    res = E.list_comments(locked, settings, password="user")
    (high,) = res["comments"]
    assert (high["comment"], high["marked_text"], high["author"]) == (None, None, "Sam")
    assert res["text_withheld"] is True and res["marked_text_complete"] is False
    res = E.list_comments(locked, settings, password="owner")
    assert (res["comments"][0]["comment"], res["comments"][0]["marked_text"]) == \
        ("why", "PAGE-1")
    assert res["text_withheld"] is False and res["marked_text_complete"] is True


def test_marked_text_on_pages_too_large_to_read(doc, settings, monkeypatch):
    _, out = _add(doc, settings, {"type": "highlight", "text": "PAGE-1"})
    monkeypatch.setattr(C, "MAX_PAGE_CONTENT", 1)
    res = E.list_comments(out, settings)
    assert res["comments"][0]["marked_text"] is None and res["marked_text_complete"] is False


def test_markup_without_quad_points_uses_its_rect(doc, settings):
    _with_annots(doc,
                 Dictionary(Type=Name.Annot, Subtype=Name.Highlight, Rect=[60, 700, 200, 740]),
                 Dictionary(Type=Name.Annot, Subtype=Name.Underline, Rect=[60, 700, 200, 740],
                            QuadPoints=[1, 2, 3]),
                 Dictionary(Type=Name.Annot, Subtype=Name.StrikeOut, Rect=[0, 0, 1, 1],
                            QuadPoints=["a"] * 8),
                 Dictionary(Type=Name.Annot, Subtype=Name.Squiggly, Rect="x"))
    assert [c["marked_text"] for c in _listed(doc, settings)] == ["PAGE-1", "PAGE-1", "", ""]


@pytest.mark.parametrize(("value", "iso"), [
    ("D:20261103115631-05'00'", "2026-11-03T11:56:31-05:00"),
    ("D:20261103115631+05'30", "2026-11-03T11:56:31+05:30"),
    ("D:20261103115631Z", "2026-11-03T11:56:31+00:00"),
    ("D:2026", "2026-01-01T00:00:00"),
    ("20261103", "2026-11-03T00:00:00"),
    ("D:20261303", None),
    ("D:20261103+30'00'", None),
    ("yesterday", None),
])
def test_dates(value, iso):
    assert C.iso_date(pikepdf.String(value)) == iso


def test_dates_written():
    when = datetime(2026, 11, 3, 9, 5, 7, tzinfo=timezone(-timedelta(hours=5, minutes=30)))
    assert C.pdf_date(when) == "D:20261103090507-05'30'"
    assert C.pdf_date(when.replace(tzinfo=None)) == "D:20261103090507+00'00'"
    assert C.iso_date(5) is None


# --- found in review --------------------------------------------------------------------


def test_finding_text_needs_copying_allowed(doc, settings, tmp_path, monkeypatch):
    from paperboy import server as S
    locked = _lock(doc, tmp_path / "l.pdf", OPEN._replace(extract=False))
    with pytest.raises(PdfUserError,
                       match="does not allow copying its content; finding text to comment"):
        _add(locked, settings, {"type": "highlight", "text": "PAGE-1"}, password="user")
    with pytest.raises(PdfUserError, match="finding text to comment on needs its owner password"):
        _add(locked, settings, {"type": "note", "text": "PAGE-1", "comment": "x"},
             password="user")
    # Rectangles and points given don't read the text.
    res, _ = _add(locked, settings, {"type": "highlight", "page": 1,
                                     "rects": [[60, 700, 200, 740]]},
                  {"type": "text_box", "page": 1, "rect": [300, 600, 500, 650], "text": "x"},
                  password="user")
    assert res["added"] == 2
    res, _ = _add(locked, settings, {"type": "highlight", "text": "PAGE-1"}, password="owner")
    assert res["added"] == 1
    monkeypatch.setattr(S, "SETTINGS", settings)
    with pytest.raises(S.ToolError, match="finding text to comment on needs"):
        S.run_pipeline(str(locked), [{"tool": "add_comments", "args": {
            "comments": [{"type": "highlight", "text": "PAGE-1"}]}}], password="user")


def test_reading_marked_text_is_bounded(make_pdf, settings, monkeypatch):
    pdf = make_pdf("d.pdf", 2)
    _, out = _add(pdf, settings, {"type": "highlight", "text": "PAGE"})
    monkeypatch.setattr(C, "MAX_MARKED_RECTS", 1)
    res = E.list_comments(out, settings)
    assert [c["marked_text"] for c in res["comments"]] == ["PAGE", None]
    assert res["marked_text_complete"] is False
    monkeypatch.setattr(C, "MAX_MARKED_RECTS", 100)
    monkeypatch.setattr(C, "MARKED_TIME_LIMIT_S", -1)
    res = E.list_comments(out, settings)
    assert [c["marked_text"] for c in res["comments"]] == [None, None]


def test_a_page_pdfium_cannot_open_has_no_marked_text(doc, settings, monkeypatch):
    _, out = _add(doc, settings, {"type": "highlight", "text": "PAGE-1"})

    class Broken:
        def __getitem__(self, index):
            raise C.pdfium.PdfiumError("no page")
    monkeypatch.setattr(C, "_open", lambda data: Broken())
    monkeypatch.setattr(C, "_close", lambda doc: None)
    res = E.list_comments(out, settings)
    assert res["comments"][0]["marked_text"] is None and res["marked_text_complete"] is False


def test_squiggles_are_bounded(doc, settings):
    _, out = _add(doc, settings, {"type": "squiggly", "page": 1,
                                  "rects": [[0, 400, 595, 400.5]] * 50})
    with pikepdf.open(out) as pdf:
        content = pdf.pages[0].Annots[0].AP.N.read_bytes()
    assert content.count(b" l") <= 50 * (C.MAX_WAVES + 1)


@pytest.mark.parametrize(("comment", "message"), [
    ({"type": "highlight", "page": 1, "rects": [[-500, -500, -10, -10]]},
     r"rects are all off the visible page \[0, 0, 595.276, 841.89\]"),
    ({"type": "note", "page": 1, "at": [-50, 100], "comment": "x"},
     "at must be on the visible page"),
    ({"type": "text_box", "page": 1, "rect": [500, 800, 700, 900], "text": "x"},
     "rect must be on the visible page"),
])
def test_comments_must_be_on_the_visible_page(comment, message, doc, settings):
    with pytest.raises(PdfUserError, match=f"Comment 1: {message}"):
        _add(doc, settings, comment)


def test_rects_are_cut_to_the_page_and_notes_kept_on_it(doc, settings):
    _, out = _add(doc, settings,
                  {"type": "highlight", "page": 1, "rects": [[-50, 700, 100, 740],
                                                            [-50, -50, -10, -10]]},
                  {"type": "note", "page": 1, "at": [590, 10], "comment": "edge"})
    high, note = _listed(out, settings)
    assert high["rect"][0] == -1 and note["rect"] == [575.28, 0, 595.28, 20]
    with pikepdf.open(out) as pdf:
        assert len(pdf.pages[0].Annots[0].QuadPoints) == 8


def test_text_outside_a_crop_is_not_commented(doc, settings):
    _edit(doc, lambda pdf: setattr(pdf.pages[0], "CropBox", [200, 0, 595, 842]))
    with pytest.raises(PdfUserError, match="'PAGE-1' is only found off the visible page"):
        _add(doc, settings, {"type": "highlight", "text": "PAGE-1"})


def test_notes_by_text_stay_on_a_cropped_page(doc, settings):
    _edit(doc, lambda pdf: setattr(pdf.pages[0], "CropBox", [70, 0, 595, 842]))
    _, out = _add(doc, settings, {"type": "note", "text": "PAGE-1", "comment": "x"})
    assert _listed(out, settings)[0]["rect"][0] == 70


@pytest.mark.parametrize(("rotation", "rect", "quad"), [
    (0, [0, 0, 100, 10], [0, 10, 100, 10, 0, 0, 100, 0]),
    (90, [0, 0, 10, 100], [0, 0, 0, 100, 10, 0, 10, 100]),
    (180, [0, 0, 100, 10], [100, 0, 0, 0, 100, 10, 0, 10]),
    (270, [0, 0, 10, 100], [10, 100, 10, 0, 0, 100, 0, 0]),
    (0, [0, 0, 10, 100], [0, 0, 0, 100, 10, 0, 10, 100]),     # a run up the page
    (90, [0, 0, 100, 10], [0, 10, 100, 10, 0, 0, 100, 0]),    # sideways as displayed
    (90, [0, 0, 10, 10], [0, 0, 0, 10, 10, 0, 10, 10]),       # one letter: as displayed
])
def test_corners_follow_the_text(rotation, rect, quad):
    assert [v for point in C.corners(rect, rotation) for v in point] == quad


def test_lines_follow_text_running_up_a_turned_page(doc, settings):
    turned = _edit(doc, lambda pdf: setattr(pdf.pages[0], "Rotate", 90))
    _, out = _add(turned, settings, {"type": "underline", "page": 1,
                                     "rects": [[300, 100, 320, 400]]})
    with pikepdf.open(out) as pdf:
        content = pdf.pages[0].Annots[0].AP.N.read_bytes().decode()
    # Along the text (up the stored page), near its foot as displayed (the right edge).
    assert "318.4 100 m 318.4 400 l" in content


def test_shared_strings_are_read_once(doc, settings, monkeypatch):
    def share(pdf):
        text = pdf.make_indirect(pikepdf.String("shared " * 10))
        pdf.pages[0].obj.Annots = pikepdf.Array([pdf.make_indirect(Dictionary(
            Type=Name.Annot, Subtype=Name.Text, Rect=[0, 0, 1, 1], Contents=text, T=text))
            for _ in range(5)])
    _edit(doc, share)
    calls = []
    real = C._string
    monkeypatch.setattr(C, "_string", lambda v, limit=C.MAX_FIELD: calls.append(limit)
                        or real(v, limit))
    listed = _listed(doc, settings)
    assert len(listed) == 5 and listed[4]["comment"] == "shared " * 10
    assert calls.count(C.MAX_FIELD) == 1  # the comment text, for all five


def test_huge_strings_are_cut_before_reading():
    assert C._string(pikepdf.String("x" * 100_000), 10) == "x" * 9 + "\u2026"
    utf16 = pikepdf.String("\u6771" * 100_000)
    assert C._string(utf16, 10) == "\u6771" * 9 + "\u2026"


def test_replies_are_linked_across_pages_and_only_to_comments(make_pdf, settings):
    pdf = make_pdf("d.pdf", 2)

    def threads(pdf):
        parent = pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Text,
                                              Rect=[0, 0, 1, 1]))
        link = pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Link,
                                            Rect=[0, 0, 1, 1]))
        own = pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Text,
                                           Rect=[0, 0, 1, 1]))
        own.IRT = own
        pdf.pages[0].obj.Annots = pikepdf.Array([parent, link, own])
        pdf.pages[1].obj.Annots = pikepdf.Array([
            parent,  # listed again: it keeps its first id
            Dictionary(Type=Name.Annot, Subtype=Name.Text, Rect=[0, 0, 1, 1], IRT=parent),
            Dictionary(Type=Name.Annot, Subtype=Name.Text, Rect=[0, 0, 1, 1], IRT=link)])
    _edit(pdf, threads)
    listed = {c["id"]: c["reply_to"] for c in _listed(pdf, settings, pages="2")}
    assert listed == {"p2-1": None, "p2-2": "p1-1", "p2-3": None}
    assert _listed(pdf, settings, pages="1")[1] == {**_listed(pdf, settings, pages="1")[1],
                                                    "id": "p1-3", "reply_to": None}


@pytest.mark.parametrize("field", ["comment", "author"])
def test_half_characters_are_refused(field, doc, settings):
    note = {"type": "note", "page": 1, "at": [100, 100], "comment": "ok"}
    kw = {}
    if field == "comment":
        note["comment"] = "bad \ud800"
    else:
        kw["author"] = "bad \udfff"
    with pytest.raises(PdfUserError, match=f"{field} must be text"):
        _add(doc, settings, note, **kw)
