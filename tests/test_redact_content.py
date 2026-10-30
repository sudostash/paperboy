"""The pieces of redaction: fonts, geometry, picture editing, the pattern worker."""
from __future__ import annotations

import io
import json
import math
import time
import zlib

import pikepdf
import pytest

from paperboy.engine import pattern_worker
from paperboy.engine import redact as R
from paperboy.engine import redact_content as C
from paperboy.errors import PdfUserError

Name = pikepdf.Name


# --- pattern worker ---------------------------------------------------------------------


def test_worker_finds_matches():
    answer = pattern_worker.run({"patterns": ["b+", "x*"], "texts": ["abba", "bob"],
                                 "ignore_case": False, "max_matches": 10})
    assert answer == {"matches": [[0, 0, 1, 3], [1, 0, 0, 1], [1, 0, 2, 3]],
                      "truncated": False}


def test_worker_ignores_case_and_stops_at_the_limit():
    answer = pattern_worker.run({"patterns": ["B"], "texts": ["bbb"], "ignore_case": True,
                                 "max_matches": 2})
    assert answer == {"matches": [[0, 0, 0, 1], [0, 0, 1, 2]], "truncated": True}


def test_worker_reports_an_invalid_pattern():
    answer = pattern_worker.run({"patterns": ["a", "("], "texts": ["a"],
                                 "ignore_case": False, "max_matches": 5})
    assert answer["invalid"] == 1 and "missing )" in answer["error"]


def test_worker_main(monkeypatch, capsys):
    request = {"patterns": ["a"], "texts": ["a"], "ignore_case": False, "max_matches": 5,
               "cpu_seconds": 30, "memory_bytes": 2**34}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(request)))
    limits = []
    monkeypatch.setattr(pattern_worker, "_limit", lambda *a: limits.append(a))
    pattern_worker.main()
    assert json.loads(capsys.readouterr().out)["matches"] == [[0, 0, 0, 1]]
    assert limits == [(30, 2**34)]


def test_worker_crash_is_reported(monkeypatch):
    class Failed:
        returncode, stdout = 1, b""
    monkeypatch.setattr(R.subprocess, "run", lambda *a, **k: Failed())
    with pytest.raises(PdfUserError, match="took too long to match"):
        R._run_patterns(["a"], ["a"], True, time.monotonic() + 10)


# --- geometry and numbers ---------------------------------------------------------------


def test_numbers_and_rectangles():
    assert C._num(3) == 3.0
    with pytest.raises(C.Unsure, match="invalid number"):
        C._num(float("nan"))
    assert C._rect(pikepdf.Array([3, 4, 1, 2])) == (1, 2, 3, 4)
    assert C._rect(pikepdf.Array([1, 2, 3])) is None
    assert C._rect(pikepdf.Array([1, 2, 3, Name.X])) is None
    assert C._rect([1, 2, 3, 4]) is None


def test_matrix():
    assert C._matrix(pikepdf.Array([1, 0, 0, 1, 5, 6])) == (1, 0, 0, 1, 5, 6)
    with pytest.raises(C.Unsure):
        C._matrix(Name.X)


def test_path_points():
    assert C._path_points("m", [1, 2], C._IDENTITY, []) == [(1, 2)]
    assert C._path_points("c", [1, 2, 3, 4, 5, 6], C._IDENTITY, [])[-1] == (5, 6)
    assert C._path_points("h", [], C._IDENTITY, [(1, 1)]) == [(1, 1)]
    assert C._path_points("l", [1], C._IDENTITY, []) is None
    assert C._path_points("re", [1, 2], C._IDENTITY, []) is None
    assert C._path_points("l", [Name.X, 1], C._IDENTITY, []) is None
    assert C._path_points("l", [float("inf"), 1], C._IDENTITY, []) is None


def test_boxes():
    assert C.boxes([(1, 2, 3, 5)], "white") == b"q 1 g 1.000 2.000 2.000 3.000 re f Q\n"
    assert C.boxes([(1, 2, 3, 5)], None) == b""
    assert C.boxes([], "black") == b""


