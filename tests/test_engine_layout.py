"""Page layout: crop, resize, N-up, blank pages, interleave. Checked by rendering."""
from __future__ import annotations

import pikepdf
import pypdfium2 as pdfium
import pytest
from PIL import Image, ImageChops, ImageStat
from reportlab.lib.colors import Color
from reportlab.pdfgen import canvas

from paperboy import engine as E
from paperboy.engine import layout
from paperboy.errors import PdfUserError

from conftest import page_labels

COLORS = [(1, 0, 0), (0, 0.6, 0), (0, 0, 1), (0.9, 0.6, 0)]


def _marked(path, pages=1, size=(400, 600)):
    """Pages with a distinct pattern: a coloured band at the top and a black square at the
    bottom left, plus the page number, so any crop, scale or turn shows."""
    c = canvas.Canvas(str(path), pagesize=size)
    w, h = size
    for n in range(pages):
        c.setFillColor(Color(*COLORS[n % 4]))
        c.rect(0, h - 60, w, 60, stroke=0, fill=1)
        c.setFillColor(Color(0, 0, 0))
        c.rect(20, 20, 80, 80, stroke=0, fill=1)
        c.setFont("Helvetica", 40)
        c.drawString(w / 2 - 40, h / 2, f"PAGE-{n + 1}")
        c.showPage()
    c.save()
    return path


def _render(path, index=0, password=None) -> Image.Image:
    doc = pdfium.PdfDocument(str(path), password=password)
    try:
        return doc[index].render(scale=1).to_pil().convert("RGB")
    finally:
        doc.close()


def _close_to(a: Image.Image, b: Image.Image, tolerance=6.0) -> bool:
    """Same size and nearly the same pixels (anti-aliasing differs at edges)."""
    if a.size != b.size:
        return False
    return max(ImageStat.Stat(ImageChops.difference(a, b)).mean) < tolerance


def _rotate(path, degrees):
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        for page in pdf.pages:
            page.obj.Rotate = degrees
        pdf.save(path)
    return path


@pytest.fixture
def marked(tmp_path):
    return _marked(tmp_path / "marked.pdf", pages=4)


@pytest.fixture
def out(tmp_path):
    return tmp_path / "out.pdf"


# --- crop ----------------------------------------------------------------------------------


@pytest.mark.parametrize("degrees", [0, 90, 180, 270])
def test_margins_are_measured_on_the_page_as_seen(tmp_path, settings, out, degrees):
    src = _rotate(_marked(tmp_path / "m.pdf"), degrees)
    before = _render(src)
    top, right, bottom, left = 50, 30, 70, 10
    res = E.crop_pages(src, out, settings, [top, right, bottom, left])
    w, h = before.size
    assert res["sizes"] == [[w - left - right, h - top - bottom]]
    expected = before.crop((left, top, w - right, h - bottom))
    assert _close_to(_render(out), expected)


def test_crop_units_pages_and_reset(marked, settings, out, tmp_path):
    res = E.crop_pages(marked, out, settings, [1, 0, 0, 0], unit="in", pages="2")
    assert res["pages"] == [2] and res["sizes"] == [[400, 528]]
    again = tmp_path / "again.pdf"
    E.crop_pages(out, again, settings, [0, 0, 10, 0], unit="mm")     # crops the crop
    with pikepdf.open(again) as pdf:
        assert [round(float(v), 2) for v in pdf.pages[1].obj.CropBox] == [0, 28.35, 400, 528]
    reset = tmp_path / "reset.pdf"
    res = E.crop_pages(again, reset, settings, reset=True)
    assert res["sizes"] == [[400, 600]] * 4
    with pikepdf.open(reset) as pdf:
        assert all("/CropBox" not in p.obj for p in pdf.pages)


def test_cropped_content_is_hidden_not_removed(marked, settings, out):
    E.crop_pages(marked, out, settings, [0, 0, 300, 0])
    assert page_labels(out)[0] == "PAGE-1"      # the text is still in the file


