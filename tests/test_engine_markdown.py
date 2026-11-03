"""pdf_to_markdown / export_markdown: structure from the layout, text that can't inject."""
from __future__ import annotations

import dataclasses

import pikepdf
import pytest
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfgen import canvas
from reportlab.platypus import ListFlowable, ListItem, Paragraph
from test_engine_tables import REGIONS, _doc, _table

from paperboy import engine as E
from paperboy.engine import markdown as M
from paperboy.engine import tables as T
from paperboy.errors import PdfUserError

STYLES = getSampleStyleSheet()
BODY = ("Paperboy reads every page in reading order and joins the lines of each paragraph "
        "so that a model sees whole sentences instead of fragments cut at the margin.")


def _draw(path, *pages, size=letter):
    """Each argument draws one page: a function taking the canvas."""
    c = canvas.Canvas(str(path), pagesize=size)
    for draw in pages:
        draw(c)
        c.showPage()
    c.save()
    return path


def _text(*rows, font="Helvetica", size=10):
    """A page drawing (x, y, text) rows; a row may name its own font and size."""
    def draw(c):
        for row in rows:
            x, y, text, *style = row
            c.setFont(*(style or [font, size]))
            c.drawString(x, y, text)
    return draw


def _md(path, settings, **kw):
    kw.setdefault("page_markers", False)
    return E.pdf_to_markdown(path, settings, **kw)


@pytest.fixture
def report(tmp_path):
    return _doc(tmp_path / "report.pdf", [
        Paragraph("Annual Report", STYLES["Title"]),
        Paragraph("Summary", STYLES["Heading1"]),
        Paragraph(BODY, STYLES["BodyText"]),
        Paragraph("Regions", STYLES["Heading2"]),
        _table(REGIONS),
        Paragraph("Next steps", STYLES["Heading2"]),
        ListFlowable([ListItem(Paragraph("Hire drivers", STYLES["BodyText"])),
                      ListItem(Paragraph("Open depots", STYLES["BodyText"]))],
                     bulletType="bullet"),
    ], [Paragraph("Outlook", STYLES["Heading1"]), Paragraph(BODY, STYLES["BodyText"])])


# --- structure -----------------------------------------------------------------------------


def test_headings_paragraphs_lists_and_tables(report, settings):
    res = _md(report, settings)
    assert res["markdown"].split("\n\n") == [
        "# Annual Report", "# Summary", BODY, "## Regions",
        "| Region | Deliveries | On time |\n| --- | --- | --- |\n"
        "| North | 12,400 | 98.1% |\n| South | 10,950 | 97.4% |\n| East | 8,210 | 96.9% |",
        "## Next steps", "- Hire drivers\n- Open depots",
        "# Outlook", BODY]
    assert (res["headings"], res["tables"], res["pages_read"]) == (5, 1, 2)
    assert res["complete"] is True and res["truncated"] is False
    assert res["characters"] == len(res["markdown"])
    assert res["pages_skipped"] == [] and res["pages_without_text"] == []


def test_page_markers_and_page_specs(report, settings):
    res = E.pdf_to_markdown(report, settings, pages="2")
    assert res["markdown"].startswith("<!-- page 2 -->\n\n# Outlook\n\n")
    assert res["pages_read"] == 1
    assert "<!-- page" not in _md(report, settings)["markdown"]


def test_without_tables_their_text_stays_text(report, settings):
    res = _md(report, settings, include_tables=False)
    assert res["tables"] == 0 and "| ---" not in res["markdown"]
    assert "North" in res["markdown"]


def test_words_hyphenated_at_a_line_break_are_mended(tmp_path, settings):
    pdf = _draw(tmp_path / "h.pdf", _text((72, 700, "The deliveries were remark-"),
                                          (72, 688, "ably punctual, and well-"),
                                          (72, 676, "Known drivers stayed.")))
    assert _md(pdf, settings)["markdown"] == \
        "The deliveries were remarkably punctual, and well- Known drivers stayed."


