"""redact_pdf: finding what to redact, rewriting pages, the checks and the fallback."""
from __future__ import annotations

import io
import zlib

import pikepdf
import pypdfium2 as pdfium
import pytest
from PIL import Image

from paperboy import engine as E
from paperboy.engine import redact as R
from paperboy.errors import PdfUserError

Name = pikepdf.Name


def helvetica(pdf):
    return pdf.make_indirect(pikepdf.Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                                BaseFont=Name.Helvetica))


def build(path, *contents, resources=None, edit=None, size=(612, 792)):
    """A PDF with one page per content stream, sharing Helvetica as /F1 and the
    resources *resources(pdf)* gives; *edit(pdf)* can change it before saving."""
    pdf = pikepdf.new()
    extra = resources(pdf) if resources else {}
    fonts = {"/F1": helvetica(pdf), **extra.pop("/Font", {})}
    res = pikepdf.Dictionary({"/Font": pikepdf.Dictionary(fonts),
                              **{k: pikepdf.Dictionary(v) for k, v in extra.items()}})
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
        textpage = doc[page].get_textpage()
        return textpage.get_text_range()
    finally:
        doc.close()


def image(pdf, picture: Image.Image, **extra):
    """An image XObject of *picture*, Flate-compressed raw samples."""
    stream = pikepdf.Stream(pdf, zlib.compress(picture.tobytes()))
    stream.Type, stream.Subtype = Name.XObject, Name.Image
    stream.Width, stream.Height = picture.size
    stream.ColorSpace = {"L": Name.DeviceGray, "RGB": Name.DeviceRGB,
                         "CMYK": Name.DeviceCMYK}[picture.mode]
    stream.BitsPerComponent, stream.Filter = 8, Name.FlateDecode
    for k, v in extra.items():
        stream[f"/{k}"] = v
    return stream


def jpeg(pdf, picture: Image.Image, **extra):
    buf = io.BytesIO()
    picture.save(buf, "JPEG")
    stream = pikepdf.Stream(pdf, buf.getvalue())
    stream.Type, stream.Subtype = Name.XObject, Name.Image
    stream.Width, stream.Height = picture.size
    stream.ColorSpace = {"L": Name.DeviceGray, "RGB": Name.DeviceRGB,
                         "CMYK": Name.DeviceCMYK}[picture.mode]
    stream.BitsPerComponent, stream.Filter = 8, Name.DCTDecode
    for k, v in extra.items():
        stream[f"/{k}"] = v
    return stream


def pixels(path, page=0, name=None):
    """The decoded pixels of the first (or named) image drawn on a page."""
    with pikepdf.open(path) as pdf:
        xobjects = pdf.pages[page].obj.Resources.XObject
        key = name or sorted(xobjects.keys())[0]
        return pikepdf.PdfImage(xobjects[key]).as_pil_image()


LINE = b"BT /F1 12 Tf 72 700 Td (Call John Smith on Friday) Tj ET"


@pytest.fixture
def letter(tmp_path):
    return build(tmp_path / "letter.pdf", LINE,
                 b"BT /F1 12 Tf 72 700 Td (Nothing to see here) Tj ET")


# --- finding what to redact -------------------------------------------------------------


def test_text_is_removed_and_the_rest_kept(letter, tmp_path, settings):
    out = tmp_path / "out.pdf"
    res = E.redact_pdf(letter, out, settings, texts=["John Smith"])
    assert res["match_count"] == 1
    assert res["matches"] == [{"page": 1, "source": "John Smith", "text": "John Smith"}]
    page = res["pages"][0]
    assert page["page"] == 1 and page["method"] == "edited" and page["reason"] is None
    assert page["text_removed"] == 10   # "John Smith", its space included
    assert text_of(out).split() == ["Call", "on", "Friday"]
    assert text_of(out, 1) == "Nothing to see here"
    assert res["pages_searched"] == 2 and res["pages_without_text"] == []
    with pikepdf.open(out) as pdf:  # painted over in black
        assert b" re f Q" in pdf.pages[0].obj.Contents.read_bytes()
        assert b"0 g" in pdf.pages[0].obj.Contents.read_bytes()


def test_the_rest_of_the_line_stays_where_it_was(letter, tmp_path, settings):
    out = tmp_path / "out.pdf"
    E.redact_pdf(letter, out, settings, texts=["John"])

    def boxes(path):
        doc = pdfium.PdfDocument(str(path))
        textpage = doc[0].get_textpage()
        found = {textpage.get_text_range(i, 1) + str(i): textpage.get_charbox(i)
                 for i in range(textpage.count_chars())}
        text = textpage.get_text_range()
        doc.close()
        return text, found

    before_text, _ = boxes(letter)
    doc = pdfium.PdfDocument(str(letter))
    tp = doc[0].get_textpage()
    friday_before = tp.get_charbox(before_text.index("Friday"))
    doc.close()
    after = pdfium.PdfDocument(str(out))
    tp = after[0].get_textpage()
    text = tp.get_text_range()
    friday_after = tp.get_charbox(text.index("Friday"))
    after.close()
    assert friday_after == pytest.approx(friday_before, abs=0.01)


@pytest.mark.parametrize(("kwargs", "found"), [
    ({}, 2), ({"match_case": True}, 1), ({"whole_word": True}, 1),
])
def test_matching_options(tmp_path, settings, kwargs, found):
    src = build(tmp_path / "in.pdf", b"BT /F1 12 Tf 72 700 Td (Ann met ann and Annie) Tj ET")
    res = E.redact_pdf(src, tmp_path / "out.pdf", settings,
                       texts=["Ann met" if "match_case" in kwargs else "Ann"], **kwargs)
    if "match_case" in kwargs:
        assert res["match_count"] == found
    elif "whole_word" in kwargs:
        assert res["match_count"] == found + 1   # "Ann", "ann"; not "Annie"
    else:
        assert res["match_count"] == found + 1


def test_patterns_find_what_matches(tmp_path, settings):
    src = build(tmp_path / "in.pdf",
                b"BT /F1 12 Tf 72 700 Td (Mail ada@example.com or bob@test.org now) Tj ET")
    out = tmp_path / "out.pdf"
    res = E.redact_pdf(src, out, settings, patterns=[r"[\w.]+@[\w.]+\w"])
    assert [m["text"] for m in res["matches"]] == ["ada@example.com", "bob@test.org"]
    assert text_of(out).split() == ["Mail", "or", "now"]


def test_pattern_case(tmp_path, settings):
    src = build(tmp_path / "in.pdf", b"BT /F1 12 Tf 72 700 Td (CODE code) Tj ET")
    assert E.redact_pdf(src, tmp_path / "a.pdf", settings,
                        patterns=["code"])["match_count"] == 2
    assert E.redact_pdf(src, tmp_path / "b.pdf", settings, patterns=["code"],
                        match_case=True)["match_count"] == 1


def test_invalid_pattern(letter, tmp_path, settings):
    with pytest.raises(PdfUserError, match="Pattern 2 is not a valid regular expression"):
        E.redact_pdf(letter, tmp_path / "out.pdf", settings, patterns=["ok", "(unclosed"])
    assert not (tmp_path / "out.pdf").exists()


