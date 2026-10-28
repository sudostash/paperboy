"""Lowering picture resolution: where images are drawn, which are re-encoded, and
optimize_pdf's max_image_dpi and target_size_mb."""
from __future__ import annotations

import io
import os
import time
import zlib

import pikepdf
import pytest
from PIL import Image

from paperboy import engine as E
from paperboy.engine import images as I
from paperboy.engine import optimize as O
from paperboy.errors import PdfUserError

FAR = float("inf")


def _picture(w, h, mode="RGB"):
    return Image.effect_noise((w, h), 60).convert(mode)


def _image(pdf, w=600, h=400, mode="RGB", jpeg=False, data=None, **extra):
    """An image XObject of noise (which shrinks a lot when made smaller)."""
    picture = _picture(w, h, mode)
    if data is None:
        if jpeg:
            buf = io.BytesIO()
            picture.save(buf, "JPEG", quality=95)
            data = buf.getvalue()
        else:
            data = zlib.compress(picture.tobytes())
    space = pikepdf.Name.DeviceRGB if mode == "RGB" else pikepdf.Name.DeviceGray
    fields = {"/Type": pikepdf.Name.XObject, "/Subtype": pikepdf.Name.Image, "/Width": w,
              "/Height": h, "/ColorSpace": space, "/BitsPerComponent": 8,
              "/Filter": pikepdf.Name.DCTDecode if jpeg else pikepdf.Name.FlateDecode}
    fields.update({f"/{k}": v for k, v in extra.items()})
    return pdf.make_indirect(pikepdf.Stream(pdf, data, pikepdf.Dictionary(fields)))


def _page(pdf, content: bytes | list, xobjects: dict, **extra):
    """A page drawing *content* with these XObjects."""
    page = pikepdf.Dictionary(Type=pikepdf.Name.Page, MediaBox=[0, 0, 612, 792],
                              Resources=pikepdf.Dictionary(
                                  XObject=pikepdf.Dictionary(xobjects)), **extra)
    if isinstance(content, list):
        page.Contents = pikepdf.Array([pdf.make_stream(c) for c in content])
    else:
        page.Contents = pdf.make_stream(content)
    pdf.pages.append(pikepdf.Page(page))
    return pdf.pages[-1]


def _form(pdf, content: bytes, xobjects: dict, matrix=None):
    form = pdf.make_stream(content)
    form.Type, form.Subtype = pikepdf.Name.XObject, pikepdf.Name.Form
    form.BBox = [0, 0, 1000, 1000]
    form.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(xobjects))
    if matrix is not None:
        form.Matrix = matrix
    return form


def _where(pdf):
    return I.placements(pdf, time.monotonic() + 60)


# --- where images are drawn ----------------------------------------------------------------


def test_the_size_an_image_is_drawn_at():
    pdf = pikepdf.new()
    im = _image(pdf)
    # 3 x 2 inches, then (after Q) 1 x 1, then rotated 90 degrees at 2 x 4 inches.
    _page(pdf, b"q 216 0 0 144 0 0 cm /Im Do Q q 72 0 0 72 0 0 cm /Im Do Q "
               b"q 0 144 -288 0 300 0 cm /Im Do Q Q", {"/Im": im})
    largest, unknown = _where(pdf)
    assert largest[im.objgen] == [3.0, 4.0] and unknown == set()


def test_forms_scale_what_they_draw():
    pdf = pikepdf.new()
    im = _image(pdf)
    inner = _form(pdf, b"72 0 0 72 0 0 cm /Im Do", {"/Im": im}, matrix=[2, 0, 0, 2, 0, 0])
    outer = _form(pdf, b"/In Do", {"/In": inner})
    _page(pdf, [b"q 1.5 0 0 1.5 0 0 cm", b"/F Do Q /F Do"], {"/F": outer})  # read once
    largest, _ = _where(pdf)
    assert largest[im.objgen] == [3.0, 3.0]


@pytest.mark.parametrize("values", [["a"] * 6, [1, 2, 3], [1, 0, 0, float("inf"), 0, 0],
                                    None])
def test_matrices_that_cant_be_followed(values):
    with pytest.raises(I._Unknown):
        I._matrix(values)


def test_unreadable_pages_leave_their_images_alone(monkeypatch):
    pdf = pikepdf.new()
    a, b, c, d = (_image(pdf, 50, 50) for _ in range(4))
    _page(pdf, b"72 0 0 72 0 0 cm /A Do", {"/A": a})
    _page(pdf, b"72 0 x 72 0 0 cm /B Do", {"/B": b})                   # a bad operand
    looping = _form(pdf, b"/Me Do", {"/C": c})
    looping.Resources.XObject.Me = looping                              # a form drawing itself
    _page(pdf, b"/L Do", {"/L": looping})
    _page(pdf, b"72 0 0 72 0 0 cm /D Do", {"/D": d}).obj.Contents = pikepdf.Array([1])
    largest, unknown = _where(pdf)
    assert a.objgen in largest and unknown == {b.objgen, c.objgen, d.objgen}


@pytest.mark.parametrize("limit", ["MAX_CONTENT", "MAX_STREAM_TOKENS", "MAX_TOKENS",
                                   "MAX_DRAWS"])
