"""clean_scans: phone photos (and scanned PDFs) made to look like clean scans."""
from __future__ import annotations

import io

import pikepdf
import pytest
from PIL import Image
from reportlab.lib.pagesizes import A4, letter
from reportlab.pdfgen import canvas

import paperboy.server as S
from paperboy.engine import cleanscan as C
from paperboy.errors import PdfUserError


@pytest.fixture(autouse=True)
def sandbox(settings, monkeypatch):
    monkeypatch.setattr(S, "SETTINGS", settings)
    return settings


cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")


def _document(path, stamp=True, pagesize=letter, lines=18):
    """A page of text (and a red PAID stamp), drawn as a phone would see it."""
    c = canvas.Canvas(str(path), pagesize=pagesize)
    c.setFont("Helvetica-Bold", 22)
    c.drawString(72, pagesize[1] - 92, "Receipt 4471")
    c.setFont("Helvetica", 12)
    for i in range(lines):
        c.drawString(72, pagesize[1] - 132 - 26 * i,
                     f"Line {i + 1}: delivery item number {1000 + i}, amount 12.{i:02d}")
    if stamp:
        c.setStrokeColorRGB(0.85, 0.1, 0.1)
        c.setFillColorRGB(0.85, 0.1, 0.1)
        c.setLineWidth(3)
        c.rect(380, 90, 160, 60)
        c.setFont("Helvetica-Bold", 20)
        c.drawString(400, 112, "PAID")
    c.showPage()
    c.save()
    return path


def _pixels(pdf_path, scale=150 / 72):
    import pypdfium2
    doc = pypdfium2.PdfDocument(str(pdf_path))
    try:
        return np.asarray(doc[0].render(scale=scale).to_pil().convert("RGB"))
    finally:
        doc.close()


def _camera(tilt_x=25, tilt_y=10, size=(2400, 1800), aspect=8.5 / 11, f=2000, away=16):
    """Where a camera sees the corners of a sheet turned by these angles (degrees)."""
    h = 11.0
    w = h * aspect
    a, b = np.radians(tilt_x), np.radians(tilt_y)
    turn_x = np.array([[1, 0, 0], [0, np.cos(a), -np.sin(a)], [0, np.sin(a), np.cos(a)]])
    turn_y = np.array([[np.cos(b), 0, np.sin(b)], [0, 1, 0], [-np.sin(b), 0, np.cos(b)]])
    corners = np.array([[-w / 2, -h / 2, 0], [w / 2, -h / 2, 0], [w / 2, h / 2, 0],
                        [-w / 2, h / 2, 0]]) @ (turn_y @ turn_x).T + [0, 0, away]
    rows, cols = size
    return [[f * x / z + cols / 2, f * y / z + rows / 2] for x, y, z in corners]


def _photo(page, corners=None, size=(2400, 1800), tilt=0.0, shade=True):
    """The page photographed on a dark table, at an angle, under uneven yellow light."""
    h, w = page.shape[:2]
    rows, cols = size
    table = np.full((rows, cols, 3), (70, 50, 35), np.float32)
    table += np.random.default_rng(1).normal(0, 8, (rows, cols, 1))
    if corners is None:
        corners = _camera(aspect=w / h, size=size)
    matrix = cv2.getPerspectiveTransform(
        np.float32([[0, 0], [w, 0], [w, h], [0, h]]), np.float32(corners))
    sheet = cv2.warpPerspective(page.astype(np.float32), matrix, (cols, rows))
    mask = cv2.warpPerspective(np.ones((h, w), np.float32), matrix, (cols, rows))
    out = table * (1 - mask[..., None]) + sheet * mask[..., None]
    if shade:
        yy, xx = np.mgrid[0:rows, 0:cols]
        light = 0.55 + 0.45 * (xx / cols * 0.5 + yy / rows * 0.5)
        out = out * light[..., None] * np.array([1.0, 0.95, 0.8])
    if tilt:
        turn = cv2.getRotationMatrix2D((cols / 2, rows / 2), tilt, 1)
        out = cv2.warpAffine(out, turn, (cols, rows), borderMode=cv2.BORDER_REPLICATE)
    return np.clip(out, 0, 255).astype(np.uint8)