def test_lists_from_inline_markers(tmp_path, settings):
    pdf = _draw(tmp_path / "l.pdf", _text(
        (72, 700, "• Vans"), (72, 680, "• Bikes"),
        (72, 640, "1. Load"), (72, 620, "2) Drive"), (72, 600, "b) Return")))
    # Markdown has no lettered lists: such items are bullets that keep their letter.
    assert _md(pdf, settings)["markdown"] == "- Vans\n- Bikes\n\n1. Load\n2. Drive\n\n- b) Return"


def test_a_bullet_drawn_apart_from_its_text(tmp_path, settings):
    # reportlab's standard fonts draw "•" as a glyph with no text: pdfminer reads it
    # as unmapped, like many real bullets.
    pdf = _draw(tmp_path / "b.pdf", _text((72, 700, "•"), (90, 700, "Apart"),
                                          (60, 650, "3."), (90, 650, "Numbered"),
                                          (40, 620, "7"), (90, 620, "Not a list"),
                                          (300, 600, "•"), (300, 560, "*")))
    # A marker with nothing beside it marks nothing: an unreadable one is dropped.
    # A bare number beside a line may be a line number: it stays text.
    assert _md(pdf, settings)["markdown"].split("\n\n") == [
        "- Apart", "3. Numbered", "7", "Not a list", "\\*"]


def test_numbers_without_dots_make_a_list_only_in_sequence(tmp_path, settings):
    pdf = _draw(tmp_path / "n.pdf",
                _text((72, 700, "1 Collect"), (72, 688, "2 Sort"), (72, 676, "3 Deliver"),
                      (72, 600, "12 apples"), (72, 588, "40 pears")))
    assert _md(pdf, settings)["markdown"] == \
        "1. Collect\n2. Sort\n3. Deliver\n\n12 apples 40 pears"


def test_a_short_bold_line_on_its_own_is_a_heading(tmp_path, settings):
    pdf = _draw(tmp_path / "bold.pdf", _text(
        (72, 720, "Big Title", "Helvetica", 20),
        (72, 690, "Delivery terms", "Helvetica-Bold", 10),
        (72, 676, BODY[:90]), (72, 664, BODY[90:]),
        (72, 630, "Bold sentence.", "Helvetica-Bold", 10)))
    assert _md(pdf, settings)["markdown"].split("\n\n") == [
        "# Big Title", "## Delivery terms", BODY, "Bold sentence."]


def test_a_heading_wrapped_onto_two_lines_is_one(tmp_path, settings):
    pdf = _draw(tmp_path / "w.pdf", _text((72, 720, "A Heading That", "Helvetica", 18),
                                          (72, 700, "Wraps", "Helvetica", 18),
                                          (72, 670, BODY[:80])))
    assert _md(pdf, settings)["markdown"].split("\n\n")[0] == "# A Heading That Wraps"


def test_heading_levels_follow_sizes_across_pages(tmp_path, settings):
    pdf = _draw(tmp_path / "s.pdf",
                _text((72, 720, "Part", "Helvetica", 24), (72, 690, "Chapter", "Helvetica", 18),
                      (72, 660, BODY[:80])),
                _text((72, 720, "Section", "Helvetica", 14), (72, 690, "Detail", "Helvetica", 12),
                      (72, 660, BODY[:80])))
    blocks = _md(pdf, settings)["markdown"].split("\n\n")
    assert blocks[:2] == ["# Part", "## Chapter"]
    assert blocks[3:5] == ["### Section", "### Detail"]


def test_columns_are_read_in_order(tmp_path, settings):
    left = [(72, 700 - 12 * n, f"left {n} column text goes here") for n in range(6)]
    right = [(320, 700 - 12 * n, f"right {n} column text goes here") for n in range(6)]
    pdf = _draw(tmp_path / "c.pdf", _text(*left, *right))
    text = _md(pdf, settings)["markdown"]
    assert text.index("left 5") < text.index("right 0")


def _column(x, y, *texts, gap=14):
    return [(x, y - gap * n, text) for n, text in enumerate(texts)]


