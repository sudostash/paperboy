"""search_text: literal search with page, snippet and position of each match."""
from __future__ import annotations

import pikepdf
import pypdfium2 as pdfium
import pytest
from reportlab.pdfgen import canvas

from paperboy import engine as E
from paperboy.engine import search
from paperboy.errors import PdfUserError


def _pdf(path, pages: list[list[str]]):
    """One list of lines per page (an empty list is a page without text)."""
    c = canvas.Canvas(str(path))
    for lines in pages:
        for n, line in enumerate(lines):
            c.drawString(72, 720 - 20 * n, line)
        if not lines:
            c.rect(100, 100, 50, 50, fill=1)   # ink, but no text
        c.showPage()
    c.save()
    return path


@pytest.fixture
def story(tmp_path):
    return _pdf(tmp_path / "story.pdf", [
        ["The quick brown fox jumps over the lazy dog.", "Foxes are quick; a foxtrot is a dance."],
        [],
        ["Nothing to see here.", "Except one more fox."],
    ])


def _found(result):
    return [(m["page"], m["text"]) for m in result["matches"]]


def test_matches_in_page_order_ignoring_case(story, settings):
    res = E.search_text(story, "fox", settings)
    assert _found(res) == [(1, "fox"), (1, "Fox"), (1, "fox"), (3, "fox")]
    assert res["match_count"] == 4 and res["truncated"] is False
    assert res["pages_searched"] == 3 and res["query"] == "fox"


def test_match_case_and_whole_word(story, settings):
    assert _found(E.search_text(story, "Fox", settings, match_case=True)) == [(1, "Fox")]
    assert _found(E.search_text(story, "fox", settings, whole_word=True)) == [(1, "fox"),
                                                                              (3, "fox")]


def test_phrases_and_the_page_spec(story, settings):
    assert _found(E.search_text(story, "lazy dog", settings)) == [(1, "lazy dog")]
    assert _found(E.search_text(story, "fox", settings, pages="3")) == [(3, "fox")]
    assert E.search_text(story, "fox", settings, pages="2")["pages_searched"] == 1


def test_snippets_show_the_text_around_a_match(story, settings):
    first, *_, last = E.search_text(story, "fox", settings)["matches"]
    assert first["context"].startswith("The quick brown fox jumps")
    assert first["context"].endswith("…")                 # more text follows
    assert last["context"] == "Nothing to see here. Except one more fox."   # whole page
    (dance,) = E.search_text(story, "dance", settings)["matches"]
    assert dance["context"].startswith("…") and dance["context"].endswith("is a dance.")
    assert len(dance["context"]) <= 1 + 60 + len("dance") + 1


def test_rects_locate_the_match_on_the_page(story, settings):
    (match,) = E.search_text(story, "lazy dog", settings)["matches"]
    ((left, bottom, right, top),) = match["rects"]
    assert 72 < left < right < 400 and 715 < bottom < top < 735   # on the first line


def test_pages_without_text_are_listed(story, settings, monkeypatch):
    assert E.search_text(story, "fox", settings)["pages_without_text"] == [2]
    monkeypatch.setattr(search, "MAX_LISTED", 0)
    assert E.search_text(story, "fox", settings)["pages_without_text"] == []


def test_results_stop_at_max_results(story, settings):
    res = E.search_text(story, "fox", settings, max_results=2)
    assert _found(res) == [(1, "fox"), (1, "Fox")] and res["truncated"] is True
    res = E.search_text(story, "fox", settings, max_results=3)
    assert res["truncated"] is True                        # page 3 still has one
    assert E.search_text(story, "fox", settings, max_results=4)["truncated"] is False


def test_no_matches(story, settings):
    res = E.search_text(story, "zebra", settings)
    assert res["matches"] == [] and res["match_count"] == 0


def test_accented_text(tmp_path, settings):
    doc = _pdf(tmp_path / "fr.pdf", [["Le café est très bon."]])
    assert _found(E.search_text(doc, "café", settings)) == [(1, "café")]
    assert _found(E.search_text(doc, "CAFÉ", settings)) == [(1, "café")]


def test_surrounding_spaces_in_the_query_are_ignored(story, settings):
    assert E.search_text(story, "  dog  ", settings)["query"] == "dog"


@pytest.mark.parametrize(("query", "max_results", "message"), [
    ("", 10, "query must be"),
    ("   ", 10, "query must be"),
    ("x" * 201, 10, "query must be"),
    (5, 10, "query must be"),
    ("fox", 0, "max_results must be 1 to 1000"),
    ("fox", 1001, "max_results must be 1 to 1000"),
    ("fox", True, "max_results must be"),
])
def test_bad_arguments(story, settings, query, max_results, message):
    with pytest.raises(PdfUserError, match=message):
        E.search_text(story, query, settings, max_results=max_results)


def test_protected_pdfs(story, settings, tmp_path):
    allowed, forbidden = tmp_path / "allowed.pdf", tmp_path / "forbidden.pdf"
    with pikepdf.open(story) as pdf:
        pdf.save(allowed, encryption=pikepdf.Encryption(user="pw", owner="owner", R=6))
        pdf.save(forbidden, encryption=pikepdf.Encryption(
            user="pw", owner="owner", R=6, allow=pikepdf.Permissions(extract=False)))
    assert len(E.search_text(allowed, "fox", settings, password="pw")["matches"]) == 4
    with pytest.raises(PdfUserError, match="does not allow copying.*searching its text"):
        E.search_text(forbidden, "fox", settings, password="pw")
    assert len(E.search_text(forbidden, "fox", settings, password="owner")["matches"]) == 4
    with pytest.raises(PdfUserError, match="supply its password"):
        E.search_text(allowed, "fox", settings)