@pytest.mark.parametrize(("kwargs", "message"), [
    ({}, "Give margins"),
    ({"margins": [1, 2, 3, 4], "reset": True}, "Give margins"),
    ({"margins": [1, 2, 3]}, "4 numbers"),
    ({"margins": "10"}, "4 numbers"),
    ({"margins": [-1, 0, 0, 0]}, "top must be"),
    ({"margins": [0, 0, 0, "x"]}, "left must be"),
    ({"margins": [0, 0, 0, float("nan")]}, "left must be"),
    ({"margins": [0, 0, 0, True]}, "left must be"),
    ({"margins": [0, 0, 0, 0], "unit": "cm"}, "unit must be"),
    ({"margins": [300, 0, 290, 0]}, "leave less than 18 pt of page 1"),
])
def test_bad_crops_are_refused(marked, settings, out, kwargs, message):
    with pytest.raises(PdfUserError, match=message):
        E.crop_pages(marked, out, settings, **kwargs)
    assert not out.exists()


# --- resize ----------------------------------------------------------------------------------


@pytest.mark.parametrize("degrees", [0, 90])
def test_content_is_scaled_to_fit_and_centred(tmp_path, settings, out, degrees):
    src = _rotate(_marked(tmp_path / "m.pdf", size=(300, 300)), degrees)
    before = _render(src)
    res = E.resize_pages(src, out, settings, width=600, height=400)
    assert res["scales"] == [pytest.approx(400 / 300, abs=1e-3)]
    after = _render(out)
    assert after.size == (400, 600)                      # square page: portrait sheet
    expected = Image.new("RGB", (400, 600), "white")
    expected.paste(before.resize((400, 400)), (0, 100))
    assert _close_to(after, expected, tolerance=10)


def test_landscape_pages_get_landscape_sheets(tmp_path, settings, out):
    src = _marked(tmp_path / "wide.pdf", size=(800, 500))
    E.resize_pages(src, out, settings, size="A4")
    with pikepdf.open(out) as pdf:
        assert [float(v) for v in pdf.pages[0].mediabox] == [0, 0, 841.89, 595.28]
    portrait = tmp_path / "p.pdf"
    res = E.resize_pages(src, portrait, settings, size="letter", orientation="portrait")
    assert _render(portrait).size == (612, 792) and res["width"] == 612


def test_hidden_margins_stay_hidden(tmp_path, settings, out):
    src = _marked(tmp_path / "m.pdf")
    cropped = tmp_path / "c.pdf"
    E.crop_pages(src, cropped, settings, [0, 0, 300, 0])      # hide the bottom half
    before = _render(cropped)
    E.resize_pages(cropped, out, settings, width=400, height=300)
    assert _close_to(_render(out), before, tolerance=10)      # same size: nothing new shows


def test_links_and_comments_move_with_the_content(tmp_path, settings, out):
    src = _marked(tmp_path / "m.pdf", size=(300, 300))
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        pdf.pages[0].obj.Annots = pdf.make_indirect(pikepdf.Array([
            pikepdf.Dictionary(Subtype=pikepdf.Name.Link, Rect=[20, 20, 100, 100],
                               QuadPoints=[20, 100, 100, 100, 20, 20, 100, 20]),
            pikepdf.Dictionary(Subtype=pikepdf.Name.Ink, Rect=[0, 0, 10, 10],
                               InkList=[[0, 0, 10, 10], 5]),
            pikepdf.Dictionary(Subtype=pikepdf.Name.Line, Rect=[0, 0, 10, 10],
                               L=[0, 0, 10, 10], Vertices=[1, 2, 3]),   # odd: left alone
            5]))
        pdf.save(src)
    E.resize_pages(src, out, settings, width=600, height=900)   # scale 2, 150 down
    with pikepdf.open(out) as pdf:
        link, ink, line, _ = pdf.pages[0].obj.Annots
        assert [float(v) for v in link.Rect] == [40, 190, 200, 350]
        assert [float(v) for v in link.QuadPoints] == [40, 350, 200, 350, 40, 190, 200, 190]
        assert [[float(v) for v in p] for p in ink.InkList] == [[0, 150, 20, 170]]
        assert [float(v) for v in line.L] == [0, 150, 20, 170]
        assert [float(v) for v in line.Vertices] == [1, 2, 3]


def test_page_boxes_are_replaced(tmp_path, settings, out):
    src = _marked(tmp_path / "m.pdf")
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        pdf.pages[0].obj.TrimBox = pikepdf.Array([10, 10, 390, 590])
        pdf.save(src)
    E.resize_pages(src, out, settings, size="a5")
    with pikepdf.open(out) as pdf:
        page = pdf.pages[0].obj
        assert "/TrimBox" not in page and [float(v) for v in page.MediaBox] == [0, 0,
                                                                                419.53, 595.28]


