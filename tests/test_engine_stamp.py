"""Watermarks, stamps and page numbers: placed correctly on any page, as displayed."""
from __future__ import annotations

import dataclasses

import pikepdf
import pypdfium2 as pdfium
import pytest
from PIL import Image
from reportlab.pdfgen import canvas

from paperboy import engine as E
from paperboy.errors import PdfUserError

from conftest import page_labels


def _blank(path, pages=3, size=(600, 800), body=True):
    c = canvas.Canvas(str(path), pagesize=size)
    for n in range(pages):
        if body:  # small content near the top-left, outside the regions tests measure
            c.setFont("Helvetica", 8)
            c.drawString(40, size[1] * 0.75, f"BODY-{n + 1}")
        c.showPage()
    c.save()
    return path


def _render(path, index=0, password=None):
    """The page as displayed (rotation and CropBox applied), grayscale."""
    doc = pdfium.PdfDocument(str(path), password=password)
    try:
        return doc[index].render(scale=0.5, grayscale=True).to_pil().copy()
    finally:
        doc.close()


def _ink(image, box):
    """Darkest pixel inside *box* (fractions of width/height, top-left origin)."""
    w, h = image.size
    left, top, right, bottom = box
    region = image.crop((int(left * w), int(top * h), int(right * w), int(bottom * h)))
    return region.getextrema()[0]


BOTTOM_CENTRE = (0.3, 0.9, 0.7, 1.0)
TOP_BAND = (0.0, 0.0, 1.0, 0.1)
CENTRE = (0.25, 0.35, 0.75, 0.65)


@pytest.fixture
def plain(tmp_path):
    return _blank(tmp_path / "doc.pdf")


# --- page numbers ------------------------------------------------------------------------


def test_page_numbers_default_format(plain, tmp_path, settings):
    out = tmp_path / "num.pdf"
    res = E.add_page_numbers(plain, out, settings)
    assert res["output"] == str(out)
    assert res["pages"] == [1, 2, 3]
    assert (res["first_label"], res["last_label"]) == ("Page 1 of 3", "Page 3 of 3")
    assert res["password_protected"] is False
    labels = page_labels(out)
    assert "Page 2 of 3" in labels[1] and "BODY-2" in labels[1]


def test_page_numbers_land_bottom_centre(plain, tmp_path, settings):
    out = tmp_path / "num.pdf"
    E.add_page_numbers(plain, out, settings)
    image = _render(out)
    assert _ink(image, BOTTOM_CENTRE) < 100   # the number
    assert _ink(image, TOP_BAND) > 200        # nothing at the top


@pytest.mark.parametrize(("position", "box"), [
    ("top-left", (0.0, 0.0, 0.35, 0.1)), ("top-right", (0.65, 0.0, 1.0, 0.1)),
    ("bottom-left", (0.0, 0.9, 0.35, 1.0)), ("bottom-right", (0.65, 0.9, 1.0, 1.0)),
    ("top-center", (0.3, 0.0, 0.7, 0.1)),
])
def test_page_number_positions(plain, tmp_path, settings, position, box):
    out = tmp_path / "num.pdf"
    E.add_page_numbers(plain, out, settings, position=position)
    assert _ink(_render(out), box) < 100


@pytest.mark.parametrize("rotate", [90, 180, 270])
def test_numbers_are_upright_at_the_bottom_of_rotated_pages(tmp_path, settings, rotate):
    plain = _blank(tmp_path / "rot.pdf", body=False)  # body text would move with rotation
    with pikepdf.open(plain, allow_overwriting_input=True) as pdf:
        for page in pdf.pages:
            page.Rotate = rotate
        pdf.save(plain)
    out = tmp_path / "num.pdf"
    E.add_page_numbers(plain, out, settings)
    image = _render(out)
    assert _ink(image, BOTTOM_CENTRE) < 100
    assert _ink(image, TOP_BAND) > 200
    # Upright text is wider than tall in the displayed image.
    band = image.crop((0, int(image.height * 0.9), image.width, image.height))
    bbox = band.point(lambda v: 255 if v < 128 else 0).getbbox()
    assert bbox and (bbox[2] - bbox[0]) >= (bbox[3] - bbox[1]) * 0.5