def test_side_by_side_headings_stay_with_their_columns(tmp_path, settings):
    left = _column(72, 700, "Left words here", "more left words", "last left words")
    right = _column(330, 700, "Right words here", "more right words")
    pdf = _draw(tmp_path / "c.pdf", _text(
        (72, 730, "Left Heading", "Helvetica-Bold", 16),
        (330, 730, "Right Heading", "Helvetica-Bold", 16), *left, *right,
        (72, 500, "After Columns", "Helvetica-Bold", 16), (72, 480, "Full width text.")))
    assert _md(pdf, settings)["markdown"].split("\n\n") == [
        "# Left Heading", "Left words here more left words last left words",
        "# Right Heading", "Right words here more right words",
        "# After Columns", "Full width text."]


def test_a_longer_column_goes_on_below_the_other(tmp_path, settings):
    left = _column(72, 700, *[f"left line {n} of the column" for n in range(4)])
    tail = _column(72, 632, "left tail of the column")  # past the right one
    right = _column(330, 700, "right line 0 of it", "right line 1 of it")
    text = _md(_draw(tmp_path / "c.pdf", _text(*left, *tail, *right)), settings)["markdown"]
    assert text.index("left tail") < text.index("right line 0")


def test_three_columns(tmp_path, settings):
    rows = [*_column(72, 700, "alpha one", "alpha two"), *_column(250, 700, "beta one",
            "beta two"), *_column(430, 700, "gamma one", "gamma two")]
    assert _md(_draw(tmp_path / "c.pdf", _text(*rows)), settings)["markdown"].split("\n\n") \
        == ["alpha one alpha two", "beta one beta two", "gamma one gamma two"]


def test_ordering_and_marker_matching_are_bounded(monkeypatch):
    def line(text, x0, top, box):
        return M.Line([text, x0, top, x0 + 8 * len(text), top + 10, 10, False, box])
    lines = [line("right", 300, 0, 1), line("left", 72, 0, 2), line("•", 60, 20, 3),
             line("item", 72, 20, 4)]
    monkeypatch.setattr(M, "MAX_BOXES", 3)  # too many boxes: pdfminer's order is kept
    assert [x.text for x in M.reading_order(lines)] == ["right", "left", "•", "item"]
    monkeypatch.setattr(M, "MAX_MARKERS", 0)  # too many bullets: they stay text
    assert [x.text for x in M._attach_markers(lines)] == ["right", "left", "•", "item"]
    monkeypatch.setattr(M, "MAX_ORDER_DEPTH", -1)  # columns within columns stop here
    assert [b.lines[0].text for b in M._order([M._Box([x]) for x in lines])] == \
        ["left", "right", "•", "item"]


def test_gaps_and_open_gutters():
    def box(x0, x1):
        return M._Box([M.Line(["x", x0, 0, x1, 10, 10, False, 1])])
    assert M._gaps([box(0, 10), box(50, 60)], 0, 100) == [(10, 50), (60, 100)]
    assert M._still_open([(10, 50)], [box(12, 20)]) == [(20, 50)]
    assert M._still_open([(10, 50)], [box(40, 48)]) == [(10, 40)]
    assert M._still_open([(10, 50)], [box(0, 45)]) == []
    assert M._still_open([(10, 50)], [box(60, 70)]) == [(10, 50)]


def test_text_in_forms_is_read(tmp_path, settings):
    def draw(c):
        c.beginForm("box")
        c.setFont("Helvetica", 10)
        c.drawString(72, 700, "Inside a form")
        c.endForm()
        c.doForm("box")
    assert _md(_draw(tmp_path / "f.pdf", draw), settings)["markdown"] == "Inside a form"


def test_pages_are_read_as_displayed(tmp_path, settings):
    def sideways(c):  # drawn running up the page, displayed upright by /Rotate 90
        c.translate(300, 100)
        c.rotate(90)
        c.setFont("Helvetica", 18)
        c.drawString(0, 0, "Landscape Title")
        c.setFont("Helvetica", 10)
        for n in range(3):
            c.drawString(0, -30 - 12 * n, f"Line {n} of the landscape body text.")
    pdf = _draw(tmp_path / "r.pdf", sideways)
    with pikepdf.open(pdf, allow_overwriting_input=True) as doc:
        doc.pages[0].Rotate = 90
        doc.save(pdf)
    assert _md(pdf, settings)["markdown"].split("\n\n") == [
        "# Landscape Title",
        "Line 0 of the landscape body text. Line 1 of the landscape body text. "
        "Line 2 of the landscape body text."]