def test_resize_only_the_pages_given(marked, settings, out):
    res = E.resize_pages(marked, out, settings, size="a4", pages="1,3")
    assert res["pages"] == [1, 3]
    with pikepdf.open(out) as pdf:
        assert [float(pdf.pages[i].mediabox[2]) for i in range(4)] == [595.28, 400, 595.28,
                                                                        400]


@pytest.mark.parametrize(("kwargs", "message"), [
    ({}, "Give a paper size"),
    ({"size": "a4", "width": 100}, "not both"),
    ({"size": "b5"}, "size must be one of"),
    ({"size": 4}, "size must be one of"),
    ({"width": 100}, "Give a paper size"),
    ({"width": 1, "height": 100}, "width must be"),
    ({"width": 100, "height": 1e9}, "height must be"),
    ({"width": 10, "height": 10, "unit": "in"}, None),
    ({"size": "a4", "orientation": "sideways"}, "orientation must be"),
])
def test_bad_sizes_are_refused(marked, settings, out, kwargs, message):
    if message is None:
        E.resize_pages(marked, out, settings, **kwargs)
        assert _render(out).size == (720, 720)
        return
    with pytest.raises(PdfUserError, match=message):
        E.resize_pages(marked, out, settings, **kwargs)


# --- N-up --------------------------------------------------------------------------------------


def _cell(img: Image.Image, box) -> Image.Image:
    return img.crop(tuple(int(v) for v in box))


def _band(img: Image.Image, cell) -> tuple[int, int, int]:
    """The colour just inside the top of a cell: where each page's band is."""
    x0, y0, x1, _ = cell
    return img.getpixel((int((x0 + x1) / 2), int(y0) + 8))


def test_four_pages_in_reading_order(marked, settings, out):
    res = E.n_up(marked, out, settings, per_sheet=4)
    assert (res["sheets"], res["pages_placed"]) == (1, 4)
    sheet = _render(out)
    assert sheet.size == (400, 600)                      # the first page's size
    w, h = sheet.size
    cw, ch = (w - 36 - 12) / 2, (h - 36 - 12) / 2
    # Each cell is 182 x 270 for a 400 x 600 page: scaled to 182/400 wide, centred.
    scaled_h = 600 * cw / 400
    pad = (ch - scaled_h) / 2
    cells = [(18 + c * (cw + 12), 18 + r * (ch + 12) + pad, 18 + c * (cw + 12) + cw,
              18 + r * (ch + 12) + pad + scaled_h) for r in range(2) for c in range(2)]
    expected = [tuple(round(v * 255) for v in color) for color in COLORS]
    for cell, color in zip(cells, expected, strict=True):
        assert sum(abs(a - b) for a, b in zip(_band(sheet, cell), color, strict=True)) < 30


def test_two_up_uses_a_landscape_sheet_and_page_order(marked, settings, out):
    res = E.n_up(marked, out, settings, per_sheet=2, size="letter", pages="4,3,1")
    assert (res["sheets"], res["sheet_width"], res["sheet_height"]) == (2, 792, 612)
    first = _render(out)
    # Cells are 372 x 576; a 400 x 600 page scales to 372 x 558, 9 pt down from the top.
    left, right = _band(first, (18, 27, 390, 585)), _band(first, (402, 27, 774, 585))
    orange, blue = (230, 153, 0), (0, 0, 255)
    assert sum(abs(a - b) for a, b in zip(left, orange, strict=True)) < 30     # page 4
    assert sum(abs(a - b) for a, b in zip(right, blue, strict=True)) < 30      # page 3
    assert page_labels(out)[1] == "PAGE-1"


def test_rotated_pages_are_placed_as_seen(tmp_path, settings, out):
    src = _rotate(_marked(tmp_path / "m.pdf", pages=2), 90)
    E.n_up(src, out, settings, per_sheet=2)
    sheet = _render(out)
    single = _render(src)                                  # 600 x 400 as seen
    w, h = sheet.size
    cw = (w - 36 - 12) / 2
    expected = single.resize((int(cw), int(cw * 400 / 600)))
    ch = (h - 36)
    top = 18 + (ch - expected.size[1]) / 2
    got = _cell(sheet, (18, top, 18 + expected.size[0], top + expected.size[1]))
    assert _close_to(got, expected, tolerance=15)