def test_pixel_boxes():
    identity = (10, 0, 0, 10, 0, 0)   # a 10 x 10 point image of 10 x 10 pixels
    assert C.pixel_boxes(identity, [(2, 2, 4.5, 4)], 10, 10) == [(2, 6, 5, 8)]
    assert C.pixel_boxes((0, 0, 0, 0, 0, 0), [(0, 0, 1, 1)], 10, 10) == []
    assert C.pixel_boxes(identity, [(20, 20, 30, 30)], 10, 10) == []
    # At 45 degrees, rows widen then narrow.
    turned = (10 / math.sqrt(2), 10 / math.sqrt(2), -10 / math.sqrt(2), 10 / math.sqrt(2),
              0, 0)
    found = C.pixel_boxes(turned, [(-1, 6, 1, 8)], 10, 10)
    assert found and all(0 <= x0 < x1 <= 10 for x0, _, x1, _ in found)


def test_clip_skips_rows_a_thin_area_misses():
    flat = (10, 0, 0, 10, 0, 0)
    assert C.pixel_boxes(flat, [(0, 5, 10, 5)], 10, 10) == []


def test_clear_bits():
    data = bytearray(b"\xff\xff\xff")
    C._clear_bits(data, 0, 3, 5)
    assert data == bytearray(b"\xe7\xff\xff")
    data = bytearray(b"\xff\xff\xff")
    C._clear_bits(data, 0, 4, 20)
    assert data == bytearray(b"\xf0\x00\x0f")
    C._clear_bits(data, 0, 5, 5)
    assert data == bytearray(b"\xf0\x00\x0f")


def test_components():
    pdf = pikepdf.new()
    icc = pdf.make_stream(b"", N=3)
    assert C._components(Name.DeviceCMYK) == 4
    assert C._components(pikepdf.Array([Name.ICCBased, icc])) == 3
    assert C._components(pikepdf.Array([Name.ICCBased, pdf.make_stream(b"", N=2)])) is None
    assert C._components(pikepdf.Array([Name.DeviceN, pikepdf.Array([Name.A, Name.B]),
                                        Name.DeviceRGB])) == 2
    assert C._components(pikepdf.Array([Name.Indexed, Name.DeviceRGB, 1, b""])) == 1
    assert C._components(pikepdf.Array([])) is None
    assert C._components(5) is None


def test_resources_copy_before_changing():
    shared = pikepdf.Dictionary(XObject=pikepdf.Dictionary(A=1, B=2))
    res = C.Resources(shared)
    assert res.get("/XObject", Name.A) == 1 and res.get("/XObject", "A") is None
    assert res.get("/Font", Name.A) is None
    assert res.add("/XObject", 3, "A") == Name("/A1")
    assert res.add("/XObject", 4, "A") == Name("/A2")
    assert set(res.res.XObject.keys()) == {"/A", "/B", "/A1", "/A2"}
    assert set(shared.XObject.keys()) == {"/A", "/B"}
    assert not C.Resources(None).private


def test_parse_measures_first():
    pdf = pikepdf.new()
    budget = C.Budget(time.monotonic() + 60)
    stream = pdf.make_stream(b"1 0 0 1 0 0 cm")
    assert len(C.parse(stream, [stream], budget)) == 1
    assert C.parse(stream, [], budget) == []
    budget.content_left = 2
    with pytest.raises(C.Unsure, match="too much content"):
        C.parse(stream, [stream], budget)
    budget = C.Budget(time.monotonic() + 60)
    budget.tokens_left = 2
    with pytest.raises(C.Unsure, match="too much content"):
        C.parse(stream, [stream], budget)
    odd = pdf.make_stream(b"x")
    odd.Filter = Name.DCTDecode
    with pytest.raises(C.Unsure, match="can't be read"):
        C.parse(odd, [odd], C.Budget(time.monotonic() + 60))
    with pytest.raises(PdfUserError, match="took too long"):
        C.parse(stream, [stream], C.Budget(time.monotonic() - 1))


def test_content_streams():
    assert C.content_streams(pikepdf.Dictionary()) == []
    with pytest.raises(C.Unsure, match="damaged"):
        C.content_streams(pikepdf.Dictionary(Contents=5))


# --- fonts ------------------------------------------------------------------------------


def font(**keys):
    return pikepdf.Dictionary(Type=Name.Font, **keys)


def test_simple_font_widths():
    f = C.Font(font(Subtype=Name.TrueType, FirstChar=65, Widths=[500, 600],
                    FontDescriptor=pikepdf.Dictionary(MissingWidth=250, Ascent=900,
                                                      Descent=-100)))
    assert f.codes(b"AB ") == [(b"A", 0.5, False), (b"B", 0.6, False), (b" ", 0.25, True)]
    assert (f.ascent, f.descent) == (0.9, -0.1)
    plain = C.Font(font(Subtype=Name.TrueType, FirstChar=Name.X, Widths=[500]))
    assert plain.codes(b"\x00") == [(b"\x00", 0.5, False)]
    assert (plain.ascent, plain.descent) == (0.8, -0.2)