def test_tables_below_the_text_come_last(tmp_path, settings):
    def draw(c):
        c.setFont("Helvetica", 10)
        c.drawString(72, 700, "Above")
        c.grid([72, 150, 230], [300, 280, 260])
        c.drawString(80, 285, "a")
        c.drawString(160, 285, "b")
        c.drawString(80, 265, "c")
        c.drawString(160, 265, "d")
    assert _md(_draw(tmp_path / "t.pdf", draw), settings)["markdown"] == \
        "Above\n\n| a | b |\n| --- | --- |\n| c | d |"


def test_wide_text_joins_without_spaces(tmp_path, settings):
    assert M.join_lines(["東京の", "配達"]) == "東京の配達"
    assert M.join_lines(["one", "", "two"]) == "one two"
    assert M.join_lines(["-", "x"]) == "- x"


# --- escaping ---------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "escaped"), [
    ("[click](javascript:alert(1))", r"\[click\](javascript:alert(1))"),
    ("<img src=x onerror=alert(1)>", r"\<img src=x onerror=alert(1)>"),
    ("**bold** _it_ `code` ~~gone~~ a|b", r"\*\*bold\*\* \_it\_ \`code\` \~\~gone\~\~ a\|b"),
    ("&lt;b&gt; AT&T &#60;", r"\&lt;b\&gt; AT&T \&#60;"),
    ("# not a heading", r"\# not a heading"),
    ("> not a quote", r"\> not a quote"),
    ("- not an item", r"\- not an item"),
    ("+ not an item", r"\+ not an item"),
    ("12. not an item", r"12\. not an item"),
    ("3) not an item", r"3\) not an item"),
    ("=== not a heading line", r"\=== not a heading line"),
    ("#hashtag", "#hashtag"),
    ("back\\slash", r"back\\slash"),
    ("tab\there\x00and\u202edone", "tab here and done"),
])
def test_text_is_escaped(text, escaped):
    assert M.escape(text) == escaped


def test_in_headings_and_cells_only_inline_markup_is_escaped():
    assert M.escape("1. Summary", line_start=False) == "1. Summary"
    assert M.escape("C# and F#", line_start=False) == r"C# and F\#"
    assert M.table_markdown([["- a", "*b*"], ["c"]]) == \
        "| - a | \\*b\\* |\n| --- | --- |\n| c |  |"


def test_injected_markdown_in_a_pdf_stays_text(tmp_path, settings):
    pdf = _draw(tmp_path / "x.pdf", _text(
        (72, 700, "# Fake heading"), (72, 660, "[Log in](https://evil.example) <script>"),
        (72, 620, "<!-- page 9 -->"), (72, 580, "| a | b |")))
    assert _md(pdf, settings)["markdown"].split("\n\n") == [
        r"\# Fake heading", r"\[Log in\](https://evil.example) \<script>",
        r"\<!-- page 9 -->", r"\| a \| b \|"]


# --- limits and reporting ---------------------------------------------------------------


def test_max_chars_cuts_at_a_block(report, settings):
    full = _md(report, settings)["markdown"]
    res = _md(report, settings, max_chars=len("# Annual Report") + 5)
    assert res["markdown"] == "# Annual Report\n\n[truncated]" and res["truncated"] is True
    tiny = _md(report, settings, max_chars=3)
    assert tiny["markdown"] == "[truncated]" and tiny["characters"] == len("[truncated]")
    assert _md(report, settings, max_chars=len(full))["truncated"] is False


@pytest.mark.parametrize("value", [0, M.MAX_CHARS + 1, True, "10"])
def test_max_chars_is_checked(report, settings, value):
    with pytest.raises(PdfUserError, match="max_chars must be 1 to 1,000,000"):
        E.pdf_to_markdown(report, settings, max_chars=value)


