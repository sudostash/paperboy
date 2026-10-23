"""compare_pdfs: pages matched, then compared by text and by how they look."""
from __future__ import annotations

import io

import pikepdf
import pytest
from PIL import Image
from reportlab.pdfgen import canvas

from paperboy import engine as E
from paperboy.engine import compare as C
from paperboy.errors import PdfUserError


def _pdf(path, pages: list[list[str]], boxes: dict | None = None):
    """One list of lines per page; *boxes* maps a page index to a filled square's x."""
    c = canvas.Canvas(str(path))
    for n, lines in enumerate(pages):
        for k, line in enumerate(lines):
            c.drawString(72, 720 - 20 * k, line)
        if boxes and n in boxes:
            c.rect(boxes[n], 300, 80, 80, fill=1)
        c.showPage()
    c.save()
    return path


INTRO = ["Delivery report", "Prepared for the board"]
REGIONS = ["North 12,400", "South 10,950", "East 8,210"]
PLANS = ["Next quarter", "Electric vans on three routes"]


@pytest.fixture
def original(tmp_path):
    return _pdf(tmp_path / "a.pdf", [INTRO, REGIONS, PLANS])


def _status(result):
    return [(d["status"], d["page_a"], d["page_b"]) for d in result["differences"]]


def test_identical_pdfs(original, settings, tmp_path):
    copy = _pdf(tmp_path / "b.pdf", [INTRO, REGIONS, PLANS])
    res, images = E.compare_pdfs(original, copy, settings)
    assert res["identical"] is True and res["same"] == 3 and res["differences"] == []
    assert images == [] and res["complete"] is True and res["mode"] == "text"


def test_a_changed_line(original, settings, tmp_path):
    edited = _pdf(tmp_path / "b.pdf", [INTRO, ["North 12,400", "South 11,020", "East 8,210"],
                                       PLANS])
    res, _ = E.compare_pdfs(original, edited, settings)
    (diff,) = res["differences"]
    assert (diff["status"], diff["page_a"], diff["page_b"]) == ("changed", 2, 2)
    assert diff["removed"] == ["South 10,950"] and diff["added"] == ["South 11,020"]
    assert 0 < diff["similarity"] < 1 and res["identical"] is False
    assert (res["same"], res["changed"]) == (2, 1)


def test_an_inserted_page_does_not_shift_the_rest(original, settings, tmp_path):
    edited = _pdf(tmp_path / "b.pdf", [INTRO, ["New: summary"], REGIONS, PLANS])
    res, _ = E.compare_pdfs(original, edited, settings)
    assert _status(res) == [("added", None, 2)]
    assert (res["same"], res["added"]) == (3, 1)


def test_a_deleted_page(original, settings, tmp_path):
    edited = _pdf(tmp_path / "b.pdf", [INTRO, PLANS])
    res, _ = E.compare_pdfs(original, edited, settings)
    assert _status(res) == [("removed", 2, None)]


def test_a_replaced_page_and_extra_pages(original, settings, tmp_path):
    edited = _pdf(tmp_path / "b.pdf", [INTRO, ["Totally", "different"], ["More"], PLANS])
    res, _ = E.compare_pdfs(original, edited, settings)
    assert _status(res) == [("changed", 2, 2), ("added", None, 3)]


def test_whitespace_and_case(original, settings, tmp_path):
    spaced = _pdf(tmp_path / "b.pdf", [["Delivery   report", "Prepared for the board"],
                                       REGIONS, PLANS])
    assert E.compare_pdfs(original, spaced, settings)[0]["identical"] is True
    shouty = _pdf(tmp_path / "c.pdf", [[line.upper() for line in INTRO], REGIONS, PLANS])
    assert E.compare_pdfs(original, shouty, settings)[0]["changed"] == 1
    assert E.compare_pdfs(original, shouty, settings, ignore_case=True)[0]["identical"]


