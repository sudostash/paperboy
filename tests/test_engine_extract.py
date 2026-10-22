"""list_attachments / extract_attachments / extract_images, and the bounded decoder."""
from __future__ import annotations

import base64
import dataclasses
import io
import os
import zlib
from pathlib import Path

import pikepdf
import pytest
from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from test_engine_scan import RAW, _attach

from paperboy import engine as E
from paperboy.engine import extract, streams
from paperboy.engine.extract import safe_filename
from paperboy.errors import PdfUserError


@pytest.fixture
def out_dir(tmp_path):
    d = tmp_path / "out"
    d.mkdir()
    return d


def _edit(path, change, **save):
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        change(pdf)
        pdf.save(path, **save)
    return path


def _images(pdf, index: int) -> list:
    return [obj for _, obj in pdf.pages[index].obj.Resources.XObject.items()]


def _is_jpeg(image) -> bool:
    return "/DCTDecode" in str(image.get("/Filter"))


def _picture(kind: str, size=(40, 30), color="red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, kind)
    return buf.getvalue()


@pytest.fixture
def pictures(tmp_path):
    """Page 1: a PNG-type picture and a photo (JPEG). Page 2: the same photo again,
    and a tiny icon. Page 3: no pictures."""
    path = tmp_path / "pictures.pdf"
    photo = ImageReader(io.BytesIO(_picture("JPEG", (64, 48), "blue")))
    c = canvas.Canvas(str(path))
    c.drawImage(ImageReader(io.BytesIO(_picture("PNG"))), 72, 600, 40, 30)
    c.drawImage(photo, 200, 600, 64, 48)
    c.showPage()
    c.drawImage(photo, 72, 600, 64, 48)
    c.drawImage(ImageReader(io.BytesIO(_picture("PNG", (8, 8), "green"))), 300, 600, 8, 8)
    c.showPage()
    c.drawString(72, 720, "no pictures here")
    c.showPage()
    c.save()
    return path


# --- file names ------------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "safe"), [
    ("report.pdf", "report.pdf"),
    ("../../etc/passwd", "passwd"),
    (r"C:\Windows\evil.exe", "evil.exe"),
    ("a\x00b<c>d:e|f?g*.txt", "a_b_c_d_e_f_g_.txt"),
    ("  .hidden.  ", "hidden"),
    ("", "attachment"),
    ("..", "attachment"),
    ("CON.txt", "attachment_CON.txt"),
    ("lpt1", "attachment_lpt1"),
    ("x" * 300 + ".pdf", "x" * 116 + ".pdf"),
    ("x" * 300, "x" * 120),
    ("x" * 300 + ".notanextension", "x" * 120),
])
def test_safe_filenames(name, safe):
    assert safe_filename(name, "attachment") == safe


def test_repeated_names_are_numbered():
    used = set()
    names = [extract._unique(n, used) for n in ("a.pdf", "A.pdf", "a.pdf", "readme", "readme")]
    assert names == ["a.pdf", "A (2).pdf", "a (3).pdf", "readme", "readme (2)"]


# --- listing attachments ---------------------------------------------------------------------


def test_document_and_page_attachments_are_listed(doc, settings):
    def add(pdf):
        _attach(pdf, "notes.txt", b"hello")
        spec = pdf.Root.Names.EmbeddedFiles.Names[1]
        spec.Desc = pikepdf.String("Meeting notes")
        spec.EF.F.Subtype = pikepdf.Name("/text/plain")
        stream = pikepdf.Stream(pdf, b"MZ\x90\x00")
        page_spec = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Filespec, F=pikepdf.String("tool.exe"),
            EF=pikepdf.Dictionary(F=pdf.make_indirect(stream))))
        pdf.pages[1].obj.Annots = pdf.make_indirect(pikepdf.Array([
            pdf.make_indirect(pikepdf.Dictionary(
                Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.FileAttachment,
                Rect=pikepdf.Array([0, 0, 10, 10]), FS=page_spec)),
            pdf.make_indirect(pikepdf.Dictionary(Subtype=pikepdf.Name.FileAttachment,
                                                 FS=pikepdf.String("outside.pdf"))),
            pdf.make_indirect(pikepdf.Dictionary(Subtype=pikepdf.Name.Link)),
            5]))
        pdf.pages[2].obj.Annots = pikepdf.Array([pdf.pages[1].obj.Annots[0]])  # repeated
    _edit(doc, add)
    res = E.list_attachments(doc, settings)
    assert res["truncated"] is False
    assert res["attachments"] == [
        {"name": "notes.txt", "description": "Meeting notes", "mime_type": "text/plain",
         "size": 5, "detected_type": "text", "executable": False,
         "extension_mismatch": False, "page": None},
        {"name": "tool.exe", "description": None, "mime_type": None, "size": None,
         "detected_type": "Windows program", "executable": True,
         "extension_mismatch": False, "page": 2}]


