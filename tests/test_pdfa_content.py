"""What pages draw: fonts used visibly without their program, device colours, and
inline pictures PDF/A forbids; within a budget, so hostile content stops early."""
from __future__ import annotations

import time
import zlib

import pikepdf
import pytest
from pikepdf import Array, Dictionary, Name, String

from paperboy.engine import pdfa_content as C


def _font(pdf, base="Helvetica", embedded=False, **entries):
    font = Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name("/" + base),
                      **entries)
    if embedded:
        font.FontDescriptor = Dictionary(FontFile=pdf.make_stream(b"font"))
    return pdf.make_indirect(font)


def _page(content: bytes, **resources):
    pdf = pikepdf.new()
    pdf.add_blank_page()
    page = pdf.pages[0]
    page.obj.Resources = Dictionary(**resources)
    page.obj.Contents = pdf.make_stream(content)
    return pdf, page


def _usage(page, deadline=60):
    usage = C.Usage(time.monotonic() + deadline)
    usage.page(page)
    return usage


def test_visible_text_in_a_font_without_its_program_is_found():
    pdf, page = _page(b"BT /F1 12 Tf (x) Tj ET")
    page.Resources.Font = Dictionary(F1=_font(pdf))
    assert _usage(page).unembedded == {"Helvetica"}


@pytest.mark.parametrize("content", [
    b"BT /F1 12 Tf ET",                             # chosen, nothing drawn
    b"BT 3 Tr /F1 12 Tf (ocr) Tj ET",               # invisible text (OCR layers)
    b"BT /F9 12 Tf (x) Tj ET",                      # a font that isn't there
    b"(x) Tj",                                      # no font chosen
])
def test_fonts_not_drawn_visibly_dont_count(content):
    pdf, page = _page(content)
    page.Resources.Font = Dictionary(F1=_font(pdf))
    assert _usage(page).unembedded == set()


def test_text_state_follows_save_and_restore():
    pdf, page = _page(b"/F1 12 Tf q 3 Tr Q (shown) Tj q 3 Tr (hidden) Tj Q Q Q")
    page.Resources.Font = Dictionary(F1=_font(pdf))
    assert _usage(page).unembedded == {"Helvetica"}


def test_embedded_and_composite_fonts():
    pdf, page = _page(b"/A 1 Tf (x) Tj /B 1 Tf (x) Tj /C 1 Tf (x) Tj /D 1 Tf (x) Tj")
    good_kid = Dictionary(Type=Name.Font, Subtype=Name.CIDFontType2, BaseFont=Name.K,
                          FontDescriptor=Dictionary(FontFile2=pdf.make_stream(b"f")))
    page.Resources.Font = Dictionary(
        A=_font(pdf, "Emb", embedded=True),
        B=Dictionary(Type=Name.Font, Subtype=Name.Type0, BaseFont=Name.Good,
                     DescendantFonts=Array([good_kid])),
        C=Dictionary(Type=Name.Font, Subtype=Name.Type0, BaseFont=Name.NoKids),
        D=Dictionary(Type=Name.Font, Subtype=Name.Type0, BaseFont=Name.BadKid,
                     DescendantFonts=Array([5])))
    assert _usage(page).unembedded == {"NoKids", "BadKid"}
    assert C.embedded(Dictionary(Subtype=Name.TrueType)) is False


def test_type3_glyphs_are_read_once_with_their_resources():
    pdf, page = _page(b"/T 1 Tf (ab) Tj (c) Tj")
    glyph = pdf.make_stream(b"/Inner 1 Tf (x) Tj")
    page.Resources.Font = Dictionary(T=pdf.make_indirect(Dictionary(
        Type=Name.Font, Subtype=Name.Type3, CharProcs=Dictionary(a=glyph, b=glyph, n=5),
        Resources=Dictionary(Font=Dictionary(Inner=_font(pdf, "Inner"))))))
    usage = _usage(page)
    assert usage.unembedded == {"Inner"}
    assert usage.draws_left == C.MAX_DRAWS - 2  # two glyphs, read once