def test_slow_pattern_is_stopped(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(R, "PATTERN_TIME_LIMIT_S", 1)
    src = build(tmp_path / "in.pdf", b"BT /F1 12 Tf 72 700 Td (" + b"a" * 40 + b"!) Tj ET")
    with pytest.raises(PdfUserError, match="took too long to match"):
        E.redact_pdf(src, tmp_path / "out.pdf", settings, patterns=["(a+)+$"])


def test_too_many_matches(letter, tmp_path, settings, monkeypatch):
    monkeypatch.setattr(R, "MAX_MATCHES", 3)
    with pytest.raises(PdfUserError, match="match more than 3 times"):
        E.redact_pdf(letter, tmp_path / "out.pdf", settings, patterns=["[a-z]"])
    with pytest.raises(PdfUserError, match="found more than 3 times"):
        E.redact_pdf(letter, tmp_path / "out.pdf", settings, texts=["o"])
    monkeypatch.setattr(R, "MAX_MATCHES", 13)
    with pytest.raises(PdfUserError, match="match more than 13 times"):
        E.redact_pdf(letter, tmp_path / "out.pdf", settings, texts=["o"], patterns=["[a-z]"])


def test_too_much_text_for_patterns(letter, tmp_path, settings, monkeypatch):
    monkeypatch.setattr(R, "MAX_PATTERN_TEXT", 10)
    with pytest.raises(PdfUserError, match="too much text to match patterns"):
        E.redact_pdf(letter, tmp_path / "out.pdf", settings, patterns=["John"])


def test_pages_limit_the_search(letter, tmp_path, settings):
    src = build(tmp_path / "in.pdf", LINE, LINE)
    res = E.redact_pdf(src, tmp_path / "out.pdf", settings, texts=["John"], pages="2")
    assert res["pages_searched"] == 1 and [p["page"] for p in res["pages"]] == [2]
    assert "John" in text_of(tmp_path / "out.pdf", 0)


def test_nothing_found(tmp_path, settings):
    scan = build(tmp_path / "scan.pdf", b"BT /F1 12 Tf 72 700 Td (words) Tj ET",
                 b"0 0 1 rg 10 10 50 50 re f")
    with pytest.raises(PdfUserError, match=r"weren't found. Pages 2 have no text"):
        E.redact_pdf(scan, tmp_path / "out.pdf", settings, texts=["absent"])
    with pytest.raises(PdfUserError, match=r"weren't found. Nothing was written"):
        E.redact_pdf(scan, tmp_path / "out.pdf", settings, texts=["absent"], pages="1")


@pytest.mark.parametrize(("kwargs", "message"), [
    ({}, "Give texts, patterns or areas"),
    ({"texts": "John"}, "must be lists"),
    ({"patterns": "x"}, "must be lists"),
    ({"texts": ["x"] * 51 + [str(i) for i in range(51)]}, "at most 50 texts"),
    ({"patterns": [str(i) for i in range(21)]}, "at most 20 patterns"),
    ({"patterns": [""]}, "1 to 300 characters"),
    ({"patterns": [5]}, "1 to 300 characters"),
    ({"texts": ["x"], "fill": "red"}, "fill must be one of"),
    ({"texts": ["x"], "rasterize": "maybe"}, "rasterize must be one of"),
    ({"texts": ["x"], "dpi": 30}, "dpi must be 72 to 300"),
    ({"texts": ["x"], "dpi": True}, "dpi must be 72 to 300"),
    ({"areas": {"page": 1}}, "areas must be a list"),
    ({"areas": [{"page": 3, "rect": [0, 0, 1, 1]}]}, "Area 1: page must be 1 to 2"),
    ({"areas": [{"page": True, "rect": [0, 0, 1, 1]}]}, "page must be"),
    ({"areas": ["x"]}, "page must be"),
    ({"areas": [{"page": 1, "rect": [0, 0, 1]}]}, "rect must be"),
    ({"areas": [{"page": 1, "rect": [0, 0, 0, 5]}]}, "rect must be"),
    ({"areas": [{"page": 1, "rect": [0, 0, True, 5]}]}, "rect must be"),
    ({"areas": [{"page": 1, "rect": [0, 0, float("inf"), 5]}]}, "rect must be"),
])
def test_bad_arguments(letter, tmp_path, settings, kwargs, message):
    with pytest.raises(PdfUserError, match=message):
        E.redact_pdf(letter, tmp_path / "out.pdf", settings, **kwargs)


def test_too_many_areas(letter, tmp_path, settings):
    with pytest.raises(PdfUserError, match="at most 2000"):
        E.redact_pdf(letter, tmp_path / "out.pdf", settings,
                     areas=[{"page": 1, "rect": [0, 0, 1, 1]}] * 2001)


# --- areas, pictures and drawings -------------------------------------------------------


def test_area_removes_only_what_is_inside(letter, tmp_path, settings):
    out = tmp_path / "out.pdf"
    # "Call " runs from 72 to about 100; the area covers the middle of "Call" only.
    res = E.redact_pdf(letter, out, settings, areas=[{"page": 1, "rect": [70, 690, 86, 715]}],
                       fill="white")
    assert res["pages"][0]["areas"] == 1
    assert text_of(out).startswith("ll John") or text_of(out).startswith("l John")
    with pikepdf.open(out) as pdf:
        assert b"1 g" in pdf.pages[0].obj.Contents.read_bytes()


def test_fill_none_paints_nothing(letter, tmp_path, settings):
    out = tmp_path / "out.pdf"
    E.redact_pdf(letter, out, settings, texts=["John"], fill="none")
    with pikepdf.open(out) as pdf:
        assert b" re f Q" not in pdf.pages[0].obj.Contents.read_bytes()
    assert "John" not in text_of(out)


def scan(pdf):
    picture = Image.new("RGB", (200, 100), (200, 50, 50))
    return {"/XObject": {"/Im1": image(pdf, picture)}}


SCAN = b"q 400 0 0 200 100 500 cm /Im1 Do Q"


def test_scanned_page_has_its_pixels_blanked(tmp_path, settings):
    src = build(tmp_path / "in.pdf", SCAN, resources=scan)
    out = tmp_path / "out.pdf"
    # The area covers image pixels x 25-75, y (from the top) 25-75.
    res = E.redact_pdf(src, out, settings, areas=[{"page": 1, "rect": [150, 550, 250, 650]}],
                       fill="none")
    assert res["pages"][0]["images_edited"] == 1
    picture = pixels(out)
    assert picture.getpixel((50, 50)) == (0, 0, 0)
    assert picture.getpixel((25, 25)) == (0, 0, 0) and picture.getpixel((74, 74)) == (0, 0, 0)
    assert picture.getpixel((24, 50)) == (200, 50, 50)
    assert picture.getpixel((76, 50)) == (200, 50, 50)
    with pikepdf.open(out) as pdf:  # the original is gone from the page
        assert list(pdf.pages[0].obj.Resources.XObject.keys()) == ["/RdIm1"]


def test_searching_a_scan_lists_it(tmp_path, settings):
    src = build(tmp_path / "in.pdf", LINE, SCAN, resources=scan)
    res = E.redact_pdf(src, tmp_path / "out.pdf", settings, texts=["John"])
    assert res["pages_without_text"] == [2]


def test_picture_wholly_inside_is_removed(tmp_path, settings):
    src = build(tmp_path / "in.pdf", SCAN, resources=scan)
    out = tmp_path / "out.pdf"
    res = E.redact_pdf(src, out, settings, areas=[{"page": 1, "rect": [90, 490, 510, 710]}])
    assert res["pages"][0]["images_removed"] == 1
    with pikepdf.open(out) as pdf:
        assert "/XObject" not in pdf.pages[0].obj.Resources or \
            not pdf.pages[0].obj.Resources.XObject.keys()


def test_area_beside_a_picture_leaves_it_alone(tmp_path, settings):
    src = build(tmp_path / "in.pdf", SCAN + b" BT /F1 12 Tf 72 300 Td (John) Tj ET",
                resources=scan)
    out = tmp_path / "out.pdf"
    res = E.redact_pdf(src, out, settings, texts=["John"])
    assert res["pages"][0]["images_edited"] == 0
    with pikepdf.open(out) as pdf:
        assert list(pdf.pages[0].obj.Resources.XObject.keys()) == ["/Im1"]


def test_shared_picture_is_copied(tmp_path, settings):
    src = build(tmp_path / "in.pdf", SCAN, SCAN, resources=scan)
    out = tmp_path / "out.pdf"
    E.redact_pdf(src, out, settings, areas=[{"page": 1, "rect": [150, 550, 250, 650]}])
    assert pixels(out, 1).getpixel((50, 50)) == (200, 50, 50)
    assert pixels(out, 0).getpixel((50, 50)) == (0, 0, 0)


def test_jpeg_picture_is_edited(tmp_path, settings):
    def res(pdf):
        return {"/XObject": {"/Im1": jpeg(pdf, Image.new("L", (100, 100), 200))}}
    src = build(tmp_path / "in.pdf", b"q 100 0 0 100 100 100 cm /Im1 Do Q", resources=res)
    out = tmp_path / "out.pdf"
    res_ = E.redact_pdf(src, out, settings, areas=[{"page": 1, "rect": [100, 100, 150, 150]}])
    assert res_["pages"][0]["images_edited"] == 1
    with pikepdf.open(out) as pdf:
        im = pdf.pages[0].obj.Resources.XObject["/RdIm1"]
        assert im.Filter == Name.DCTDecode
    picture = pixels(out)
    assert picture.getpixel((20, 80)) < 10 and picture.getpixel((80, 20)) > 190


def test_rgb_jpeg_with_old_colour_settings_is_edited(tmp_path, settings):
    def res(pdf):
        return {"/XObject": {"/Im1": jpeg(pdf, Image.new("RGB", (40, 40), (0, 200, 0)),
                                          DecodeParms=pikepdf.Dictionary(ColorTransform=1))}}
    src = build(tmp_path / "in.pdf", b"q 100 0 0 100 100 100 cm /Im1 Do Q", resources=res)
    out = tmp_path / "out.pdf"
    E.redact_pdf(src, out, settings, areas=[{"page": 1, "rect": [100, 100, 150, 150]}])
    with pikepdf.open(out) as pdf:
        assert "/DecodeParms" not in pdf.pages[0].obj.Resources.XObject["/RdIm1"]


@pytest.mark.parametrize(("make", "reason"), [
    (lambda pdf: jpeg(pdf, Image.new("CMYK", (40, 40))), "CMYK JPEG"),
    (lambda pdf: jpeg(pdf, Image.new("L", (40, 40)), Decode=pikepdf.Array([1, 0])),
     "unusual colour settings"),
    (lambda pdf: _codec(pdf, "/JPXDecode"), "picture data that can't be read"),
    (lambda pdf: _codec(pdf, "/JBIG2Decode"), "JBIG2Decode compression"),
    (lambda pdf: image(pdf, Image.new("L", (40, 40)), ColorSpace=Name.Pattern),
     "layout this can't edit"),
    (lambda pdf: image(pdf, Image.new("L", (40, 40)), Width=10**5, Height=10**5),
     "can't be edited: it is 100000"),
])
def test_pictures_that_cant_be_edited_make_a_picture_page(tmp_path, settings, make, reason):
    src = build(tmp_path / "in.pdf", b"q 100 0 0 100 100 100 cm /Im1 Do Q " + LINE,
                resources=lambda pdf: {"/XObject": {"/Im1": make(pdf)}})
    out = tmp_path / "out.pdf"
    res = E.redact_pdf(src, out, settings, areas=[{"page": 1, "rect": [100, 100, 150, 150]}])
    page = res["pages"][0]
    assert page["method"] == "rasterized" and reason in page["reason"]
    assert text_of(out) == ""


def _codec(pdf, codec):
    stream = pikepdf.Stream(pdf, b"\0" * 64)
    stream.Type, stream.Subtype, stream.Width, stream.Height = (
        Name.XObject, Name.Image, 40, 40)
    stream.ColorSpace, stream.BitsPerComponent, stream.Filter = (
        Name.DeviceGray, 8, Name(codec))
    return stream


def test_jpeg_that_cant_be_decoded(tmp_path, settings, monkeypatch):
    from paperboy.engine import redact_content as C

    def res(pdf):
        return {"/XObject": {"/Im1": jpeg(pdf, Image.new("L", (40, 40), 9))}}
    src = build(tmp_path / "in.pdf", b"q 100 0 0 100 100 100 cm /Im1 Do Q", resources=res)
    monkeypatch.setattr(C, "encoded_image", lambda stream, limit: b"not a jpeg")
    res_ = E.redact_pdf(src, tmp_path / "out.pdf", settings,
                        areas=[{"page": 1, "rect": [100, 100, 150, 150]}])
    assert res_["pages"][0]["reason"] == "a JPEG picture under an area can't be read"


def test_masks_are_blanked_too(tmp_path, settings):
    def res(pdf):
        mask = image(pdf, Image.new("L", (20, 20), 255))
        return {"/XObject": {"/Im1": image(pdf, Image.new("RGB", (20, 20), (9, 9, 9)),
                                           SMask=mask)}}
    src = build(tmp_path / "in.pdf", b"q 100 0 0 100 100 100 cm /Im1 Do Q", resources=res)
    out = tmp_path / "out.pdf"
    E.redact_pdf(src, out, settings, areas=[{"page": 1, "rect": [100, 100, 150, 150]}])
    with pikepdf.open(out) as pdf:
        picture = pdf.pages[0].obj.Resources.XObject["/RdIm1"]
        mask = pikepdf.PdfImage(picture.SMask).as_pil_image()
        assert mask.getpixel((5, 15)) == 0 and mask.getpixel((15, 5)) == 255


def test_stencil_and_low_bit_pictures(tmp_path, settings):
    def res(pdf):
        stencil = pikepdf.Stream(pdf, zlib.compress(b"\xff" * (2 * 16)))
        stencil.Type, stencil.Subtype = Name.XObject, Name.Image
        stencil.Width, stencil.Height, stencil.ImageMask = 16, 16, True
        stencil.Filter = Name.FlateDecode
        four = pikepdf.Stream(pdf, zlib.compress(b"\xff" * (8 * 16)))
        four.Type, four.Subtype = Name.XObject, Name.Image
        four.Width, four.Height, four.BitsPerComponent = 16, 16, 4
        four.ColorSpace, four.Filter = Name.DeviceGray, Name.FlateDecode
        return {"/XObject": {"/Im1": stencil, "/Im2": four}}
    src = build(tmp_path / "in.pdf", b"q 160 0 0 160 0 0 cm /Im1 Do Q "
                b"q 160 0 0 160 200 0 cm /Im2 Do Q", resources=res)
    out = tmp_path / "out.pdf"
    res_ = E.redact_pdf(src, out, settings, areas=[{"page": 1, "rect": [35, 0, 45, 160]},
                                                   {"page": 1, "rect": [235, 0, 245, 160]}])
    assert res_["pages"][0]["images_edited"] == 2
    with pikepdf.open(out) as pdf:
        xobjects = pdf.pages[0].obj.Resources.XObject
        stencil = xobjects["/RdIm1"].read_bytes()
        assert stencil[0] == 0b11100111 and stencil[1] == 0xFF  # pixels 3 and 4 cleared
        four = xobjects["/RdIm2"].read_bytes()
        assert four[:3] == b"\xff\xf0\x0f"  # pixel 3 shares a byte with 2, 4 with 5


def test_rotated_picture(tmp_path, settings):
    src = build(tmp_path / "in.pdf", b"q 0 100 -100 0 300 100 cm /Im1 Do Q",
                resources=lambda pdf: {"/XObject": {"/Im1": image(
                    pdf, Image.new("L", (100, 100), 200))}})
    out = tmp_path / "out.pdf"
    E.redact_pdf(src, out, settings, areas=[{"page": 1, "rect": [200, 100, 250, 200]}],
                 fill="none")
    picture = pixels(out)
    # The image's columns run up the page, its rows from right to left.
    assert picture.getpixel((50, 25)) == 0 and picture.getpixel((50, 75)) == 200


def test_short_picture_data_is_padded(tmp_path, settings):
    def res(pdf):
        stream = image(pdf, Image.new("L", (10, 10), 7))
        stream.write(zlib.compress(b"\x07" * 50), filter=Name.FlateDecode)
        return {"/XObject": {"/Im1": stream}}
    src = build(tmp_path / "in.pdf", b"q 100 0 0 100 0 0 cm /Im1 Do Q", resources=res)
    out = tmp_path / "out.pdf"
    E.redact_pdf(src, out, settings, areas=[{"page": 1, "rect": [0, 0, 20, 20]}])
    with pikepdf.open(out) as pdf:
        assert len(pdf.pages[0].obj.Resources.XObject["/RdIm1"].read_bytes()) == 100


def test_damaged_picture(tmp_path, settings):
    def res(pdf):
        stream = image(pdf, Image.new("L", (10, 10), 7))
        stream.write(b"\x78\x9c\x00garbage", filter=Name.FlateDecode)
        return {"/XObject": {"/Im1": stream}}
    src = build(tmp_path / "in.pdf", b"q 100 0 0 100 0 0 cm /Im1 Do Q", resources=res)
    res_ = E.redact_pdf(src, tmp_path / "out.pdf", settings,
                        areas=[{"page": 1, "rect": [0, 0, 20, 20]}])
    assert res_["pages"][0]["method"] == "rasterized"


def test_inline_pictures(tmp_path, settings):
    inline = b"q 50 0 0 50 100 100 cm BI /W 2 /H 2 /CS /G /BPC 8 ID \x00\xff\xff\x00 EI Q "
    src = build(tmp_path / "in.pdf", inline + LINE)
    out = tmp_path / "out.pdf"
    res = E.redact_pdf(src, out, settings, areas=[{"page": 1, "rect": [90, 90, 160, 160]}])
    assert res["pages"][0]["images_removed"] == 1
    with pikepdf.open(out) as pdf:
        assert b"BI" not in pdf.pages[0].obj.Contents.read_bytes()
    kept = tmp_path / "kept.pdf"
    E.redact_pdf(src, kept, settings, texts=["John"])
    with pikepdf.open(kept) as pdf:
        assert b"BI" in pdf.pages[0].obj.Contents.read_bytes()
    partly = E.redact_pdf(src, tmp_path / "partly.pdf", settings,
                          areas=[{"page": 1, "rect": [90, 90, 120, 120]}])
    assert "partly under an area" in partly["pages"][0]["reason"]


def test_drawings(tmp_path, settings):
    content = (b"0 0 1 rg 100 100 20 20 re f "        # inside: removed
               b"100 300 m 400 300 l S "               # crosses the area: kept
               b"q 100 500 20 20 re W n 0 0 612 792 re f Q "  # a clip inside: kept
               b"100 600 30 30 re W f "                # inside, and clips: becomes n
               b"8 w 120 200 m 125 200 l S")           # a thick stroke wider than the area
    src = build(tmp_path / "in.pdf", content)
    out = tmp_path / "out.pdf"
    res = E.redact_pdf(src, out, settings, areas=[
        {"page": 1, "rect": [90, 90, 130, 130]}, {"page": 1, "rect": [90, 290, 200, 310]},
        {"page": 1, "rect": [90, 490, 130, 530]}, {"page": 1, "rect": [90, 590, 140, 640]},
        {"page": 1, "rect": [119, 199, 126, 201]}])
    # Removed: the square, the clipping square; cut: the line, the page background
    # around the clip area, the thick stroke (wholly under its area, so nothing left).
    assert res["pages"][0]["drawings_removed"] == 5
    with pikepdf.open(out) as pdf:
        ops = [(str(op), [float(v) for v in operands if isinstance(v, (int, float))
                          or hasattr(v, "as_integer_ratio")])
               for operands, op in pikepdf.parse_content_stream(pdf.pages[0].obj)]
    names = [op for op, _ in ops]
    assert ("re", [100.0, 100.0, 20.0, 20.0]) not in ops
    assert ("re", [100.0, 600.0, 30.0, 30.0]) in ops and "W" in names
    assert names.count("S") == 1
    assert ("re", [0.0, 0.0, 612.0, 792.0]) not in ops   # the background, in pieces
    # The kept piece stops half the line's width short of the area.
    assert ("m", [200.5, 300.0]) in ops and ("l", [400.0, 300.0]) in ops


def test_operators_inside_a_path_keep_it(tmp_path, settings):
    src = build(tmp_path / "in.pdf", b"100 100 m 0 g 120 120 l S")
    out = tmp_path / "out.pdf"
    res = E.redact_pdf(src, out, settings, areas=[{"page": 1, "rect": [90, 90, 130, 130]}])
    assert res["pages"][0]["drawings_removed"] == 0


def test_unreadable_path_is_kept(tmp_path, settings):
    src = build(tmp_path / "in.pdf", b"100 100 /x 20 re f 100 100 1 2 3 re f 0 0 1 2 3 c f")
    res = E.redact_pdf(src, tmp_path / "out.pdf", settings,
                       areas=[{"page": 1, "rect": [0, 0, 612, 792]}])
    assert res["pages"][0]["drawings_removed"] == 0


# --- forms ------------------------------------------------------------------------------


def form(pdf, content=b"BT /F1 12 Tf 0 0 Td (Shared secret) Tj ET", **extra):
    stream = pdf.make_stream(content)
    stream.Type, stream.Subtype = Name.XObject, Name.Form
    stream.BBox = [0, -5, 200, 20]
    for k, v in extra.items():
        stream[f"/{k}"] = v
    return stream


def test_shared_form_is_copied_and_redacted(tmp_path, settings):
    src = build(tmp_path / "in.pdf", b"q 1 0 0 1 100 200 cm /Fm1 Do Q",
                b"q 1 0 0 1 100 200 cm /Fm1 Do Q",
                resources=lambda pdf: {"/XObject": {"/Fm1": form(pdf)}})
    out = tmp_path / "out.pdf"
    res = E.redact_pdf(src, out, settings, texts=["secret"], pages="1")
    assert res["pages"][0]["text_removed"] == 6
    assert text_of(out).strip() == "Shared"
    assert text_of(out, 1) == "Shared secret"
    with pikepdf.open(out) as pdf:
        assert list(pdf.pages[0].obj.Resources.XObject.keys()) == ["/RdFm1"]


def test_form_with_its_own_resources(tmp_path, settings):
    def res(pdf):
        inner = pikepdf.Dictionary(Font=pikepdf.Dictionary(F9=helvetica(pdf)),
                                   XObject=pikepdf.Dictionary(Im1=image(
                                       pdf, Image.new("L", (10, 10), 99))))
        return {"/XObject": {"/Fm1": form(
            pdf, b"BT /F9 12 Tf 0 0 Td (Shared secret) Tj ET q 20 0 0 10 0 0 cm /Im1 Do Q",
            Resources=inner)}}
    src = build(tmp_path / "in.pdf", b"q 1 0 0 1 100 200 cm /Fm1 Do Q", resources=res)
    out = tmp_path / "out.pdf"
    r = E.redact_pdf(src, out, settings, areas=[{"page": 1, "rect": [100, 195, 110, 215]}])
    assert r["pages"][0]["images_edited"] == 1
    with pikepdf.open(out) as pdf:
        copy = pdf.pages[0].obj.Resources.XObject["/RdFm1"]
        assert sorted(copy.Resources.XObject.keys()) == ["/RdIm1"]


def test_form_wholly_inside_is_removed(tmp_path, settings):
    src = build(tmp_path / "in.pdf", b"q 1 0 0 1 100 200 cm /Fm1 Do Q",
                resources=lambda pdf: {"/XObject": {"/Fm1": form(pdf)}})
    res = E.redact_pdf(src, tmp_path / "out.pdf", settings,
                       areas=[{"page": 1, "rect": [99, 194, 301, 221]}])
    assert res["pages"][0]["drawings_removed"] == 1
    assert text_of(tmp_path / "out.pdf") == ""


def test_form_without_bbox_or_with_a_matrix(tmp_path, settings):
    def res(pdf):
        plain = form(pdf)
        del plain["/BBox"]
        scaled = form(pdf, Matrix=pikepdf.Array([2, 0, 0, 2, 0, 0]))
        return {"/XObject": {"/Fm1": plain, "/Fm2": scaled}}
    src = build(tmp_path / "in.pdf", b"q 1 0 0 1 100 200 cm /Fm1 Do Q "
                b"q 1 0 0 1 100 400 cm /Fm2 Do Q", resources=res)
    res_ = E.redact_pdf(src, tmp_path / "out.pdf", settings, texts=["secret"])
    assert res_["match_count"] == 2 and res_["pages"][0]["text_removed"] == 12


def test_form_untouched_by_an_area_stays(tmp_path, settings):
    src = build(tmp_path / "in.pdf", b"q 1 0 0 1 100 200 cm /Fm1 Do Q " + LINE,
                resources=lambda pdf: {"/XObject": {"/Fm1": form(pdf)}})
    out = tmp_path / "out.pdf"
    E.redact_pdf(src, out, settings, texts=["John"])
    with pikepdf.open(out) as pdf:
        assert list(pdf.pages[0].obj.Resources.XObject.keys()) == ["/Fm1"]


def test_form_overlapping_but_with_nothing_under_it(tmp_path, settings):
    src = build(tmp_path / "in.pdf", b"q 1 0 0 1 100 200 cm /Fm1 Do Q",
                resources=lambda pdf: {"/XObject": {"/Fm1": form(pdf)}})
    out = tmp_path / "out.pdf"
    E.redact_pdf(src, out, settings, areas=[{"page": 1, "rect": [250, 210, 299, 219]}])
    with pikepdf.open(out) as pdf:
        assert list(pdf.pages[0].obj.Resources.XObject.keys()) == ["/Fm1"]


def test_forms_too_deep(tmp_path, settings, monkeypatch):
    from paperboy.engine import redact_content as C
    monkeypatch.setattr(C, "MAX_FORM_DEPTH", 0)
    src = build(tmp_path / "in.pdf", b"q 1 0 0 1 100 200 cm /Fm1 Do Q",
                resources=lambda pdf: {"/XObject": {"/Fm1": form(pdf)}})
    res = E.redact_pdf(src, tmp_path / "out.pdf", settings,
                       areas=[{"page": 1, "rect": [100, 200, 110, 210]}])
    assert res["pages"][0]["reason"] == "forms inside forms go too deep"


def test_form_drawing_itself(tmp_path, settings):
    def edit(pdf):
        fm = pdf.pages[0].obj.Resources.XObject.Fm1
        fm.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Fm1=fm))
        fm.write(b"/Fm1 Do")
    src = build(tmp_path / "in.pdf", b"q 1 0 0 1 100 200 cm /Fm1 Do Q " + LINE,
                resources=lambda pdf: {"/XObject": {"/Fm1": form(pdf)}}, edit=edit)
    res = E.redact_pdf(src, tmp_path / "out.pdf", settings,
                       areas=[{"page": 1, "rect": [100, 200, 110, 210]},
                              {"page": 1, "rect": [70, 690, 300, 715]}])
    assert res["pages"][0]["reason"] == "it has too much content to check"
    assert text_of(tmp_path / "out.pdf") == ""