def test_attachments_in_a_name_tree_with_kids(doc, settings):
    def add(pdf):
        _attach(pdf, "a.txt", b"a")
        leaf = pdf.Root.Names.EmbeddedFiles
        pdf.Root.Names.EmbeddedFiles = pikepdf.Dictionary(Kids=pikepdf.Array([
            pdf.make_indirect(leaf)]))
    _edit(doc, add)
    assert [a["name"] for a in E.list_attachments(doc, settings)["attachments"]] == ["a.txt"]


def test_no_attachments(doc, settings, out_dir):
    assert E.list_attachments(doc, settings)["attachments"] == []
    with pytest.raises(PdfUserError, match="no attachments"):
        E.extract_attachments(doc, out_dir, settings)


def test_listing_is_capped(doc, settings, monkeypatch):
    monkeypatch.setattr(extract, "MAX_ATTACHMENTS", 2)
    _edit(doc, lambda pdf: [_attach(pdf, f"{n}.txt", b"x") for n in range(3)])
    res = E.list_attachments(doc, settings)
    assert len(res["attachments"]) == 2 and res["truncated"] is True


# --- extracting attachments ------------------------------------------------------------------


def test_files_are_saved_byte_for_byte(doc, settings, out_dir):
    payload = bytes(range(256)) * 100
    _edit(doc, lambda pdf: (_attach(pdf, "data.bin", payload),
                            _attach(pdf, "plain.txt", b"plain", compress=False)))
    res = E.extract_attachments(doc, out_dir, settings)
    assert res["skipped"] == []
    assert {(f["name"], f["size"]) for f in res["files"]} == {("data.bin", 25600),
                                                              ("plain.txt", 5)}
    assert (out_dir / "data.bin").read_bytes() == payload
    assert (out_dir / "plain.txt").read_bytes() == b"plain"


def test_hostile_names_stay_in_the_folder(doc, settings, out_dir, tmp_path):
    _edit(doc, lambda pdf: [_attach(pdf, n, n.encode()) for n in
                            ("../../escape.txt", "/etc/passwd", "a/passwd", "CON")])
    res = E.extract_attachments(doc, out_dir, settings)
    assert sorted(p.name for p in out_dir.iterdir()) == [
        "attachment_CON", "escape.txt", "passwd", "passwd (2)"]
    assert not (tmp_path / "escape.txt").exists()
    assert all(os.path.dirname(f["file"]) == str(out_dir) for f in res["files"])


def test_programs_are_skipped_unless_allowed(doc, settings, out_dir):
    _edit(doc, lambda pdf: (_attach(pdf, "invoice.pdf.exe", b"MZ\x90\x00"),
                            _attach(pdf, "notes.txt", b"hi")))
    res = E.extract_attachments(doc, out_dir, settings)
    assert [f["name"] for f in res["files"]] == ["notes.txt"]
    assert res["skipped"][0]["name"] == "invoice.pdf.exe"
    assert "allow_executables" in res["skipped"][0]["reason"]
    res = E.extract_attachments(doc, out_dir, settings, names=["invoice.pdf.exe"],
                                allow_executables=True)
    assert res["files"][0]["executable"] is True
    assert not os.access(out_dir / "invoice.pdf.exe", os.X_OK)   # never made runnable


def test_only_the_named_files(doc, settings, out_dir):
    _edit(doc, lambda pdf: (_attach(pdf, "a.txt", b"a"), _attach(pdf, "b.txt", b"b")))
    res = E.extract_attachments(doc, out_dir, settings, names=["b.txt"])
    assert [f["name"] for f in res["files"]] == ["b.txt"]
    with pytest.raises(PdfUserError, match="No attachment named 'c.txt'.*a.txt, b.txt"):
        E.extract_attachments(doc, out_dir, settings, names=["c.txt"])


def test_existing_files_stop_everything_unless_overwriting(doc, settings, out_dir):
    _edit(doc, lambda pdf: (_attach(pdf, "a.txt", b"new a"), _attach(pdf, "b.txt", b"new b")))
    (out_dir / "b.txt").write_bytes(b"old")
    with pytest.raises(PdfUserError, match="already exists"):
        E.extract_attachments(doc, out_dir, settings)
    assert sorted(p.name for p in out_dir.iterdir()) == ["b.txt"]   # nothing half-written
    E.extract_attachments(doc, out_dir, settings, overwrite=True)
    assert (out_dir / "b.txt").read_bytes() == b"new b"