def test_long_diffs_are_capped(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(C, "MAX_DIFF_LINES", 2)
    a = _pdf(tmp_path / "a.pdf", [[f"line {n}" for n in range(5)]])
    b = _pdf(tmp_path / "b.pdf", [[f"row {n}" for n in range(5)]])
    (diff,) = E.compare_pdfs(a, b, settings)[0]["differences"]
    assert len(diff["added"]) == 2 and diff["lines_truncated"] is True


def test_pages_without_text_are_unknown_in_text_mode(tmp_path, settings):
    a = _pdf(tmp_path / "a.pdf", [[]], boxes={0: 100})
    b = _pdf(tmp_path / "b.pdf", [[]], boxes={0: 300})
    res, _ = E.compare_pdfs(a, b, settings)
    assert _status(res) == [("unknown", 1, 1)] and res["identical"] is False
    res, images = E.compare_pdfs(a, b, settings, mode="visual")
    assert _status(res) == [("changed", 1, 1)] and res["differences"][0]["pixel_change"] > 0
    assert len(images) == 1 and res["images"][0]["page_a"] == 1


def test_visual_differences_show_removed_in_red_and_added_in_green(tmp_path, settings):
    a = _pdf(tmp_path / "a.pdf", [["Same text"]], boxes={0: 100})
    b = _pdf(tmp_path / "b.pdf", [["Same text"]], boxes={0: 400})
    res, (png,) = E.compare_pdfs(a, b, settings, mode="both")
    (diff,) = res["differences"]
    assert diff["status"] == "changed" and diff["similarity"] is None   # text is the same
    assert 0 < diff["pixel_change"] < 10
    picture = Image.open(io.BytesIO(png)).convert("RGB")
    w, h = picture.size
    scale = w / 612
    old = picture.getpixel((int(140 * scale), int(h - 340 * scale)))
    new = picture.getpixel((int(440 * scale), int(h - 340 * scale)))
    assert old[0] > 180 and old[1] < 80        # red: was in A
    assert new[1] > 120 and new[0] < 80        # green: added in B


def test_visually_identical_pages_are_the_same(original, settings, tmp_path):
    copy = _pdf(tmp_path / "b.pdf", [INTRO, REGIONS, PLANS])
    res, images = E.compare_pdfs(original, copy, settings, mode="visual")
    assert res["identical"] is True and images == []


def test_visual_mode_matches_pages_by_position(original, settings, tmp_path):
    longer = _pdf(tmp_path / "b.pdf", [INTRO, REGIONS, PLANS, ["Appendix"]])
    res, _ = E.compare_pdfs(original, longer, settings, mode="visual")
    assert _status(res) == [("added", None, 4)]
    res, _ = E.compare_pdfs(longer, original, settings, mode="visual")
    assert _status(res) == [("removed", 4, None)]


def test_different_page_sizes_are_compared(tmp_path, settings):
    a = _pdf(tmp_path / "a.pdf", [["x"]])
    b = tmp_path / "b.pdf"
    c = canvas.Canvas(str(b), pagesize=(400, 400))
    c.drawString(72, 300, "x")
    c.save()
    res, _ = E.compare_pdfs(a, b, settings, mode="visual")
    assert res["changed"] == 1


def test_only_the_most_changed_pages_come_back_as_images(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(C, "MAX_IMAGES", 1)
    a = _pdf(tmp_path / "a.pdf", [["p1"], ["p2"]], boxes={0: 100, 1: 100})
    b = _pdf(tmp_path / "b.pdf", [["p1"], ["p2"]], boxes={0: 110, 1: 400})
    res, images = E.compare_pdfs(a, b, settings, mode="visual")
    assert len(images) == 1 and res["images"][0]["page_a"] == 2


def test_heavy_pages_are_skipped(original, settings, tmp_path, monkeypatch):
    monkeypatch.setattr(C, "MAX_PAGE_CONTENT", 60)
    copy = _pdf(tmp_path / "b.pdf", [INTRO, REGIONS, PLANS])
    res, _ = E.compare_pdfs(original, copy, settings)
    skipped = {(s["document"], s["page"]) for s in res["pages_skipped"]}
    assert ("a", 2) in skipped and ("b", 2) in skipped
    assert res["unknown"] >= 1 and res["identical"] is False


def test_the_time_limit(original, settings, tmp_path, monkeypatch):
    monkeypatch.setattr(C, "COMPARE_TIME_LIMIT_S", -1)
    copy = _pdf(tmp_path / "b.pdf", [INTRO, REGIONS, PLANS])
    res, _ = E.compare_pdfs(original, copy, settings)
    assert res["complete"] is False and res["unknown"] == 3


def test_visual_render_limit(original, settings, tmp_path, monkeypatch):
    monkeypatch.setattr(C, "MAX_VISUAL_PAIRS", 1)
    copy = _pdf(tmp_path / "b.pdf", [INTRO, REGIONS, PLANS])
    res, _ = E.compare_pdfs(original, copy, settings, mode="visual")
    assert res["complete"] is False and (res["same"], res["unknown"]) == (1, 2)


def test_many_differences_are_capped(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(C, "MAX_DIFFERENCES", 1)
    a = _pdf(tmp_path / "a.pdf", [["a1"], ["a2"]])
    b = _pdf(tmp_path / "b.pdf", [["b1"], ["b2"]])
    res, _ = E.compare_pdfs(a, b, settings)
    assert len(res["differences"]) == 1 and res["truncated"] is True


def test_long_lines_and_many_lines_are_cut_but_still_compared(monkeypatch):
    monkeypatch.setattr(C, "MAX_LINE", 5)
    monkeypatch.setattr(C, "MAX_LINES", 1)
    page = C._read("abcdefgh\n\n  second  line ", ignore_case=False)
    assert page.keys == ["abcde"] and page.cut is True
    other = C._read("abcdefgh\nsecond line changed", ignore_case=False)
    assert other.keys == page.keys and other.digest != page.digest   # all of it counts


def test_bad_mode(original, settings):
    with pytest.raises(PdfUserError, match="mode must be one of"):
        E.compare_pdfs(original, original, settings, mode="pixels")


def test_protected_pdfs(original, settings, tmp_path):
    locked = tmp_path / "locked.pdf"
    with pikepdf.open(original) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(
            user="pw", owner="owner", R=6, allow=pikepdf.Permissions(extract=False)))
    with pytest.raises(PdfUserError, match="supply its password"):
        E.compare_pdfs(original, locked, settings)
    with pytest.raises(PdfUserError, match="does not allow copying.*comparing its text"):
        E.compare_pdfs(original, locked, settings, password_b="pw")
    assert E.compare_pdfs(original, locked, settings, password_b="owner")[0]["identical"]
    # Looking is always allowed: visual mode only needs the open password.
    assert E.compare_pdfs(original, locked, settings, mode="visual",
                          password_b="pw")[0]["identical"]


def test_a_page_pdfium_cannot_read(original, settings, monkeypatch):
    import pypdfium2 as pdfium

    def broken(self, index):
        raise pdfium.PdfiumError("bad page")
    monkeypatch.setattr(pdfium.PdfDocument, "__getitem__", broken)
    with pytest.raises(PdfUserError, match="Could not read page 1"):
        E.compare_pdfs(original, original, settings)



# --- review findings ---------------------------------------------------------------------------


def _pages(texts):
    return [C._read(t, ignore_case=False) for t in texts]


def test_matching_repeated_pages_stays_fast():
    # SequenceMatcher took about 7 s at 1000 pages, and grows with the cube.
    import time
    a = _pages(["same page"] * 2000)
    b = _pages(["same page", "other page"] * 1000)
    start = time.monotonic()
    pairs = C._match(a, b, C._Clock(60))
    assert time.monotonic() - start < 3
    assert len([p for p in pairs if None not in p]) == 2000


def test_matching_stops_at_the_deadline():
    a = _pages([f"page {n} alpha beta" for n in range(300)])
    b = _pages([f"page {n} alpha beta gamma" for n in range(300)])
    pairs = C._match(a, b, C._Clock(-1))           # out of time: matched by position
    assert pairs == [(n, n) for n in range(300)]


def test_matching_past_its_budget_falls_back_to_position(monkeypatch):
    monkeypatch.setattr(C, "MATCH_CELLS", 3)
    a = _pages(["x one", "y two"])
    b = _pages(["new", "x one!", "y two!"])
    assert C._match(a, b, C._Clock(60)) == [(0, 0), (1, 1), (None, 2)]


def test_blank_pages_do_not_anchor_matches():
    a = _pages(["Intro", "", "Appendix v1"])
    b = _pages(["Intro", "Appendix v2", ""])
    pairs = C._match(a, b, C._Clock(60))
    assert pairs == [(0, 0), (1, None), (2, 1), (None, 2)]   # the blank page moved


def test_an_inserted_page_with_page_number_footers(tmp_path, settings):
    def doc(path, bodies):
        n = len(bodies)
        return _pdf(path, [[body, f"Clause text for {body}", f"Page {k + 1} of {n}"]
                           for k, body in enumerate(bodies)])
    a = doc(tmp_path / "a.pdf", ["Clause 1", "Clause 2", "Clause 3", "Clause 4"])
    b = doc(tmp_path / "b.pdf", ["Clause 1", "NEW clause", "Clause 2", "Clause 3",
                                 "Clause 4"])
    res, _ = E.compare_pdfs(a, b, settings)
    pairs = {(d["page_a"], d["page_b"]): d for d in res["differences"]}
    assert pairs[(None, 2)]["status"] == "added"
    for k in range(2, 5):                          # every later page: only the footer moved
        diff = pairs[(k, k + 1)]
        assert diff["removed"] == [f"Page {k} of 4"] and diff["added"] == [f"Page {k + 1} of 5"]


def test_line_differences_stay_fast_on_repeated_lines():
    import time
    a = C._read("\n".join(["alpha line"] * 2000), ignore_case=False)
    b = C._read("\n".join(["alpha line", "beta line"] * 1000), ignore_case=False)
    start = time.monotonic()
    removed, added, alike = C._line_changes(a, b)
    assert time.monotonic() - start < 1
    assert removed == ["alpha line"] * 1000 and added == ["beta line"] * 1000
    assert alike == 0.5


def test_the_deadline_covers_measuring_pages(original, settings, tmp_path, monkeypatch):
    monkeypatch.setattr(C, "COMPARE_TIME_LIMIT_S", -1)
    copy = _pdf(tmp_path / "b.pdf", [INTRO, REGIONS, PLANS])
    res, _ = E.compare_pdfs(original, copy, settings, mode="visual")
    assert res["complete"] is False and res["unknown"] == 3 and res["identical"] is False


def test_the_deadline_covers_line_differences(original, settings, tmp_path, monkeypatch):
    edited = _pdf(tmp_path / "b.pdf", [INTRO, ["changed"], PLANS])
    late = {"now": False}
    monkeypatch.setattr(C._Clock, "out", property(lambda self: late["now"]))
    real = C._match

    def match_then_run_out(a, b, clock):
        pairs = real(a, b, clock)
        late["now"] = True                         # time runs out after matching
        return pairs
    monkeypatch.setattr(C, "_match", match_then_run_out)
    res, _ = E.compare_pdfs(original, edited, settings)
    assert res["complete"] is False and (res["same"], res["unknown"]) == (2, 1)


def test_listed_lines_share_one_size_budget(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(C, "MAX_LISTED_CHARS", 12)
    a = _pdf(tmp_path / "a.pdf", [["aaaaaaaaaa", "bbbbbbbbbb"]])
    b = _pdf(tmp_path / "b.pdf", [["cccccccccc", "dddddddddd"]])
    res, _ = E.compare_pdfs(a, b, settings)
    (diff,) = res["differences"]
    assert diff["removed"] == ["aaaaaaaaaa"] and diff["added"] == []
    assert diff["lines_truncated"] is True and res["truncated"] is True


def test_a_change_past_the_shown_part_of_a_line_counts(tmp_path, settings):
    a = _pdf(tmp_path / "a.pdf", [["x" * 520 + " PAY 100"]])
    b = _pdf(tmp_path / "b.pdf", [["x" * 520 + " PAY 900"]])
    res, _ = E.compare_pdfs(a, b, settings)
    (diff,) = res["differences"]
    assert diff["status"] == "changed" and diff["lines_truncated"] is True
    assert res["identical"] is False


def _coloured(path, rgb, highlight=False):
    c = canvas.Canvas(str(path))
    if highlight:
        c.setFillColorRGB(1, 1, 0.4)
        c.rect(70, 715, 200, 18, stroke=0, fill=1)
    c.setFillColorRGB(*rgb)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(72, 720, "Total due: 900")
    c.save()
    return path


def test_colour_only_changes_are_seen(tmp_path, settings):
    red = _coloured(tmp_path / "a.pdf", (1, 0, 0))
    green = _coloured(tmp_path / "b.pdf", (0, 0.5, 0))
    marked = _coloured(tmp_path / "c.pdf", (1, 0, 0), highlight=True)
    for other in (green, marked):
        res, images = E.compare_pdfs(red, other, settings, mode="visual")
        assert res["identical"] is False and res["differences"][0]["pixel_change"] > 0
        assert len(images) == 1


def test_case_folding_and_ligatures(tmp_path, settings):
    a = _pdf(tmp_path / "a.pdf", [["Strasse fine", "Total: ACME Corp"]])
    b = _pdf(tmp_path / "b.pdf", [["STRASSE fine", "Total: ACME Corp."]])
    res, _ = E.compare_pdfs(a, b, settings, ignore_case=True)
    (diff,) = res["differences"]
    assert diff["removed"] == ["Total: ACME Corp"]          # shown as written
    assert diff["added"] == ["Total: ACME Corp."]
    assert C._read("\ufb01le", ignore_case=False).keys == ["file"]   # NFKC
    assert C._read("STRASSE", True).digest == C._read("stra\u00dfe", True).digest


def test_a_blank_page_against_a_page_with_text(tmp_path, settings):
    a = _pdf(tmp_path / "a.pdf", [["Intro"], []])
    b = _pdf(tmp_path / "b.pdf", [["Intro"], ["Now with text"]])
    (diff,) = E.compare_pdfs(a, b, settings)[0]["differences"]
    assert diff["removed"] == [] and diff["added"] == ["Now with text"]


def test_pages_of_different_sizes_are_padded_not_stretched():
    a = Image.new("RGB", (100, 100), "white")
    b = Image.new("RGB", (100, 140), "white")
    share, picture = C._visual(a, b)
    assert share == 0 and picture.size == (100, 140)


def test_a_pdf_without_pages(tmp_path, settings, original):
    empty = tmp_path / "empty.pdf"
    with pikepdf.new() as pdf:
        pdf.save(empty)
    with pytest.raises(PdfUserError, match="empty.pdf has no pages"):
        E.compare_pdfs(original, empty, settings)


def test_unencrypted_files_reach_pdfium_unchanged(original, settings, monkeypatch):
    seen = []
    real = C._open
    monkeypatch.setattr(C, "_open", lambda data: seen.append(data) or real(data))
    E.compare_pdfs(original, original, settings)
    assert seen[0] == original.read_bytes()


def test_copy_restrictions_apply_to_both_files(original, settings, tmp_path):
    locked = tmp_path / "locked.pdf"
    with pikepdf.open(original) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(
            user="pw", owner="owner", R=6, allow=pikepdf.Permissions(extract=False)))
    for kwargs in ({"password_a": "pw"}, {}):
        a, b = (locked, original) if kwargs else (original, locked)
        with pytest.raises(PdfUserError, match="does not allow copying"):
            E.compare_pdfs(a, b, settings, mode="both",
                           password_a="pw" if kwargs else None,
                           password_b=None if kwargs else "pw")


def test_only_the_most_changed_pictures_are_kept(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(C, "MAX_IMAGES", 2)
    a = _pdf(tmp_path / "a.pdf", [["p"]] * 4, boxes={0: 100, 1: 100, 2: 100, 3: 100})
    b = _pdf(tmp_path / "b.pdf", [["p"]] * 4, boxes={0: 101, 1: 400, 2: 104, 3: 300})
    res, images = E.compare_pdfs(a, b, settings, mode="visual")
    assert [x["page_a"] for x in res["images"]] in ([2, 4], [4, 2]) and len(images) == 2
    assert res["images"][0]["pixel_change"] >= res["images"][1]["pixel_change"]


def test_the_deadline_covers_reading_text(original, settings, tmp_path, monkeypatch):
    copy = _pdf(tmp_path / "b.pdf", [INTRO, REGIONS, PLANS])
    late = {"now": False}
    monkeypatch.setattr(C._Clock, "out", property(lambda self: late["now"]))
    real = C._open

    def open_then_run_out(data):
        late["now"] = True                         # time runs out once pages are measured
        return real(data)
    monkeypatch.setattr(C, "_open", open_then_run_out)
    res, _ = E.compare_pdfs(original, copy, settings)
    assert res["complete"] is False and res["unknown"] == 3