@pytest.mark.parametrize(("descriptor", "heights"), [
    ({"Ascent": 700, "Descent": 200}, (0.7, 0.0)),        # a descent above the baseline
    ({"Ascent": Name.X, "Descent": 0}, (0.8, -0.2)),
    ({"Ascent": 0, "Descent": 0, "FontBBox": [0, -300, 1000, 900]}, (0.9, -0.3)),
    ({"Ascent": 50000, "Descent": 0, "FontBBox": [0, 0, 1000, 10]}, (0.8, -0.2)),
])
def test_font_heights(descriptor, heights):
    f = C.Font(font(Subtype=Name.TrueType, Widths=[1],
                    FontDescriptor=pikepdf.Dictionary(**descriptor)))
    assert (f.ascent, f.descent) == pytest.approx(heights)


def test_standard_fonts():
    assert C._standard(font(BaseFont=Name("/ABCDEF+Helvetica-Bold"))) == "Helvetica-Bold"
    assert C._standard(font(BaseFont=Name("/Arial,BoldItalic"))) == "Helvetica-BoldOblique"
    assert C._standard(font(BaseFont=Name("/Arial-BoldMT"))) == "Helvetica-Bold"
    assert C._standard(font(BaseFont=Name("/CourierNewPSMT"))) == "Courier"
    assert C._standard(font(BaseFont=Name("/Times-Italic"))) == "Times-Italic"
    assert C._standard(font(BaseFont=Name("/ArialNarrow"))) is None
    assert C._standard(font(BaseFont=Name("/Verdana"))) is None


def test_encodings_and_differences():
    f = C.Font(font(Subtype=Name.Type1, BaseFont=Name.Helvetica, Encoding=pikepdf.Dictionary(
        Differences=pikepdf.Array([65, Name.W, Name.i, 300, Name.A, Name.X, Name.Y]))))
    assert f.codes(b"AB")[0][1] == pytest.approx(0.944)   # "W" in Helvetica
    assert f.codes(b"AB")[1][1] == pytest.approx(0.222)   # "i"
    symbol = C.Font(font(Subtype=Name.Type1, BaseFont=Name.Symbol))
    assert symbol.codes(b"a")[0][1] == pytest.approx(0.631)  # alpha


def test_type3_heights():
    f = C.Font(font(Subtype=Name.Type3, FontMatrix=[0.01, 0, 0, -0.01, 0, 0],
                    FontBBox=[0, 0, 50, 80], Widths=[50], FirstChar=0))
    assert f.codes(b"\x00") == [(b"\x00", 0.5, False)]
    assert (f.descent, f.ascent) == pytest.approx((-0.8, 0.0))
    bare = C.Font(font(Subtype=Name.Type3, FontMatrix=[0.001, 0, 0, 0.001, 0, 0],
                       Widths=[500]))
    assert (bare.ascent, bare.descent) == (0.8, -0.2)
    with pytest.raises(C.Unsure, match="no widths"):
        C.Font(font(Subtype=Name.Type3, FontMatrix=[0.001, 0, 0, 0.001, 0, 0]))


IDENTITY_H = Name("/Identity-H")


def composite(encoding=IDENTITY_H, **cid):
    return font(Subtype=Name.Type0, Encoding=encoding, DescendantFonts=pikepdf.Array(
        [pikepdf.Dictionary(Subtype=Name.CIDFontType2, **cid)]))


def test_composite_widths():
    f = C.Font(composite(DW=500, W=pikepdf.Array([1, [100, 200], 10, 20, 300])))
    assert [w for _, w, _ in f.codes(b"\x00\x01\x00\x02\x00\x0f\x00\x30\x00\x00")] == \
        [0.1, 0.2, 0.3, 0.5, 0.5]
    assert C.Font(composite()).codes(b"\x00\x01")[0][1] == 1.0   # DW defaults to 1000


@pytest.mark.parametrize(("widths", "reason"), [
    ([Name.X, 1], "damaged"),
    ([1, 2], "damaged"),
    ([1, [1] * 11], "too many widths"),
    ([1, 2, 3] * 11, "too many widths"),
])
def test_composite_widths_that_cant_be_read(monkeypatch, widths, reason):
    monkeypatch.setattr(C, "MAX_WIDTHS", 10)
    with pytest.raises(C.Unsure, match=reason):
        C.Font(composite(W=pikepdf.Array(widths)))