def test_a_planted_link_is_never_followed(doc, settings, out_dir, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"precious")
    (out_dir / "a.txt").symlink_to(victim)
    (out_dir / "b.txt").symlink_to(tmp_path / "created-outside.txt")   # dangling
    _edit(doc, lambda pdf: (_attach(pdf, "a.txt", b"evil"), _attach(pdf, "b.txt", b"evil")))
    for overwrite in (False, True):
        with pytest.raises(PdfUserError, match="not a regular file"):
            E.extract_attachments(doc, out_dir, settings, overwrite=overwrite)
    assert victim.read_bytes() == b"precious"
    assert not (tmp_path / "created-outside.txt").exists()


def test_a_folder_in_the_way_is_refused(doc, settings, out_dir):
    _edit(doc, lambda pdf: _attach(pdf, "a.txt", b"a"))
    (out_dir / "a.txt").mkdir()
    with pytest.raises(PdfUserError, match="not a regular file"):
        E.extract_attachments(doc, out_dir, settings, overwrite=True)


def test_damaged_unsupported_and_oversized_files_are_skipped(doc, settings, out_dir):
    def add(pdf):
        _attach(pdf, "cut.bin", b"x")
        _attach(pdf, "lzw.bin", b"x", compress=False)
        _attach(pdf, "big.bin", b"y" * 5000)
        _attach(pdf, "ok.txt", b"fine")
        names = pdf.Root.Names.EmbeddedFiles.Names
        names[1].EF.F.write(zlib.compress(b"z" * 1000)[:-8], filter=pikepdf.Name.FlateDecode)
        names[3].EF.F.write(b"\x80\x0b", filter=pikepdf.Name.LZWDecode)
    _edit(doc, add, **RAW)
    small = dataclasses.replace(settings, max_file_bytes=4000)
    res = E.extract_attachments(doc, out_dir, small)
    reasons = {s["name"]: s["reason"] for s in res["skipped"]}
    assert "damaged" in reasons["cut.bin"] and "encoding" in reasons["lzw.bin"]
    assert "larger than" in reasons["big.bin"]
    assert [f["name"] for f in res["files"]] == ["ok.txt"]
    assert sorted(p.name for p in out_dir.iterdir()) == ["ok.txt"]


def test_one_extraction_has_a_total_size_budget(doc, settings, out_dir):
    _edit(doc, lambda pdf: [_attach(pdf, f"{n}.bin", bytes([n]) * 900) for n in range(6)])
    res = E.extract_attachments(doc, out_dir, dataclasses.replace(settings,
                                                                  max_file_bytes=1000))
    assert len(res["files"]) == 4 and len(res["skipped"]) == 2
    assert all("total size" in s["reason"] for s in res["skipped"])


def test_copy_restriction_needs_the_owner_password(doc, settings, out_dir, tmp_path):
    _edit(doc, lambda pdf: _attach(pdf, "a.txt", b"a"))
    locked = tmp_path / "locked.pdf"
    with pikepdf.open(doc) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(
            user="pw", owner="owner", R=6, allow=pikepdf.Permissions(extract=False)))
    assert E.list_attachments(locked, settings, password="pw")["attachments"]
    with pytest.raises(PdfUserError, match="does not allow copying"):
        E.extract_attachments(locked, out_dir, settings, password="pw")
    assert E.extract_attachments(locked, out_dir, settings, password="owner")["files"]


# --- images ------------------------------------------------------------------------------------


def test_pictures_are_saved_once_each(pictures, settings, out_dir):
    res = E.extract_images(pictures, out_dir, settings)
    assert res["skipped"] == [] and res["truncated"] is False
    shown = {(i["format"], i["width"], i["height"], tuple(i["pages"])) for i in res["images"]}
    assert shown == {("png", 40, 30, (1,)), ("jpg", 64, 48, (1, 2)), ("png", 8, 8, (2,))}
    names = sorted(os.path.basename(i["file"]).rsplit(".", 1)[0] for i in res["images"])
    assert names == ["page001-image01", "page001-image02", "page002-image01"]
    png = next(i["file"] for i in res["images"] if i["width"] == 40)
    with Image.open(png) as img:
        r, g, b = img.convert("RGB").getpixel((5, 5))
        assert img.size == (40, 30) and r > 250 and g < 5 and b < 5