def test_reading_content_is_bounded(monkeypatch, limit):
    pdf = pikepdf.new()
    im = _image(pdf, 50, 50)
    _page(pdf, b"q Q " * 50 + b"72 0 0 72 0 0 cm /A Do", {"/A": im})
    monkeypatch.setattr(I, limit, 10)
    assert _where(pdf)[1] == {im.objgen}


def test_forms_nest_only_so_deep(monkeypatch):
    pdf = pikepdf.new()
    im = _image(pdf, 50, 50)
    form = _form(pdf, b"/A Do", {"/A": im})
    for _ in range(3):
        form = _form(pdf, b"/F Do", {"/F": form})
    _page(pdf, b"/F Do", {"/F": form})
    monkeypatch.setattr(I, "MAX_FORM_DEPTH", 2)
    assert _where(pdf)[1] == {im.objgen}


def test_damaged_content_leaves_images_alone(monkeypatch):
    pdf = pikepdf.new()
    im = _image(pdf, 50, 50)
    _page(pdf, b"72 0 0 72 0 0 cm /A Do", {"/A": im})

    def broken(*args):
        raise pikepdf.PdfError("damaged")
    monkeypatch.setattr(pikepdf, "parse_content_stream", broken)
    assert _where(pdf)[1] == {im.objgen}


def test_running_out_of_time_leaves_images_alone():
    pdf = pikepdf.new()
    im = _image(pdf, 50, 50)
    _page(pdf, b"72 0 0 72 0 0 cm /A Do", {"/A": im})
    assert I.placements(pdf, time.monotonic() - 1) == ({}, None)


def test_odd_drawing_operators_are_ignored():
    pdf = pikepdf.new()
    im = _image(pdf, 50, 50)
    text = pdf.make_stream(b"")
    _page(pdf, b"Q Do /Missing Do /Text Do 72 0 0 72 0 0 cm /A Do", {"/A": im, "/Text": text})
    _page(pdf, b"/A Do", {})
    _page(pdf, b"/Text Do", {"/Text": text})
    page = _page(pdf, b"/A Do", {})
    del page.obj.Resources["/XObject"]
    largest, unknown = _where(pdf)
    assert largest[im.objgen] == [1.0, 1.0] and unknown == set()


def test_user_units_scale_the_page():
    pdf = pikepdf.new()
    im = _image(pdf, 50, 50)
    _page(pdf, b"72 0 0 72 0 0 cm /A Do", {"/A": im}, UserUnit=10)
    assert _where(pdf)[0][im.objgen] == [10.0, 10.0]
    for bad in (0, -1, "x"):
        pdf = pikepdf.new()
        im = _image(pdf, 50, 50)
        page = _page(pdf, b"72 0 0 72 0 0 cm /A Do", {"/A": im})
        page.obj.UserUnit = pikepdf.String(bad) if isinstance(bad, str) else bad
        assert _where(pdf)[1] == {im.objgen}, bad


def test_a_form_without_resources_uses_its_callers():
    pdf = pikepdf.new()
    im = _image(pdf, 50, 50)
    form = pdf.make_stream(b"576 0 0 576 0 0 cm /A Do", Type=pikepdf.Name.XObject,
                           Subtype=pikepdf.Name.Form, BBox=[0, 0, 1000, 1000])
    _page(pdf, b"q 72 0 0 72 0 0 cm /A Do Q /F Do", {"/A": im, "/F": form})
    assert _where(pdf)[0][im.objgen] == [8.0, 8.0]


def test_appearances_are_left_alone_even_when_pages_draw_them():
    pdf = pikepdf.new()
    a = _image(pdf, 50, 50)
    form = _form(pdf, b"/A Do", {"/A": a})
    page = _page(pdf, b"72 0 0 72 0 0 cm /F Do", {"/F": form})
    page.obj.Annots = pikepdf.Array([pikepdf.Dictionary(
        Subtype=pikepdf.Name.Stamp, Rect=[0, 0, 576, 576], AP=pikepdf.Dictionary(
            N=pikepdf.Dictionary(On=form, Off=pdf.make_stream(b"")), D=form)), 5])
    assert _where(pdf)[1] == {a.objgen}


def test_type3_glyphs_leave_their_images_alone():
    pdf = pikepdf.new()
    a, b = _image(pdf, 50, 50), _image(pdf, 50, 50)
    without = pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type3)
    page = _page(pdf, b"72 0 0 72 0 0 cm /A Do", {"/A": a})
    page.obj.Resources.Font = pikepdf.Dictionary(
        T=without, H=pikepdf.Dictionary(Subtype=pikepdf.Name.Type1), N=1)
    # A glyph without resources of its own draws with the page's: /A could be big.
    assert _where(pdf)[1] == {a.objgen}
    pdf = pikepdf.new()
    a, b = _image(pdf, 50, 50), _image(pdf, 50, 50)
    page = _page(pdf, b"72 0 0 72 0 0 cm /A Do", {"/A": a})
    page.obj.Resources.Font = pikepdf.Dictionary(T=pikepdf.Dictionary(   # a direct font
        Subtype=pikepdf.Name.Type3, Resources=pikepdf.Dictionary(
            XObject=pikepdf.Dictionary(B=b))))
    assert _where(pdf)[1] == {b.objgen}