@pytest.mark.parametrize(("kwargs", "message"), [
    ({"per_sheet": 3}, "per_sheet must be 2 or 4"),
    ({"per_sheet": True}, "per_sheet must be 2 or 4"),
    ({"size": "b5"}, "size must be one of"),
])
def test_bad_n_up_arguments(marked, settings, out, kwargs, message):
    with pytest.raises(PdfUserError, match=message):
        E.n_up(marked, out, settings, **kwargs)


# --- blank pages -------------------------------------------------------------------------------


def test_blank_pages_go_where_asked(marked, settings, out):
    res = E.insert_blank_pages(marked, out, settings, after=[0, 2, 4])
    assert res["pages"] == 7 and res["inserted"] == [1, 4, 7]
    assert page_labels(out) == ["", "PAGE-1", "PAGE-2", "", "PAGE-3", "PAGE-4", ""]


def test_several_blank_pages_and_their_size(tmp_path, settings, out):
    src = _rotate(_marked(tmp_path / "m.pdf", pages=2), 90)
    res = E.insert_blank_pages(src, out, settings, after=[1], count=2)
    assert res["inserted"] == [2, 3]
    with pikepdf.open(out) as pdf:
        blank = pdf.pages[1]
        assert [float(v) for v in blank.mediabox] == [0, 0, 600, 400]   # as page 1 is seen
        assert "/Rotate" not in blank.obj


@pytest.mark.parametrize(("after", "count", "message"), [
    ([], 1, "after must list"),
    ("3", 1, "after must list"),
    ([5], 1, "from 0 to 4"),
    ([-1], 1, "from 0 to 4"),
    ([True], 1, "from 0 to 4"),
    ([1], 0, "count must be 1 to 100"),
    ([1], 101, "count must be 1 to 100"),
    ([1] * 11, 100, "at most 1000"),
])
def test_bad_blank_page_requests(marked, settings, out, after, count, message):
    with pytest.raises(PdfUserError, match=message):
        E.insert_blank_pages(marked, out, settings, after=after, count=count)


def test_blank_pages_respect_the_page_cap(marked, settings, out):
    import dataclasses
    with pytest.raises(PdfUserError, match="5 pages in all"):
        E.insert_blank_pages(marked, out, dataclasses.replace(settings, max_pages=5),
                             after=[1, 2])


# --- interleave --------------------------------------------------------------------------------


@pytest.fixture
def scans(make_pdf, tmp_path):
    fronts = make_pdf("fronts.pdf", 3)            # PAGE-1..3
    backs = tmp_path / "backs.pdf"
    c = canvas.Canvas(str(backs))
    for label in ("BACK-3", "BACK-2", "BACK-1"):  # scanned with the stack turned over
        c.drawString(72, 720, label)
        c.showPage()
    c.save()
    return fronts, backs


def test_fronts_and_reversed_backs(scans, settings, out):
    res = E.interleave_pdfs(*scans, out, settings)
    assert (res["pages"], res["fronts"], res["backs"]) == (6, 3, 3)
    assert page_labels(out) == ["PAGE-1", "BACK-1", "PAGE-2", "BACK-2", "PAGE-3", "BACK-3"]


def test_backs_in_order(scans, settings, out):
    E.interleave_pdfs(*scans, out, settings, reverse_backs=False)
    assert page_labels(out)[:2] == ["PAGE-1", "BACK-3"]


def test_one_fewer_back(make_pdf, settings, out, tmp_path):
    fronts, backs = make_pdf("f.pdf", 3), make_pdf("b.pdf", 2)
    res = E.interleave_pdfs(fronts, backs, out, settings, reverse_backs=False)
    assert res["pages"] == 5
    assert page_labels(out) == ["PAGE-1", "PAGE-1", "PAGE-2", "PAGE-2", "PAGE-3"]


def test_mismatched_scans_are_refused(make_pdf, settings, out):
    with pytest.raises(PdfUserError, match="as many pages, or one fewer"):
        E.interleave_pdfs(make_pdf("f.pdf", 3), make_pdf("b.pdf", 1), out, settings)
    with pytest.raises(PdfUserError, match="as many pages, or one fewer"):
        E.interleave_pdfs(make_pdf("f2.pdf", 2), make_pdf("b2.pdf", 3), out, settings)