def test_photos_are_saved_exactly_as_stored(pictures, settings, out_dir):
    res = E.extract_images(pictures, out_dir, settings)
    with pikepdf.open(pictures) as pdf:
        # The JPEG itself, unwrapped from any text encoding around it but not re-encoded.
        photo = [img for img in _images(pdf, 0) if _is_jpeg(img)][0]
        stored = photo.read_raw_bytes()
        if "/ASCII85Decode" in str(photo.Filter):     # reportlab wraps JPEGs in ASCII85
            stored = base64.a85decode(stored.strip().removesuffix(b"~>"), adobe=False)
    jpg = next(i["file"] for i in res["images"] if i["format"] == "jpg")
    assert Path(jpg).read_bytes() == stored


def test_pages_and_minimum_size(pictures, settings, out_dir):
    res = E.extract_images(pictures, out_dir, settings, pages="2-3", min_size=10)
    assert [(i["page"], i["pages"], i["width"]) for i in res["images"]] == [(2, [2], 64)]
    with pytest.raises(PdfUserError, match="min_size"):
        E.extract_images(pictures, out_dir, settings, min_size=-1)


def test_images_inside_forms_are_found(pictures, settings, out_dir):
    def nest(pdf):
        page = pdf.pages[2]
        image = _images(pdf, 0)[0]
        inner = pikepdf.Stream(pdf, b"/Im0 Do")
        inner.Subtype, inner.BBox = pikepdf.Name.Form, pikepdf.Array([0, 0, 10, 10])
        inner.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=image))
        inner = pdf.make_indirect(inner)
        outer = pikepdf.Stream(pdf, b"/Fm1 Do")
        outer.Subtype, outer.BBox = pikepdf.Name.Form, pikepdf.Array([0, 0, 10, 10])
        outer = pdf.make_indirect(outer)
        outer.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(
            Fm1=inner, Fm2=outer, Junk=pikepdf.Dictionary()))   # a loop, and a non-stream
        page.obj.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Fm0=outer))
    _edit(pictures, nest)
    res = E.extract_images(pictures, out_dir, settings, pages="3")
    assert len(res["images"]) == 1 and res["images"][0]["pages"] == [3]


def test_oversized_and_hostile_images_are_skipped(pictures, settings, out_dir, monkeypatch):
    def plant(pdf):
        bomb = [i for i in _images(pdf, 0) if not _is_jpeg(i)][0]
        bomb.write(zlib.compress(b"\0" * 5_000_000), filter=pikepdf.Name.FlateDecode)
        for img in _images(pdf, 1):
            if img.get("/Width") == 8:
                del img.Width
    _edit(pictures, plant, **RAW)
    res = E.extract_images(pictures, out_dir, settings)
    reasons = sorted(s["reason"] for s in res["skipped"])
    assert any("far larger" in r for r in reasons) and any("no valid size" in r for r in reasons)
    monkeypatch.setattr(extract, "MAX_PIXELS", 100)
    res = E.extract_images(pictures, out_dir, settings, pages="1", overwrite=True)
    assert any("million pixel limit" in s["reason"] for s in res["skipped"])


def test_images_that_cannot_be_converted_are_skipped(pictures, settings, out_dir):
    def spoil(pdf):
        for img in _images(pdf, 0):
            img.ColorSpace = pikepdf.Name("/NoSuchSpace")
    _edit(pictures, spoil)
    res = E.extract_images(pictures, out_dir, settings, pages="1")
    assert res["images"] == [] and len(res["skipped"]) == 2
    assert all(s["reason"].startswith("it can't be converted") for s in res["skipped"])


def test_existing_images_stop_everything_unless_overwriting(pictures, settings, out_dir):
    (out_dir / "page002-image01.png").write_bytes(b"old")
    with pytest.raises(PdfUserError, match="already exists"):
        E.extract_images(pictures, out_dir, settings)
    assert sorted(p.name for p in out_dir.iterdir()) == ["page002-image01.png"]
    E.extract_images(pictures, out_dir, settings, overwrite=True)
    assert (out_dir / "page002-image01.png").read_bytes() != b"old"


def test_image_count_and_total_size_are_capped(pictures, settings, out_dir, monkeypatch):
    monkeypatch.setattr(extract, "MAX_IMAGES", 2)
    res = E.extract_images(pictures, out_dir, settings)
    assert len(res["images"]) == 2 and res["truncated"] is True
    tight = dataclasses.replace(settings, max_file_bytes=1)
    res = E.extract_images(pictures, out_dir, tight, overwrite=True)
    assert res["images"] == [] and all("total size" in s["reason"] for s in res["skipped"])


def test_image_copy_restriction(pictures, settings, out_dir, tmp_path):
    locked = tmp_path / "locked.pdf"
    with pikepdf.open(pictures) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(
            user="pw", owner="owner", R=6, allow=pikepdf.Permissions(extract=False)))
    with pytest.raises(PdfUserError, match="does not allow copying"):
        E.extract_images(locked, out_dir, settings, password="pw")