def test_hostile_resource_sharing_is_cheap():
    pdf = pikepdf.new()
    shared = pdf.make_indirect(pikepdf.Dictionary())
    for n in range(2000):
        form = pdf.make_stream(b"", Type=pikepdf.Name.XObject, Subtype=pikepdf.Name.Form,
                               BBox=[0, 0, 1, 1], Resources=pikepdf.Dictionary(XObject=shared))
        shared[f"/F{n}"] = form
    _page(pdf, b"", {}).obj.Resources.XObject = shared
    start = time.monotonic()
    assert _where(pdf)[1] == set() and time.monotonic() - start < 10


def test_finding_images_is_bounded(monkeypatch):
    pdf = pikepdf.new()
    a = _image(pdf, 50, 50)
    _page(pdf, b"72 0 0 72 0 0 cm /A Do /X Do", {"/A": a, "/X": pdf.make_stream(b"")}) \
        .obj.Contents = pikepdf.Array([1])
    monkeypatch.setattr(I, "MAX_VISITS", 1)
    assert _where(pdf) == ({}, None)


def test_damage_found_while_looking_leaves_everything_alone(monkeypatch):
    pdf = pikepdf.new()
    _page(pdf, b"72 0 0 72 0 0 cm /A Do", {"/A": _image(pdf, 50, 50)})

    def broken(*args):
        raise pikepdf.PdfError("damaged")
    monkeypatch.setattr(I, "_user_unit", broken)
    assert _where(pdf) == ({}, None)


def test_images_drawn_other_than_by_pages_are_left_alone():
    pdf = pikepdf.new()
    a, b, c, d = (_image(pdf, 50, 50) for _ in range(4))
    appearance = _form(pdf, b"/A Do", {"/A": a})
    pattern = pdf.make_stream(b"/B Do", PatternType=1,
                              Resources=pikepdf.Dictionary(XObject=pikepdf.Dictionary(B=b)))
    pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type3,
                                         Resources=pikepdf.Dictionary(
                                             XObject=pikepdf.Dictionary(C=c))))
    page = _page(pdf, b"72 0 0 72 0 0 cm /A Do /D Do", {"/A": a, "/D": d})
    page.obj.Annots = pdf.make_indirect(pikepdf.Array([pikepdf.Dictionary(
        Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Stamp, Rect=[0, 0, 9, 9],
        AP=pikepdf.Dictionary(N=appearance))]))
    page.obj.Resources.Pattern = pikepdf.Dictionary(P=pattern)
    _, unknown = _where(pdf)
    assert unknown == {a.objgen, b.objgen, c.objgen}


# --- which images are re-encoded -------------------------------------------------------------


def _icc(pdf, n):
    return pikepdf.Array([pikepdf.Name.ICCBased, pdf.make_stream(b"", N=n)])


def test_colour_spaces_that_are_re_encoded():
    pdf = pikepdf.new()
    assert I._colour(_image(pdf, 8, 8)) == "RGB"
    assert I._colour(_image(pdf, 8, 8, "L")) == "L"
    assert I._colour(_image(pdf, 8, 8, ColorSpace=_icc(pdf, 3))) == "RGB"
    assert I._colour(_image(pdf, 8, 8, "L", ColorSpace=_icc(pdf, 1))) == "L"
    for extra in ({"ColorSpace": _icc(pdf, 4)}, {"ColorSpace": pikepdf.Name.DeviceCMYK},
                  {"ColorSpace": pikepdf.Array([pikepdf.Name.Indexed])},
                  {"BitsPerComponent": 1}, {"ImageMask": True}, {"Decode": [1, 0] * 3},
                  {"Mask": [0, 0, 0, 0, 0, 0]}):
        assert I._colour(_image(pdf, 8, 8, **extra)) is None, extra


def test_codecs_that_are_re_encoded():
    pdf = pikepdf.new()
    ok = [[], ["/FlateDecode"], ["/DCTDecode"], ["/ASCII85Decode", "/DCTDecode"],
          ["/ASCIIHexDecode", "/FlateDecode", "/FlateDecode"]]
    bad = [["/JPXDecode"], ["/LZWDecode", "/DCTDecode"], ["/FlateDecode"] * 5]
    for filters, expected in [(f, True) for f in ok] + [(f, False) for f in bad]:
        image = _image(pdf, 8, 8)
        if filters:
            image.Filter = pikepdf.Array([pikepdf.Name(f) for f in filters])
        else:
            del image["/Filter"]
        assert I._codec_ok(image) is expected, filters
    image = _image(pdf, 8, 8, jpeg=True, DecodeParms=pikepdf.Dictionary(ColorTransform=0))
    assert I._codec_ok(image) is False


def test_slow_progressive_jpegs_are_kept(monkeypatch):
    pdf = pikepdf.new()
    image = _image(pdf, 64, 64, jpeg=True)
    assert I._slow_jpeg(image) is False and I._slow_jpeg(_image(pdf, 8, 8)) is False
    monkeypatch.setattr(I, "MAX_JPEG_SCANS", 0)
    assert I._slow_jpeg(image) is True
    mask = _image(pdf, 8, 8, "L", jpeg=True)
    assert I._soft_mask(_image(pdf, 8, 8, SMask=mask)) is False
    monkeypatch.setattr(I, "encoded_image", lambda *a: None)
    assert I._slow_jpeg(image) is True