def test_type3_glyphs_without_their_own_resources_use_the_page_s():
    pdf, page = _page(b"/T 1 Tf (a) Tj")
    page.Resources.Font = Dictionary(
        T=Dictionary(Type=Name.Font, Subtype=Name.Type3,
                     CharProcs=Dictionary(a=pdf.make_stream(b"/P 1 Tf (x) Tj"))),
        P=_font(pdf, "PageFont"))
    assert _usage(page).unembedded == {"PageFont"}


def test_forms_are_followed_with_the_state_and_their_own_resources():
    pdf, page = _page(b"/F 9 Tf /X Do /Y Do /Img Do /Missing Do /Mask Do")
    inner = pdf.make_stream(b"(x) Tj", Type=Name.XObject, Subtype=Name.Form)  # page's font
    own = pdf.make_stream(b"/G 1 Tf (y) Tj", Type=Name.XObject, Subtype=Name.Form,
                          Resources=Dictionary(Font=Dictionary(G=_font(pdf, "Own"))))
    page.Resources.Font = Dictionary(F=_font(pdf, "Page"))
    page.Resources.XObject = Dictionary(
        X=inner, Y=own, Img=pdf.make_stream(b"\0", Subtype=Name.Image,
                                            ColorSpace=Name.DeviceCMYK),
        Mask=pdf.make_stream(b"\0", Subtype=Name.Image, ImageMask=True,
                             ColorSpace=Name.DeviceRGB))
    usage = _usage(page)
    assert usage.unembedded == {"Page", "Own"}
    assert usage.colours == {"CMYK"}


@pytest.mark.parametrize("content, colours", [
    (b"1 0 0 rg", {"RGB"}), (b"1 0 0 RG", {"RGB"}), (b"0 0 0 1 k", {"CMYK"}),
    (b"0 0 0 1 K", {"CMYK"}), (b"0.5 g", set()), (b"/DeviceRGB cs", {"RGB"}),
    (b"/DeviceCMYK CS", {"CMYK"}), (b"/Idx cs", {"CMYK"}), (b"/Sep cs", {"CMYK"}),
    (b"/Pat cs", {"RGB"}), (b"/ICC cs", set()), (b"/Odd cs", set()), (b"/Nope cs", set()),
    (b"/Sh sh", {"RGB"}), (b"/Gone sh", set()), (b"/Loop cs", set()),
])
def test_device_colours_painted(content, colours):
    pdf, page = _page(content)
    page.Resources.ColorSpace = Dictionary(
        Idx=Array([Name.Indexed, Name.DeviceCMYK, 0, String("\0\0\0\0")]),
        Sep=Array([Name.Separation, Name.Spot, Name.DeviceCMYK, Dictionary()]),
        Pat=Array([Name.Pattern, Name.DeviceRGB]),
        ICC=Array([Name.ICCBased, pdf.make_stream(b"", N=3)]),
        Odd=Array([Name.Lab]), Loop=Name.Loop)
    page.Resources.Shading = Dictionary(Sh=Dictionary(ShadingType=2,
                                                      ColorSpace=Name.DeviceRGB))
    assert _usage(page).colours == colours


def test_deeply_nested_colour_spaces_stop():
    usage = C.Usage(time.monotonic() + 60)
    space = Name.DeviceCMYK
    for _ in range(8):
        space = Array([Name.Indexed, space, 0, String("")])
    usage._space(space, None)
    assert usage.colours == set()