def test_a_pdf_without_pictures(doc, settings, out_dir):
    res = E.extract_images(doc, out_dir, settings)
    assert res == {"output_dir": str(out_dir), "images": [], "skipped": [],
                   "truncated": False}


# --- the bounded decoder -------------------------------------------------------------------------


def _stream(data=b"", filters=None, **parms):
    pdf = pikepdf.new()
    s = pikepdf.Stream(pdf, b"")
    if filters is None:
        s.write(data)
    else:
        s.write(data, filter=pikepdf.Array([pikepdf.Name(f) for f in filters]),
                decode_parms=pikepdf.Dictionary(**parms) if parms else None)
    return pdf, s


def test_strict_decoding_refuses_cut_data():
    pdf, s = _stream(zlib.compress(b"a" * 1000)[:-6], ["/FlateDecode"])
    assert streams.decode(s, 2000) is not None          # lenient: what could be read
    assert streams.decode(s, 2000, strict=True) is None
    pdf, s = _stream(zlib.compress(b"a" * 1000), ["/FlateDecode"])
    assert streams.decode(s, 10, strict=True) == b"a" * 10   # cut by the limit is fine


@pytest.mark.parametrize(("data", "filters", "cap", "ok"), [
    (b"abc", None, 3, True),
    (b"abcd", None, 3, False),
    (zlib.compress(b"\0" * 10_000_000), ["/FlateDecode"], 10_000_000, True),
    (zlib.compress(b"\0" * 10_000_001), ["/FlateDecode"], 10_000_000, False),
    (zlib.compress(zlib.compress(b"\0" * 5000)), ["/FlateDecode", "/FlateDecode"], 4000, False),
    (b"616263>", ["/ASCIIHexDecode", "/DCTDecode"], 3, True),
    (b"\x80" * 10, ["/LZWDecode"], 30_000, True),
    (b"\x80" * 11, ["/LZWDecode"], 30_000, False),
    (b"\x80" * 10, ["/RunLengthDecode"], 640, True),
    (b"x", ["/FlateDecode"] * 5, 10**9, False),
    (b"not zlib", ["/ASCIIHexDecode", "/FlateDecode"], 10, True),  # cut Flate: nothing more
    (b"zz", ["/Crypt", "/FlateDecode"], 10, False),
])
def test_size_checks_before_decoding(data, filters, cap, ok):
    _, s = _stream(data, filters)
    assert streams.within(s, cap) is ok


def test_predictors_are_not_decoded_but_are_sized():
    _, s = _stream(zlib.compress(b"\0" * 100), ["/FlateDecode"], Predictor=12, Columns=4)
    assert streams.decode(s, 1000) is None
    assert streams.within(s, 100) is True and streams.within(s, 99) is False


def test_damaged_text_encodings():
    _, s = _stream(b"\x7f\x7f~>", ["/ASCII85Decode"])
    assert streams.decode(s, 100) is None
    assert streams.within(s, 100) is False  # unmeasurable: another decoder may read more


def test_a_file_reference_without_embedded_data_is_not_an_attachment(doc, settings):
    def add(pdf):
        spec = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Filespec,
                                                    F=pikepdf.String("elsewhere.pdf")))
        pdf.pages[0].obj.Annots = pikepdf.Array([pdf.make_indirect(pikepdf.Dictionary(
            Subtype=pikepdf.Name.FileAttachment, FS=spec))])
    _edit(doc, add)
    assert E.list_attachments(doc, settings)["attachments"] == []



def test_the_input_itself_is_never_replaced(doc, settings):
    original = doc.read_bytes()
    _edit(doc, lambda pdf: _attach(pdf, doc.name, b"not a pdf"))
    with_attachment = doc.read_bytes()
    with pytest.raises(PdfUserError, match="same as the input"):
        E.extract_attachments(doc, doc.parent, settings, overwrite=True)
    assert doc.read_bytes() == with_attachment != original


def test_no_temporary_files_are_left_behind(doc, settings, out_dir, monkeypatch):
    _edit(doc, lambda pdf: _attach(pdf, "a.txt", b"a"))
    monkeypatch.setattr(extract.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises(OSError):
        E.extract_attachments(doc, out_dir, settings)
    assert list(out_dir.iterdir()) == []


# --- review findings ---------------------------------------------------------------------------


def _image_pdf(path, image_dict: dict, data: bytes, filters=None, parms=None):
    """A one-page PDF drawing a single image XObject built from raw parts."""
    pdf = pikepdf.new()
    pdf.add_blank_page()
    img = pikepdf.Stream(pdf, b"")
    if filters:
        img.write(data, filter=pikepdf.Array([pikepdf.Name(f) for f in filters]),
                  decode_parms=parms)
    else:
        img.write(data)
    for k, v in image_dict.items():
        img[k] = v
    img.Type, img.Subtype = pikepdf.Name.XObject, pikepdf.Name.Image
    pdf.pages[0].obj.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(
        Im0=pdf.make_indirect(img)))
    pdf.save(path, **RAW)
    return path