def test_rotation_inherited_from_the_page_tree(plain, tmp_path, settings):
    with pikepdf.open(plain, allow_overwriting_input=True) as pdf:
        pdf.Root.Pages.Rotate = 90
        for page in pdf.pages:
            if "/Rotate" in page.obj:
                del page.obj.Rotate
        pdf.save(plain)
    out = tmp_path / "num.pdf"
    E.add_page_numbers(plain, out, settings)
    assert _ink(_render(out), BOTTOM_CENTRE) < 100


def test_numbers_stay_inside_an_offset_crop_box(plain, tmp_path, settings):
    with pikepdf.open(plain, allow_overwriting_input=True) as pdf:
        for page in pdf.pages:
            page.CropBox = pikepdf.Array([100, 100, 500, 700])
        pdf.save(plain)
    out = tmp_path / "num.pdf"
    E.add_page_numbers(plain, out, settings)
    assert _ink(_render(out), BOTTOM_CENTRE) < 100


def test_content_that_leaves_the_graphics_state_changed(plain, tmp_path, settings):
    # A page whose content ends with an unrestored transform must not move the stamp.
    with pikepdf.open(plain, allow_overwriting_input=True) as pdf:
        pdf.pages[0].contents_add(pikepdf.Stream(pdf, b"2 0 0 2 300 400 cm"))
        pdf.save(plain)
    out = tmp_path / "num.pdf"
    E.add_page_numbers(plain, out, settings)
    assert _ink(_render(out), BOTTOM_CENTRE) < 100


def test_bates_numbering_with_zero_padding(plain, tmp_path, settings):
    out = tmp_path / "bates.pdf"
    res = E.add_page_numbers(plain, out, settings, format="ACME-{n:06}", start=41,
                             position="bottom-right")
    assert (res["first_label"], res["last_label"]) == ("ACME-000041", "ACME-000043")
    assert "ACME-000042" in page_labels(out)[1]


def test_numbering_selected_pages_keeps_their_position(plain, tmp_path, settings):
    out = tmp_path / "num.pdf"
    res = E.add_page_numbers(plain, out, settings, pages="2-", format="{n}/{total}")
    assert res["pages"] == [2, 3]
    assert (res["first_label"], res["last_label"]) == ("2/3", "3/3")
    assert "1/3" not in page_labels(out)[0]


def test_start_shifts_numbers_and_total(plain, tmp_path, settings):
    res = E.add_page_numbers(plain, tmp_path / "n.pdf", settings, start=0, pages="2-")
    assert (res["first_label"], res["last_label"]) == ("Page 1 of 2", "Page 2 of 2")


@pytest.mark.parametrize(("fmt", "message"), [
    ("{x}", "format can only use"), ("{n.__class__}", "format can only use"),
    ("{n:abc}", "format can only use"), ("{", "format can only use"),
    ("Page", "must include {n}"), ("", "must include {n}"),
])
def test_bad_formats_are_rejected(plain, tmp_path, settings, fmt, message):
    with pytest.raises(PdfUserError, match=message):
        E.add_page_numbers(plain, tmp_path / "n.pdf", settings, format=fmt)


@pytest.mark.parametrize("kwargs", [
    {"start": -1}, {"font_size": 2}, {"font_size": 100}, {"margin": -5},
    {"position": "middle-ish"}, {"color": "chartreuse-ish"}, {"color": "#12345"},
])
def test_bad_page_number_options_are_rejected(plain, tmp_path, settings, kwargs):
    with pytest.raises(PdfUserError):
        E.add_page_numbers(plain, tmp_path / "n.pdf", settings, **kwargs)


# --- watermarks and stamps ----------------------------------------------------------------


def test_text_watermark_across_the_centre(plain, tmp_path, settings):
    out = tmp_path / "wm.pdf"
    res = E.add_watermark(plain, out, settings, text="CONFIDENTIAL", opacity=1.0)
    assert res["pages"] == [1, 2, 3]
    assert _ink(_render(out), CENTRE) < 140
    assert "CONFIDENTIAL" in page_labels(out)[0]