def test_other_xobjects_and_missing_names(tmp_path, settings):
    def res(pdf):
        ps = pdf.make_stream(b"")
        ps.Type, ps.Subtype = Name.XObject, Name.PS
        return {"/XObject": {"/Ps1": ps, "/Bad": pikepdf.Dictionary()}}
    src = build(tmp_path / "in.pdf", b"/Ps1 Do /Nope Do /Bad Do " + LINE, resources=res)
    res_ = E.redact_pdf(src, tmp_path / "out.pdf", settings, texts=["John"])
    assert res_["pages"][0]["method"] == "edited"


# --- fonts and text ---------------------------------------------------------------------


def test_text_operators_and_spacing(tmp_path, settings):
    content = (b"BT /F1 10 Tf 12 TL 2 Tc 3 Tw 90 Tz 1 Ts 72 700 Td "
               b"[(Keep) -200 (Gone) 100 (Keep)] TJ "
               b"(Gone line) ' 1 1 (Gone too) \" 0 -14 TD (Gone) Tj T* (end) Tj "
               b"1 0 0 1 300 300 Tm (Gone) Tj ET")
    src = build(tmp_path / "in.pdf", content)
    out = tmp_path / "out.pdf"
    res = E.redact_pdf(src, out, settings, texts=["Gone"])
    assert res["match_count"] == 5 and res["pages"][0]["method"] == "edited"
    assert "Gone" not in text_of(out) and text_of(out).count("Keep") == 2
    assert "line" in text_of(out) and "too" in text_of(out) and "end" in text_of(out)


def test_rotated_text_on_a_rotated_page(tmp_path, settings):
    def edit(pdf):
        pdf.pages[0].obj.Rotate = 90
        pdf.pages[0].obj.MediaBox = [100, 100, 712, 892]
    src = build(tmp_path / "in.pdf",
                b"q 0 1 -1 0 400 300 cm BT /F1 12 Tf 0 0 Td (Rotated secret here) Tj ET Q",
                edit=edit)
    out = tmp_path / "out.pdf"
    res = E.redact_pdf(src, out, settings, texts=["secret"])
    assert res["pages"][0]["method"] == "edited"
    assert text_of(out).split() == ["Rotated", "here"]


def test_text_state_from_graphics_state(tmp_path, settings):
    def res(pdf):
        return {"/ExtGState": {"/G1": pikepdf.Dictionary(
            Font=pikepdf.Array([helvetica(pdf), 12]), LW=2)}}
    src = build(tmp_path / "in.pdf", b"/G1 gs /Nope gs BT 72 700 Td (Some secret) Tj ET",
                resources=res)
    out = tmp_path / "out.pdf"
    E.redact_pdf(src, out, settings, texts=["secret"])
    assert text_of(out).strip() == "Some"


def test_standard_fonts_with_encodings(tmp_path, settings):
    def res(pdf):
        diff = pikepdf.Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name("/Arial,Bold"),
                                  Encoding=pikepdf.Dictionary(
                                      BaseEncoding=Name.WinAnsiEncoding,
                                      Differences=pikepdf.Array([65, Name.B, Name.A])))
        times = pikepdf.Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                   BaseFont=Name("/ABCDEF+TimesNewRomanPS-ItalicMT"),
                                   Encoding=Name.MacRomanEncoding)
        symbol = pikepdf.Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Symbol)
        return {"/Font": {"/F2": pdf.make_indirect(diff), "/F3": times, "/F4": symbol}}
    content = (b"BT /F2 12 Tf 72 700 Td (Alpha secret one) Tj ET "
               b"BT /F3 12 Tf 72 650 Td (Beta secret two) Tj ET "
               b"BT /F4 12 Tf 72 600 Td (abc) Tj ET")
    src = build(tmp_path / "in.pdf", content, resources=res)
    out = tmp_path / "out.pdf"
    r = E.redact_pdf(src, out, settings, texts=["secret"])
    assert r["pages"][0]["method"] == "edited" and r["match_count"] == 2


@pytest.mark.parametrize(("font", "reason"), [
    ({"BaseFont": Name("/SomeEmbedded"), "FontDescriptor": pikepdf.Dictionary(
        FontFile2=pikepdf.Dictionary())}, "a font has no widths"),
    ({"BaseFont": Name("/Unknown")}, "a font has no widths"),
    ({"BaseFont": Name("/ArialNarrow")}, "a font has no widths"),
    ({"BaseFont": Name.Helvetica, "Encoding": Name("/OddEncoding")},
     "a font uses an unknown encoding (OddEncoding)"),
])
def test_fonts_that_cant_be_followed(tmp_path, settings, font, reason):
    def res(pdf):
        return {"/Font": {"/F2": pikepdf.Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                                    **font)}}
    src = build(tmp_path / "in.pdf", b"BT /F2 12 Tf 72 700 Td (A secret) Tj ET", resources=res)
    out = tmp_path / "out.pdf"
    r = E.redact_pdf(src, out, settings, texts=["secret"])
    assert r["pages"][0]["method"] == "rasterized" and r["pages"][0]["reason"] == reason
    assert text_of(out) == ""
    with pytest.raises(PdfUserError, match=f"Page 1 can't be redacted while keeping its text: "
                                           f"{reason[:10]}"):
        E.redact_pdf(src, tmp_path / "never.pdf", settings, texts=["secret"],
                     rasterize="never")
    assert not (tmp_path / "never.pdf").exists()


def test_missing_font(tmp_path, settings):
    src = build(tmp_path / "in.pdf", b"BT 72 700 Td (A secret) Tj ET " + LINE)
    r = E.redact_pdf(src, tmp_path / "out.pdf", settings, texts=["John"])
    assert r["pages"][0]["reason"] == "text uses a font that is missing"


def test_widths_and_missing_width(tmp_path, settings):
    def res(pdf):
        widths = pikepdf.Array([500] * 95)
        return {"/Font": {"/F2": pikepdf.Dictionary(
            Type=Name.Font, Subtype=Name.TrueType, BaseFont=Name("/Mono"), FirstChar=32,
            Widths=widths, FontDescriptor=pikepdf.Dictionary(
                MissingWidth=500, Ascent=800, Descent=-200))}}
    src = build(tmp_path / "in.pdf", b"BT /F2 12 Tf 72 700 Td (keep secret\xe9keep) Tj ET",
                resources=res)
    r = E.redact_pdf(src, tmp_path / "out.pdf", settings,
                     areas=[{"page": 1, "rect": [72 + 6 * 5 - 1, 690, 72 + 6 * 11 + 1, 715]}])
    assert r["pages"][0]["text_removed"] == 6


IDENTITY_H = Name("/Identity-H")


def type0(pdf, encoding=IDENTITY_H, widths=None):
    cid = pikepdf.Dictionary(Type=Name.Font, Subtype=Name.CIDFontType2,
                             BaseFont=Name("/Mono"), DW=600,
                             CIDSystemInfo=pikepdf.Dictionary(
                                 Registry=pikepdf.String("Adobe"),
                                 Ordering=pikepdf.String("Identity"), Supplement=0),
                             FontDescriptor=pikepdf.Dictionary(
                                 Type=Name.FontDescriptor, FontName=Name("/Mono"), Flags=32,
                                 FontBBox=[0, -200, 600, 800], ItalicAngle=0, Ascent=800,
                                 Descent=-200, CapHeight=700, StemV=80))
    if widths is not None:
        cid.W = widths
    unicode = pdf.make_stream(
        b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap "
        b"1 begincodespacerange <0000> <FFFF> endcodespacerange "
        b"1 beginbfrange <0000> <00FF> <0000> endbfrange endcmap "
        b"CMapName currentdict /CMap defineresource pop end end")
    return pdf.make_indirect(pikepdf.Dictionary(
        Type=Name.Font, Subtype=Name.Type0, BaseFont=Name("/Mono"), Encoding=encoding,
        DescendantFonts=pikepdf.Array([cid]), ToUnicode=unicode))


def utf16(text: str) -> bytes:
    return text.encode("utf-16-be").hex().encode()


def test_composite_font(tmp_path, settings):
    def res(pdf):
        return {"/Font": {"/F2": type0(pdf, widths=pikepdf.Array(
            [32, [600, 600], 65, 90, 600, 97, [600] * 26]))}}
    content = b"BT /F2 10 Tf 72 700 Td <" + utf16("keep SECRET keep") + b"> Tj ET"
    src = build(tmp_path / "in.pdf", content, resources=res)
    out = tmp_path / "out.pdf"
    r = E.redact_pdf(src, out, settings, texts=["SECRET"])
    assert r["pages"][0]["method"] == "edited" and r["pages"][0]["text_removed"] == 6
    assert text_of(out).split() == ["keep", "keep"]


def test_composite_font_with_a_cmap(tmp_path, settings):
    def res(pdf):
        cmap = pdf.make_stream(
            b"/CIDInit /ProcSet findresource begin begincmap "
            b"2 begincodespacerange <00> <7F> <8000> <FFFF> endcodespacerange "
            b"1 begincidrange <20> <7F> 32 endcidrange "
            b"1 begincidchar <8001> 65 endcidchar endcmap end")
        return {"/Font": {"/F2": type0(pdf, encoding=cmap)}}
    content = b"BT /F2 10 Tf 72 700 Td (keep SECRET keep) Tj ET"
    src = build(tmp_path / "in.pdf", content, resources=res)
    r = E.redact_pdf(src, tmp_path / "out.pdf", settings,
                     areas=[{"page": 1, "rect": [72 + 30 - 1, 690, 72 + 66 + 1, 712]}])
    assert r["pages"][0]["method"] == "edited" and r["pages"][0]["text_removed"] == 6