def test_composite_without_descendants():
    bad = font(Subtype=Name.Type0, Encoding=Name("/Identity-H"))
    with pytest.raises(C.Unsure, match="a font is damaged"):
        C.Font(bad)


def test_cmaps():
    pdf = pikepdf.new()
    cmap = pdf.make_stream(
        b"begincodespacerange <00> <7F> <8000> <FFFF> <01> endcodespacerange "
        b"begincidrange <20> <7F> 100 <8000> <80FF> 1000 <01> <02> endcidrange "
        b"begincidchar <8001> 7 5 6 endcidchar")
    f = C.Font(composite(encoding=cmap, W=pikepdf.Array([7, [70], 100, 200, 10])))
    codes = f.codes(b"\x20\x80\x01\x80\x05")
    assert [c for c, _, _ in codes] == [b" ", b"\x80\x01", b"\x80\x05"]
    assert codes[0] == (b" ", 0.01, True) and codes[1][1] == 0.07
    assert f._cid(b"\x80\x05") == 1005 and f._cid(b"\x7f") == 195
    assert f._cid(b"\x00\x00\x00") == 0


@pytest.mark.parametrize(("data", "keys", "reason"), [
    (b"", {"UseCMap": Name.X}, "built on another"),
    (b"", {"WMode": 1}, "vertical writing"),
    (b"x" * 20, {}, "can't be read"),
])
def test_cmaps_that_cant_be_read(monkeypatch, data, keys, reason):
    monkeypatch.setattr(C, "MAX_CMAP", 10)
    pdf = pikepdf.new()
    with pytest.raises(C.Unsure, match=reason):
        C.Font(composite(encoding=pdf.make_stream(data, **keys)))


# --- pictures ---------------------------------------------------------------------------


def test_picture_without_pixels_under_the_area_is_kept():
    pdf = pikepdf.new()
    picture = pikepdf.Stream(pdf, zlib.compress(b"\x00" * 100))
    picture.Type, picture.Subtype, picture.Width, picture.Height = (
        Name.XObject, Name.Image, 10, 10)
    picture.ColorSpace, picture.BitsPerComponent = Name.DeviceGray, 8
    picture.Filter = Name.FlateDecode
    assert C.edit_image(pdf, picture, (10, 0, 0, 10, 0, 0), [(0, 5, 10, 5)]) is None


# --- the rest of redact.py --------------------------------------------------------------


def test_places():
    pdf = pikepdf.new()
    info = pdf.make_indirect(pikepdf.Dictionary())
    assert R._place(info, info) == "document info"
    assert R._place(pikepdf.Dictionary(FT=Name.Tx), None) == "form fields"
    assert R._place(pikepdf.Dictionary(Rect=[], Subtype=Name.Text), None) == "comments"
    assert R._place(pikepdf.Dictionary(Title=1, Parent=1), None) == "bookmarks"
    assert R._place(pikepdf.Dictionary(S=Name.P, P=1), None) == "accessibility structure"
    assert R._place(pikepdf.Dictionary(), info) == "other"


def test_merge():
    assert R._merge([(5, 8), (0, 3), (2, 4), (8, 9)]) == [(0, 4), (5, 9)]


def test_lines():
    a, b = (0, 0, 5, 10), (5, 0, 10, 10)
    assert R._lines([a, b]) == [(0, 0, 10, 10)]
    down = [(0, 20, 10, 30), (0, 10, 10, 20)]
    assert R._lines(down) == [(0, 10, 10, 30)]
    apart = [(0, 0, 5, 10), (40, 40, 45, 50)]
    assert R._lines(apart) == apart


def test_structure_budget(monkeypatch):
    monkeypatch.setattr(R, "MAX_STRUCTURE", 1)
    pdf = pikepdf.new()
    page = pdf.add_blank_page().obj
    kids = [pdf.make_indirect(pikepdf.Dictionary(S=Name.P, Pg=page, K=0,
                                                 Alt=pikepdf.String("a")))
            for _ in range(3)]
    pdf.Root.StructTreeRoot = pikepdf.Dictionary(K=pikepdf.Array(kids))
    with pytest.raises(PdfUserError, match="structure is too large"):
        R._strip_alt_text(pdf, {page.objgen: None})
    assert R._strip_alt_text(pdf, {}) == 0