def test_opacity_makes_the_watermark_faint(plain, tmp_path, settings):
    strong, faint = tmp_path / "s.pdf", tmp_path / "f.pdf"
    E.add_watermark(plain, strong, settings, text="DRAFT", opacity=1.0, color="#000000")
    E.add_watermark(plain, faint, settings, text="DRAFT", opacity=0.15, color="#000000")
    assert _ink(_render(strong), CENTRE) < 60
    assert _ink(_render(faint), CENTRE) > 180


def test_watermark_under_opaque_content_is_hidden_over_is_not(tmp_path, settings):
    path = tmp_path / "scan.pdf"
    c = canvas.Canvas(str(path), pagesize=(600, 800))
    c.setFillColorRGB(1, 1, 1)
    c.rect(0, 0, 600, 800, fill=1, stroke=0)  # an opaque scanned-page background
    c.showPage()
    c.save()
    under, over = tmp_path / "u.pdf", tmp_path / "o.pdf"
    E.add_watermark(path, under, settings, text="COPY", opacity=1, layer="under")
    E.add_watermark(path, over, settings, text="COPY", opacity=1, layer="over")
    assert _ink(_render(under), CENTRE) > 250
    assert _ink(_render(over), CENTRE) < 150


def test_corner_stamp(plain, tmp_path, settings):
    out = tmp_path / "stamp.pdf"
    E.add_watermark(plain, out, settings, text="APPROVED", position="top-right", rotation=0,
                    font_size=24, color="#cc0000", opacity=1)
    image = _render(out)
    assert _ink(image, (0.6, 0.0, 1.0, 0.12)) < 140
    assert _ink(image, (0.0, 0.85, 1.0, 1.0)) > 200  # nothing at the bottom


def test_watermark_on_rotated_pages_reads_as_displayed(plain, tmp_path, settings):
    with pikepdf.open(plain, allow_overwriting_input=True) as pdf:
        pdf.pages[0].Rotate = 90
        pdf.save(plain)
    out = tmp_path / "wm.pdf"
    E.add_watermark(plain, out, settings, text="TOP", position="top-center", rotation=0,
                    font_size=40, opacity=1, color="black")
    image = _render(out)
    assert _ink(image, (0.3, 0.0, 0.7, 0.15)) < 100   # at the top as displayed
    assert _ink(image, (0.0, 0.85, 1.0, 1.0)) > 200


def test_image_watermark(plain, tmp_path, settings):
    logo = tmp_path / "logo.png"
    Image.new("RGB", (100, 50), (0, 0, 0)).save(logo)
    out = tmp_path / "wm.pdf"
    res = E.add_watermark(plain, out, settings, image=logo, opacity=1, scale=0.3)
    assert res["pages"] == [1, 2, 3]
    assert _ink(_render(out), (0.45, 0.45, 0.55, 0.55)) < 30


def test_selected_pages_only(plain, tmp_path, settings):
    out = tmp_path / "wm.pdf"
    res = E.add_watermark(plain, out, settings, text="X", pages="2", opacity=1)
    assert res["pages"] == [2]
    assert "X" not in page_labels(out)[0].replace("BODY-1", "")


@pytest.mark.parametrize(("kwargs", "message"), [
    ({}, "text or an image"),
    ({"text": "A", "image": "x.png"}, "text or an image, not both"),
    ({"text": "機密"}, "can only use Latin characters"),
    ({"text": "x" * 201}, "at most 200"),
    ({"text": "A", "opacity": 0}, "opacity"), ({"text": "A", "opacity": 1.5}, "opacity"),
    ({"text": "A", "rotation": 400}, "rotation"),
    ({"text": "A", "layer": "middle"}, "layer"),
    ({"text": "A", "scale": 0}, "scale"),
])
def test_bad_watermark_options_are_rejected(plain, tmp_path, settings, kwargs, message):
    with pytest.raises(PdfUserError, match=message):
        E.add_watermark(plain, tmp_path / "wm.pdf", settings, **kwargs)
    assert not (tmp_path / "wm.pdf").exists()


def test_unreadable_image_is_a_user_error(plain, tmp_path, settings):
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image")
    with pytest.raises(PdfUserError, match="not a readable image"):
        E.add_watermark(plain, tmp_path / "wm.pdf", settings, image=bad)


# --- protection and permissions ---------------------------------------------------------------