@pytest.mark.parametrize(("encoding", "reason"), [
    (Name("/Identity-V"), "an encoding this can't follow (Identity-V)"),
    (None, "an encoding this can't follow (?)"),
    (b"usecmap", "vertical writing or a CMap built on another"),
    (b"begincodespacerange endcodespacerange", "has no code space"),
    (b"begincodespacerange <0> <FFF> endcodespacerange", "CMap is damaged"),
    (b"1 begincodespacerange <80> <FF> endcodespacerange", "outside its font's code space"),
])
def test_composite_fonts_that_cant_be_followed(tmp_path, settings, encoding, reason):
    def res(pdf):
        enc = pdf.make_stream(encoding) if isinstance(encoding, bytes) else encoding
        font = type0(pdf, encoding=enc if enc is not None else Name("/Identity-H"))
        if enc is None:
            del font["/Encoding"]
        return {"/Font": {"/F2": font}}
    src = build(tmp_path / "in.pdf", b"BT /F2 10 Tf 72 700 Td (ab) Tj ET " + LINE,
                resources=res)
    r = E.redact_pdf(src, tmp_path / "out.pdf", settings, texts=["John"])
    assert reason in r["pages"][0]["reason"]


def test_type3_font(tmp_path, settings):
    def res(pdf):
        procs = pikepdf.Dictionary({f"/g{c}": pdf.make_stream(b"500 0 d0 0 0 400 600 re f")
                                    for c in "abc"})
        t3 = pikepdf.Dictionary(
            Type=Name.Font, Subtype=Name.Type3, FontMatrix=[0.001, 0, 0, 0.001, 0, 0],
            FontBBox=[0, 0, 500, 700], FirstChar=97, LastChar=99, Widths=[500, 500, 500],
            Encoding=pikepdf.Dictionary(Differences=[97, Name.ga, Name.gb, Name.gc]),
            CharProcs=procs, Resources=pikepdf.Dictionary())
        return {"/Font": {"/T3": t3}}
    src = build(tmp_path / "in.pdf", b"BT /T3 20 Tf 100 700 Td (abcabc) Tj ET", resources=res)
    r = E.redact_pdf(src, tmp_path / "out.pdf", settings,
                     areas=[{"page": 1, "rect": [130.5, 690, 159.5, 720]}])
    assert r["pages"][0]["text_removed"] == 3
    reaching = E.redact_pdf(src, tmp_path / "reach.pdf", settings,
                            areas=[{"page": 1, "rect": [129, 690, 161, 720]}])
    assert reaching["pages"][0]["reason"] == "a Type 3 character is partly under an area"


def test_rotated_type3_font(tmp_path, settings):
    def res(pdf):
        return {"/Font": {"/T3": pikepdf.Dictionary(
            Type=Name.Font, Subtype=Name.Type3, FontMatrix=[0.001, 0.001, 0, 0.001, 0, 0],
            FontBBox=[0, 0, 500, 700], FirstChar=97, LastChar=97, Widths=[500],
            CharProcs=pikepdf.Dictionary())}}
    src = build(tmp_path / "in.pdf", b"BT /T3 20 Tf 100 700 Td (a) Tj ET " + LINE,
                resources=res)
    r = E.redact_pdf(src, tmp_path / "out.pdf", settings, texts=["John"])
    assert r["pages"][0]["reason"] == "a Type 3 font is rotated or slanted"


@pytest.mark.parametrize(("content", "reason"), [
    (b"BT /F1 12 Tf 7 Tr 72 700 Td (Clip John) Tj ET", "text that clips other content"),
    (b"BT /F1 12 Tf 0 Tz 72 700 Td (John) Tj ET", "text of size zero"),
    (b"1 0 0 cm " + LINE, "invalid matrix"),
    (b"/x Tc " + LINE, "invalid number"),
    (b"BT (a) (b) \" ET " + LINE, "invalid text operator"),
])
def test_content_that_cant_be_followed(tmp_path, settings, content, reason):
    src = build(tmp_path / "in.pdf", content + b" " + LINE)
    r = E.redact_pdf(src, tmp_path / "out.pdf", settings,
                     areas=[{"page": 1, "rect": [70, 690, 110, 715]}])
    assert reason in r["pages"][0]["reason"]


def test_damaged_content(tmp_path, settings, monkeypatch):
    from paperboy.engine import redact_content as C

    def broken(*args):
        raise TypeError("no")
    src = build(tmp_path / "in.pdf", LINE)
    monkeypatch.setattr(C.pikepdf, "parse_content_stream", broken)
    monkeypatch.setattr(R, "_content_size", lambda *args: 0)
    r = E.redact_pdf(src, tmp_path / "out.pdf", settings,
                     areas=[{"page": 1, "rect": [70, 690, 110, 715]}])
    assert r["pages"][0]["reason"] == "its content is damaged"


def test_odd_operands_are_kept(tmp_path, settings):
    src = build(tmp_path / "in.pdf", b"BT /F1 12 Tf 72 700 Td [(John) /x (Smith)] TJ ET")
    out = tmp_path / "out.pdf"
    r = E.redact_pdf(src, out, settings, texts=["Smith"])
    assert r["pages"][0]["method"] == "edited" and "John" in text_of(out)


def test_unbalanced_content_still_gets_its_box(tmp_path, settings):
    src = build(tmp_path / "in.pdf", b"Q Q q 2 0 0 2 0 0 cm BT /F1 12 Tf 36 350 Td (John) Tj")
    out = tmp_path / "out.pdf"
    E.redact_pdf(src, out, settings, texts=["John"])
    with pikepdf.open(out) as pdf:
        data = pdf.pages[0].obj.Contents.read_bytes()
    assert data.startswith(b"q\n") and b"ET\nQ\nQ\nq 0 g" in data
    assert b"Q\nQ\nq\n2 0 0 2" not in data  # the unmatched Qs are gone


# --- marked content and structure -------------------------------------------------------


def test_actual_text_is_dropped(tmp_path, settings):
    def res(pdf):
        return {"/Properties": {"/P1": pdf.make_indirect(pikepdf.Dictionary(
            ActualText=pikepdf.String("Smith"), Lang=pikepdf.String("en")))}}
    content = (b"/Span <</ActualText (John) /MCID 0>> BDC BT /F1 12 Tf 72 700 Td (John) Tj "
               b"ET EMC /Span /P1 BDC BT /F1 12 Tf 72 650 Td (Smith) Tj ET EMC "
               b"/Span <</ActualText (kept)>> BDC BT /F1 12 Tf 72 600 Td (kept) Tj ET EMC "
               b"/Artifact BMC EMC /Span /Nope BDC BT /F1 12 Tf 72 550 Td (Smith) Tj ET EMC "
               b"/Span <</MCID 5>> BDC BT /F1 12 Tf 72 500 Td (Smith) Tj ET")
    src = build(tmp_path / "in.pdf", content, resources=res)
    out = tmp_path / "out.pdf"
    E.redact_pdf(src, out, settings, texts=["John", "Smith"])
    with pikepdf.open(out) as pdf:
        data = pdf.pages[0].obj.Contents.read_bytes()
        props = pdf.pages[0].obj.Resources.Properties
        assert b"(John)" not in data and b"/ActualText (kept)" in data
        assert b"/MCID 0" in data
        assert "/ActualText" not in props.RdP1 and props.RdP1.Lang == "en"
        assert "/P1" not in props   # the original, with its ActualText, is gone


def test_actual_text_partly_redacted_makes_a_picture_page(tmp_path, settings):
    content = (b"/Span <</ActualText (John Smith)>> BDC BT /F1 12 Tf 72 700 Td (JS) Tj "
               b"( and more) Tj ET EMC")
    src = build(tmp_path / "in.pdf", content)
    r = E.redact_pdf(src, tmp_path / "out.pdf", settings,
                     areas=[{"page": 1, "rect": [70, 690, 80, 715]}])
    assert r["pages"][0]["reason"] == ("text with replacement text (ActualText) is only "
                                       "partly under an area")


def test_alt_text_of_a_partly_redacted_figure_goes(tmp_path, settings):
    content = (b"/Figure <</Alt (Chart)>> BDC q 100 0 0 100 100 100 cm /Im1 Do Q "
               b"/Fm1 Do EMC")
    src = build(tmp_path / "in.pdf", content, resources=lambda pdf: {"/XObject": {
        "/Im1": image(pdf, Image.new("L", (10, 10), 50)), "/Fm1": form(pdf)}})
    out = tmp_path / "out.pdf"
    r = E.redact_pdf(src, out, settings, areas=[{"page": 1, "rect": [100, 100, 150, 150]}])
    assert r["pages"][0]["method"] == "edited"
    with pikepdf.open(out) as pdf:
        assert b"/Alt" not in pdf.pages[0].obj.Contents.read_bytes()


def test_structure_alt_text_is_dropped(tmp_path, settings):
    def edit(pdf):
        page = pdf.pages[0].obj
        figure = pdf.make_indirect(pikepdf.Dictionary(
            S=Name.Figure, Pg=page, K=0, Alt=pikepdf.String("Photo of John")))
        other = pdf.make_indirect(pikepdf.Dictionary(
            S=Name.Figure, Pg=page, K=pikepdf.Array([pikepdf.Dictionary(
                Type=Name.MCR, MCID=1, Pg=page)]), Alt=pikepdf.String("Logo")))
        root = pdf.make_indirect(pikepdf.Dictionary(
            Type=Name.StructTreeRoot, K=pikepdf.Array([figure, other, figure])))
        figure.P = other.P = root
        pdf.Root.StructTreeRoot = root
    content = (b"/Figure <</MCID 0>> BDC q 1 0 0 rg 100 100 20 20 re f Q EMC "
               b"/Figure <</MCID 1>> BDC q 0 0 1 rg 300 300 20 20 re f Q EMC")
    src = build(tmp_path / "in.pdf", content, edit=edit)
    out = tmp_path / "out.pdf"
    r = E.redact_pdf(src, out, settings, areas=[{"page": 1, "rect": [90, 90, 130, 130]}])
    assert r["alt_text_removed"] == 1
    with pikepdf.open(out) as pdf:
        kids = pdf.Root.StructTreeRoot.K
        assert "/Alt" not in kids[0] and kids[1].Alt == "Logo"


def test_rasterized_page_loses_its_alt_text(tmp_path, settings):
    def edit(pdf):
        figure = pdf.make_indirect(pikepdf.Dictionary(
            S=Name.Figure, Pg=pdf.pages[0].obj, K=pikepdf.Array([3]),
            ActualText=pikepdf.String("x")))
        pdf.Root.StructTreeRoot = pikepdf.Dictionary(Type=Name.StructTreeRoot, K=figure)
    src = build(tmp_path / "in.pdf", LINE, edit=edit)
    r = E.redact_pdf(src, tmp_path / "out.pdf", settings, texts=["John"],
                     rasterize="always")
    assert r["alt_text_removed"] == 1


def test_damaged_structure_is_left(tmp_path, settings):
    def edit(pdf):
        pdf.Root.StructTreeRoot = pikepdf.Dictionary(K=pikepdf.Array([5, pikepdf.Dictionary(
            S=Name.P, K=pikepdf.Dictionary(Type=Name.MCR, MCID=0), Alt=pikepdf.String("a"))]))
    src = build(tmp_path / "in.pdf", LINE, edit=edit)
    r = E.redact_pdf(src, tmp_path / "out.pdf", settings, texts=["John"])
    assert r["alt_text_removed"] == 0


# --- annotations and fields -------------------------------------------------------------


def annotations(pdf):
    page = pdf.pages[0].obj
    note = pdf.make_indirect(pikepdf.Dictionary(
        Type=Name.Annot, Subtype=Name.Text, Rect=[100, 700, 120, 720],
        Contents=pikepdf.String("about John")))
    popup = pdf.make_indirect(pikepdf.Dictionary(
        Type=Name.Annot, Subtype=Name.Popup, Rect=[300, 300, 400, 400], Parent=note))
    note.Popup = popup
    reply = pdf.make_indirect(pikepdf.Dictionary(
        Type=Name.Annot, Subtype=Name.Text, Rect=[500, 500, 520, 520], IRT=note,
        Contents=pikepdf.String("reply")))
    link = pdf.make_indirect(pikepdf.Dictionary(
        Type=Name.Annot, Subtype=Name.Link, Rect=[10, 10, 50, 50]))
    field = pdf.make_indirect(pikepdf.Dictionary(T=pikepdf.String("name"), FT=Name.Tx,
                                                 V=pikepdf.String("John Smith")))
    widget = pdf.make_indirect(pikepdf.Dictionary(
        Type=Name.Annot, Subtype=Name.Widget, Rect=[100, 600, 200, 620], Parent=field, P=page))
    second = pdf.make_indirect(pikepdf.Dictionary(
        Type=Name.Annot, Subtype=Name.Widget, Rect=[100, 100, 200, 120], Parent=field))
    field.Kids = pikepdf.Array([widget, second])
    merged = pdf.make_indirect(pikepdf.Dictionary(
        Type=Name.Annot, Subtype=Name.Widget, Rect=[300, 600, 400, 620],
        T=pikepdf.String("kept"), FT=Name.Tx, V=pikepdf.String("value")))
    page.Annots = pikepdf.Array([note, popup, reply, link, widget, second, merged,
                                 pikepdf.Dictionary(Subtype=Name.Square), 5])
    pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array([field, merged]))


def test_annotations_touching_an_area_are_removed(tmp_path, settings):
    src = build(tmp_path / "in.pdf", LINE, edit=annotations)
    out = tmp_path / "out.pdf"
    r = E.redact_pdf(src, out, settings, areas=[{"page": 1, "rect": [95, 595, 125, 725]}])
    assert r["pages"][0]["annotations_removed"] == 4   # note, reply, both widgets
    with pikepdf.open(out) as pdf:
        kinds = [str(a.get("/Subtype")) for a in pdf.pages[0].obj.Annots
                 if isinstance(a, pikepdf.Dictionary)]
        assert kinds == ["/Link", "/Widget", "/Square"]
        assert [str(f.T) for f in pdf.Root.AcroForm.Fields] == ["kept"]