def test_interleaving_respects_the_page_cap(make_pdf, settings, out):
    import dataclasses
    with pytest.raises(PdfUserError, match="page limit"):
        E.interleave_pdfs(make_pdf("f.pdf", 3), make_pdf("b.pdf", 3), out,
                          dataclasses.replace(settings, max_pages=5))


def test_protected_scans_stay_protected(scans, settings, out, tmp_path):
    fronts, backs = scans
    locked = tmp_path / "locked.pdf"
    with pikepdf.open(backs) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(user="pw", owner="pw", R=6))
    res = E.interleave_pdfs(fronts, locked, out, settings, back_password="pw")
    assert res["password_protected"] is True
    assert len(page_labels(out, password="pw")) == 6


# --- protection ----------------------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["resize", "n_up"])
def test_changing_content_needs_permission(marked, settings, out, tmp_path, tool):
    locked = tmp_path / "locked.pdf"
    with pikepdf.open(marked) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(
            user="pw", owner="owner", R=6, allow=pikepdf.Permissions(modify_other=False)))
    call = {"resize": lambda pw: E.resize_pages(locked, out, settings, size="a4",
                                                password=pw),
            "n_up": lambda pw: E.n_up(locked, out, settings, password=pw)}[tool]
    with pytest.raises(PdfUserError, match="does not allow changing"):
        call("pw")
    assert call("owner")["password_protected"] is True


def test_crop_and_blanks_keep_protection(marked, settings, out, tmp_path):
    locked = tmp_path / "locked.pdf"
    with pikepdf.open(marked) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(user="pw", owner="pw", R=6))
    assert E.crop_pages(locked, out, settings, [10, 10, 10, 10],
                        password="pw")["password_protected"] is True
    assert E.insert_blank_pages(locked, tmp_path / "b.pdf", settings, after=[1],
                                password="pw")["password_protected"] is True


def test_inputs_are_untouched(marked, settings, tmp_path):
    before = marked.read_bytes()
    E.crop_pages(marked, tmp_path / "a.pdf", settings, [10, 10, 10, 10])
    E.resize_pages(marked, tmp_path / "b.pdf", settings, size="a4")
    E.n_up(marked, tmp_path / "c.pdf", settings)
    E.insert_blank_pages(marked, tmp_path / "d.pdf", settings, after=[1])
    assert marked.read_bytes() == before


def test_units_table():
    assert layout.UNITS["in"] == 72 and round(layout.UNITS["mm"] * 25.4, 6) == 72


# --- review findings ---------------------------------------------------------------------------


def _with_shared_link(path, pages=2):
    src = _marked(path, pages=pages, size=(300, 300))
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        link = pdf.make_indirect(pikepdf.Dictionary(Subtype=pikepdf.Name.Link,
                                                    Rect=[20, 20, 100, 100]))
        shared = pdf.make_indirect(pikepdf.Array([link]))
        for page in pdf.pages:
            page.obj.Annots = shared
        pdf.save(src)
    return src


def _rects(path):
    with pikepdf.open(path) as pdf:
        return [[float(v) for v in page.obj.Annots[0].Rect] for page in pdf.pages]


def test_a_link_shared_by_pages_is_moved_once(tmp_path, settings, out):
    E.resize_pages(_with_shared_link(tmp_path / "s.pdf"), out, settings, width=600, height=900)
    assert _rects(out) == [[40, 190, 200, 350]] * 2


def test_resizing_some_pages_leaves_the_others_links_alone(tmp_path, settings, out):
    E.resize_pages(_with_shared_link(tmp_path / "s.pdf"), out, settings, width=600,
                   height=900, pages="2")
    assert _rects(out) == [[20, 20, 100, 100], [40, 190, 200, 350]]


def test_many_pages_sharing_a_link_stay_finite(tmp_path, settings, out):
    src = _with_shared_link(tmp_path / "s.pdf", pages=200)
    E.resize_pages(src, out, settings, width=600, height=900)
    assert _rects(out)[-1] == [40, 190, 200, 350]