def test_soft_masks_that_are_resized():
    pdf = pikepdf.new()
    assert I._soft_mask(_image(pdf, 8, 8)) is None
    mask = _image(pdf, 8, 8, "L")
    assert I._soft_mask(_image(pdf, 8, 8, SMask=mask)) is not None
    for extra in ({"ColorSpace": pikepdf.Name.DeviceRGB}, {"Matte": [0, 0, 0]},
                  {"Filter": pikepdf.Name.JPXDecode}, {"Decode": [1, 0]}):
        assert I._soft_mask(_image(pdf, 8, 8, SMask=_image(pdf, 8, 8, "L", **extra))) \
            is False, extra
    assert I._soft_mask(_image(pdf, 8, 8, SMask=pikepdf.Name.Foo)) is False


def test_pictures_that_dont_decode_as_declared_are_kept():
    pdf = pikepdf.new()
    assert I._decode(_image(pdf, 8, 8, data=b"junk"), "RGB") is None
    assert I._decode(_image(pdf, 8, 8, "L"), "RGB") is None      # declared RGB, holds grey
    assert I._decode(_image(pdf, 8, 8), "RGB").size == (8, 8)


# --- lowering resolution ---------------------------------------------------------------------


def _doc(tmp_path, *images, inches=2, name="in.pdf"):
    """A PDF with a page per image, each drawn *inches* square; returns its path."""
    pdf = pikepdf.new()
    for make in images:
        im = make(pdf)
        size = inches * 72
        _page(pdf, f"{size} 0 0 {size} 0 0 cm /Im Do".encode(), {"/Im": im})
    pdf.save(tmp_path / name)
    return tmp_path / name


def _images(path, password=""):
    with pikepdf.open(path, password=password) as pdf:
        return [(int(im.Width), int(im.Height), str(im.get("/Filter")),
                 im.get("/SMask") is not None and (int(im.SMask.Width), int(im.SMask.Height)))
                for page in pdf.pages for _, im in page.obj.Resources.XObject.items()]


def test_pictures_are_drawn_at_no_more_than_the_dpi(tmp_path, settings):
    src = _doc(tmp_path, lambda p: _image(p, 1200, 800, jpeg=True),       # 600 x 400 dpi
               lambda p: _image(p, 1000, 1000, "L"),                      # 500 dpi
               lambda p: _image(p, 200, 200))                             # 100 dpi: kept
    res = E.optimize_pdf(src, tmp_path / "o.pdf", settings, max_image_dpi=150)
    # Drawn square, the wide one is sharpest across (400 dpi): 150 / 400 of each side.
    assert _images(tmp_path / "o.pdf") == [(450, 300, "/DCTDecode", False),
                                           (300, 300, "/FlateDecode", False),
                                           (200, 200, "/FlateDecode", False)]
    assert res["images_resampled"] == 2 and res["image_dpi"] == 150
    assert res["size_after"] < res["size_before"] / 3
    assert res["target_size_bytes"] is None and res["target_met"] is None


def test_soft_masks_shrink_with_their_picture(tmp_path, settings):
    def masked(pdf):
        return _image(pdf, 800, 800, SMask=_image(pdf, 400, 400, "L"))
    E.optimize_pdf(_doc(tmp_path, masked), tmp_path / "o.pdf", settings, max_image_dpi=100)
    assert _images(tmp_path / "o.pdf") == [(200, 200, "/FlateDecode", (200, 200))]


def test_a_mask_that_wont_decode_keeps_the_picture(tmp_path, settings):
    def masked(pdf):
        return _image(pdf, 800, 800, SMask=_image(pdf, 400, 400, "L", data=b"junk" * 9))
    res = E.optimize_pdf(_doc(tmp_path, masked), tmp_path / "o.pdf", settings,
                         max_image_dpi=100)
    assert res["images_resampled"] == 0 and _images(tmp_path / "o.pdf")[0][:2] == (800, 800)


def test_what_isnt_re_encoded_stays(tmp_path, settings):
    src = _doc(tmp_path, lambda p: _image(p, 800, 800, ColorSpace=pikepdf.Name.DeviceCMYK),
               lambda p: _image(p, 800, 800, data=b"junk" * 99))
    res = E.optimize_pdf(src, tmp_path / "o.pdf", settings, max_image_dpi=72)
    # Unsupported and undecodable: kept.
    assert [i[:2] for i in _images(tmp_path / "o.pdf")] == [(800, 800)] * 2
    assert res["images_resampled"] == 0 and res["image_dpi"] is None


@pytest.mark.parametrize("fails", ["RGB", "L"])
def test_a_picture_or_mask_that_wont_decode_is_kept(tmp_path, settings, monkeypatch, fails):
    def masked(pdf):
        return _image(pdf, 800, 800, SMask=_image(pdf, 800, 800, "L"))
    real = I._decode
    monkeypatch.setattr(I, "_decode", lambda image, mode: None if mode == fails
                        else real(image, mode))
    res = E.optimize_pdf(_doc(tmp_path, masked), tmp_path / "o.pdf", settings,
                         max_image_dpi=72)
    assert res["images_resampled"] == 0