def test_field_in_a_hierarchy(tmp_path, settings):
    def edit(pdf):
        parent = pdf.make_indirect(pikepdf.Dictionary(T=pikepdf.String("person")))
        child = pdf.make_indirect(pikepdf.Dictionary(
            Type=Name.Annot, Subtype=Name.Widget, Rect=[100, 600, 200, 620],
            T=pikepdf.String("name"), FT=Name.Tx, Parent=parent))
        keep = pdf.make_indirect(pikepdf.Dictionary(
            Type=Name.Annot, Subtype=Name.Widget, Rect=[300, 600, 400, 620],
            T=pikepdf.String("age"), FT=Name.Tx, Parent=parent))
        parent.Kids = pikepdf.Array([child, keep])
        pdf.pages[0].obj.Annots = pikepdf.Array([child, keep])
        pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array([parent]))
    src = build(tmp_path / "in.pdf", LINE, edit=edit)
    out = tmp_path / "out.pdf"
    E.redact_pdf(src, out, settings, areas=[{"page": 1, "rect": [95, 595, 125, 625]}])
    with pikepdf.open(out) as pdf:
        assert [str(k.T) for k in pdf.Root.AcroForm.Fields[0].Kids] == ["age"]


def test_widget_without_a_form(tmp_path, settings):
    def edit(pdf):
        pdf.pages[0].obj.Annots = pikepdf.Array([pdf.make_indirect(pikepdf.Dictionary(
            Type=Name.Annot, Subtype=Name.Widget, Rect=[100, 600, 200, 620],
            T=pikepdf.String("x")))])
    src = build(tmp_path / "in.pdf", LINE, edit=edit)
    r = E.redact_pdf(src, tmp_path / "out.pdf", settings,
                     areas=[{"page": 1, "rect": [95, 595, 125, 625]}])
    assert r["pages"][0]["annotations_removed"] == 1


# --- text elsewhere in the document -----------------------------------------------------


def elsewhere(pdf):
    with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
        meta["dc:title"] = "Letter to John Smith"
    pdf.trailer.Info = pdf.make_indirect(pikepdf.Dictionary(
        Title=pikepdf.String("Letter to John Smith"), Author=pikepdf.String("someone")))
    outline = pdf.make_indirect(pikepdf.Dictionary(Title=pikepdf.String("John"),
                                                   Parent=pdf.Root))
    pdf.Root.Outlines = pikepdf.Dictionary(First=outline, Last=outline, Count=1)
    comment = pdf.make_indirect(pikepdf.Dictionary(
        Type=Name.Annot, Subtype=Name.FreeText, Rect=[400, 50, 500, 80],
        Contents=pikepdf.String("Ask JOHN"), AP=pikepdf.Dictionary(N=pdf.make_stream(b""))))
    field = pdf.make_indirect(pikepdf.Dictionary(
        Type=Name.Annot, Subtype=Name.Widget, Rect=[400, 100, 500, 120],
        T=pikepdf.String("to"), FT=Name.Ch, V=pikepdf.String("john   smith"),
        Opt=pikepdf.Array([pikepdf.Array([pikepdf.String("j"), pikepdf.String("John")])]),
        AP=pikepdf.Dictionary(N=pdf.make_stream(b""))))
    palette = pikepdf.Array([Name.Indexed, Name.DeviceRGB, 0, pikepdf.String("John")])
    pdf.pages[0].obj.Resources.ColorSpace = pikepdf.Dictionary(CS0=palette)
    pdf.pages[0].obj.Annots = pikepdf.Array([comment, field])
    pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array([field]))
    pdf.Root.Extra = pikepdf.Dictionary(Note=pikepdf.String("John"), ID=pikepdf.String("John"))
    attached = pdf.make_stream(b"John Smith's file")
    pdf.Root.Names = pikepdf.Dictionary(EmbeddedFiles=pikepdf.Dictionary(Names=pikepdf.Array(
        [pikepdf.String("a.txt"), pikepdf.Dictionary(Type=Name.Filespec,
                                                     F=pikepdf.String("a.txt"),
                                                     EF=pikepdf.Dictionary(F=attached))])))


def test_text_elsewhere_is_replaced(tmp_path, settings):
    src = build(tmp_path / "in.pdf", LINE, edit=elsewhere)
    out = tmp_path / "out.pdf"
    r = E.redact_pdf(src, out, settings, texts=["John"])
    assert r["also_redacted_in"] == ["XMP metadata", "bookmarks", "comments",
                                     "document info", "form fields"]
    assert r["attachments_not_searched"] == 1
    with pikepdf.open(out) as pdf:
        assert str(pdf.trailer.Info.Title) == "Letter to [redacted] Smith"
        assert "/Metadata" not in pdf.Root
        assert str(pdf.Root.Outlines.First.Title) == "[redacted]"
        comment, field = pdf.pages[0].obj.Annots
        assert str(comment.Contents) == "Ask [redacted]" and "/AP" not in comment
        assert str(field.V) == "[redacted]   smith" and "/AP" not in field
        assert str(field.Opt[0][1]) == "[redacted]"
        assert pdf.Root.AcroForm.NeedAppearances is True
        # Only keys known to hold text are changed; others may be identifiers.
        assert str(pdf.Root.Extra.Note) == "John" and str(pdf.Root.Extra.ID) == "John"
        assert bytes(pdf.pages[0].obj.Resources.ColorSpace.CS0[3]) == b"John"


def test_phrases_and_patterns_elsewhere(tmp_path, settings):
    src = build(tmp_path / "in.pdf", LINE, edit=elsewhere)
    out = tmp_path / "out.pdf"
    r = E.redact_pdf(src, out, settings, texts=["John Smith"], whole_word=True,
                     patterns=["some(one|thing)"])
    assert "XMP metadata" in r["also_redacted_in"]
    with pikepdf.open(out) as pdf:
        assert str(pdf.trailer.Info.Author) == "[redacted]"
        assert str(pdf.pages[0].obj.Annots[1].V) == "[redacted]"


def test_patterns_reach_xmp(tmp_path, settings):
    def edit(pdf):
        with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
            meta["dc:description"] = "id 12-34"
    src = build(tmp_path / "in.pdf", b"BT /F1 12 Tf 72 700 Td (id 12-34) Tj ET", edit=edit)
    out = tmp_path / "out.pdf"
    r = E.redact_pdf(src, out, settings, patterns=[r"\d\d-\d\d"])
    assert "XMP metadata" in r["also_redacted_in"]


def test_unreadable_xmp_is_removed(tmp_path, settings):
    def edit(pdf):
        meta = pdf.make_stream(b"\x00")
        meta.Filter = Name("/Bogus")
        pdf.Root.Metadata = meta
    src = build(tmp_path / "in.pdf", LINE, edit=edit)
    out = tmp_path / "out.pdf"
    E.redact_pdf(src, out, settings, texts=["John"])
    with pikepdf.open(out) as pdf:
        assert "/Metadata" not in pdf.Root


def test_areas_alone_leave_strings(tmp_path, settings):
    src = build(tmp_path / "in.pdf", LINE, edit=elsewhere)
    out = tmp_path / "out.pdf"
    r = E.redact_pdf(src, out, settings, areas=[{"page": 1, "rect": [70, 690, 110, 715]}])
    assert r["also_redacted_in"] == [] and r["attachments_not_searched"] == 0
    with pikepdf.open(out) as pdf:
        assert "John" in str(pdf.trailer.Info.Title)


def test_too_much_text_outside_pages(tmp_path, settings, monkeypatch):
    src = build(tmp_path / "in.pdf", LINE, edit=elsewhere)
    real = R._run_patterns
    monkeypatch.setattr(R, "_run_patterns", lambda *a: (
        monkeypatch.setattr(R, "MAX_PATTERN_TEXT", 10), real(*a))[1])
    with pytest.raises(PdfUserError, match="too much text outside its pages"):
        E.redact_pdf(src, tmp_path / "out.pdf", settings, patterns=["John"])


def test_thumbnails_and_editor_data_go(tmp_path, settings):
    def edit(pdf):
        pdf.pages[0].obj.Thumb = pdf.make_stream(b"x")
        pdf.pages[0].obj.PieceInfo = pikepdf.Dictionary(Illustrator=pikepdf.Dictionary())
        pdf.pages[1].obj.Thumb = pdf.make_stream(b"x")
    src = build(tmp_path / "in.pdf", LINE, LINE, edit=edit)
    out = tmp_path / "out.pdf"
    E.redact_pdf(src, out, settings, texts=["John"], pages="1")
    with pikepdf.open(out) as pdf:
        assert "/Thumb" not in pdf.pages[0].obj and "/PieceInfo" not in pdf.pages[0].obj
        assert "/Thumb" in pdf.pages[1].obj


# --- checks and the fallback ------------------------------------------------------------


def test_failed_check_makes_a_picture_page(letter, tmp_path, settings, monkeypatch):
    monkeypatch.setattr(R, "_verify", lambda before, after, areas: "it looked wrong")
    out = tmp_path / "out.pdf"
    r = E.redact_pdf(letter, out, settings, texts=["John"])
    assert r["pages"][0] == {"page": 1, "areas": 1, "method": "rasterized",
                             "reason": "it looked wrong", "text_removed": 0,
                             "images_edited": 0, "images_removed": 0,
                             "drawings_removed": 0, "annotations_removed": 0}
    with pytest.raises(PdfUserError, match="it looked wrong"):
        E.redact_pdf(letter, tmp_path / "never.pdf", settings, texts=["John"],
                     rasterize="never")


def test_verify():
    area = [(10, 10, 20, 20)]
    a, b, c = (65, 5, 5), (66, 15, 15), (67, 30, 30)
    assert R._verify([a, b, c], [a, c], area) is None
    assert R._verify([a, b, c], [a, b, c], area) == "text was still found under an area"
    assert R._verify([a, b, c], [a], area) == "text outside the areas would have changed"
    assert R._verify([a, c], [a, c, (68, 40, 40)], area) == \
        "text outside the areas would have changed"
    edge = (69, 10.5, 15)   # within the tolerance of the edge: may go either way
    assert R._verify([a, edge, c], [a, c], area) is None
    assert R._verify([a, edge, c], [a, edge, c], area) is None
    assert R._verify([a], [a], [(0, 0, 1, 1)]) is None   # a tiny area has no inside


def test_picture_page(tmp_path, settings):
    def edit(pdf):
        page = pdf.pages[0].obj
        page.Rotate = 90
        page.CropBox = [50, 50, 562, 742]
        page.Annots = pikepdf.Array([pdf.make_indirect(pikepdf.Dictionary(
            Type=Name.Annot, Subtype=Name.Link, Rect=[300, 300, 320, 320]))])
        page.Group = pikepdf.Dictionary(S=Name.Transparency)
    src = build(tmp_path / "in.pdf", LINE, edit=edit)
    out = tmp_path / "out.pdf"
    r = E.redact_pdf(src, out, settings, texts=["John"], rasterize="always", dpi=72)
    assert r["pages"][0]["method"] == "rasterized"
    assert r["pages"][0]["reason"] == 'rasterize was "always"'
    with pikepdf.open(out) as pdf:
        page = pdf.pages[0].obj
        assert [float(v) for v in page.MediaBox] == [50, 50, 562, 742]
        assert page.Rotate == 90 and len(page.Annots) == 1 and "/Group" not in page
        picture = pikepdf.PdfImage(page.Resources.XObject.RdPage).as_pil_image()
        assert picture.size == (512, 692)
        # "John" is at about x 98-125, y 697-711: pixels x 48-75, y 31-45 from the top.
        assert picture.getpixel((60, 38)) == (0, 0, 0)
    assert text_of(out) == ""
    white = tmp_path / "white.pdf"
    E.redact_pdf(src, white, settings, texts=["John"], rasterize="always", dpi=72,
                 fill="none")
    with pikepdf.open(white) as pdf:
        picture = pikepdf.PdfImage(pdf.pages[0].obj.Resources.XObject.RdPage).as_pil_image()
        assert picture.getpixel((60, 38)) == (255, 255, 255)


def test_picture_pages_have_a_pixel_limit(letter, tmp_path, settings, monkeypatch):
    monkeypatch.setattr(R, "MAX_TOTAL_PIXELS", 1000)
    with pytest.raises(PdfUserError, match="pixel limit"):
        E.redact_pdf(letter, tmp_path / "out.pdf", settings, texts=["John"],
                     rasterize="always")


def test_large_pages_are_rendered_smaller(letter, tmp_path, settings, monkeypatch):
    monkeypatch.setattr(R, "MAX_PIXELS", 10_000)
    E.redact_pdf(letter, tmp_path / "out.pdf", settings, texts=["John"], rasterize="always",
                 dpi=300)
    with pikepdf.open(tmp_path / "out.pdf") as pdf:
        assert pdf.pages[0].obj.Resources.XObject.RdPage.Width < 100


def test_page_without_a_visible_area(tmp_path, settings):
    def edit(pdf):
        pdf.pages[0].obj.CropBox = [700, 700, 800, 800]
    src = build(tmp_path / "in.pdf", LINE, edit=edit)
    with pytest.raises(PdfUserError, match="no visible area"):
        E.redact_pdf(src, tmp_path / "out.pdf", settings,
                     areas=[{"page": 1, "rect": [0, 0, 10, 10]}], rasterize="always")


def test_text_left_on_a_picture_page_is_refused(letter, tmp_path, settings, monkeypatch):
    monkeypatch.setattr(R, "_rasterize", lambda *args: (0, set()))
    with pytest.raises(PdfUserError, match="could not be confirmed"):
        E.redact_pdf(letter, tmp_path / "out.pdf", settings, texts=["John"],
                     rasterize="always")
    assert not (tmp_path / "out.pdf").exists()


def test_heavy_pages(letter, tmp_path, settings, monkeypatch):
    monkeypatch.setattr(R, "MAX_PAGE_CONTENT", 10)
    with pytest.raises(PdfUserError, match="Pages 1, 2 have too much content to search"):
        E.redact_pdf(letter, tmp_path / "out.pdf", settings, texts=["John"])
    r = E.redact_pdf(letter, tmp_path / "out.pdf", settings,
                     areas=[{"page": 1, "rect": [70, 690, 110, 715]}])
    assert r["pages"][0]["reason"] == "it has too much content to check"


def test_many_heavy_pages_are_listed_briefly(tmp_path, settings, monkeypatch):
    src = build(tmp_path / "in.pdf", *[LINE] * 22)
    monkeypatch.setattr(R, "MAX_PAGE_CONTENT", 10)
    with pytest.raises(PdfUserError, match=r"Pages 1, 2, .*, 20 … have too much"):
        E.redact_pdf(src, tmp_path / "out.pdf", settings, texts=["John"])