def _save(array, path, **kw):
    Image.fromarray(array).save(path, **kw)
    return path


@pytest.fixture
def page(tmp_path):
    return _pixels(_document(tmp_path / "source.pdf"))


@pytest.fixture
def photo(tmp_path, page):
    return _save(_photo(page), tmp_path / "receipt.jpg", quality=90)


def _image(pdf_path, n=0):
    with pikepdf.open(pdf_path) as pdf:
        p = pdf.pages[n]
        xobj = p.Resources.XObject.Scan
        return ([float(v) for v in p.MediaBox], str(xobj.ColorSpace),
                int(xobj.BitsPerComponent), str(xobj.Filter))


def test_a_photo_at_an_angle_becomes_a_flat_white_page(photo, tmp_path):
    res = S.clean_scans([str(photo)])
    out = tmp_path / "receipt_clean.pdf"
    assert res["output"] == str(out) and res["pages"] == 1
    item = res["cleaned"][0]
    assert item["page_found"] and item["page"] is None and item["source"] == str(photo)
    assert item["mode"] == "color"                       # the red stamp is kept
    assert abs(item["width_px"] / item["height_px"] - 612 / 792) < 0.03
    box, space, bits, kind = _image(out)
    assert box == [0, 0, 612, 792] and space == "/DeviceRGB" and kind == "/DCTDecode"
    clean = _pixels(out, 1.0)
    corners = [clean[:20, :20], clean[:20, -20:], clean[-20:, :20], clean[-20:, -20:]]
    assert all(c.mean() > 245 for c in corners), [c.mean() for c in corners]  # no table
    gray = cv2.cvtColor(clean, cv2.COLOR_RGB2GRAY)
    assert (gray < 100).mean() > 0.005                   # the text is still there
    assert abs(C._skew(gray)) < 0.5                      # and level
    red = (clean[:, :, 0] > 150) & (clean[:, :, 1] < 90) & (clean[:, :, 2] < 90)
    assert red[clean.shape[0] // 2:, clean.shape[1] // 2:].sum() > 200  # bottom right


@pytest.mark.parametrize("tilt", [4.0, -6.0])
def test_a_slanted_page_is_levelled(tmp_path, page, tilt):
    shot = _save(_photo(page, corners=[[0, 0], [1700, 0], [1700, 2200], [0, 2200]],
                        size=(2200, 1700), tilt=tilt), tmp_path / "tilted.png")
    item = S.clean_scans([str(shot)])["cleaned"][0]
    assert not item["page_found"]                         # the photo is all page
    assert abs(item["straightened_degrees"] - tilt) < 0.5
    out = _pixels(tmp_path / "tilted_clean.pdf", 1.0)
    assert abs(C._skew(cv2.cvtColor(out, cv2.COLOR_RGB2GRAY))) < 0.5


def test_modes(photo, tmp_path):
    for mode, space, bits, kind in (("grayscale", "/DeviceGray", 8, "/DCTDecode"),
                                    ("black_and_white", "/DeviceGray", 1, "/FlateDecode"),
                                    ("color", "/DeviceRGB", 8, "/DCTDecode")):
        out = tmp_path / f"{mode}.pdf"
        res = S.clean_scans([str(photo)], output_path=str(out), mode=mode)
        assert res["cleaned"][0]["mode"] == mode
        assert _image(out)[1:] == (space, bits, kind)
    with pikepdf.open(tmp_path / "black_and_white.pdf") as pdf:
        bw = pikepdf.PdfImage(pdf.pages[0].Resources.XObject.Scan).as_pil_image()
    assert bw.mode == "1"
    assert (tmp_path / "black_and_white.pdf").stat().st_size < \
        (tmp_path / "grayscale.pdf").stat().st_size / 3


def test_a_page_without_colour_is_kept_in_grey(tmp_path):
    plain = _pixels(_document(tmp_path / "plain.pdf", stamp=False))
    shot = _save(_photo(plain), tmp_path / "plain.jpg")
    assert S.clean_scans([str(shot)])["cleaned"][0]["mode"] == "grayscale"


def test_steps_can_be_turned_off(photo, tmp_path):
    item = S.clean_scans([str(photo)], find_page=False, straighten=False)["cleaned"][0]
    assert (item["page_found"], item["straightened_degrees"]) == (False, 0.0)
    assert (item["width_px"], item["height_px"]) == (1800, 2400) or \
        max(item["width_px"], item["height_px"]) < 2400  # stored at most 300 dpi


def test_photos_and_pdf_pages_go_in_order(photo, tmp_path, make_encrypted):
    scanned = tmp_path / "scanned.pdf"
    with pikepdf.open(_document(tmp_path / "doc.pdf", lines=4)) as one, \
            pikepdf.new() as both:
        both.pages.extend(one.pages)
        both.pages.extend(one.pages)
        both.pages.extend(one.pages)
        both.save(scanned)
    out = tmp_path / "all.pdf"
    res = S.clean_scans([str(scanned), str(photo)], output_path=str(out), pages="2-3")
    assert [(i["source"].rsplit("/", 1)[-1], i["page"]) for i in res["cleaned"]] == [
        ("scanned.pdf", 2), ("scanned.pdf", 3), ("receipt.jpg", None)]
    assert res["pages"] == 3
    locked = make_encrypted("locked.pdf", password="pw", pages=1)
    res = S.clean_scans([str(locked)], password="pw")
    assert res["pages"] == 1
    with pytest.raises(S.ToolError, match="password"):
        S.clean_scans([str(locked)], output_path=str(tmp_path / "x.pdf"), overwrite=True)


def test_the_phone_s_turn_is_followed(tmp_path, page):
    sideways = np.ascontiguousarray(np.rot90(_photo(page)))   # stored on its side...
    img = Image.fromarray(sideways)
    exif = img.getexif()
    exif[0x0112] = 6                                           # ...and tagged to turn
    path = tmp_path / "turned.jpg"
    img.save(path, exif=exif.tobytes())
    item = S.clean_scans([str(path)])["cleaned"][0]
    assert item["page_found"] and item["height_px"] > item["width_px"]


@pytest.mark.parametrize("width, height, size, box", [
    (850, 1100, "auto", (612.0, 792.0)),        # Letter-shaped
    (1100, 850, "auto", (792.0, 612.0)),        # on its side
    (827, 1169, "auto", (595.28, 841.89)),      # A4
    (300, 1200, "auto", (198.0, 792.0)),        # a till receipt: 11 inches long
    (1200, 300, "auto", (792.0, 198.0)),
    (850, 1100, "a4", (595.28, 841.89)),
    (1100, 850, "letter", (792.0, 612.0)),
])
def test_page_sizes(width, height, size, box):
    assert C._page_box(width, height, size) == pytest.approx(box)


def test_a_huge_picture_is_stored_at_most_300_dpi(tmp_path):
    big = np.full((6000, 4600, 3), 255, np.uint8)
    cv2.putText(big, "BIG", (500, 3000), cv2.FONT_HERSHEY_SIMPLEX, 20, (0, 0, 0), 40)
    with pikepdf.new() as pdf:
        w, h = C._add_page(pdf, big, "grayscale", "letter")
    assert max(w, h) <= round(300 / 72 * 792) + 1


def test_a_blank_page_stays_blank(tmp_path):
    blank = _save(np.full((800, 600, 3), 230, np.uint8), tmp_path / "blank.png")
    res = S.clean_scans([str(blank)])
    assert res["cleaned"][0]["mode"] == "grayscale"
    assert _pixels(tmp_path / "blank_clean.pdf", 0.5).min() > 240


def test_too_few_lines_or_too_steep_aren_t_levelled():
    empty = np.full((400, 400), 255, np.uint8)
    assert C._skew(empty) == 0.0
    steep = np.full((1200, 1200), 255, np.uint8)
    for i in range(8):                                        # lines at 30 degrees
        cv2.line(steep, (100, 300 + 90 * i), (1100, 300 + 90 * i - 577), 0, 6)
    assert C._skew(steep) == 0.0


def test_a_slight_slant_is_left_alone(tmp_path, page, monkeypatch):
    shot = _save(_photo(page, corners=[[0, 0], [1700, 0], [1700, 2200], [0, 2200]],
                        size=(2200, 1700), tilt=0.1, shade=False), tmp_path / "t.png")
    assert S.clean_scans([str(shot)])["cleaned"][0]["straightened_degrees"] == 0.0


@pytest.mark.parametrize("kwargs, message", [
    ({"mode": "sepia"}, "mode must be one of"),
    ({"page_size": "a5"}, "page_size must be one of"),
])
def test_bad_options_are_refused(photo, settings, kwargs, message):
    with pytest.raises(PdfUserError, match=message):
        C.clean_scans([photo], photo.with_name("o.pdf"), settings, **kwargs)


def test_inputs_are_checked(photo, tmp_path, settings, monkeypatch):
    with pytest.raises(PdfUserError, match="at least one"):
        C.clean_scans([], tmp_path / "o.pdf", settings)
    notes = tmp_path / "notes.txt"
    notes.write_text("x")
    with pytest.raises(S.ToolError, match="Not photos or PDFs: notes.txt"):
        S.clean_scans([str(notes)])
    monkeypatch.setattr(C, "MAX_INPUTS", 1)
    with pytest.raises(S.ToolError, match="at most 1 files"):
        S.clean_scans([str(photo), str(photo)], output_path=str(tmp_path / "two.pdf"))
    with pytest.raises(S.ToolError, match="same as an input"):
        S.clean_scans([str(photo)], output_path=str(photo))


def test_limits_on_pages_and_time(photo, tmp_path, monkeypatch):
    monkeypatch.setattr(C, "MAX_PAGES", 1)
    with pytest.raises(S.ToolError, match="over 1 pages"):
        S.clean_scans([str(photo), str(photo)], output_path=str(tmp_path / "two.pdf"))
    monkeypatch.setattr(C, "MAX_PAGES", 100)
    monkeypatch.setattr(C, "TIME_LIMIT_S", -1)
    with pytest.raises(S.ToolError, match="Cleaning took longer"):
        S.clean_scans([str(photo)])
    assert not (tmp_path / "receipt_clean.pdf").exists()
    assert not list(tmp_path.glob(".paperboy-*"))            # nothing half-written


def test_a_broken_photo_is_refused(tmp_path):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not a picture")
    with pytest.raises(S.ToolError, match="not a readable image"):
        S.clean_scans([str(bad)])


def test_jpeg_output_is_valid(photo, tmp_path):
    S.clean_scans([str(photo)], mode="grayscale")
    with pikepdf.open(tmp_path / "receipt_clean.pdf") as pdf:
        data = pdf.pages[0].Resources.XObject.Scan.read_raw_bytes()
    assert Image.open(io.BytesIO(data)).mode == "L"


def test_a4_documents(tmp_path):
    a4 = _pixels(_document(tmp_path / "a4.pdf", pagesize=A4, stamp=False))
    shot = _save(_photo(a4), tmp_path / "a4.jpg")
    S.clean_scans([str(shot)])
    assert _image(tmp_path / "a4_clean.pdf")[0] == pytest.approx([0, 0, 595.28, 841.89])


def test_the_page_s_real_shape_is_recovered_from_its_corners():
    for tilt_x, tilt_y in ((25, 10), (35, -15), (-20, 25)):
        corners = np.float32(_camera(tilt_x, tilt_y))
        assert C._aspect(corners, 1800, 2400) == pytest.approx(8.5 / 11, rel=0.005)
    square_on = np.float32([[100, 100], [950, 100], [950, 1200], [100, 1200]])
    assert C._aspect(square_on, 1800, 2400) == pytest.approx(850 / 1100)
    # Narrower at the bottom, about the photo's top corner: no camera at its centre
    # sees a rectangle so, so the edges are averaged.
    no_camera = np.float32([[0, 0], [1000, 0], [900, 1000], [100, 1000]])
    side = float(np.hypot(100, 1000))
    assert C._aspect(no_camera, 1800, 2400) == pytest.approx(1800 / (2 * side))


def test_a_wide_sheet_is_flattened_to_its_shape(tmp_path):
    wide = _pixels(_document(tmp_path / "wide.pdf", pagesize=(792, 612), stamp=False))
    shot = _save(_photo(wide, corners=_camera(20, 8, aspect=11 / 8.5, away=24)),
                 tmp_path / "w.jpg")
    item = S.clean_scans([str(shot)])["cleaned"][0]
    assert item["page_found"]
    assert item["width_px"] / item["height_px"] == pytest.approx(11 / 8.5, rel=0.03)


def test_corners_no_camera_could_see_fall_back_to_the_edges():
    odd = np.float32([[446, 243], [1129, 15], [1669, 2321], [425, 2157]])
    tl, tr, br, bl = odd
    edges = (np.linalg.norm(tr - tl) + np.linalg.norm(br - bl)) / \
        (np.linalg.norm(bl - tl) + np.linalg.norm(br - tr))
    assert C._aspect(odd, 1800, 2400) == pytest.approx(edges)


def test_a_sheet_turned_sideways_keeps_its_height():
    corners = np.float32(_camera(5, 30))      # turned about its upright: looks narrower
    flat = C._flatten(np.zeros((2400, 1800, 3), np.uint8), corners)
    assert flat.shape[1] / flat.shape[0] == pytest.approx(8.5 / 11, rel=0.01)


def test_a_signature_in_blue_ink_keeps_the_page_in_colour(tmp_path):
    path = tmp_path / "signed.pdf"
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica", 12)
    for i in range(10):
        c.drawString(72, 700 - 26 * i, f"Clause {i + 1}: the tenant keeps the garden tidy.")
    c.setStrokeColorRGB(0.12, 0.25, 0.66)
    c.setLineWidth(1.6)
    p = c.beginPath()
    p.moveTo(80, 300)
    for k in range(1, 40):
        p.lineTo(80 + k * 5.5, 302 + 14 * np.sin(k / 2.3) * np.cos(k / 7.0))
    c.drawPath(p)
    c.showPage()
    c.save()
    shot = _save(_photo(_pixels(path)), tmp_path / "signed.jpg")
    assert S.clean_scans([str(shot)])["cleaned"][0]["mode"] == "color"


def test_a_huge_pdf_page_is_drawn_no_bigger_than_the_limit(tmp_path, monkeypatch):
    path = tmp_path / "poster.pdf"
    c = canvas.Canvas(str(path), pagesize=(8000, 8000))  # 111 inches square, 539 bytes
    c.drawString(100, 4000, "Poster")
    c.showPage()
    c.save()
    drawn = []
    real = C._render

    def render(doc, index, scale):
        image = real(doc, index, scale)
        drawn.append(image.size)
        return image
    monkeypatch.setattr(C, "_render", render)
    S.clean_scans([str(path)])
    assert max(drawn[0]) <= C.MAX_SIDE


def test_all_the_pages_together_are_within_a_pixel_budget(photo, tmp_path, monkeypatch):
    monkeypatch.setattr(C, "MAX_TOTAL_PIXELS", 1_000_000)
    with pytest.raises(S.ToolError, match="over 1 million pixels"):
        S.clean_scans([str(photo)])
    doc = _document(tmp_path / "doc.pdf", lines=2)
    with pytest.raises(S.ToolError, match="over 1 million pixels"):
        S.clean_scans([str(doc)])


def test_pages_are_counted_before_any_is_cleaned(photo, tmp_path, monkeypatch):
    monkeypatch.setattr(C, "MAX_PAGES", 2)

    def never(*args, **kwargs):
        raise AssertionError("cleaned before the pages were counted")
    monkeypatch.setattr(C, "_clean_one", never)
    doc = _document(tmp_path / "two.pdf")
    with pikepdf.open(doc, allow_overwriting_input=True) as pdf:
        pdf.pages.append(pdf.pages[0])
        pdf.save(doc)
    with pytest.raises(S.ToolError, match="over 2 pages"):
        S.clean_scans([str(photo), str(doc)], output_path=str(tmp_path / "o.pdf"))


def test_the_default_output_never_replaces_an_input(tmp_path):
    a = tmp_path / "a.png"
    Image.new("RGB", (60, 60), "white").save(a)
    taken = _document(tmp_path / "a_clean.pdf", lines=1)
    before = taken.read_bytes()
    with pytest.raises(S.ToolError, match="is one of the inputs"):
        S.clean_scans([str(a), str(taken)], overwrite=True)
    assert taken.read_bytes() == before


def test_a_typed_password_opens_the_pdfs_among_the_inputs(photo, tmp_path, make_encrypted):
    locked = make_encrypted("locked.pdf", password="pw", pages=1)
    asks, refs, bound = S._wants({"input_paths": [str(photo), str(locked)],
                                  "password": "ask"}, "clean_scans")
    (where, label, new, purpose), = asks
    assert where == ("password",) and label == "opening “locked.pdf”" and not new
    assert purpose[0] == "clean_scans" and str(locked.resolve()) in purpose[-1]
    assert bound is None
    with pytest.raises(S.ToolError, match="names none"):
        S._wants({"input_paths": [str(photo)], "password": "ask"}, "clean_scans")
    other = make_encrypted("other.pdf", password="pw", pages=1)
    asks2 = S._wants({"input_paths": [str(other), str(locked)], "password": "ask"},
                     "clean_scans")[0]
    assert asks2[0][1] == "opening the 2 PDFs" and asks2[0][3] != purpose


def test_corners_are_ordered_whatever_their_shape():
    diamond = np.float32([[50, 0], [100, 50], [50, 100], [0, 50]])
    ordered = C._order(diamond)
    assert len({tuple(p) for p in ordered.tolist()}) == 4
    assert ordered.tolist()[0] == [0, 50] or ordered.tolist()[0] == [50, 0]
    square = np.float32([[90, 10], [10, 10], [10, 90], [90, 90]])
    assert C._order(square).tolist() == [[10, 10], [90, 10], [90, 90], [10, 90]]
    assert C._order(np.float32([[0, 0], [0, 0], [10, 10], [0, 10]])) is None


def test_a_warp_is_never_bigger_than_the_limit(monkeypatch):
    monkeypatch.setattr(C, "MAX_SIDE", 500)
    corners = np.float32(_camera(25, 10))
    flat = C._flatten(np.zeros((2400, 1800, 3), np.uint8), corners)
    assert max(flat.shape[:2]) <= 500


def test_a_dark_photo_isnt_amplified_into_noise():
    dark = np.random.default_rng(0).integers(0, 12, (400, 300), dtype=np.uint8)
    assert C._whiten(dark).mean() < 20


def test_sixteen_bit_and_transparent_pictures(tmp_path):
    ramp = (np.tile(np.linspace(0, 65535, 400), (300, 1))).astype(np.uint16)
    deep = tmp_path / "deep.png"
    Image.fromarray(ramp).save(deep)                     # 16-bit grey
    assert Image.open(deep).mode.startswith("I")
    S.clean_scans([str(deep)], output_path=str(tmp_path / "deep.pdf"), find_page=False,
                  straighten=False)
    assert _pixels(tmp_path / "deep.pdf", 0.5).min() < 60     # not clipped to white
    clear = tmp_path / "clear.png"
    Image.new("RGBA", (300, 400), (0, 0, 0, 0)).save(clear)   # all transparent
    S.clean_scans([str(clear)], output_path=str(tmp_path / "clear.pdf"))
    assert _pixels(tmp_path / "clear.pdf", 0.5).min() > 240  # white paper, not black
    eight = Image.fromarray((np.tile(np.linspace(0, 255, 400), (300, 1))).astype(np.int32),
                            mode="I")
    assert C._rgb(eight)[:, -1].min() == 255 and C._rgb(eight)[:, 0].max() == 0


def test_every_page_of_a_multi_page_tiff(tmp_path, page):
    frames = [Image.fromarray(_photo(page)).resize((600, 800)) for _ in range(3)]
    tiff = tmp_path / "fax.tiff"
    frames[0].save(tiff, save_all=True, append_images=frames[1:])
    res = S.clean_scans([str(tiff)])
    assert res["pages"] == 3 and [i["page"] for i in res["cleaned"]] == [1, 2, 3]
    gif = tmp_path / "anim.gif"
    frames[0].save(gif, save_all=True, append_images=frames[1:])
    assert S.clean_scans([str(gif)])["pages"] == 1          # an animation isn't pages


def test_an_outline_with_corners_on_top_of_each_other_isnt_a_page(page, monkeypatch):
    monkeypatch.setattr(C, "_order", lambda quad: None)
    assert C._find_page(_photo(page)) is None