@pytest.mark.parametrize("image, colours, problem", [
    (b"BI /W 1 /H 1 /CS /CMYK /BPC 8 ID \0\0\0\0 EI", {"CMYK"}, False),
    (b"BI /W 1 /H 1 /CS /RGB /BPC 8 ID \0\0\0 EI", {"RGB"}, False),
    (b"BI /W 1 /H 1 /CS /G /BPC 8 /F /LZW ID \x80\0 EI", set(), True),
    (b"BI /W 1 /H 1 /CS /G /BPC 8 /F [/AHx /LZW] ID 00> EI", set(), True),
    (b"BI /W 1 /H 1 /CS /G /BPC 8 /I true ID \0 EI", set(), True),
    (b"BI /W 1 /H 1 /IM true /CS /RGB ID \0 EI", set(), False),
    (b"BI /W 1 /H 1 /CS /Named /BPC 8 ID \0\0\0\0 EI", {"CMYK"}, False),
])
def test_inline_pictures(image, colours, problem):
    pdf, page = _page(b"q " + image + b" Q")
    page.Resources.ColorSpace = Dictionary(Named=Name.DeviceCMYK)
    usage = _usage(page)
    assert usage.colours == colours
    assert (usage.problems == {"inline pictures PDF/A doesn't allow"}) is problem


def test_an_unreadable_inline_picture_is_a_problem():
    usage = C.Usage(time.monotonic() + 60)

    class Broken:
        @property
        def obj(self):
            raise ValueError("bad inline image")

    usage._inline(Broken(), None)
    assert usage.problems == {"inline pictures PDF/A doesn't allow"}


def test_annotation_appearances_are_read():
    pdf, page = _page(b"")
    font = _font(pdf, "InAppearance")
    look = pdf.make_stream(b"/F 1 Tf (x) Tj", Resources=Dictionary(Font=Dictionary(F=font)))
    page.obj.Annots = Array([
        Dictionary(Subtype=Name.Square, AP=Dictionary(N=look)),
        Dictionary(Subtype=Name.Widget, AP=Dictionary(N=Dictionary(On=look, Off=5))),
        Dictionary(Subtype=Name.Square, AP=5), 7])
    assert _usage(page).unembedded == {"InAppearance"}


def test_pages_with_several_or_no_content_streams_and_inherited_resources():
    pdf, page = _page(b"")
    page.obj.Contents = Array([pdf.make_stream(b"/F 1 Tf"), pdf.make_stream(b"(x) Tj")])
    del page.obj["/Resources"]
    pdf.Root.Pages.Resources = Dictionary(Font=Dictionary(F=_font(pdf, "Inherited")))
    assert _usage(page).unembedded == {"Inherited"}
    del page.obj["/Contents"]
    assert _usage(page).unembedded == set()
    assert C.page_resources(Dictionary()) is None