def test_content_budgets(letter, tmp_path, settings, monkeypatch):
    from paperboy.engine import redact_content as C
    for name, reason in (("MAX_CONTENT", "too much content"),
                         ("MAX_STREAM_TOKENS", "too much content"),
                         ("MAX_OPS", "too much content"), ("MAX_GLYPHS", "too much text")):
        with monkeypatch.context() as patch:
            patch.setattr(C, name, 3)
            r = E.redact_pdf(letter, tmp_path / f"{name}.pdf", settings, texts=["John"])
        assert reason in r["pages"][0]["reason"], name


def test_undecodable_content(tmp_path, settings):
    def edit(pdf):
        pdf.pages[0].obj.Contents.write(b"\x00", filter=Name("/Bogus"))
    src = build(tmp_path / "in.pdf", LINE, edit=edit)
    r = E.redact_pdf(src, tmp_path / "out.pdf", settings,
                     areas=[{"page": 1, "rect": [0, 0, 10, 10]}])
    assert r["pages"][0]["reason"] == "it has too much content to check"


def test_damaged_contents_entry(tmp_path, settings):
    def edit(pdf):
        pdf.pages[0].obj.Contents = pikepdf.Array([pdf.make_stream(LINE), 5])
    src = build(tmp_path / "in.pdf", LINE, edit=edit)
    r = E.redact_pdf(src, tmp_path / "out.pdf", settings,
                     areas=[{"page": 1, "rect": [70, 690, 110, 715]}])
    assert r["pages"][0]["method"] == "rasterized"


def test_page_without_content(tmp_path, settings):
    def edit(pdf):
        del pdf.pages[0].obj["/Contents"]
    src = build(tmp_path / "in.pdf", LINE, edit=edit)
    out = tmp_path / "out.pdf"
    r = E.redact_pdf(src, out, settings, areas=[{"page": 1, "rect": [0, 0, 10, 10]}])
    assert r["pages"][0]["method"] == "edited"


def test_inherited_resources(tmp_path, settings):
    def edit(pdf):
        page = pdf.pages[0].obj
        pdf.Root.Pages.Resources = page.Resources
        del page["/Resources"]
    src = build(tmp_path / "in.pdf", LINE, edit=edit)
    out = tmp_path / "out.pdf"
    E.redact_pdf(src, out, settings, texts=["John"])
    assert text_of(out).split() == ["Call", "Smith", "on", "Friday"]


def test_time_limit(letter, tmp_path, settings, monkeypatch):
    monkeypatch.setattr(R, "REDACT_TIME_LIMIT_S", -1)
    with pytest.raises(PdfUserError, match="took longer than"):
        E.redact_pdf(letter, tmp_path / "out.pdf", settings, texts=["John"])
    with pytest.raises(PdfUserError, match="took too long; nothing was written"):
        E.redact_pdf(letter, tmp_path / "out.pdf", settings,
                     areas=[{"page": 1, "rect": [70, 690, 110, 715]}])


# --- permissions and protection ---------------------------------------------------------


def test_encrypted_input_stays_protected(letter, tmp_path, settings):
    locked = tmp_path / "locked.pdf"
    with pikepdf.open(letter) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(user="pw", owner="pw", R=6))
    out = tmp_path / "out.pdf"
    r = E.redact_pdf(locked, out, settings, texts=["John"], password="pw")
    assert r["password_protected"] is True
    assert "John" not in text_of(out, password="pw")


def test_copy_protected_input_needs_the_owner_password(letter, tmp_path, settings):
    locked = tmp_path / "locked.pdf"
    with pikepdf.open(letter) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(
            user="", owner="owner", R=6, allow=pikepdf.Permissions(extract=False)))
    with pytest.raises(PdfUserError, match="owner password"):
        E.redact_pdf(locked, tmp_path / "out.pdf", settings, texts=["John"])
    r = E.redact_pdf(locked, tmp_path / "out.pdf", settings,
                     areas=[{"page": 1, "rect": [70, 690, 110, 715]}])
    assert r["permissions_restricted"] is True


def test_modify_protected_input_is_refused(letter, tmp_path, settings):
    locked = tmp_path / "locked.pdf"
    with pikepdf.open(letter) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(
            user="", owner="owner", R=6, allow=pikepdf.Permissions(modify_other=False)))
    with pytest.raises(PdfUserError, match="does not allow changing"):
        E.redact_pdf(locked, tmp_path / "out.pdf", settings, texts=["John"])


def test_signed_input_is_refused(tmp_path, settings):
    def edit(pdf):
        pdf.Root.Perms = pikepdf.Dictionary(DocMDP=pikepdf.Dictionary())
    src = build(tmp_path / "in.pdf", LINE, edit=edit)
    with pytest.raises(PdfUserError, match="digitally signed"):
        E.redact_pdf(src, tmp_path / "out.pdf", settings, texts=["John"])


def test_matches_list_is_capped(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(R, "MAX_LISTED", 1)
    src = build(tmp_path / "in.pdf", LINE, LINE)
    r = E.redact_pdf(src, tmp_path / "out.pdf", settings, texts=["John"])
    assert r["match_count"] == 2 and len(r["matches"]) == 1 and r["matches_truncated"]


# --- through the server -----------------------------------------------------------------


def test_tool_and_pipeline_step(letter, tmp_path, settings, monkeypatch):
    from paperboy import server as S
    monkeypatch.setattr(S, "SETTINGS", settings)
    res = S.redact_pdf(str(letter), texts=["John"],
                       areas=[{"page": 2, "rect": [70, 690, 110, 715]}])
    assert res["output"] == str(tmp_path / "letter_redacted.pdf")
    assert [p["page"] for p in res["pages"]] == [1, 2]
    piped = S.run_pipeline(str(letter), [
        {"tool": "redact_pdf", "args": {"texts": ["Smith"], "fill": "white"}},
        {"tool": "delete_pages", "args": {"pages": "2"}}])
    assert text_of(piped["output"]).split() == ["Call", "John", "on", "Friday"]


def test_text_found_again_makes_a_picture_page(letter, tmp_path, settings, monkeypatch):
    # Should the rewrite and its check both miss something, searching the result finds it.
    monkeypatch.setattr(R.Redactor, "page", lambda self, obj, res, fill: None)
    monkeypatch.setattr(R, "_verify", lambda before, after, areas: None)
    out = tmp_path / "out.pdf"
    r = E.redact_pdf(letter, out, settings, texts=["John"])
    assert r["pages"][0]["method"] == "rasterized"
    assert r["pages"][0]["reason"] == "the text could still be found on it"
    assert text_of(out) == ""


def test_a_match_that_cant_be_located_is_refused(letter, tmp_path, settings, monkeypatch):
    monkeypatch.setattr(R, "_match_boxes", lambda chars, start, count: [])
    with pytest.raises(PdfUserError, match="can't be located on the page"):
        E.redact_pdf(letter, tmp_path / "out.pdf", settings, texts=["John"])
    assert not (tmp_path / "out.pdf").exists()


# --- nothing redacted stays elsewhere in the file ---------------------------------------


def everything(path) -> bytes:
    """Every object of a PDF, decoded where possible: what a determined reader can get."""
    found = []
    with pikepdf.open(path) as pdf:
        for obj in pdf.objects:
            if isinstance(obj, pikepdf.Stream):
                try:
                    found.append(obj.read_bytes())
                except pikepdf.PdfError:
                    found.append(obj.read_raw_bytes())
            found.append(bytes(repr(obj), "utf-8", "replace"))
    return b"\n".join(found)


@pytest.mark.parametrize("inherited", [False, True])
def test_originals_in_shared_resources_are_removed(tmp_path, settings, inherited):
    def res(pdf):
        return {"/XObject": {"/Im1": image(pdf, Image.new("L", (10, 10), 0xAB)),
                             "/Fm1": form(pdf, b"BT /F1 12 Tf 0 0 Td (SECRET-4242) Tj ET")}}

    def edit(pdf):
        if inherited:
            pdf.Root.Pages.Resources = pdf.make_indirect(pdf.pages[0].obj.Resources)
            for page in pdf.pages:
                del page.obj["/Resources"]
    src = build(tmp_path / "in.pdf", b"q 100 0 0 100 100 100 cm /Im1 Do Q "
                b"q 1 0 0 1 100 400 cm /Fm1 Do Q", LINE, resources=res, edit=edit)
    out = tmp_path / "out.pdf"
    r = E.redact_pdf(src, out, settings, areas=[{"page": 1, "rect": [90, 90, 210, 210]},
                                                {"page": 1, "rect": [90, 390, 310, 420]}])
    assert r["originals_kept"] == 0
    data = everything(out)
    assert b"SECRET-4242" not in data and b"\xab" * 20 not in data
    assert text_of(out, 1) == "Call John Smith on Friday"


def test_original_drawn_by_another_page_stays_and_is_counted(tmp_path, settings):
    src = build(tmp_path / "in.pdf", SCAN, SCAN, resources=scan)
    out = tmp_path / "out.pdf"
    r = E.redact_pdf(src, out, settings, areas=[{"page": 1, "rect": [150, 550, 250, 650]}])
    assert r["originals_kept"] == 1
    assert pixels(out, 1).getpixel((50, 50)) == (200, 50, 50)


def test_forms_listed_but_not_drawn_are_removed(tmp_path, settings):
    src = build(tmp_path / "in.pdf", LINE, resources=lambda pdf: {"/XObject": {
        "/Fm9": form(pdf, b"BT /F1 12 Tf 0 0 Td (HIDDEN-77) Tj ET")}})
    out = tmp_path / "out.pdf"
    E.redact_pdf(src, out, settings, texts=["John"])
    assert b"HIDDEN-77" not in everything(out)


def test_content_without_resources_keeps_what_it_draws(tmp_path, settings):
    def res(pdf):
        bare = form(pdf, b"q 50 0 0 50 0 0 cm /Im1 Do Q")
        return {"/XObject": {"/Im1": image(pdf, Image.new("L", (10, 10), 0)), "/Fm1": bare}}
    src = build(tmp_path / "in.pdf", b"q 1 0 0 1 300 300 cm /Fm1 Do Q " + LINE,
                resources=res)
    out = tmp_path / "out.pdf"
    E.redact_pdf(src, out, settings, texts=["John"])
    with pikepdf.open(out) as pdf:
        assert "/Im1" in pdf.pages[0].obj.Resources.XObject


def test_removed_things_lose_their_other_references(tmp_path, settings):
    def edit(pdf):
        annotations(pdf)
        field = pdf.Root.AcroForm.Fields[0]
        pdf.Root.AcroForm.CO = pikepdf.Array([field])
        note = pdf.pages[0].obj.Annots[0]
        fm = pdf.pages[0].obj.Resources.XObject.Fm1
        pdf.Root.StructTreeRoot = pikepdf.Dictionary(K=pikepdf.Array([
            pikepdf.Dictionary(S=Name.Annot, K=pikepdf.Dictionary(Type=Name.OBJR, Obj=note)),
            pikepdf.Dictionary(S=Name.P, K=pikepdf.Dictionary(Type=Name.MCR, MCID=0,
                                                              Stm=fm))]))
    src = build(tmp_path / "in.pdf", b"q 1 0 0 1 100 500 cm /Fm1 Do Q",
                resources=lambda pdf: {"/XObject": {"/Fm1": form(
                    pdf, b"BT /F1 12 Tf 0 0 Td (FORM-SECRET-808) Tj ET")}}, edit=edit)
    out = tmp_path / "out.pdf"
    E.redact_pdf(src, out, settings, areas=[{"page": 1, "rect": [95, 495, 330, 725]}])
    data = everything(out)
    assert b"FORM-SECRET-808" not in data and b"about John" not in data
    assert b"John Smith" not in data   # the removed field's value


def test_pattern_under_an_area(tmp_path, settings):
    def res(pdf):
        cell = form(pdf, b"BT /F1 8 Tf 0 0 Td (PATTERN-SECRET) Tj ET")
        del cell["/Subtype"]
        cell.Type, cell.PatternType, cell.PaintType, cell.TilingType = \
            Name.Pattern, 1, 1, 1
        cell.XStep, cell.YStep = 200, 20
        cell.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=helvetica(pdf)))
        return {"/Pattern": {"/P1": cell}}
    src = build(tmp_path / "in.pdf", b"/Pattern cs /P1 scn 90 390 220 60 re f",
                b"/Pattern cs /P1 scn 90 390 220 60 re f 0 g", resources=res)
    out = tmp_path / "out.pdf"
    r = E.redact_pdf(src, out, settings, areas=[{"page": 1, "rect": [100, 400, 200, 440]}])
    assert r["pages"][0]["reason"] == "a drawing filled with a pattern is partly under " \
        "an area"
    inside = E.redact_pdf(src, tmp_path / "in2.pdf", settings,
                          areas=[{"page": 1, "rect": [80, 380, 320, 460]},
                                 {"page": 2, "rect": [80, 380, 320, 460]}])
    assert [p["drawings_removed"] for p in inside["pages"]] == [1, 1]
    assert b"PATTERN-SECRET" not in everything(tmp_path / "in2.pdf")


@pytest.mark.parametrize(("shading", "reason"), [
    (4, "a shading that can hold a picture is under an area"), (2, None)])
def test_shadings(tmp_path, settings, shading, reason):
    def res(pdf):
        return {"/Shading": {"/Sh1": pikepdf.Dictionary(
            ShadingType=shading, ColorSpace=Name.DeviceGray,
            Coords=[0, 0, 100, 0], Function=pikepdf.Dictionary(
                FunctionType=2, Domain=[0, 1], C0=[0], C1=[1], N=1))}}
    src = build(tmp_path / "in.pdf", b"q 100 100 100 100 re W n /Sh1 sh Q " + LINE,
                resources=res)
    r = E.redact_pdf(src, tmp_path / "out.pdf", settings, texts=["John"])
    assert r["pages"][0]["reason"] == reason


@pytest.mark.parametrize(("content", "reason"), [
    (b"2 w 110 110 m 115 140 l 120 110 l 125 140 l S", None),          # wholly inside
    (b"2 w 110 110 m 115 140 l 120 110 l 305 140 l S", None),          # cut exactly
    (b"110 110 m 115 140 l 305 140 l h f", "partly under"),            # a filled shape
    (b"2 w 110 110 m 305 140 l S 2 w 110 110 m 305 140 l B", "partly under"),
    (b"110 110 m 150 160 330 160 330 120 c W n", "clipping shape"),
    (b"2 w 110 110 m 330 110 l S", None),                              # one line: kept
    (b"2 w 50 50 m 350 50 l 350 120 l 50 120 l h S", None),            # a border: kept
    (b"2 w 110 110 m 150 160 330 160 330 120 c S", "partly under"),      # a curve
])
def test_drawings_crossing_an_area(tmp_path, settings, content, reason):
    src = build(tmp_path / "in.pdf", content + b" " + LINE)
    r = E.redact_pdf(src, tmp_path / "out.pdf", settings,
                     areas=[{"page": 1, "rect": [100, 100, 300, 150]}])
    assert (r["pages"][0]["reason"] or "").find(reason or "") >= 0
    assert (r["pages"][0]["reason"] is None) == (reason is None)