def test_comments_outside_a_crop_stay_hidden_after_resizing(tmp_path, settings, out):
    src = _marked(tmp_path / "m.pdf")
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0].obj
        page.CropBox = pikepdf.Array([0, 300, 400, 600])
        page.Annots = pdf.make_indirect(pikepdf.Array([
            pikepdf.Dictionary(Subtype=pikepdf.Name.Square, Rect=[10, 10, 50, 50]),
            pikepdf.Dictionary(Subtype=pikepdf.Name.Square, Rect=[10, 400, 50, 450], F=4)]))
        pdf.save(src)
    E.resize_pages(src, out, settings, size="a4", orientation="portrait")
    with pikepdf.open(out) as pdf:
        outside, inside = pdf.pages[0].obj.Annots
        assert int(outside.F) & 2 and int(inside.F) == 4


def test_annotations_with_odd_coordinates_are_left_alone(tmp_path, settings, out):
    src = _marked(tmp_path / "m.pdf", size=(300, 300))
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        pdf.pages[0].obj.Annots = pdf.make_indirect(pikepdf.Array([
            pikepdf.Dictionary(Subtype=pikepdf.Name.Link, Rect=[0, 0, pikepdf.Name.X, 1]),
            pikepdf.Dictionary(Subtype=pikepdf.Name.Ink, Rect=[0, 0, 1, 1],
                               InkList=[[0, pikepdf.Name.X], [1, 1]],
                               QuadPoints=[0, pikepdf.String("x")])]))
        pdf.save(src)
    E.resize_pages(src, out, settings, width=600, height=900)
    with pikepdf.open(out) as pdf:
        bad_rect, ink = pdf.pages[0].obj.Annots
        assert bad_rect.Rect[2] == pikepdf.Name.X
        assert [[float(v) for v in p] for p in ink.InkList] == [[2, 152]]


def test_page_rotations_written_oddly_are_placed_as_seen(tmp_path, settings, out):
    reference = _rotate(_marked(tmp_path / "ref.pdf", pages=2), 90)
    E.n_up(reference, tmp_path / "ref_2up.pdf", settings)
    expected = _render(tmp_path / "ref_2up.pdf")
    for n, value in enumerate([-270, 450, pikepdf.Object.parse(b"90.0")]):
        src = _marked(tmp_path / f"r{n}.pdf", pages=2)
        with pikepdf.open(src, allow_overwriting_input=True) as pdf:
            for page in pdf.pages:
                page.obj.Rotate = value
            pdf.save(src)
        target = tmp_path / f"r{n}_2up.pdf"
        E.n_up(src, target, settings)
        assert _close_to(_render(target), expected), value


def test_n_up_frames_pages_as_viewers_show_them(tmp_path, settings, out):
    src = _marked(tmp_path / "m.pdf", pages=2)
    plain = tmp_path / "plain_2up.pdf"
    E.n_up(src, plain, settings)
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        for page in pdf.pages:
            page.obj.TrimBox = pikepdf.Array([100, 100, 300, 500])   # ignored by viewers
        pdf.save(src)
    E.n_up(src, out, settings)
    assert _close_to(_render(out), _render(plain))


def test_n_up_and_rearranging_are_capped(marked, settings, out):
    import dataclasses
    tight = dataclasses.replace(settings, max_pages=6)
    with pytest.raises(PdfUserError, match="places 8 pages; at most 6"):
        E.n_up(marked, out, tight, pages="-,-")
    with pytest.raises(PdfUserError, match="has 8 pages; at most 6"):
        E.rearrange_pages(marked, out, "-,-", tight)


@pytest.mark.parametrize("size", [(40, 40), (20000, 20000)])
def test_n_up_refuses_first_pages_unfit_for_a_sheet(tmp_path, settings, out, size):
    src = tmp_path / "odd.pdf"
    with pikepdf.new() as pdf:
        pdf.add_blank_page(page_size=(50, 50))
        pdf.pages[0].obj.MediaBox = pikepdf.Array([0, 0, *size])
        pdf.save(src)
    with pytest.raises(PdfUserError, match="give a paper size"):
        E.n_up(src, out, settings)
    E.n_up(src, out, settings, size="a4")          # with a size, any page fits


def _odd_pdf(path, change):
    with pikepdf.new() as pdf:
        pdf.add_blank_page(page_size=(200, 300))
        change(pdf.pages[0].obj)
        pdf.save(path)
    return path