GRAY = {"/Width": 100, "/Height": 100, "/ColorSpace": pikepdf.Name.DeviceGray,
        "/BitsPerComponent": 8}


@pytest.mark.parametrize("filters", [
    ["/RunLengthDecode", "/FlateDecode"],      # a measured outer layer, unmeasured inside
    ["/LZWDecode", "/FlateDecode"],
])
def test_layers_that_cannot_be_measured_are_refused(tmp_path, settings, out_dir, filters):
    pdf = _image_pdf(tmp_path / "x.pdf", GRAY, b"\x80" * 10, filters)
    res = E.extract_images(pdf, out_dir, settings)
    assert "far larger" in res["skipped"][0]["reason"]


def test_ascii85_ignores_what_follows_its_end_marker(tmp_path, settings, out_dir):
    body = base64.a85encode(zlib.compress(b"\x80" * 10_000)) + b"~>junk!!"
    pdf = _image_pdf(tmp_path / "x.pdf", GRAY, body, ["/ASCII85Decode", "/FlateDecode"])
    (image,) = E.extract_images(pdf, out_dir, settings)["images"]
    assert (image["width"], image["height"]) == (100, 100)


def test_a_cut_intermediate_layer_is_refused():
    inner = zlib.compress(b"\0" * 3000, 0)        # stored blocks: no smaller than the data
    _, s = _stream(zlib.compress(inner), ["/FlateDecode", "/FlateDecode"])
    assert streams.within(s, 1000) is False


def test_predictors_wider_than_the_image_are_refused(tmp_path, settings, out_dir):
    pdf = _image_pdf(tmp_path / "x.pdf", GRAY, zlib.compress(b"\0" * 10), ["/FlateDecode"],
                     pikepdf.Dictionary(Predictor=15, Columns=1_000_000_000))
    res = E.extract_images(pdf, out_dir, settings)
    assert "decoding settings" in res["skipped"][0]["reason"]


def test_matching_predictors_are_fine(tmp_path, settings, out_dir):
    rows = b"".join(b"\x00" + bytes([n]) * 100 for n in range(100))  # PNG "none" rows
    pdf = _image_pdf(tmp_path / "x.pdf", GRAY, zlib.compress(rows), ["/FlateDecode"],
                     pikepdf.Dictionary(Predictor=15, Columns=100))
    assert len(E.extract_images(pdf, out_dir, settings)["images"]) == 1


@pytest.mark.parametrize("extra", [{}, {"/ColorTransform": 0}])
def test_jpegs_bigger_than_they_declare_are_refused(tmp_path, settings, out_dir, extra):
    big = _picture("JPEG", (3000, 2000))
    pdf = _image_pdf(tmp_path / "x.pdf", {**GRAY, "/ColorSpace": pikepdf.Name.DeviceRGB,
                                          **extra}, big, ["/DCTDecode"])
    res = E.extract_images(pdf, out_dir, settings)
    assert res["images"] == []
    assert "3000 x 2000 picture, not the 100 x 100" in res["skipped"][0]["reason"]


def test_unreadable_jpegs_are_refused(tmp_path, settings, out_dir):
    pdf = _image_pdf(tmp_path / "x.pdf", GRAY, b"not a jpeg", ["/DCTDecode"])
    assert "can't be read" in E.extract_images(pdf, out_dir, settings)["skipped"][0]["reason"]


def test_masks_are_checked_too(tmp_path, settings, out_dir):
    def add_mask(pdf):
        image = _images(pdf, 0)[0]
        mask = pikepdf.Stream(pdf, b"")
        mask.write(zlib.compress(b"\0" * 5_000_000), filter=pikepdf.Name.FlateDecode)
        mask.Width, mask.Height, mask.BitsPerComponent = 10, 10, 8
        mask.ColorSpace = pikepdf.Name.DeviceGray
        image.SMask = pdf.make_indirect(mask)
    pdf = _image_pdf(tmp_path / "x.pdf", GRAY, b"\x80" * 10_000)
    _edit(pdf, add_mask, **RAW)
    reason = E.extract_images(pdf, out_dir, settings)["skipped"][0]["reason"]
    assert reason.startswith("its mask has data far larger")