def test_unreadable_drawings_on_a_page_with_areas(tmp_path, settings):
    src = build(tmp_path / "in.pdf", b"100 100 /x 20 re f " + LINE)
    r = E.redact_pdf(src, tmp_path / "out.pdf", settings, texts=["John"])
    assert r["pages"][0]["reason"] == "a drawing it can't read is on a page with areas"
    interrupted = build(tmp_path / "i.pdf", b"100 100 m 0 g 120 120 l S " + LINE)
    r = E.redact_pdf(interrupted, tmp_path / "i2.pdf", settings, texts=["John"])
    assert r["pages"][0]["reason"] == "a drawing it can't read is on a page with areas"


def test_text_shown_by_annotations_is_found(tmp_path, settings):
    def edit(pdf):
        ap = pdf.make_stream(b"BT /F1 10 Tf 2 2 Td (SSN-123-45-6789) Tj ET")
        ap.Type, ap.Subtype, ap.BBox = Name.XObject, Name.Form, [0, 0, 150, 20]
        ap.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=helvetica(pdf)))
        pdf.pages[0].obj.Annots = pikepdf.Array([pdf.make_indirect(pikepdf.Dictionary(
            Type=Name.Annot, Subtype=Name.Stamp, Rect=[300, 300, 450, 320], F=2,
            AP=pikepdf.Dictionary(N=ap)))])
    src = build(tmp_path / "in.pdf", LINE, edit=edit)
    out = tmp_path / "out.pdf"
    r = E.redact_pdf(src, out, settings, patterns=[r"SSN-[\d-]+"])
    assert r["match_count"] == 1 and r["pages"][0]["annotations_removed"] == 1
    assert b"SSN-123" not in everything(out)


def test_damaged_annotations_are_refused(letter, tmp_path, settings, monkeypatch):
    def edit(pdf):
        pdf.pages[0].obj.Annots = pikepdf.Array([pikepdf.Dictionary(
            Subtype=Name.Text, Rect=[0, 0, 1, 1], AP=pikepdf.Dictionary(
                N=form(pdf, b"(x) Tj")))])
    src = build(tmp_path / "in.pdf", LINE, edit=edit)
    monkeypatch.setattr(pikepdf.Pdf, "flatten_annotations",
                        lambda self, mode: (_ for _ in ()).throw(RuntimeError("no")))
    with pytest.raises(PdfUserError, match="annotations of this PDF can't be read"):
        E.redact_pdf(src, tmp_path / "out.pdf", settings, texts=["John"])


def test_text_in_scripts_rich_text_and_xfa(tmp_path, settings):
    def edit(pdf):
        js = pdf.make_stream(b"app.alert('John Smith');")
        rich = pdf.make_stream("﻿<p>John</p>".encode("utf-16-be"))
        latin = pdf.make_stream("Café John".encode("latin-1"))
        broken = pdf.make_stream(b"x")
        broken.Filter = Name("/Bogus")
        pdf.Root.OpenAction = pikepdf.Dictionary(S=Name.JavaScript, JS=js)
        pdf.Root.Extra = pikepdf.Dictionary(RC=rich, JS=latin)
        pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array(), XFA=pikepdf.Array(
            [pikepdf.String("datasets"), pdf.make_stream(b"<name>John</name>"),
             pikepdf.String("x"), broken]))
    src = build(tmp_path / "in.pdf", LINE, edit=edit)
    out = tmp_path / "out.pdf"
    r = E.redact_pdf(src, out, settings, texts=["John"])
    assert r["also_redacted_in"] == ["XFA form data", "comments", "scripts"]
    assert b"John" not in everything(out).replace(b"/Contents", b"")
    with pikepdf.open(out) as pdf:
        xfa = pdf.Root.AcroForm.XFA
        assert len(xfa) == 4 and xfa[3].read_bytes() == b""   # unreadable: emptied
    patterned = E.redact_pdf(src, tmp_path / "p.pdf", settings, patterns=["Jo.n"])
    assert "scripts" in patterned["also_redacted_in"]


def test_page_thumbnails_in_xmp_and_page_metadata_go(tmp_path, settings):
    def edit(pdf):
        with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
            meta["dc:title"] = "Report"
        meta = pdf.Root.Metadata.read_bytes().replace(
            b"</rdf:RDF>", b"<rdf:Description><xmp:Thumbnails>x</xmp:Thumbnails>"
            b"</rdf:Description></rdf:RDF>")
        pdf.Root.Metadata = pdf.make_stream(meta)
        pdf.pages[0].obj.Metadata = pdf.make_stream(b"PASSPORT-998877")
    src = build(tmp_path / "in.pdf", LINE, edit=edit)
    out = tmp_path / "out.pdf"
    r = E.redact_pdf(src, out, settings, areas=[{"page": 1, "rect": [70, 690, 110, 715]}])
    assert r["also_redacted_in"] == ["XMP metadata"]
    data = everything(out)
    assert b"Thumbnails" not in data and b"PASSPORT" not in data


def test_reader_usage_rights_are_dropped(tmp_path, settings):
    def edit(pdf):
        pdf.Root.Perms = pikepdf.Dictionary(UR3=pdf.make_indirect(pikepdf.Dictionary(
            Type=Name.Sig, ByteRange=[0, 1, 2, 3])))
    src = build(tmp_path / "in.pdf", LINE, edit=edit)
    out = tmp_path / "out.pdf"
    E.redact_pdf(src, out, settings, texts=["John"])
    with pikepdf.open(out) as pdf:
        assert "/Perms" not in pdf.Root


def test_areas_are_kept_to_the_page(letter, tmp_path, settings):
    r = E.redact_pdf(letter, tmp_path / "out.pdf", settings,
                     areas=[{"page": 1, "rect": [-1e300, -1e300, 1e300, 1e300]}],
                     rasterize="always", dpi=72)
    assert r["pages"][0]["method"] == "rasterized"
    with pytest.raises(PdfUserError, match="Area 1 is outside page 1"):
        E.redact_pdf(letter, tmp_path / "x.pdf", settings,
                     areas=[{"page": 1, "rect": [1e5, 1e5, 2e5, 2e5]}])


def test_kept_text_that_moves_fails_the_check():
    a, b = (65, 30, 30), (66, 15, 15)
    assert R._verify([a], [(65, 31.5, 30)], [(10, 10, 20, 20)]) == \
        "text outside the areas would have changed"
    assert R._verify([a, b], [(65, 30.5, 30)], [(10, 10, 20, 20)]) is None


def test_a_character_mostly_under_an_area(tmp_path, settings):
    src = build(tmp_path / "in.pdf", b"BT /F1 40 Tf 72 600 Td (ACCOUNT) Tj ET")
    r = E.redact_pdf(src, tmp_path / "out.pdf", settings,
                     areas=[{"page": 1, "rect": [60, 611, 300, 660]}])
    assert r["pages"][0]["reason"] == "a character is partly under an area"


def test_structure_kept_within_the_budget(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(R, "MAX_OBJECTS", 3)
    src = build(tmp_path / "in.pdf", LINE)
    with pytest.raises(PdfUserError, match="too many objects to check"):
        E.redact_pdf(src, tmp_path / "out.pdf", settings, texts=["John"])


def test_content_that_cant_be_read_doesnt_keep_an_original(tmp_path, settings):
    def edit(pdf):
        pdf.pages[1].obj.Contents.write(b"\x00", filter=Name("/Bogus"))
    src = build(tmp_path / "in.pdf", SCAN, SCAN, resources=scan, edit=edit)
    out = tmp_path / "out.pdf"
    r = E.redact_pdf(src, out, settings, areas=[{"page": 1, "rect": [150, 550, 250, 650]}])
    assert r["originals_kept"] == 0   # page 2 may draw it, but that can't be known
    with pikepdf.open(out) as pdf:
        assert "/Im1" not in pdf.pages[1].obj.Resources.XObject


# --- the second review --------------------------------------------------------------------


def test_layers_listed_on_a_redacted_page_stay(tmp_path, settings):
    def edit(pdf):
        layer = pdf.make_indirect(pikepdf.Dictionary(Type=Name.OCG, Name=pikepdf.String("D")))
        pdf.Root.OCProperties = pikepdf.Dictionary(
            OCGs=pikepdf.Array([layer]), D=pikepdf.Dictionary(OFF=pikepdf.Array([layer])))
        for page in pdf.pages:
            page.obj.Resources.Properties = pikepdf.Dictionary(L1=layer)
    src = build(tmp_path / "in.pdf", LINE,
                b"/OC /L1 BDC 0 0 1 rg 100 100 60 90 re f EMC", edit=edit)
    out = tmp_path / "out.pdf"
    E.redact_pdf(src, out, settings, texts=["John"], pages="1")
    with pikepdf.open(out) as pdf:
        assert len(pdf.Root.OCProperties.OCGs) == 1
        assert len(pdf.Root.OCProperties.D.OFF) == 1


@pytest.mark.parametrize("content", [
    b"/Pattern cs /P1 scn q 0 g Q 300 300 50 50 re f",     # chosen, then painted after Q
    b"/Pattern CS /P1 SCN 1 Tr BT /F1 30 Tf 300 300 Td (Hi) Tj ET",  # stroked text
    b"/Pattern cs /P1 scn q 1 0 0 1 300 300 cm /Fm1 Do Q",  # a form painting with it
])
def test_patterns_still_painted_with_stay(tmp_path, settings, content):
    def res(pdf):
        cell = form(pdf, b"0 0 1 rg 0 0 5 5 re f")
        del cell["/Subtype"]
        cell.Type, cell.PatternType, cell.PaintType, cell.TilingType = \
            Name.Pattern, 1, 1, 1
        cell.XStep, cell.YStep = 10, 10
        return {"/Pattern": {"/P1": cell}, "/XObject": {"/Fm1": form(pdf, b"0 0 50 50 re f")}}
    src = build(tmp_path / "in.pdf", content + b" " + LINE, resources=res)
    out = tmp_path / "out.pdf"
    E.redact_pdf(src, out, settings, texts=["John"])
    with pikepdf.open(out) as pdf:
        assert "/P1" in pdf.pages[0].obj.Resources.Pattern


def test_masks_and_shadings_used_elsewhere_stay(tmp_path, settings):
    def res(pdf):
        mask = image(pdf, Image.new("L", (10, 10), 255))
        shading = pdf.make_indirect(pikepdf.Dictionary(
            ShadingType=2, ColorSpace=Name.DeviceGray, Coords=[0, 0, 100, 0],
            Function=pikepdf.Dictionary(FunctionType=2, Domain=[0, 1], C0=[0], C1=[1], N=1)))
        return {"/XObject": {"/Im1": image(pdf, Image.new("L", (10, 10), 9), SMask=mask),
                             "/Mask": mask},
                "/Shading": {"/Sh1": shading},
                "/Pattern": {"/P2": pikepdf.Dictionary(PatternType=2, Shading=shading)}}
    src = build(tmp_path / "in.pdf", b"q 50 0 0 50 300 300 cm /Im1 Do Q "
                b"/Pattern cs /P2 scn 300 400 50 50 re f " + LINE, resources=res)
    out = tmp_path / "out.pdf"
    E.redact_pdf(src, out, settings, texts=["John"])
    with pikepdf.open(out) as pdf:
        res_ = pdf.pages[0].obj.Resources
        assert "/SMask" in res_.XObject.Im1 and "/Shading" in res_.Pattern.P2


def test_direct_property_lists_with_replacement_text_go(tmp_path, settings):
    def res(pdf):
        return {"/Properties": {"/MC0": pikepdf.Dictionary(
            ActualText=pikepdf.String("DIRECT-AT-11"))}}
    src = build(tmp_path / "in.pdf", b"/Span /MC0 BDC " + LINE + b" EMC", LINE,
                resources=res)
    out = tmp_path / "out.pdf"
    E.redact_pdf(src, out, settings, areas=[{"page": 1, "rect": [60, 680, 300, 720]}])
    assert b"DIRECT-AT-11" not in everything(out)


def test_xfa_parts_keep_their_places(tmp_path, settings):
    def edit(pdf):
        broken = pdf.make_stream(b"x")
        broken.Filter = Name("/Bogus")
        pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array(), XFA=pikepdf.Array([
            pikepdf.String("template"), broken,
            pikepdf.String("datasets"), pdf.make_stream(b"<ssn>John</ssn>"),
            pikepdf.String("postamble"), pdf.make_stream(b"</xdp>")]))
    src = build(tmp_path / "in.pdf", LINE, edit=edit)
    out = tmp_path / "out.pdf"
    E.redact_pdf(src, out, settings, texts=["John"])
    with pikepdf.open(out) as pdf:
        xfa = list(pdf.Root.AcroForm.XFA)
        assert [str(x) for x in xfa[::2]] == ["template", "datasets", "postamble"]
        assert xfa[1].read_bytes() == b""   # unreadable: emptied, in its place
        assert xfa[3].read_bytes() == b"<ssn>[redacted]</ssn>"
        assert xfa[5].read_bytes() == b"</xdp>"


def test_entity_encoded_text_in_streams(tmp_path, settings):
    def edit(pdf):
        pdf.Root.Extra = pikepdf.Dictionary(RC=pdf.make_stream(b"<p>&#74;ohn</p>"))
    src = build(tmp_path / "in.pdf", LINE, edit=edit)
    out = tmp_path / "out.pdf"
    E.redact_pdf(src, out, settings, texts=["John"])
    with pikepdf.open(out) as pdf:
        assert "/RC" not in pdf.Root.Extra