def test_predictor_settings_go_with_the_old_data(tmp_path, settings):
    def predicted(pdf):
        rows = _picture(400, 400).tobytes()
        data = b"".join(b"\0" + rows[i:i + 1200] for i in range(0, len(rows), 1200))
        return _image(pdf, 400, 400, data=zlib.compress(data), DecodeParms=pikepdf.Dictionary(
            Predictor=15, Colors=3, Columns=400, BitsPerComponent=8))
    E.optimize_pdf(_doc(tmp_path, predicted), tmp_path / "o.pdf", settings, max_image_dpi=72)
    with pikepdf.open(tmp_path / "o.pdf") as pdf:
        image = pdf.pages[0].obj.Resources.XObject.Im
        assert "/DecodeParms" not in image and image.Width == 144
        assert pikepdf.PdfImage(image).as_pil_image().size == (144, 144)


def test_a_picture_is_replaced_only_if_that_makes_it_smaller(tmp_path, settings,
                                                             monkeypatch):
    src = _doc(tmp_path, lambda p: _image(p, 800, 800, jpeg=True))
    monkeypatch.setattr(I, "_jpeg", lambda picture, quality: b"x" * 10**7)
    res = E.optimize_pdf(src, tmp_path / "o.pdf", settings, max_image_dpi=72)
    assert res["images_resampled"] == 0 and _images(tmp_path / "o.pdf")[0][:2] == (800, 800)


def test_hostile_images_are_never_decoded(tmp_path, settings, monkeypatch):
    bomb = zlib.compress(b"\0" * 50_000_000)
    src = _doc(tmp_path, lambda p: _image(p, 800, 800, data=bomb))   # far more than 800 x 800
    decoded = []
    monkeypatch.setattr(I, "_decode", lambda *a: decoded.append(a))
    E.optimize_pdf(src, tmp_path / "o.pdf", settings, max_image_dpi=72)
    assert decoded == []


def test_images_never_get_tiny(tmp_path, settings):
    src = _doc(tmp_path, lambda p: _image(p, 1000, 1000), lambda p: _image(p, 8, 8),
               inches=0.01)
    E.optimize_pdf(src, tmp_path / "o.pdf", settings, max_image_dpi=36)
    assert [i[:2] for i in _images(tmp_path / "o.pdf")] == [(8, 8), (8, 8)]


def test_per_call_budgets(tmp_path, settings, monkeypatch):
    src = _doc(tmp_path, lambda p: _image(p, 800, 800), lambda p: _image(p, 800, 800))
    monkeypatch.setattr(I, "MAX_TOTAL_PIXELS", 800 * 800)
    res = E.optimize_pdf(src, tmp_path / "o.pdf", settings, max_image_dpi=72)
    assert res["images_resampled"] == 1
    pdf = pikepdf.open(src)
    largest, unknown = _where(pdf)
    found = I.candidates(pdf, largest, unknown, FAR)
    assert I.lower_resolution(pdf, 72, 80, found, time.monotonic() - 1, False) == {
        "resampled": 0, "kept": 2}
    assert I.candidates(pdf, largest, None, FAR) == []
    assert I.candidates(pdf, {k: [0, 0] for k in largest}, set(), FAR) == []
    assert I.candidates(pdf, largest, unknown, time.monotonic() - 1) == []   # none reached


def test_mask_pixels_count_too(tmp_path, settings, monkeypatch):
    def masked(pdf):
        return _image(pdf, 800, 800, SMask=_image(pdf, 800, 800, "L"))
    monkeypatch.setattr(I, "MAX_TOTAL_PIXELS", 800 * 800 + 10)
    res = E.optimize_pdf(_doc(tmp_path, masked), tmp_path / "o.pdf", settings,
                         max_image_dpi=72)
    assert res["images_resampled"] == 0


def test_damaged_picture_data_keeps_the_picture(tmp_path, settings, monkeypatch):
    def broken(*args):
        raise pikepdf.PdfError("damaged")
    monkeypatch.setattr(I, "_shrink", broken)
    res = E.optimize_pdf(_doc(tmp_path, lambda p: _image(p, 800, 800)), tmp_path / "o.pdf",
                         settings, max_image_dpi=72)
    assert res["images_resampled"] == 0


def test_a_shared_mask_is_not_changed_for_its_other_picture(tmp_path, settings):
    pdf = pikepdf.new()
    mask = _image(pdf, 1200, 1200, "L")
    a, b = _image(pdf, 1200, 1200, SMask=mask), _image(pdf, 1200, 1200, SMask=mask)
    _page(pdf, b"q 72 0 0 72 0 0 cm /A Do Q q 576 0 0 576 0 0 cm /B Do Q", {"/A": a, "/B": b})
    pdf.save(tmp_path / "in.pdf")
    E.optimize_pdf(tmp_path / "in.pdf", tmp_path / "o.pdf", settings, max_image_dpi=150)
    with pikepdf.open(tmp_path / "o.pdf") as out:
        xobjects = out.pages[0].obj.Resources.XObject
        assert (int(xobjects.A.Width), int(xobjects.A.SMask.Width)) == (150, 150)
        assert (int(xobjects.B.Width), int(xobjects.B.SMask.Width)) == (1200, 1200)