def test_a_conversion_crash_skips_only_that_image(pictures, settings, out_dir, monkeypatch):
    real = extract.PdfImage
    calls = []

    def flaky(image):
        calls.append(1)
        if len(calls) == 2:
            raise MemoryError("decoder gave up")
        return real(image)
    monkeypatch.setattr(extract, "PdfImage", flaky)
    res = E.extract_images(pictures, out_dir, settings)
    assert len(res["images"]) == 2
    assert res["skipped"][0]["reason"] == "it can't be converted: decoder gave up"


def test_a_failed_run_removes_what_it_wrote(pictures, settings, tmp_path, monkeypatch):
    out = tmp_path / "fresh"
    real, calls = extract._write, []

    def failing(path, data):
        calls.append(path)
        if len(calls) == 2:
            raise OSError("disk full")
        real(path, data)
    monkeypatch.setattr(extract, "_write", failing)
    with pytest.raises(OSError, match="disk full"):
        E.extract_images(pictures, out, settings)
    assert not out.exists()           # its files, and the folder it made, are gone


def test_a_failed_run_keeps_a_folder_it_did_not_make(doc, settings, out_dir, monkeypatch):
    _edit(doc, lambda pdf: (_attach(pdf, "a.txt", b"a"), _attach(pdf, "b.txt", b"b")))
    (out_dir / "keep.txt").write_bytes(b"mine")
    real, calls = extract._write, []

    def failing(path, data):
        calls.append(path)
        if len(calls) == 2:
            raise OSError("disk full")
        real(path, data)
    monkeypatch.setattr(extract, "_write", failing)
    with pytest.raises(OSError):
        E.extract_attachments(doc, out_dir, settings)
    assert sorted(p.name for p in out_dir.iterdir()) == ["keep.txt"]


def test_shortening_a_name_cannot_unlock_a_program(doc, settings, out_dir):
    name = "A" * 116 + ".bat.x" + "y" * 10
    _edit(doc, lambda pdf: _attach(pdf, name, b"@echo off\r\nstart calc"))
    res = E.extract_attachments(doc, out_dir, settings)
    assert res["files"] == [] and "allow_executables" in res["skipped"][0]["reason"]
    res = E.extract_attachments(doc, out_dir, settings, allow_executables=True)
    assert res["files"][0]["executable"] is True
    assert res["files"][0]["file"].endswith(".bat")


def test_names_equal_after_unicode_normalization_do_not_collide(doc, settings, out_dir):
    nfc, nfd = "café.txt", "café.txt"
    _edit(doc, lambda pdf: (_attach(pdf, nfc, b"first"), _attach(pdf, nfd, b"second")))
    res = E.extract_attachments(doc, out_dir, settings)
    assert len(res["files"]) == 2 and len({f["file"] for f in res["files"]}) == 2
    assert sorted(p.read_bytes() for p in out_dir.iterdir()) == [b"first", b"second"]


@pytest.mark.parametrize(("name", "safe"), [
    ("CON .txt", "attachment_CON .txt"),
    ("COM¹.txt", "attachment_COM¹.txt"),
    ("CONIN$", "attachment_CONIN$"),
    ("invoice‮txt.exe", "invoicetxt.exe"),          # right-to-left override removed
])
def test_more_reserved_and_invisible_names(name, safe):
    assert safe_filename(name, "attachment") == safe


@pytest.mark.parametrize("name", ["install.command", "run.py", "setup.dmg", "x.desktop"])
def test_more_names_that_run(doc, settings, out_dir, name):
    _edit(doc, lambda pdf: _attach(pdf, name, b"text"))
    assert E.extract_attachments(doc, out_dir, settings)["files"] == []


def test_extraction_says_when_attachments_were_left_unread(doc, settings, out_dir,
                                                           monkeypatch):
    monkeypatch.setattr(extract, "MAX_ATTACHMENTS", 2)
    _edit(doc, lambda pdf: [_attach(pdf, f"{n}.txt", b"x") for n in range(3)])
    res = E.extract_attachments(doc, out_dir, settings)
    assert len(res["files"]) == 2 and res["truncated"] is True
    with pytest.raises(PdfUserError, match="only the first 1,000 are read"):
        E.extract_attachments(doc, out_dir, settings, names=["missing.txt"])


def test_skipped_attachments_count_toward_the_work_budget(doc, settings, out_dir):
    big = b"z" * 1500
    _edit(doc, lambda pdf: [_attach(pdf, f"{n}.bin", big) for n in range(4)] +
          [_attach(pdf, "last.txt", b"small")])
    res = E.extract_attachments(doc, out_dir, dataclasses.replace(settings,
                                                                  max_file_bytes=1000))
    assert res["files"] == []                 # four oversized files used up the budget
    assert any("total size" in s["reason"] for s in res["skipped"])