@pytest.mark.parametrize("option", ["include_tables", "page_markers"])
def test_options_must_be_true_or_false(report, settings, option, tmp_path):
    with pytest.raises(PdfUserError, match=f"{option} must be true or false"):
        E.pdf_to_markdown(report, settings, **{option: "yes"})
    with pytest.raises(PdfUserError, match=f"{option} must be true or false"):
        E.export_markdown(report, tmp_path / "o.md", settings, **{option: 1})


def test_pages_without_text_are_listed(tmp_path, settings, monkeypatch):
    pdf = _draw(tmp_path / "e.pdf", _text((72, 700, "Words")), lambda c: None)
    res = E.pdf_to_markdown(pdf, settings)
    assert res["pages_without_text"] == [2]
    assert res["markdown"] == "<!-- page 1 -->\n\nWords\n\n<!-- page 2 -->"
    monkeypatch.setattr(M, "MAX_LISTED", 0)  # the list stops at its limit
    assert E.pdf_to_markdown(pdf, settings)["pages_without_text"] == []


def test_the_line_limit_stops_with_truncated(report, settings, monkeypatch):
    monkeypatch.setattr(M, "MAX_LINES", 3)
    res = _md(report, settings)
    assert res["truncated"] is True and res["complete"] is True and res["pages_read"] == 1
    # The lines read so far, and that page's tables, are kept.
    assert res["markdown"].split("\n\n")[:3] == ["# Annual Report", "# Summary",
                                                 BODY[:BODY.index(" whole")]]
    assert res["tables"] == 1


def test_the_table_limit_stops_with_truncated(report, settings, monkeypatch):
    monkeypatch.setattr(M, "MAX_TABLES", 0)
    res = _md(report, settings)
    assert res["truncated"] is True and res["tables"] == 0 and res["pages_read"] == 1


def test_heavy_pages_are_skipped(report, settings, monkeypatch):
    monkeypatch.setattr(T, "MAX_PAGE_CONTENT", 10)
    res = _md(report, settings)
    assert res["pages_skipped"] == [{"page": 1, "reason": "too much content to read safely"},
                                    {"page": 2, "reason": "too much content to read safely"}]
    assert res["markdown"] == "" and res["pages_read"] == 0 and res["complete"] is True


def test_when_time_runs_out_the_result_says_so(report, settings, monkeypatch):
    monkeypatch.setattr(M, "MARKDOWN_TIME_LIMIT_S", 0)
    res = _md(report, settings)
    assert res["complete"] is False and res["pages_read"] == 0


def test_copying_forbidden_needs_the_owner_password(make_encrypted, settings):
    locked = make_encrypted(owner="boss", allow=pikepdf.Permissions(extract=False,
                                                                    modify_assembly=True))
    with pytest.raises(PdfUserError, match="converting its text to Markdown"):
        E.pdf_to_markdown(locked, settings, password="pw")
    assert "PAGE-1" in _md(locked, settings, password="boss")["markdown"]


# --- the worker's records are checked ---------------------------------------------------


def _fake(monkeypatch, *records, finished=True):
    monkeypatch.setattr(M, "_run", lambda module, request, seconds: (list(records), finished))


GOOD = ["x", 1, 2, 3, 4, 10, False, 1]


@pytest.mark.parametrize("record", [
    {"page": 0, "lines": "x", "tables": []},
    {"page": 0, "lines": [], "tables": {}},
    {"page": 0, "lines": [GOOD[:7]], "tables": []},
    {"page": 0, "lines": [[1, *GOOD[1:]]], "tables": []},
    {"page": 0, "lines": [GOOD[:6] + ["no", 1]], "tables": []},
    {"page": 0, "lines": [GOOD[:5] + [True, False, 1]], "tables": []},
    {"page": 0, "lines": [GOOD[:7] + ["1"]], "tables": []},
    {"page": 0, "lines": [], "tables": [{"bbox": [0, 0, 1], "rows": [["a"]]}]},
    {"page": 0, "lines": [], "tables": [{"bbox": [0, 0, 1, 1], "rows": []}]},
    {"page": 0, "lines": [], "tables": [{"bbox": [0, 0, 1, 1], "rows": [[1]]}]},
    {"page": 0, "lines": [], "tables": ["x"]},
    {"page": 0, "lines": [GOOD[:1] + [float("inf")] + GOOD[2:]], "tables": []},
    {"page": 0, "lines": [GOOD[:5] + [float("nan")] + GOOD[6:]], "tables": []},
    {"page": 0, "lines": [], "tables": [{"bbox": [0, 0, 1, float("inf")], "rows": [["a"]]}]},
])
def test_malformed_records_are_errors(doc, settings, monkeypatch, record):
    _fake(monkeypatch, record)
    with pytest.raises(RuntimeError, match="markdown worker sent a malformed"):
        E.pdf_to_markdown(doc, settings)