def test_pages_without_a_size_are_refused(tmp_path, settings, out):
    src = _odd_pdf(tmp_path / "z.pdf", lambda p: setattr(p, "MediaBox",
                                                        pikepdf.Array([0, 0, 0, 300])))
    with pytest.raises(PdfUserError, match="Page 1 has no size"):
        E.resize_pages(src, out, settings, size="a4")
    with pytest.raises(PdfUserError, match="Page 1 has no size"):
        E.crop_pages(src, out, settings, [1, 1, 1, 1])
    with pytest.raises(PdfUserError, match="give a paper size|has no size"):
        E.n_up(src, out, settings)


def test_a_malformed_crop_box_is_ignored_as_viewers_do(tmp_path, settings, out):
    src = _odd_pdf(tmp_path / "c.pdf", lambda p: setattr(p, "CropBox", pikepdf.String("x")))
    assert E.crop_pages(src, out, settings, [10, 10, 10, 10])["sizes"] == [[180, 280]]
    assert E.resize_pages(src, tmp_path / "r.pdf", settings, size="a4")["scales"]


def test_user_units_are_points_per_unit(tmp_path, settings, out):
    src = _odd_pdf(tmp_path / "u.pdf", lambda p: setattr(p, "UserUnit", 2))  # 400 x 600 pt
    res = E.crop_pages(src, out, settings, [1, 0, 0, 0], unit="in")
    assert res["sizes"] == [[400, 528]]
    resized = tmp_path / "r.pdf"
    res = E.resize_pages(src, resized, settings, width=800, height=1200)
    assert res["scales"] == [2.0]
    with pikepdf.open(resized) as pdf:
        assert "/UserUnit" not in pdf.pages[0].obj
        assert [float(v) for v in pdf.pages[0].mediabox] == [0, 0, 800, 1200]
    blanks = tmp_path / "b.pdf"
    E.insert_blank_pages(src, blanks, settings, after=[1])
    with pikepdf.open(blanks) as pdf:
        assert [float(v) for v in pdf.pages[1].mediabox] == [0, 0, 400, 600]


@pytest.mark.parametrize("bad_unit", [0, -1, pikepdf.Name.X])
def test_odd_user_units_count_as_one(tmp_path, settings, out, bad_unit):
    src = _odd_pdf(tmp_path / "u.pdf", lambda p: setattr(p, "UserUnit", bad_unit))
    assert E.crop_pages(src, out, settings, [0, 0, 0, 0])["sizes"] == [[200, 300]]


def test_repeated_blank_positions_add_up(marked, settings, out):
    res = E.insert_blank_pages(marked, out, settings, after=[2, 2])
    assert res["pages"] == 6 and res["inserted"] == [3, 4]


def test_documents_without_pages(tmp_path, settings, out):
    empty = tmp_path / "empty.pdf"
    with pikepdf.new() as pdf:
        pdf.save(empty)
    with pytest.raises(PdfUserError, match="no pages"):
        E.n_up(empty, out, settings)
    with pytest.raises(PdfUserError, match="no pages"):
        E.insert_blank_pages(empty, out, settings, after=[0])


def test_watermarks_ignore_a_malformed_crop_box_too(tmp_path, settings, out):
    src = _odd_pdf(tmp_path / "c.pdf", lambda p: setattr(p, "CropBox", pikepdf.String("x")))
    assert E.add_watermark(src, out, settings, text="DRAFT")["pages"] == [1]


def test_n_up_copies_a_repeated_page_once(marked, settings, out, tmp_path):
    E.n_up(marked, out, settings, per_sheet=4, pages=",".join(["1"] * 40))
    with pikepdf.open(out) as pdf:
        forms = {obj.objgen for page in pdf.pages
                 for _, obj in page.obj.Resources.XObject.items()}
    assert len(forms) == 1                         # one copy of page 1, drawn 40 times


def test_n_up_of_n_ups_is_refused_before_it_explodes(marked, settings, tmp_path, monkeypatch):
    monkeypatch.setattr(layout, "N_UP_MAX_CONTENT", 200_000)
    current = marked
    with pytest.raises(PdfUserError, match="too much content .* nested drawings"):
        for k in range(12):                   # each level draws the last one 4 times
            nxt = tmp_path / f"level{k}.pdf"
            E.n_up(current, nxt, settings, per_sheet=4, pages="1,1,1,1")
            current = nxt