# --- optimize_pdf: max_image_dpi and target_size_mb ----------------------------------------


@pytest.mark.parametrize(("args", "message"), [
    ({"max_image_dpi": 35}, "max_image_dpi must be 36 to 1200"),
    ({"max_image_dpi": 1201}, "max_image_dpi"), ({"max_image_dpi": 150.5}, "max_image_dpi"),
    ({"max_image_dpi": True}, "max_image_dpi"), ({"target_size_mb": 0}, "target_size_mb"),
    ({"target_size_mb": -1}, "target_size_mb"), ({"target_size_mb": "5"}, "target_size_mb"),
    ({"target_size_mb": 100_001}, "target_size_mb"), ({"target_size_mb": 1e-10}, "target"),
    ({"target_size_mb": False}, "target"),
])
def test_bad_values(doc, tmp_path, settings, args, message):
    with pytest.raises(PdfUserError, match=message):
        E.optimize_pdf(doc, tmp_path / "o.pdf", settings, **args)
    assert not (tmp_path / "o.pdf").exists()


@pytest.fixture(autouse=True)
def tiny_targets(monkeypatch):
    """Targets no file can meet, to try every step."""
    monkeypatch.setattr(O, "MIN_TARGET_MB", 1e-9)


@pytest.fixture
def photos(tmp_path):
    return _doc(tmp_path, lambda p: _image(p, 1500, 1500, jpeg=True),
                lambda p: _image(p, 1500, 1500))


def test_a_target_lowers_resolution_only_as_far_as_needed(photos, tmp_path, settings):
    full = E.optimize_pdf(photos, tmp_path / "full.pdf", settings)["size_after"]
    first = E.optimize_pdf(photos, tmp_path / "a.pdf", settings,
                           target_size_mb=full * 0.6 / 1e6)
    assert first["target_met"] and first["image_dpi"] == 300
    assert first["size_after"] <= first["target_size_bytes"] == int(full * 0.6)
    assert _images(tmp_path / "a.pdf")[1][2] == "/DCTDecode"     # pictures become JPEG
    deeper = E.optimize_pdf(photos, tmp_path / "b.pdf", settings,
                            target_size_mb=full * 0.03 / 1e6)
    assert deeper["image_dpi"] < 300 and deeper["target_met"]


def test_a_file_already_small_enough_is_only_optimized(photos, tmp_path, settings):
    res = E.optimize_pdf(photos, tmp_path / "o.pdf", settings, target_size_mb=1000)
    assert res["target_met"] and res["images_resampled"] == 0 and res["image_dpi"] is None


def test_an_unreachable_target_keeps_the_smallest_result(photos, tmp_path, settings):
    res = E.optimize_pdf(photos, tmp_path / "o.pdf", settings, target_size_mb=0.000001)
    assert res["target_met"] is False and res["image_dpi"] == 72
    assert (tmp_path / "o.pdf").stat().st_size == res["size_after"]
    assert not [f for f in os.listdir(tmp_path) if "trying" in f]


def test_max_image_dpi_caps_the_steps(photos, tmp_path, settings):
    res = E.optimize_pdf(photos, tmp_path / "o.pdf", settings, max_image_dpi=100,
                         target_size_mb=0.000001)
    assert res["image_dpi"] == 72
    res = E.optimize_pdf(photos, tmp_path / "p.pdf", settings, max_image_dpi=1200,
                         target_size_mb=1000)
    assert res["image_dpi"] is None or res["image_dpi"] == 1200


def test_steps_stop_when_time_runs_out(photos, tmp_path, settings, monkeypatch):
    monkeypatch.setattr(O, "OPTIMIZE_TIME_LIMIT_S", 0)
    res = E.optimize_pdf(photos, tmp_path / "o.pdf", settings, target_size_mb=0.000001)
    assert res["target_met"] is False and res["images_resampled"] == 0


def test_a_step_that_isnt_smaller_is_not_kept(photos, tmp_path, settings, monkeypatch):
    monkeypatch.setattr(O, "TARGET_STEPS", ((1200, 100),))
    res = E.optimize_pdf(photos, tmp_path / "o.pdf", settings, target_size_mb=0.000001)
    assert res["images_resampled"] == 0


def test_a_failed_step_leaves_nothing_behind(photos, tmp_path, settings, monkeypatch):
    def boom(*args):
        raise RuntimeError("disk full")
    monkeypatch.setattr(I, "lower_resolution", boom)
    with pytest.raises(RuntimeError):
        E.optimize_pdf(photos, tmp_path / "o.pdf", settings, target_size_mb=0.000001)
    assert not (tmp_path / "o.pdf").exists()
    assert not [f for f in os.listdir(tmp_path) if "trying" in f]


def test_protection_is_kept(photos, tmp_path, settings):
    with pikepdf.open(photos) as pdf:
        pdf.save(tmp_path / "locked.pdf", encryption=pikepdf.Encryption(
            user="u", owner="o", R=6))
    res = E.optimize_pdf(tmp_path / "locked.pdf", tmp_path / "o.pdf", settings,
                         password="o", target_size_mb=0.000001)
    assert res["password_protected"] and res["images_resampled"] == 2
    assert _images(tmp_path / "o.pdf", password="o")[0][:2] == (144, 144)