def _locked(src, tmp_path, allow=None):
    out = tmp_path / "locked.pdf"
    with pikepdf.open(src) as pdf:
        pdf.save(out, encryption=pikepdf.Encryption(user="u", owner="o", R=6,
                                                    allow=allow or pikepdf.Permissions()))
    return out


@pytest.mark.parametrize("tool", ["numbers", "watermark"])
def test_protected_input_stays_protected(plain, tmp_path, settings, tool):
    locked, out = _locked(plain, tmp_path), tmp_path / "out.pdf"
    if tool == "numbers":
        res = E.add_page_numbers(locked, out, settings, password="u")
    else:
        res = E.add_watermark(locked, out, settings, text="X", password="u")
    assert res["password_protected"] is True
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(out)
    assert _ink(_render(out, password="u"), (0, 0, 1, 1)) < 200


@pytest.mark.parametrize("tool", ["numbers", "watermark"])
def test_changing_pages_needs_permission(plain, tmp_path, settings, tool):
    locked = _locked(plain, tmp_path, pikepdf.Permissions(modify_other=False))
    call = (lambda pw: E.add_page_numbers(locked, tmp_path / "o.pdf", settings, password=pw)) \
        if tool == "numbers" else \
        (lambda pw: E.add_watermark(locked, tmp_path / "o.pdf", settings, text="X",
                                    password=pw))
    with pytest.raises(PdfUserError, match="does not allow changing its pages.*owner"):
        call("u")
    call("o")


def test_page_cap(plain, tmp_path, settings):
    with pytest.raises(PdfUserError, match="page limit"):
        E.add_page_numbers(plain, tmp_path / "n.pdf", dataclasses.replace(settings, max_pages=2))


def test_input_is_unchanged(plain, tmp_path, settings):
    before = plain.read_bytes()
    E.add_watermark(plain, tmp_path / "wm.pdf", settings, text="X")
    E.add_page_numbers(plain, tmp_path / "n.pdf", settings)
    assert plain.read_bytes() == before


def test_rotation_lookup_walks_up_the_page_tree():
    from paperboy.engine.stamp import _rotation
    pdf = pikepdf.new()
    pdf.add_blank_page()
    page = pdf.pages[0]
    grandparent = pikepdf.Dictionary(Rotate=270)
    page.obj.Parent = pikepdf.Dictionary(Parent=grandparent)
    if "/Rotate" in page.obj:
        del page.obj.Rotate
    assert _rotation(page) == 270
    grandparent.Rotate = 95          # not a multiple of 90: rounded down
    assert _rotation(page) == 90
    del grandparent.Rotate
    assert _rotation(page) == 0      # none anywhere


def test_corner_stamp_without_a_font_size_fits_the_corner(plain, tmp_path, settings):
    out = tmp_path / "stamp.pdf"
    E.add_watermark(plain, out, settings, text="APPROVED BY THE BOARD", position="top-left",
                    rotation=0, opacity=1, color="black")
    image = _render(out)
    assert _ink(image, (0.0, 0.0, 0.5, 0.12)) < 100
    assert _ink(image, (0.55, 0.0, 1.0, 0.12)) > 200  # fitted: doesn't run across the page


def test_watermark_position_is_validated(plain, tmp_path, settings):
    with pytest.raises(PdfUserError, match="position must be one of"):
        E.add_watermark(plain, tmp_path / "w.pdf", settings, text="X", position="left")


# --- review findings ----------------------------------------------------------------------


def test_crop_box_larger_than_the_page_is_clipped(tmp_path, settings):
    plain = _blank(tmp_path / "big.pdf", body=False)
    with pikepdf.open(plain, allow_overwriting_input=True) as pdf:
        for page in pdf.pages:
            page.CropBox = pikepdf.Array([-200, -200, 812, 1000])  # beyond the MediaBox
        pdf.save(plain)
    out = tmp_path / "n.pdf"
    E.add_page_numbers(plain, out, settings)
    assert _ink(_render(out), BOTTOM_CENTRE) < 100