def test_ascii85_input_is_bounded_by_what_is_asked_for():
    _, s = _stream(base64.a85encode(b"x" * 1_000_000), ["/ASCII85Decode"])
    assert streams.decode(s, 4096) == b"x" * 4096


def test_resources_shared_by_many_pages_are_read_once(tmp_path, settings, out_dir,
                                                      monkeypatch):
    pdf_path = _image_pdf(tmp_path / "x.pdf", GRAY, b"\x80" * 10_000)

    def share(pdf):
        resources = pdf.make_indirect(pdf.pages[0].obj.Resources)
        pdf.pages[0].obj.Resources = resources
        for _ in range(50):
            pdf.add_blank_page()
            pdf.pages[-1].obj.Resources = resources
    _edit(pdf_path, share, **RAW)
    walks = []
    real = extract._images_of
    monkeypatch.setattr(extract, "_images_of",
                        lambda r, d, s, c: walks.append(1) or real(r, d, s, c))
    (image,) = E.extract_images(pdf_path, out_dir, settings)["images"]
    assert image["pages"] == list(range(1, 52))
    assert len(walks) == 51 and len(extract._images_of.__defaults__ or ()) == 0


def test_inherited_page_resources_are_used(tmp_path, settings, out_dir):
    pdf_path = _image_pdf(tmp_path / "x.pdf", GRAY, b"\x80" * 10_000)

    def inherit(pdf):
        pdf.Root.Pages.Resources = pdf.pages[0].obj.Resources
        del pdf.pages[0].obj.Resources
    _edit(pdf_path, inherit, **RAW)
    assert len(E.extract_images(pdf_path, out_dir, settings)["images"]) == 1


def test_small_images_do_not_crowd_out_the_limit(pictures, settings, out_dir, monkeypatch):
    monkeypatch.setattr(extract, "MAX_IMAGES", 2)
    res = E.extract_images(pictures, out_dir, settings, min_size=10)
    assert len(res["images"]) == 2 and res["truncated"] is False


def test_total_pixels_per_call_are_capped(pictures, settings, out_dir, monkeypatch):
    monkeypatch.setattr(extract, "MAX_TOTAL_PIXELS", 40 * 30 + 64)
    res = E.extract_images(pictures, out_dir, settings)
    assert any("total size" in s["reason"] for s in res["skipped"])


def test_error_reasons_do_not_reveal_the_full_path(tmp_path):
    src = tmp_path / "secret" / "in.pdf"
    assert extract._reason(ValueError(f"{src} (object 4): bad"), src) == "in.pdf (object 4): bad"
    assert extract._reason(ValueError(""), src) == "ValueError"


def test_cleanup_keeps_a_folder_someone_else_wrote_into(pictures, settings, tmp_path,
                                                        monkeypatch):
    out = tmp_path / "fresh"
    real, calls = extract._write, []

    def failing(path, data):
        calls.append(path)
        if len(calls) == 2:
            (out / "theirs.txt").write_bytes(b"x")   # appeared meanwhile
            raise OSError("disk full")
        real(path, data)
    monkeypatch.setattr(extract, "_write", failing)
    with pytest.raises(OSError):
        E.extract_images(pictures, out, settings)
    assert sorted(p.name for p in out.iterdir()) == ["theirs.txt"]


def test_page_tree_search_is_bounded(tmp_path, settings, out_dir, monkeypatch):
    pdf_path = _image_pdf(tmp_path / "x.pdf", GRAY, b"\x80" * 10_000)

    def inherit(pdf):
        pdf.Root.Pages.Resources = pdf.pages[0].obj.Resources
        del pdf.pages[0].obj.Resources
    _edit(pdf_path, inherit, **RAW)
    monkeypatch.setattr(extract, "MAX_PARENTS", 0)
    assert E.extract_images(pdf_path, out_dir, settings)["images"] == []


def test_image_codec_data_needs_a_codec():
    _, s = _stream(b"plain")
    assert streams.encoded_image(s, 10) is None



def test_ascii85_text_beyond_its_cap_is_not_decoded(monkeypatch):
    monkeypatch.setattr(streams, "A85_MAX_TEXT", 100)
    _, s = _stream(base64.a85encode(b"x" * 1000), ["/ASCII85Decode"])
    assert streams.decode(s, 50) == b"x" * 50          # a little is still fine
    assert streams.decode(s, 1000) is None             # all of it needs too much text



def test_names_fit_file_systems_in_bytes_too():
    name = safe_filename("\U0001f600" * 120 + ".txt", "attachment")
    assert name.endswith(".txt") and len(name.encode()) <= 200