# --- the tool ------------------------------------------------------------------------------


@pytest.fixture
def server(settings, monkeypatch):
    import paperboy.server as S
    monkeypatch.setattr(S, "SETTINGS", settings)
    return S


def test_the_tool_and_a_pipeline_step(server, photos, tmp_path):
    res = server.optimize_pdf(str(photos), output_path=str(tmp_path / "t.pdf"),
                              max_image_dpi=100)
    assert res["images_resampled"] == 2 and res["image_dpi"] == 100
    res = server.run_pipeline(str(photos), [
        {"tool": "delete_pages", "args": {"pages": "2"}},
        {"tool": "optimize_pdf", "args": {"target_size_mb": 0.05, "linearize": False}}])
    with pikepdf.open(res["output"]) as pdf:
        assert len(pdf.pages) == 1 and pdf.pages[0].obj.Resources.XObject.Im.Width < 1500
    with pytest.raises(server.ToolError, match="max_image_dpi must be 36 to 1200"):
        server.optimize_pdf(str(photos), max_image_dpi=20)


async def test_the_schema_bounds_the_new_arguments(server):
    tools = {t.name: t for t in await server.mcp.list_tools()}
    tool = tools["optimize_pdf"]
    props = getattr(tool, "input_schema", None) or tool.inputSchema
    props = props["properties"]
    assert "max_image_dpi" in props and "target_size_mb" in props
    text = json_dumps(props)
    assert "1200" in text and "100000" in text
    out = (getattr(tool, "output_schema", None) or tool.outputSchema)["properties"]
    assert {"images_resampled", "image_dpi", "target_size_bytes", "target_met"} <= set(out)


def json_dumps(value):
    import json
    return json.dumps(value)


def test_the_smallest_target(doc, tmp_path, settings, monkeypatch):
    monkeypatch.setattr(O, "MIN_TARGET_MB", 0.01)
    with pytest.raises(PdfUserError, match="target_size_mb must be 0.01 to 100000"):
        E.optimize_pdf(doc, tmp_path / "o.pdf", settings, target_size_mb=0.005)


def test_a_failure_leaves_an_existing_output_as_it_was(photos, tmp_path, settings,
                                                        monkeypatch):
    (tmp_path / "o.pdf").write_bytes(b"the user's file")

    def boom(*args):
        raise RuntimeError("disk full")
    monkeypatch.setattr(I, "lower_resolution", boom)
    with pytest.raises(RuntimeError):
        E.optimize_pdf(photos, tmp_path / "o.pdf", settings, target_size_mb=0.000001)
    assert (tmp_path / "o.pdf").read_bytes() == b"the user's file"
    assert sorted(os.listdir(tmp_path)) == ["in.pdf", "o.pdf"]


def test_steps_stop_when_no_picture_can_shrink(doc, tmp_path, settings, monkeypatch):
    tries = []
    real = I.lower_resolution
    monkeypatch.setattr(I, "lower_resolution", lambda *a: tries.append(a) or real(*a))
    res = E.optimize_pdf(doc, tmp_path / "o.pdf", settings, target_size_mb=0.000001)
    assert len(tries) == 1 and res["target_met"] is False


def test_the_output_has_the_usual_mode(photos, tmp_path, settings):
    import stat
    E.optimize_pdf(photos, tmp_path / "o.pdf", settings, target_size_mb=0.000001)
    made = tmp_path / "plain.pdf"
    made.write_bytes(b"")
    assert stat.S_IMODE(os.stat(tmp_path / "o.pdf").st_mode) == \
        stat.S_IMODE(os.stat(made).st_mode)


def test_odd_resources_and_appearances(monkeypatch):
    pdf = pikepdf.new()
    a = _image(pdf, 50, 50)
    postscript = pdf.make_stream(b"", Subtype=pikepdf.Name.PS)
    page = _page(pdf, b"72 0 0 72 0 0 cm /A Do", {"/A": a, "/P": postscript})
    page.obj.Annots = pikepdf.Array([pikepdf.Dictionary(AP=pikepdf.Dictionary(N=3))])
    bare = _page(pdf, b"", {})
    del bare.obj["/Resources"]
    assert _where(pdf) == ({a.objgen: [1.0, 1.0]}, set())
    monkeypatch.setattr(I, "MAX_IMAGES", 0)
    assert I.candidates(pdf, {a.objgen: [1.0, 1.0]}, set(), FAR) == []


def test_time_runs_out_going_through_the_file():
    assert I.placements(pikepdf.new(), time.monotonic() - 1) == ({}, None)


# --- second review --------------------------------------------------------------------------


def test_the_token_estimate_counts_every_separator(monkeypatch):
    for content in (b"q\x0b" * 80, b"]" * 40 + b" q"):
        pdf = pikepdf.new()
        im = _image(pdf, 50, 50)
        _page(pdf, content + b" 72 0 0 72 0 0 cm /A Do", {"/A": im})
        monkeypatch.setattr(I, "MAX_STREAM_TOKENS", 60)
        assert _where(pdf)[1] == {im.objgen}, content[:4]