def test_a_page_pdfium_cannot_read_is_reported(story, settings, monkeypatch):
    def broken(self, index):
        raise pdfium.PdfiumError("bad page")
    monkeypatch.setattr(pdfium.PdfDocument, "__getitem__", broken)
    with pytest.raises(PdfUserError, match="Could not read page 1"):
        E.search_text(story, "fox", settings)


# --- review findings ---------------------------------------------------------------------------


@pytest.mark.parametrize(("query", "message"), [
    ("\x00", "control characters"),           # PDFium would search for "" forever
    ("\x00fox", "control characters"),
    ("fox\x00x", "control characters"),
    ("a\x07b", "control characters"),
    ("\ud800", "invalid character"),
    ("\U0001f600", "beyond U\\+FFFF"),
])
def test_queries_pdfium_cannot_handle_are_refused(story, settings, query, message):
    with pytest.raises(PdfUserError, match=message):
        E.search_text(story, query, settings)


@pytest.mark.parametrize(("query", "searched"), [
    ("lazy   dog", "lazy dog"),                # whitespace runs are one space
    ("lazy dog", "lazy dog"),             # a no-break space is a space
    ("lazy\tdog\n", "lazy dog"),
    ("ﬁ", "fi"),                          # the "fi" ligature finds "fi"
])
def test_queries_are_normalized(tmp_path, settings, query, searched):
    doc = _pdf(tmp_path / "d.pdf", [["the lazy dog has a fine coat"]])
    res = E.search_text(doc, query, settings)
    assert res["query"] == searched and res["match_count"] == 1


def test_a_space_matches_across_a_line_break(story, settings):
    (match,) = E.search_text(story, "dog. Foxes", settings)["matches"]
    assert len(match["rects"]) == 2 and match["rects_truncated"] is False


def test_rectangles_per_match_are_capped(story, settings, monkeypatch):
    monkeypatch.setattr(search, "MAX_RECTS", 1)
    (match,) = E.search_text(story, "dog. Foxes", settings)["matches"]
    assert len(match["rects"]) == 1 and match["rects_truncated"] is True


def test_rectangles_are_in_the_pages_own_coordinates(story, settings):
    with pikepdf.open(story, allow_overwriting_input=True) as pdf:
        pdf.pages[0].obj.Rotate = 90
        pdf.save(story)
    (match,) = E.search_text(story, "lazy dog", settings)["matches"]
    left, bottom, _, _ = match["rects"][0]
    assert 72 < left and 715 < bottom < 735     # as drawn, not rotated


def test_pages_with_too_much_content_are_skipped(story, settings, monkeypatch):
    monkeypatch.setattr(search, "MAX_PAGE_CONTENT", 170)  # page 1 has 190 bytes, page 3 148
    res = E.search_text(story, "fox", settings)
    assert res["pages_skipped"] == [1] and _found(res) == [(3, "fox")]
    assert res["pages_searched"] == 3 and res["complete"] is True


def test_content_in_forms_counts_and_shared_streams_are_measured_once(story, settings,
                                                                       monkeypatch):
    def move_into_form(pdf):
        page = pdf.pages[2].obj
        form = pikepdf.Stream(pdf, b"")
        form.write(b"x" * 500)
        form.Subtype, form.BBox = pikepdf.Name.Form, pikepdf.Array([0, 0, 10, 10])
        form = pdf.make_indirect(form)
        form.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Self=form))  # a loop
        page.Resources.XObject = pikepdf.Dictionary(Fm0=form)
    with pikepdf.open(story, allow_overwriting_input=True) as pdf:
        move_into_form(pdf)
        pdf.save(story)
    monkeypatch.setattr(search, "MAX_PAGE_CONTENT", 400)
    measured = []
    real = search.decoded_size
    monkeypatch.setattr(search, "decoded_size", lambda s, c: measured.append(s.objgen)
                        or real(s, c))
    res = E.search_text(story, "fox", settings)
    assert 3 in res["pages_skipped"]
    assert len(measured) == len(set(measured))


def test_content_that_cannot_be_measured_is_skipped(story, settings):
    with pikepdf.open(story, allow_overwriting_input=True) as pdf:
        contents = pdf.pages[2].obj.Contents
        contents.write(b"\x80\x0b", filter=pikepdf.Array([pikepdf.Name.LZWDecode,
                                                           pikepdf.Name.FlateDecode]))
        pdf.save(story, compress_streams=False,
                 stream_decode_level=pikepdf.StreamDecodeLevel.none)
    assert E.search_text(story, "fox", settings)["pages_skipped"] == [3]


def test_the_time_limit_returns_a_partial_result(story, settings, monkeypatch):
    monkeypatch.setattr(search, "SEARCH_TIME_LIMIT_S", -1)
    res = E.search_text(story, "fox", settings)
    assert res["complete"] is False and res["pages_searched"] == 1
    assert _found(res) == [(1, "fox"), (1, "Fox"), (1, "fox")]


def test_a_hostile_query_does_not_hold_the_pdfium_lock(story, settings):
    with pytest.raises(PdfUserError):
        E.search_text(story, "\x00", settings)
    from paperboy.engine.render import _PDFIUM_LOCK
    assert _PDFIUM_LOCK.acquire(timeout=1)
    _PDFIUM_LOCK.release()


def test_the_lock_is_released_when_a_page_fails(story, settings, monkeypatch):
    def broken(self, index):
        raise pdfium.PdfiumError("bad page")
    monkeypatch.setattr(pdfium.PdfDocument, "__getitem__", broken)
    with pytest.raises(PdfUserError):
        E.search_text(story, "fox", settings)
    from paperboy.engine.render import _PDFIUM_LOCK
    assert _PDFIUM_LOCK.acquire(timeout=1)
    _PDFIUM_LOCK.release()