def test_crop_box_outside_the_page_falls_back_to_the_media_box(tmp_path, settings):
    plain = _blank(tmp_path / "off.pdf", body=False)
    with pikepdf.open(plain, allow_overwriting_input=True) as pdf:
        pdf.pages[0].CropBox = pikepdf.Array([5000, 5000, 6000, 6000])  # no overlap at all
        pdf.save(plain)
    out = tmp_path / "n.pdf"
    E.add_page_numbers(plain, out, settings)
    # PDFium can't render an impossible CropBox; drop it to see where the mark went.
    with pikepdf.open(out, allow_overwriting_input=True) as pdf:
        del pdf.pages[0].obj.CropBox
        pdf.save(out)
    assert _ink(_render(out), BOTTOM_CENTRE) < 100


def test_extra_restore_in_page_content_cannot_move_the_mark(tmp_path, settings):
    plain = _blank(tmp_path / "q.pdf", body=False)
    with pikepdf.open(plain, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        page.obj.Contents = pikepdf.Stream(pdf, b"q Q Q 0.25 0 0 0.25 0 0 cm")
        pdf.save(plain)
    out = tmp_path / "n.pdf"
    E.add_page_numbers(plain, out, settings)
    assert _ink(_render(out), BOTTOM_CENTRE) < 100


def test_unclosed_text_object_is_closed_before_the_mark(tmp_path, settings):
    plain = _blank(tmp_path / "bt.pdf", body=False)
    with pikepdf.open(plain, allow_overwriting_input=True) as pdf:
        pdf.pages[0].obj.Contents = pikepdf.Stream(pdf, b"BT /F1 12 Tf")
        pdf.save(plain)
    out = tmp_path / "n.pdf"
    E.add_page_numbers(plain, out, settings)
    with pikepdf.open(out) as pdf:
        ops = [str(op) for _, op in pikepdf.parse_content_stream(pdf.pages[0])]
    assert ops.count("BT") == ops.count("ET")


def test_shared_resources_are_not_polluted(tmp_path, settings):
    plain = _blank(tmp_path / "shared.pdf", pages=4, body=False)
    with pikepdf.open(plain, allow_overwriting_input=True) as pdf:
        shared = pdf.make_indirect(pikepdf.Dictionary(XObject=pikepdf.Dictionary()))
        for page in pdf.pages:
            page.obj.Resources = shared
        pdf.save(plain)
    out = tmp_path / "n.pdf"
    E.add_page_numbers(plain, out, settings)
    with pikepdf.open(out) as pdf:
        counts = [len(p.obj.Resources.XObject) for p in pdf.pages]
    assert counts == [1, 1, 1, 1]  # each page references only its own label


def test_decimal_rotate_values_are_honoured(tmp_path, settings):
    plain = _blank(tmp_path / "dec.pdf", body=False)
    with pikepdf.open(plain, allow_overwriting_input=True) as pdf:
        pdf.pages[0].obj.Rotate = pikepdf.Object.parse(b"90.0")
        pdf.save(plain)
    out = tmp_path / "n.pdf"
    E.add_page_numbers(plain, out, settings)
    image = _render(out)
    assert image.width > image.height and _ink(image, BOTTOM_CENTRE) < 100


@pytest.mark.parametrize(("fmt", "message"), [
    ("{n}" + "x" * 200, "at most 100 characters"),
    ("{n:099}", "padding"),
])
def test_page_number_format_is_bounded(plain, tmp_path, settings, fmt, message):
    with pytest.raises(PdfUserError, match=message):
        E.add_page_numbers(plain, tmp_path / "n.pdf", settings, format=fmt)


@pytest.mark.parametrize("text", ["A\tB", "\x01SECRET", "line\nbreak"])
def test_control_characters_are_rejected(plain, tmp_path, settings, text):
    with pytest.raises(PdfUserError, match="control characters"):
        E.add_watermark(plain, tmp_path / "w.pdf", settings, text=text)


def test_transparent_palette_png_stays_transparent(plain, tmp_path, settings):
    logo = tmp_path / "logo.png"
    image = Image.new("P", (100, 100), 0)
    image.putpalette([255, 255, 255, 0, 0, 0] + [0] * 762)
    for x in range(40, 60):
        for y in range(100):
            image.putpixel((x, y), 1)          # a black bar
    image.save(logo, transparency=0)            # index 0 (white) is transparent
    out = tmp_path / "w.pdf"
    E.add_watermark(plain, out, settings, image=logo, opacity=1, scale=0.5)
    rendered = _render(out)
    # Left of the bar is transparent: page white shows through, not black.
    assert _ink(rendered, (0.28, 0.45, 0.33, 0.55)) > 240
    assert _ink(rendered, (0.47, 0.45, 0.53, 0.55)) < 50


def test_large_images_are_downscaled_before_embedding(plain, tmp_path, settings):
    logo = tmp_path / "big.png"
    Image.new("RGB", (6000, 3000), (0, 0, 0)).save(logo)
    out = tmp_path / "w.pdf"
    E.add_watermark(plain, out, settings, image=logo, opacity=1)
    with pikepdf.open(out) as pdf:
        sizes = [(int(o.Width), int(o.Height)) for o in pdf.objects
                 if isinstance(o, pikepdf.Stream) and o.get("/Subtype") == "/Image"]
    assert sizes and max(max(s) for s in sizes) <= 2500


def test_only_common_image_formats_are_opened(plain, tmp_path, settings):
    eps = tmp_path / "logo.eps"
    eps.write_bytes(b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\n")
    with pytest.raises(PdfUserError, match="not a readable image"):
        E.add_watermark(plain, tmp_path / "w.pdf", settings, image=eps)


@pytest.mark.parametrize(("size", "text", "position"), [
    ((1000, 50), "A", "center"),                    # a banner: height is the limit
    ((792, 612), "CONFIDENTIAL", "center"),         # landscape
    ((600, 800), "APPROVED BY THE BOARD", "top-left"),
    ((600, 800), "APPROVED BY THE BOARD", "bottom-right"),
])
def test_auto_sized_text_stays_on_the_page(tmp_path, settings, size, text, position):
    plain = _blank(tmp_path / "p.pdf", pages=1, size=size, body=False)
    out = tmp_path / "w.pdf"
    E.add_watermark(plain, out, settings, text=text, position=position, opacity=1,
                    color="black")
    rendered = _render(out)
    border = 2
    edges = [rendered.crop((0, 0, rendered.width, border)),
             rendered.crop((0, rendered.height - border, rendered.width, rendered.height)),
             rendered.crop((0, 0, border, rendered.height)),
             rendered.crop((rendered.width - border, 0, rendered.width, rendered.height))]
    assert all(edge.getextrema()[0] > 200 for edge in edges)  # nothing runs off the page
    assert rendered.getextrema()[0] < 100                      # but it is there


def test_corner_positions_default_to_no_tilt(plain, tmp_path, settings):
    out = tmp_path / "w.pdf"
    E.add_watermark(plain, out, settings, text="APPROVED", position="top-right", opacity=1,
                    color="black")
    image = _render(out)
    assert _ink(image, (0.5, 0.0, 1.0, 0.1)) < 100
    assert _ink(image, (0.5, 0.2, 1.0, 0.5)) > 200  # not slanting down into the page


def test_text_that_cannot_fit_is_refused(tmp_path, settings):
    tiny = _blank(tmp_path / "t.pdf", pages=1, size=(72, 72), body=False)
    with pytest.raises(PdfUserError, match="too long to fit"):
        E.add_watermark(tiny, tmp_path / "w.pdf", settings, text="X" * 200)


def test_unparseable_content_falls_back_to_a_simple_wrap(plain, tmp_path, settings,
                                                         monkeypatch):
    def unparseable(*args, **kwargs):
        raise pikepdf.PdfError("content stream is damaged")

    monkeypatch.setattr(pikepdf, "parse_content_stream", unparseable)
    out = tmp_path / "n.pdf"
    E.add_page_numbers(plain, out, settings)
    assert _ink(_render(out), BOTTOM_CENTRE) < 100


def test_page_without_resources(tmp_path, settings):
    plain = _blank(tmp_path / "nores.pdf", pages=1, body=False)
    with pikepdf.open(plain, allow_overwriting_input=True) as pdf:
        if "/Resources" in pdf.pages[0].obj:
            del pdf.pages[0].obj.Resources
        pdf.save(plain)
    out = tmp_path / "n.pdf"
    E.add_page_numbers(plain, out, settings)
    assert _ink(_render(out), BOTTOM_CENTRE) < 100