@pytest.mark.parametrize("page", [7, 0])
def test_pages_not_asked_for_are_errors(doc, settings, monkeypatch, page):
    _fake(monkeypatch, {"page": 0, "lines": [GOOD], "tables": []},
          {"page": page, "lines": [], "tables": []})
    with pytest.raises(RuntimeError, match="a page it wasn't asked for"):
        E.pdf_to_markdown(doc, settings)


def test_a_file_the_worker_cannot_read(doc, settings, monkeypatch):
    _fake(monkeypatch, {"error": "broken"})
    with pytest.raises(PdfUserError, match="Could not read the PDF's layout: broken"):
        E.pdf_to_markdown(doc, settings)


def test_pages_after_a_worker_dies_are_still_read(doc, settings, monkeypatch):
    calls = []

    def run(module, request, seconds):
        calls.append(list(request["pages"]))
        if request["pages"][0] == 0:
            return [{"page": 0, "lines": [GOOD], "tables": []}], False
        return [{"page": i, "lines": [GOOD], "tables": []} for i in request["pages"]], True
    monkeypatch.setattr(M, "_run", run)
    res = E.pdf_to_markdown(doc, settings)
    assert calls == [[0, 1, 2, 3, 4], [2, 3, 4]]
    assert res["pages_skipped"] == [{"page": 2,
                                     "reason": "it needs too much time or memory to read"}]
    assert res["pages_read"] == 4 and res["complete"] is True


def test_pages_the_worker_skips(doc, settings, monkeypatch):
    _fake(monkeypatch, *[{"page": i, "skipped": "it can't be read: bad"} if i == 1 else
                         {"page": i, "lines": [], "tables": []} for i in range(5)])
    res = E.pdf_to_markdown(doc, settings)
    assert res["pages_skipped"] == [{"page": 2, "reason": "it can't be read: bad"}]


# --- export_markdown --------------------------------------------------------------------


def test_export_writes_the_whole_document(report, settings, tmp_path):
    out = tmp_path / "out" / "report.md"
    out.parent.mkdir()
    res = E.export_markdown(report, out, settings)
    text = out.read_text("utf-8")
    assert text == E.pdf_to_markdown(report, settings, max_chars=M.MAX_CHARS)["markdown"] + "\n"
    assert res["output"] == str(out) and res["characters"] == len(text)
    assert (res["headings"], res["tables"], res["truncated"]) == (5, 1, False)


def test_export_refuses_to_replace_unless_asked(report, settings, tmp_path):
    out = tmp_path / "r.md"
    out.write_text("keep")
    with pytest.raises(PdfUserError, match="already exists"):
        E.export_markdown(report, out, settings)
    assert out.read_text() == "keep"
    E.export_markdown(report, out, settings, overwrite=True)
    assert out.read_text().startswith("<!-- page 1 -->")


def test_export_never_writes_through_a_link(report, settings, tmp_path):
    target = tmp_path / "elsewhere.md"
    target.write_text("keep")
    (tmp_path / "r.md").symlink_to(target)
    with pytest.raises(PdfUserError, match="not a regular file"):
        E.export_markdown(report, tmp_path / "r.md", settings, overwrite=True)
    assert target.read_text() == "keep"


def test_settings_page_limit_applies(report, settings):
    with pytest.raises(PdfUserError, match="page"):
        E.pdf_to_markdown(report, dataclasses.replace(settings, max_pages=1))


# --- found in review --------------------------------------------------------------------