def test_more_text_keys_and_named_destinations(tmp_path, settings):
    def edit(pdf):
        dest = pikepdf.Array([pdf.pages[0].obj, Name.Fit])
        pdf.Root.Names = pikepdf.Dictionary(Dests=pikepdf.Dictionary(Names=pikepdf.Array([
            pikepdf.String("a-first"), dest, pikepdf.String("John-page"), dest])))
        pdf.Root.OpenAction = pikepdf.Dictionary(S=Name.GoTo, D=pikepdf.String("John-page"))
        pdf.pages[0].obj.Annots = pikepdf.Array([pdf.make_indirect(pikepdf.Dictionary(
            Type=Name.Annot, Subtype=Name.Link, Rect=[0, 0, 10, 10], A=pikepdf.Dictionary(
                S=Name.URI, URI=pikepdf.String("mailto:john@example.com"))))])
        pdf.Root.Extra = pikepdf.Dictionary(JS=pikepdf.String("alert('John')"))
    src = build(tmp_path / "in.pdf", LINE, edit=edit)
    out = tmp_path / "out.pdf"
    E.redact_pdf(src, out, settings, texts=["John"])
    data = everything(out)
    assert b"John" not in data.replace(b"Call", b"")  # page text is font codes anyway
    with pikepdf.open(out) as pdf:
        names = [str(n) for n in list(pdf.Root.Names.Dests.Names)[::2]]
        assert names == sorted(names) and str(pdf.Root.OpenAction.D) in names


def test_text_in_other_appearance_states_is_found(tmp_path, settings):
    def edit(pdf):
        on = form(pdf, b"BT /F1 10 Tf 2 2 Td (ACCT-55501) Tj ET")
        off = form(pdf, b"")
        pdf.pages[0].obj.Annots = pikepdf.Array([pdf.make_indirect(pikepdf.Dictionary(
            Type=Name.Annot, Subtype=Name.Widget, Rect=[300, 300, 450, 320], FT=Name.Btn,
            T=pikepdf.String("box"), AS=Name.Off,
            AP=pikepdf.Dictionary(N=pikepdf.Dictionary(Off=off), D=pikepdf.Dictionary(
                Yes=on))))])
        pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array(
            [pdf.pages[0].obj.Annots[0]]), NeedAppearances=True)
    src = build(tmp_path / "in.pdf", LINE, edit=edit)
    out = tmp_path / "out.pdf"
    r = E.redact_pdf(src, out, settings, texts=["ACCT-55501"])
    assert r["pages"][0]["annotations_removed"] == 1
    assert b"ACCT-55501" not in everything(out)


def test_annotations_with_too_much_to_search(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(R, "MAX_PAGE_CONTENT", 100)

    def edit(pdf):
        ap = form(pdf, b"BT /F1 10 Tf 2 2 Td (" + b"x" * 300 + b") Tj ET")
        pdf.pages[0].obj.Annots = pikepdf.Array([pdf.make_indirect(pikepdf.Dictionary(
            Type=Name.Annot, Subtype=Name.Stamp, Rect=[0, 0, 9, 9], AP=pikepdf.Dictionary(
                N=ap)))])
    src = build(tmp_path / "in.pdf", b"BT /F1 9 Tf 9 9 Td (John) Tj ET", edit=edit)
    with pytest.raises(PdfUserError, match="annotations on page 1 have too much content"):
        E.redact_pdf(src, tmp_path / "out.pdf", settings, texts=["John"])


def test_barcodes_are_cut(tmp_path, settings):
    bars = b" ".join(b"%d 100 %d 60 re" % (x, 2 + x % 3) for x in range(100, 300, 8))
    strokes = b" ".join(b"%d 200 m %d 260 l" % (x, x) for x in range(100, 300, 8))
    src = build(tmp_path / "in.pdf", bars + b" f 2 w " + strokes + b" S")
    out = tmp_path / "out.pdf"
    r = E.redact_pdf(src, out, settings, areas=[{"page": 1, "rect": [90, 105, 310, 255]}],
                     fill="none")
    assert r["pages"][0]["method"] == "edited"
    doc = pdfium.PdfDocument(str(out))
    picture = doc[0].render(scale=1).to_pil().convert("L")
    doc.close()
    inside = picture.crop((95, 792 - 250, 305, 792 - 110))
    assert min(inside.tobytes()) > 200   # nothing left inside the area
    outside = picture.crop((95, 792 - 104, 305, 792 - 100))
    assert min(outside.tobytes()) < 100  # the bars' ends outside it are still there


def test_rotated_rectangles_crossing_an_area(tmp_path, settings):
    src = build(tmp_path / "in.pdf", b"q 0.7 0.7 -0.7 0.7 200 100 cm 0 0 100 20 re f Q " + LINE)
    r = E.redact_pdf(src, tmp_path / "out.pdf", settings,
                     areas=[{"page": 1, "rect": [150, 100, 200, 160]}])
    assert r["pages"][0]["reason"] == "a drawing is partly under an area"


# --- the third review ---------------------------------------------------------------------


@pytest.mark.parametrize("where", ["thumb", "alternates", "pieceinfo", "af", "rendition"])
def test_originals_referenced_elsewhere_are_cut(tmp_path, settings, where):
    def edit(pdf):
        picture = pdf.pages[0].obj.Resources.XObject.Im1
        note = pdf.make_indirect(pikepdf.Dictionary(
            Type=Name.Annot, Subtype=Name.Text, Rect=[110, 510, 120, 520],
            Contents=pikepdf.String("NOTE-9")))
        pdf.pages[0].obj.Annots = pikepdf.Array([note])
        if where == "thumb":
            pdf.pages[1].obj.Thumb = picture
        elif where == "alternates":
            other = image(pdf, Image.new("L", (4, 4)))
            other.Alternates = pikepdf.Array([pikepdf.Dictionary(Image=picture)])
            pdf.pages[1].obj.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(
                Im2=other))
        elif where == "pieceinfo":
            pdf.Root.PieceInfo = pikepdf.Dictionary(App=pikepdf.Dictionary(Private=picture))
        elif where == "af":
            pdf.Root.AF = pikepdf.Array([picture])
        else:
            pdf.Root.OpenAction = pikepdf.Dictionary(S=Name.Rendition, AN=note)
    src = build(tmp_path / "in.pdf", SCAN, LINE, resources=scan, edit=edit)
    out = tmp_path / "out.pdf"
    r = E.redact_pdf(src, out, settings, areas=[{"page": 1, "rect": [90, 490, 510, 710]}])
    assert r["originals_kept"] == 0
    data = everything(out)
    assert b"NOTE-9" not in data
    with pikepdf.open(out) as pdf:
        for obj in pdf.objects:
            if isinstance(obj, pikepdf.Stream) and obj.get("/Width") == 200:
                pytest.fail("the original picture is still in the file")


def test_too_many_appearances(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(R, "MAX_APPEARANCES", 3)

    def edit(pdf):
        states = pikepdf.Dictionary({f"/S{i}": form(pdf, b"(x) Tj") for i in range(5)})
        pdf.pages[0].obj.Annots = pikepdf.Array([pdf.make_indirect(pikepdf.Dictionary(
            Type=Name.Annot, Subtype=Name.Widget, Rect=[0, 0, 9, 9],
            AP=pikepdf.Dictionary(N=states)))])
    src = build(tmp_path / "in.pdf", LINE, edit=edit)
    with pytest.raises(PdfUserError, match="too many annotation appearances"):
        E.redact_pdf(src, tmp_path / "out.pdf", settings, texts=["John"])


def test_appearances_drawing_large_forms_are_measured(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(R, "MAX_PAGE_CONTENT", 20_000)

    def edit(pdf):
        big = form(pdf, b"0 0 1 1 re f " * 2000)
        ap = form(pdf, b"/X Do", Resources=pikepdf.Dictionary(XObject=pikepdf.Dictionary(
            X=big)))
        pdf.pages[0].obj.Annots = pikepdf.Array([pdf.make_indirect(pikepdf.Dictionary(
            Type=Name.Annot, Subtype=Name.Stamp, Rect=[0, 0, 9, 9],
            AP=pikepdf.Dictionary(N=ap)))])
    src = build(tmp_path / "in.pdf", LINE, edit=edit)
    with pytest.raises(PdfUserError, match="annotations on page 1 have too much content"):
        E.redact_pdf(src, tmp_path / "out.pdf", settings, texts=["John"])


@pytest.mark.parametrize(("content", "check"), [
    # s closes the outline: the closing side survives the cut too
    (b"2 w 100 100 m 300 100 l 300 200 l 100 200 l s", ("l", [100.0, 100.0])),
    # a rectangle drawn the other way round keeps its direction (and so its winding)
    (b"300 100 -200 100 re 150 120 50 50 re f", ("re", [210.0, 100.0, -110.0, 100.0])),
])
def test_cuts_keep_the_shape(tmp_path, settings, content, check):
    src = build(tmp_path / "in.pdf", content)
    out = tmp_path / "out.pdf"
    E.redact_pdf(src, out, settings, areas=[{"page": 1, "rect": [210, 90, 320, 210]}])
    with pikepdf.open(out) as pdf:
        ops = [(str(op), [float(v) for v in operands])
               for operands, op in pikepdf.parse_content_stream(pdf.pages[0].obj)
               if str(op) in ("m", "l", "re")]
    assert check in ops


def test_dashed_lines_crossing_an_area(tmp_path, settings):
    src = build(tmp_path / "in.pdf", b"[3 2] 0 d 100 100 m 400 100 l S " + LINE)
    r = E.redact_pdf(src, tmp_path / "out.pdf", settings,
                     areas=[{"page": 1, "rect": [200, 90, 250, 110]}])
    assert r["pages"][0]["reason"] == "a dashed line is partly under an area"


def test_too_many_pieces(tmp_path, settings, monkeypatch):
    from paperboy.engine import redact_content as C
    monkeypatch.setattr(C, "MAX_PIECES", 3)
    src = build(tmp_path / "in.pdf", b"0 0 612 792 re f 100 100 m 400 100 l S")
    r = E.redact_pdf(src, tmp_path / "out.pdf", settings,
                     areas=[{"page": 1, "rect": [10, 10, 20, 20]},
                            {"page": 1, "rect": [50, 50, 60, 60]}])
    assert r["pages"][0]["reason"] == "a drawing would be cut into too many pieces"
    src = build(tmp_path / "s.pdf", b"100 100 m 400 100 l 400 400 l 100 400 l S")
    r = E.redact_pdf(src, tmp_path / "s2.pdf", settings,
                     areas=[{"page": 1, "rect": [90, 90, 110, 110]}])
    assert r["pages"][0]["reason"] == "a drawing would be cut into too many pieces"


def test_name_trees_are_rebuilt_in_order(tmp_path, settings):
    def edit(pdf):
        dest = pikepdf.Array([pdf.pages[0].obj, Name.Fit])
        leaf = lambda *names: pdf.make_indirect(pikepdf.Dictionary(  # noqa: E731
            Limits=pikepdf.Array([pikepdf.String(names[0]), pikepdf.String(names[-1])]),
            Names=pikepdf.Array([x for n in names for x in (pikepdf.String(n), dest)])))
        pdf.Root.Names = pikepdf.Dictionary(Dests=pikepdf.Dictionary(Kids=pikepdf.Array([
            leaf("Apple", "John a"), leaf("John b", "Zed"), leaf("john b")])))
    src = build(tmp_path / "in.pdf", LINE, edit=edit)
    out = tmp_path / "out.pdf"
    E.redact_pdf(src, out, settings, texts=["John"])
    with pikepdf.open(out) as pdf:
        tree = pdf.Root.Names.Dests
        assert "/Kids" not in tree
        names = [str(n) for n in list(tree.Names)[::2]]
    assert names == ["Apple", "Zed", "[redacted] a", "[redacted] b", "[redacted] b (2)"]


# --- the fourth review --------------------------------------------------------------------


def test_many_checkboxes_can_be_searched(tmp_path, settings):
    def edit(pdf):
        on = form(pdf, b"BT /ZaDb 8 Tf 2 2 Td (4) Tj ET")
        off = form(pdf, b"")
        boxes = []
        for i in range(300):
            boxes.append(pdf.make_indirect(pikepdf.Dictionary(
                Type=Name.Annot, Subtype=Name.Widget, FT=Name.Btn,
                T=pikepdf.String(f"b{i}"), Rect=[10 + i % 30 * 18, 10 + i // 30 * 18,
                                                 22 + i % 30 * 18, 22 + i // 30 * 18],
                AS=Name.Off, AP=pikepdf.Dictionary(
                    N=pikepdf.Dictionary(On=on, Off=off),
                    D=pikepdf.Dictionary(On=on, Off=off)))))
        pdf.pages[0].obj.Annots = pikepdf.Array(boxes)
        pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array(boxes))
    src = build(tmp_path / "in.pdf", LINE, edit=edit)
    r = E.redact_pdf(src, tmp_path / "out.pdf", settings, texts=["John"])
    assert r["pages"][0]["method"] == "edited"


def test_a_removed_picture_that_is_also_a_mask_stays_a_mask(tmp_path, settings):
    def res(pdf):
        shared = image(pdf, Image.new("L", (10, 10), 255))
        return {"/XObject": {"/Im1": shared, "/Im2": image(
            pdf, Image.new("L", (10, 10), 9), SMask=shared)}}
    src = build(tmp_path / "in.pdf", b"q 50 0 0 50 100 100 cm /Im1 Do Q",
                b"q 50 0 0 50 100 100 cm /Im2 Do Q", resources=res)
    out = tmp_path / "out.pdf"
    r = E.redact_pdf(src, out, settings, areas=[{"page": 1, "rect": [90, 90, 160, 160]}],
                     pages="1")
    assert r["originals_kept"] == 1
    with pikepdf.open(out) as pdf:
        assert "/SMask" in pdf.pages[1].obj.Resources.XObject.Im2


def test_name_tree_rebuild_keeps_other_keys_as_they_were(tmp_path, settings):
    def edit(pdf):
        dest = pikepdf.Array([pdf.pages[0].obj, Name.Fit])
        utf16 = pikepdf.String("Résumé".encode("utf-16"))  # with a byte-order mark
        pdf.Root.Names = pikepdf.Dictionary(
            Dests=pikepdf.Dictionary(Names=pikepdf.Array([
                pikepdf.String("Dup"), dest, pikepdf.String("Dup"), dest,
                pikepdf.String("John"), dest, utf16, dest])),
            JavaScript=pikepdf.Dictionary(Names=pikepdf.Array([pikepdf.String("x"), dest])))
    src = build(tmp_path / "in.pdf", LINE, edit=edit)
    out = tmp_path / "out.pdf"
    E.redact_pdf(src, out, settings, texts=["John"])
    with pikepdf.open(out) as pdf:
        keys = [bytes(k) for k in list(pdf.Root.Names.Dests.Names)[::2]]
        assert keys == sorted(keys)
        assert keys.count(b"Dup") == 2   # duplicates already there are left alone
        assert "Résumé".encode("utf-16") in keys
        assert b"[redacted]" in keys