@pytest.mark.parametrize("make", [
    lambda pdf: pdf.make_stream(b"x", Filter=Name.LZWDecode),         # can't be measured
    lambda pdf: Array([pdf.make_stream(b"q"), Dictionary()]),          # not a stream
    lambda pdf: pdf.make_stream(zlib.compress(b"q " * (C.MAX_STREAM // 2 + 10)),
                                Filter=Name.FlateDecode),              # too big
    lambda pdf: pdf.make_stream(b"q\n" * (C.MAX_STREAM_TOKENS + 10)),   # too many tokens
])
def test_content_that_cant_be_read_safely_stops(make):
    pdf, page = _page(b"")
    page.obj.Contents = make(pdf)
    with pytest.raises(C.TooComplex):
        _usage(page)


def test_content_pikepdf_cant_parse_stops(monkeypatch):
    def broken(*args, **kwargs):
        raise pikepdf.PdfError("parse error")

    pdf, page = _page(b"q Q")
    monkeypatch.setattr(C.pikepdf, "parse_content_stream", broken)
    with pytest.raises(C.TooComplex):
        _usage(page)


def test_the_call_budget_is_shared(monkeypatch):
    monkeypatch.setattr(C, "MAX_TOTAL", 100)
    pdf, page = _page(b"q Q " * 20)
    usage = C.Usage(time.monotonic() + 60)
    usage.page(page)
    with pytest.raises(C.TooComplex):
        usage.page(page)  # the same content again: over the bytes left


def test_forms_nested_too_deep_or_too_often_stop(monkeypatch):
    pdf, page = _page(b"/X Do")
    form = pdf.make_stream(b"/X Do", Subtype=Name.Form)
    form.Resources = Dictionary(XObject=Dictionary(X=form))
    page.Resources.XObject = Dictionary(X=form)
    with pytest.raises(C.TooComplex):
        _usage(page)
    monkeypatch.setattr(C, "MAX_DRAWS", 3)
    with pytest.raises(C.TooComplex):
        _usage(page)


def test_the_deadline_stops_reading():
    pdf, page = _page(b"/X Do")
    page.Resources.XObject = Dictionary(X=pdf.make_stream(b"", Subtype=Name.Form))
    with pytest.raises(C.TooComplex):
        _usage(page, deadline=-1)


def test_operands_of_the_wrong_kind_are_ignored():
    pdf, page = _page(b"(x) Tf /n Tr 5 Do 5 cs 5 sh Tf Do cs sh")
    page.Resources.Font = 5
    usage = _usage(page)
    assert usage.unembedded == set() and usage.colours == set()


def test_fonts_set_through_a_graphics_state():
    pdf, page = _page(b"/G gs (x) Tj /Bad gs /Gone gs (y) Tj")
    page.Resources.ExtGState = Dictionary(G=Dictionary(Font=Array([_font(pdf, "ViaGs"), 9])),
                                          Bad=Dictionary(Font=Array([5, 9])))
    assert _usage(page).unembedded == {"ViaGs"}


def test_soft_mask_groups_are_read():
    pdf, page = _page(b"/M gs")
    group = pdf.make_stream(b"0 0 0 1 k", Subtype=Name.Form)
    page.Resources.ExtGState = Dictionary(M=Dictionary(SMask=Dictionary(G=group)))
    assert _usage(page).colours == {"CMYK"}


def test_patterns_painted_with_scn():
    pdf, page = _page(b"/Pattern cs /P0 scn /Pattern CS /Sh SCN /Gone scn 0.5 scn /Odd scn")
    tile = pdf.make_stream(b"/T 9 Tf (x) Tj 1 0 0 rg", PatternType=1,
                           Resources=Dictionary(Font=Dictionary(T=_font(pdf, "InTile"))))
    page.Resources.Pattern = Dictionary(
        P0=tile,
        Sh=Dictionary(PatternType=2, Shading=Dictionary(ColorSpace=Name.DeviceCMYK)),
        Odd=Dictionary(PatternType=2, Shading=5))
    usage = _usage(page)
    assert usage.unembedded == {"InTile"} and usage.colours == {"RGB", "CMYK"}


def test_transparency_groups_colour_spaces():
    pdf, page = _page(b"/X Do")
    page.obj.Group = Dictionary(S=Name.Transparency, CS=Name.DeviceCMYK)
    form = pdf.make_stream(b"", Subtype=Name.Form,
                           Group=Dictionary(S=Name.Transparency, CS=Name.DeviceRGB))
    page.Resources.XObject = Dictionary(X=form)
    assert _usage(page).colours == {"CMYK", "RGB"}


def test_measuring_stops_at_what_the_budget_has_left(monkeypatch):
    pdf, page = _page(b"q Q " * 100)
    usage = C.Usage(time.monotonic() + 60)
    usage.bytes_left = 10
    with pytest.raises(C.TooComplex):
        usage.page(page)


def test_a_stream_listed_many_times_is_decoded_once(monkeypatch):
    pdf, page = _page(b"")
    stream = pdf.make_stream(b"q Q ")
    page.obj.Contents = Array([stream] * 1000)
    decoded = []
    real = C.decode
    monkeypatch.setattr(C, "decode", lambda s, limit: decoded.append(1) or real(s, limit))
    usage = _usage(page)
    assert len(decoded) == 1 and usage.bytes_left == C.MAX_TOTAL - 4000