def test_text_at_an_impossible_size_is_left_out_not_fatal(tmp_path, settings):
    huge = "9" * 300 + ".0"  # pdfminer makes this size infinite
    pdf = pikepdf.new()
    font = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Font,
                                                Subtype=pikepdf.Name.Type1,
                                                BaseFont=pikepdf.Name.Helvetica))
    content = (f"BT /F1 {huge} Tf {huge} 0 0 {huge} 72 700 Tm (Huge) Tj ET "
               "BT /F1 12 Tf 72 600 Td (Fine) Tj ET").encode()
    pdf.add_blank_page(page_size=(612, 792))
    pdf.pages[0].Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    pdf.pages[0].Contents = pdf.make_stream(content)
    pdf.save(tmp_path / "inf.pdf")
    assert _md(tmp_path / "inf.pdf", settings)["markdown"] == "Fine"


def test_the_text_budget_stops_with_truncated(report, settings, monkeypatch):
    monkeypatch.setattr(M, "MAX_TEXT", 30)
    res = _md(report, settings)
    assert res["truncated"] is True and res["markdown"] == "# Annual Report\n\n# Summary"


def test_lettered_markers_drawn_apart(tmp_path, settings):
    pdf = _draw(tmp_path / "a.pdf", _text((60, 700, "(a)"), (90, 700, "First option"),
                                          (60, 680, "(b)"), (90, 680, "Second option")))
    assert _md(pdf, settings)["markdown"] == "- (a) First option\n- (b) Second option"


def test_markers_find_the_nearest_lines_first(monkeypatch):
    rows = [M.Line(["x", 0, top, 5, top + 10, 10, False, 1]) for top in range(0, 100, 10)]
    middles = [r.middle for r in rows]
    # Closest first; of two as close, the one above.
    assert [r.top for r in M._nearest(rows, middles, 45, 12)] == [40, 30, 50]
    monkeypatch.setattr(M, "MAX_ROW_SCAN", 2)
    assert [r.top for r in M._nearest(rows, middles, 45, 100)] == [40, 30]
    assert M._nearest([], [], 5, 10) == []


def test_running_out_of_time_on_a_page_means_incomplete(doc, settings, monkeypatch):
    import time

    def slow(module, request, seconds):
        time.sleep(seconds + 0.05)
        return [], False
    monkeypatch.setattr(M, "_run", slow)
    monkeypatch.setattr(M, "MARKDOWN_TIME_LIMIT_S", 0.2)
    res = E.pdf_to_markdown(doc, settings)
    assert res["complete"] is False and res["pages_read"] == 0
    assert res["pages_skipped"] == [{"page": 1, "reason": "it took too long to read"}]


def test_invisible_characters_are_removed():
    assert M.escape("pay​pal ‮noisrev⁦ \U000e0041\U000e0042x﻿") == \
        "paypal  noisrev  x"
    assert M.escape("क्‍ष") == "क्‍ष"  # joiners stay


def test_a_long_block_is_cut_to_fit(tmp_path, settings):
    long = " ".join(["word"] * 100)
    pdf = _draw(tmp_path / "l.pdf", _text(*[(72, 700 - 12 * n, " ".join(["word"] * 12))
                                            for n in range(9)]))
    res = _md(pdf, settings, max_chars=60)
    first = res["markdown"].split("\n\n")[0]
    assert first == " ".join(["word"] * 12) and res["truncated"] is True
    assert len(long) > 60


@pytest.mark.parametrize(("block", "room", "part"), [
    ("one two three four five six seven eight nine ten eleven twelve", 43,
     "one two three four five six seven eight"),
    ("x" * 100, 50, "x" * 50),
    ("x" * 48 + "\\[" + "y", 49, "x" * 48),
    ("- first item of the list here\n- second item of it", 40,
     "- first item of the list here\n- second"),
    ("- first item of the list goes here today\n- second item", 43,
     "- first item of the list goes here today"),
    ("1. first item of the list goes here now\n2. second item", 43,
     "1. first item of the list goes here now"),
    ("| a | b |\n| --- | --- |", 100, None),
    ("<!-- page 3 -->", 100, None),
    ("short words but no room", 30, None),
])
def test_cutting_a_block(block, room, part):
    assert M._part(block, room) == part