def test_a_stream_repeated_in_a_page_counts_each_time(monkeypatch):
    pdf = pikepdf.new()
    im = _image(pdf, 50, 50)
    page = _page(pdf, b"72 0 0 72 0 0 cm /A Do", {"/A": im})
    one = page.obj.Contents
    page.obj.Contents = pikepdf.Array([one] * 4)
    monkeypatch.setattr(I, "MAX_CONTENT", 3 * len(one.read_bytes()))
    assert _where(pdf)[1] == {im.objgen}
    monkeypatch.setattr(I, "MAX_CONTENT", 16 * 2**20)
    monkeypatch.setattr(I, "MAX_TOTAL_CONTENT", 3 * len(one.read_bytes()))
    assert _where(pdf)[1] == {im.objgen}


def test_content_pikepdf_cant_represent_leaves_images_alone(tmp_path, settings):
    pdf = pikepdf.new()
    im = _image(pdf, 800, 800)
    _page(pdf, b"[+] Q 72 0 0 72 0 0 cm /A Do", {"/A": im})
    assert _where(pdf)[1] == {im.objgen}
    pdf.save(tmp_path / "in.pdf")
    res = E.optimize_pdf(tmp_path / "in.pdf", tmp_path / "o.pdf", settings, max_image_dpi=72)
    assert res["images_resampled"] == 0


def test_images_left_alone_are_never_capped(monkeypatch):
    monkeypatch.setattr(I, "MAX_IMAGES", 1)
    pdf = pikepdf.new()
    many = [_image(pdf, 8, 8) for _ in range(5)]
    form = _form(pdf, b"", {f"/I{n}": im for n, im in enumerate(many)})
    page = _page(pdf, b"72 0 0 72 0 0 cm /I4 Do", {"/I4": many[4]})
    page.obj.Annots = pikepdf.Array([pikepdf.Dictionary(AP=pikepdf.Dictionary(N=form))])
    assert _where(pdf)[1] == {im.objgen for im in many}


def test_glyphs_and_patterns_on_unreadable_pages_are_left_alone():
    pdf = pikepdf.new()
    a, b, c = (_image(pdf, 50, 50) for _ in range(3))
    _page(pdf, b"72 0 0 72 0 0 cm /A Do", {"/A": a, "/B": b, "/C": c})
    page = _page(pdf, b"72 0 x 72 0 0 cm", {})                   # unreadable
    page.obj.Resources.Font = pikepdf.Dictionary(T=pikepdf.Dictionary(
        Subtype=pikepdf.Name.Type3, Resources=pikepdf.Dictionary(
            XObject=pikepdf.Dictionary(A=a))), H=pikepdf.Dictionary(Subtype=pikepdf.Name.Type1))
    page.obj.Resources.Pattern = pikepdf.Dictionary(P=pdf.make_stream(
        b"", PatternType=1, Resources=pikepdf.Dictionary(XObject=pikepdf.Dictionary(B=b))))
    assert _where(pdf)[1] == {a.objgen, b.objgen}


def test_replacing_a_file_keeps_its_permissions(photos, tmp_path, settings):
    import stat
    out = tmp_path / "o.pdf"
    out.write_bytes(b"old")
    out.chmod(0o600)
    E.optimize_pdf(photos, out, settings, max_image_dpi=100)
    assert stat.S_IMODE(out.stat().st_mode) == 0o600 and out.read_bytes()[:4] == b"%PDF"


def test_what_cant_be_replaced(photos, tmp_path, settings):
    locked = tmp_path / "locked.pdf"
    locked.write_bytes(b"old")
    locked.chmod(0o400)
    (tmp_path / "folder.pdf").mkdir()
    (tmp_path / "link.pdf").symlink_to(tmp_path / "elsewhere.pdf")
    for out, message in ((locked, "read-only"), (tmp_path / "folder.pdf", "not a regular"),
                         (tmp_path / "link.pdf", "not a regular")):
        with pytest.raises(PdfUserError, match=message):
            E.optimize_pdf(photos, out, settings)
    assert locked.read_bytes() == b"old" and not (tmp_path / "elsewhere.pdf").exists()


def test_long_output_names_work(photos, tmp_path, settings):
    out = tmp_path / ("n" * 240 + ".pdf")
    assert E.optimize_pdf(photos, out, settings)["output"] == str(out)


def test_beaten_attempts_are_removed_as_it_goes(photos, tmp_path, settings, monkeypatch):
    seen = []
    real = I.lower_resolution

    def spy(*args):
        seen.append(len([p for p in tmp_path.rglob("*.pdf") if ".paperboy-" in str(p)]))
        return real(*args)
    monkeypatch.setattr(I, "lower_resolution", spy)
    E.optimize_pdf(photos, tmp_path / "o.pdf", settings, target_size_mb=0.000001)
    assert len(seen) > 2 and max(seen) <= 1


def test_content_in_a_filter_that_cant_be_measured_leaves_images_alone():
    pdf = pikepdf.new()
    im = _image(pdf, 50, 50)
    page = _page(pdf, b"72 0 0 72 0 0 cm /A Do", {"/A": im})
    page.obj.Contents.Filter = pikepdf.Name.LZWDecode
    assert _where(pdf)[1] == {im.objgen}
