"""Rendering pages to images: files on disk (render_pages) and inline (view_pages)."""
from __future__ import annotations

import dataclasses
import io
import threading

import pikepdf
import pytest
from PIL import Image
from reportlab.pdfgen import canvas

from paperboy import engine as E
from paperboy.engine import render
from paperboy.errors import PdfUserError


def _marked_pdf(path, pages=3, size=(595, 842)):
    """Each page: a black square at the bottom-left and its number, on white."""
    c = canvas.Canvas(str(path), pagesize=size)
    for n in range(1, pages + 1):
        c.setFillColorRGB(0, 0, 0)
        c.rect(0, 0, 100, 100, fill=1, stroke=0)
        c.setFont("Helvetica", 40)
        c.drawString(200, size[1] / 2, f"PAGE {n}")
        c.showPage()
    c.save()
    return path


@pytest.fixture
def marked(tmp_path):
    return _marked_pdf(tmp_path / "doc.pdf")


def _restricted(tmp_path, src, allow_extract=False):
    out = tmp_path / "restricted.pdf"
    with pikepdf.open(src) as pdf:
        pdf.save(out, encryption=pikepdf.Encryption(
            user="user", owner="owner", R=6, allow=pikepdf.Permissions(extract=allow_extract)))
    return out


# --- render_pages: files -------------------------------------------------------------


def test_renders_every_page_to_png_by_default(marked, tmp_path, settings):
    out_dir = tmp_path / "pages"
    res = E.render_pages(marked, out_dir, settings)
    assert res["output_dir"] == str(out_dir)
    assert res["pages"] == [1, 2, 3]
    assert res["dpi"] == 150 and res["format"] == "png"
    assert res["files"] == [str(out_dir / f"doc_p00{n}.png") for n in (1, 2, 3)]
    with Image.open(res["files"][0]) as im:
        assert im.format == "PNG"
        assert im.size == pytest.approx((1240, 1754), abs=1)  # A4 at 150 dpi, rounding


def test_rendered_pixels_match_the_page(marked, tmp_path, settings):
    res = E.render_pages(marked, tmp_path / "p", settings, pages="1", dpi=72)
    with Image.open(res["files"][0]) as im:
        rgb = im.convert("RGB")
        assert rgb.getpixel((20, rgb.height - 20)) == (0, 0, 0)        # the black square
        assert rgb.getpixel((rgb.width - 20, 20)) == (255, 255, 255)   # blank corner


def test_selected_pages_are_deduplicated_and_named_by_page_number(marked, tmp_path, settings):
    res = E.render_pages(marked, tmp_path / "p", settings, pages="3,1,3")
    assert res["pages"] == [3, 1]
    assert [f.rsplit("/", 1)[1] for f in res["files"]] == ["doc_p003.png", "doc_p001.png"]


def test_dpi_scales_the_image(marked, tmp_path, settings):
    small = E.render_pages(marked, tmp_path / "s", settings, pages="1", dpi=72)
    big = E.render_pages(marked, tmp_path / "b", settings, pages="1", dpi=144)
    with Image.open(small["files"][0]) as a, Image.open(big["files"][0]) as b:
        assert a.size[0] in (595, 596)
        assert b.size[0] == pytest.approx(a.size[0] * 2, abs=2)


@pytest.mark.parametrize("fmt", ["jpeg", "JPEG", "jpg"])
def test_jpeg_output(marked, tmp_path, settings, fmt):
    res = E.render_pages(marked, tmp_path / "j", settings, pages="1", fmt=fmt)
    assert res["format"] == "jpeg"
    assert res["files"][0].endswith("doc_p001.jpg")
    with Image.open(res["files"][0]) as im:
        assert im.format == "JPEG"


def test_unknown_format_is_rejected(marked, tmp_path, settings):
    with pytest.raises(PdfUserError, match="format must be"):
        E.render_pages(marked, tmp_path / "x", settings, fmt="gif")


@pytest.mark.parametrize("dpi", [0, 35, 601, -72])
def test_dpi_out_of_range_is_rejected(marked, tmp_path, settings, dpi):
    with pytest.raises(PdfUserError, match="dpi must be between 36 and 600"):
        E.render_pages(marked, tmp_path / "x", settings, dpi=dpi)


def test_rotated_pages_render_in_their_displayed_orientation(marked, tmp_path, settings):
    with pikepdf.open(marked, allow_overwriting_input=True) as pdf:
        pdf.pages[1].rotate(90, relative=True)
        pdf.save(marked)
    res = E.render_pages(marked, tmp_path / "r", settings, pages="1-2", dpi=72)
    with Image.open(res["files"][0]) as portrait, Image.open(res["files"][1]) as landscape:
        assert portrait.width < portrait.height
        assert landscape.width > landscape.height


def test_oversized_page_is_refused(tmp_path, settings):
    huge = _marked_pdf(tmp_path / "huge.pdf", pages=1, size=(14400, 14400))  # 200 x 200 in
    with pytest.raises(PdfUserError, match="lower dpi"):
        E.render_pages(huge, tmp_path / "h", settings, dpi=300)
    assert not (tmp_path / "h").exists() or not any((tmp_path / "h").iterdir())


def test_existing_files_are_not_overwritten_and_nothing_is_written(marked, tmp_path, settings):
    out_dir = tmp_path / "p"
    out_dir.mkdir()
    (out_dir / "doc_p002.png").write_bytes(b"keep")
    with pytest.raises(PdfUserError, match="already exists"):
        E.render_pages(marked, out_dir, settings)
    assert sorted(f.name for f in out_dir.iterdir()) == ["doc_p002.png"]  # no partial output
    assert (out_dir / "doc_p002.png").read_bytes() == b"keep"


def test_overwrite_replaces_existing_files(marked, tmp_path, settings):
    out_dir = tmp_path / "p"
    out_dir.mkdir()
    (out_dir / "doc_p001.png").write_bytes(b"old")
    E.render_pages(marked, out_dir, settings, pages="1", overwrite=True)
    with Image.open(out_dir / "doc_p001.png") as im:
        assert im.format == "PNG"


def test_bad_page_spec(marked, tmp_path, settings):
    with pytest.raises(PdfUserError, match="out of range"):
        E.render_pages(marked, tmp_path / "p", settings, pages="9")


def test_page_cap(marked, tmp_path, settings):
    with pytest.raises(PdfUserError, match="page limit"):
        E.render_pages(marked, tmp_path / "p", dataclasses.replace(settings, max_pages=2))


def test_non_pdf(tmp_path, settings):
    junk = tmp_path / "junk.pdf"
    junk.write_text("nope")
    with pytest.raises(PdfUserError, match="Could not open PDF"):
        E.render_pages(junk, tmp_path / "p", settings)


# --- passwords and permissions ---------------------------------------------------------


@pytest.mark.parametrize(("password", "message"), [
    (None, "supply its password"), ("bad", "Incorrect password")])
def test_encrypted_input_needs_its_password(make_encrypted, tmp_path, settings, password,
                                            message):
    with pytest.raises(PdfUserError, match=message):
        E.render_pages(make_encrypted(), tmp_path / "p", settings, password=password)


def test_encrypted_input_renders_with_its_password(make_encrypted, tmp_path, settings):
    res = E.render_pages(make_encrypted(password="pw"), tmp_path / "p", settings,
                         password="pw")
    assert len(res["files"]) == 3


def test_copy_protected_pdf_is_not_exported_with_the_user_password(marked, tmp_path,
                                                                   settings):
    locked = _restricted(tmp_path, marked)
    with pytest.raises(PdfUserError, match="does not allow copying"):
        E.render_pages(locked, tmp_path / "p", settings, password="user")
    assert not (tmp_path / "p").exists() or not any((tmp_path / "p").iterdir())


def test_copy_protected_pdf_exports_with_the_owner_password(marked, tmp_path, settings):
    locked = _restricted(tmp_path, marked)
    assert len(E.render_pages(locked, tmp_path / "p", settings, password="owner")["files"]) == 3


def test_copy_allowed_pdf_exports_with_the_user_password(marked, tmp_path, settings):
    locked = _restricted(tmp_path, marked, allow_extract=True)
    assert len(E.render_pages(locked, tmp_path / "p", settings, password="user")["files"]) == 3


# --- view_pages: inline images -------------------------------------------------------------


def _decode(png: bytes) -> Image.Image:
    im = Image.open(io.BytesIO(png))
    im.load()
    return im


def test_view_returns_png_images_fitted_to_the_size_limit(marked, settings):
    result, images = E.view_pages(marked, settings, pages="1-2")
    assert result["path"] == str(marked)
    assert result["total_pages"] == 3
    assert [p["page"] for p in result["pages"]] == [1, 2]
    for meta, png in zip(result["pages"], images, strict=True):
        im = _decode(png)
        assert im.format == "PNG"
        assert max(im.size) == 1568          # long side fitted exactly
        assert (meta["width_px"], meta["height_px"]) == im.size


def test_view_defaults_to_the_first_page(marked, settings):
    result, images = E.view_pages(marked, settings)
    assert [p["page"] for p in result["pages"]] == [1]
    assert len(images) == 1


@pytest.mark.parametrize("side", [200, 800, 2000])
def test_view_respects_max_side(marked, settings, side):
    _, images = E.view_pages(marked, settings, max_side_px=side)
    assert max(_decode(images[0]).size) == side


@pytest.mark.parametrize("side", [199, 2001, 0])
def test_view_max_side_out_of_range(marked, settings, side):
    with pytest.raises(PdfUserError, match="max_side_px must be between 200 and 2000"):
        E.view_pages(marked, settings, max_side_px=side)


def test_view_limits_pages_per_call(tmp_path, settings):
    doc = _marked_pdf(tmp_path / "long.pdf", pages=8)
    with pytest.raises(PdfUserError, match="at most 5 pages"):
        E.view_pages(doc, settings, pages="1-6")
    assert len(E.view_pages(doc, settings, pages="1-5")[1]) == 5


def test_view_shows_the_right_page(marked, settings):
    _, (png,) = E.view_pages(marked, settings, pages="1")
    im = _decode(png).convert("RGB")
    assert im.getpixel((5, im.height - 5)) == (0, 0, 0)


def test_view_of_copy_protected_pdf_is_allowed(marked, tmp_path, settings):
    # Display is always permitted; only exporting copies is restricted.
    locked = _restricted(tmp_path, marked)
    result, images = E.view_pages(locked, settings, password="user")
    assert len(images) == 1


def test_view_needs_password(make_encrypted, settings):
    with pytest.raises(PdfUserError, match="supply its password"):
        E.view_pages(make_encrypted(), settings)


# --- PDFium is not thread-safe -------------------------------------------------------------


def test_concurrent_renders_are_serialized_and_correct(tmp_path, settings):
    docs = [_marked_pdf(tmp_path / f"d{n}.pdf", pages=2) for n in range(6)]
    results, errors = {}, []

    def work(n):
        try:
            results[n] = E.render_pages(docs[n], tmp_path / f"out{n}", settings, dpi=72)
        except Exception as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    for res in results.values():
        with Image.open(res["files"][0]) as im:
            assert im.convert("RGB").getpixel((20, im.height - 20)) == (0, 0, 0)


def test_rendering_holds_the_pdfium_lock(marked, tmp_path, settings, monkeypatch):
    held = []
    real = render.pdfium.PdfPage.render

    def spy(self, *args, **kwargs):
        held.append(render._PDFIUM_LOCK.locked())
        return real(self, *args, **kwargs)

    monkeypatch.setattr(render.pdfium.PdfPage, "render", spy)
    E.render_pages(marked, tmp_path / "p", settings, pages="1")
    E.view_pages(marked, settings)
    assert held and all(held)


def test_failure_midway_removes_files_already_written(marked, tmp_path, settings,
                                                      monkeypatch):
    real, calls = render._render, []

    def fail_on_second(doc, index, scale):
        calls.append(index)
        if len(calls) == 2:
            raise RuntimeError("disk full")
        return real(doc, index, scale)

    monkeypatch.setattr(render, "_render", fail_on_second)
    with pytest.raises(RuntimeError, match="disk full"):
        E.render_pages(marked, tmp_path / "p", settings)
    assert list((tmp_path / "p").iterdir()) == []  # page 1 was written, then removed


def test_pdfium_refusing_a_file_pikepdf_accepted_is_a_user_error(marked, tmp_path, settings,
                                                                 monkeypatch):
    def refuse(*args, **kwargs):
        raise render.pdfium.PdfiumError("Failed to load document (PDFium: Data format error).")

    monkeypatch.setattr(render.pdfium, "PdfDocument", refuse)
    with pytest.raises(PdfUserError, match="Could not open PDF"):
        E.view_pages(marked, settings)


# --- review findings: writing safely ---------------------------------------------------


@pytest.fixture
def outside(tmp_path_factory):
    return tmp_path_factory.mktemp("outside")


@pytest.mark.parametrize("overwrite", [False, True])
def test_dangling_symlink_target_is_refused(marked, tmp_path, outside, settings, overwrite):
    out_dir = tmp_path / "p"
    out_dir.mkdir()
    (out_dir / "doc_p001.png").symlink_to(outside / "new.png")
    with pytest.raises(PdfUserError, match="not a regular file"):
        E.render_pages(marked, out_dir, settings, pages="1", overwrite=overwrite)
    assert not (outside / "new.png").exists()


def test_symlinked_target_file_is_never_written_through(marked, tmp_path, outside, settings):
    victim = outside / "victim.txt"
    victim.write_text("keep me")
    out_dir = tmp_path / "p"
    out_dir.mkdir()
    (out_dir / "doc_p001.png").symlink_to(victim)
    with pytest.raises(PdfUserError, match="not a regular file"):
        E.render_pages(marked, out_dir, settings, pages="1", overwrite=True)
    assert victim.read_text() == "keep me"


def test_hard_link_to_the_input_is_never_overwritten(marked, tmp_path, settings):
    import os
    out_dir = tmp_path / "p"
    out_dir.mkdir()
    os.link(marked, out_dir / "doc_p001.png")
    before = marked.read_bytes()
    with pytest.raises(PdfUserError, match="same as the input"):
        E.render_pages(marked, out_dir, settings, pages="1", overwrite=True)
    assert marked.read_bytes() == before


def test_overwrite_replaces_the_entry_not_a_shared_inode(marked, tmp_path, settings):
    import os
    out_dir = tmp_path / "p"
    out_dir.mkdir()
    keep = tmp_path / "other.png"
    keep.write_bytes(b"not mine")
    os.link(keep, out_dir / "doc_p001.png")  # another file's hard link
    E.render_pages(marked, out_dir, settings, pages="1", overwrite=True)
    assert keep.read_bytes() == b"not mine"   # written via replace, not in place


def test_output_dir_that_is_a_file_is_a_user_error(marked, tmp_path, settings):
    (tmp_path / "p").write_text("file")
    with pytest.raises(PdfUserError, match="not a folder"):
        E.render_pages(marked, tmp_path / "p", settings)


def test_document_with_no_pages(tmp_path, settings):
    empty = tmp_path / "empty.pdf"
    pdf = pikepdf.new()
    pdf.save(empty)
    with pytest.raises(PdfUserError, match="no pages"):
        E.render_pages(empty, tmp_path / "p", settings)
    assert not (tmp_path / "p").exists()


# --- review findings: work budget and lock scope ---------------------------------------------


def test_total_output_pixels_per_call_are_capped(tmp_path, settings):
    doc = _marked_pdf(tmp_path / "many.pdf", pages=20)  # A4: ~34.8 MP per page at 600 dpi
    with pytest.raises(PdfUserError, match="fewer pages or a lower dpi"):
        E.render_pages(doc, tmp_path / "p", settings, dpi=600)
    assert not (tmp_path / "p").exists()
    assert len(E.render_pages(doc, tmp_path / "q", settings, dpi=72)["files"]) == 20


def test_encoding_and_saving_happen_outside_the_pdfium_lock(marked, tmp_path, settings,
                                                            monkeypatch):
    from PIL import Image as PILImage
    held = []
    real_save = PILImage.Image.save

    def spy(self, *args, **kwargs):
        held.append(render._PDFIUM_LOCK.locked())
        return real_save(self, *args, **kwargs)

    monkeypatch.setattr(PILImage.Image, "save", spy)
    E.render_pages(marked, tmp_path / "p", settings)
    E.view_pages(marked, settings)
    assert held and not any(held)


# --- review findings: robustness -----------------------------------------------------------


def _undercounted(tmp_path):
    """3 real pages, but /Count says 1: pikepdf repairs to 3, PDFium believes 1."""
    src = _marked_pdf(tmp_path / "src.pdf", pages=3)
    data = src.read_bytes()
    with pikepdf.open(src) as pdf:
        pdf.save(tmp_path / "plain.pdf", compress_streams=False,
                 object_stream_mode=pikepdf.ObjectStreamMode.disable)
    data = (tmp_path / "plain.pdf").read_bytes().replace(b"/Count 3", b"/Count 1", 1)
    bad = tmp_path / "undercounted.pdf"
    bad.write_bytes(data)
    return bad


def test_pages_pdfium_cannot_load_are_user_errors(tmp_path, settings):
    bad = _undercounted(tmp_path)
    with pytest.raises(PdfUserError, match="Could not render page 3"):
        E.view_pages(bad, settings, pages="3")
    with pytest.raises(PdfUserError, match="Could not render page"):
        E.render_pages(bad, tmp_path / "p", settings)


def test_the_checked_bytes_are_the_rendered_bytes(marked, tmp_path, settings, monkeypatch):
    # Review finding S2: the file is read once; swapping it after the permission check
    # must not change what gets rendered.
    swapped = _marked_pdf(tmp_path / "swap.pdf", pages=1)
    real_count = render._page_count

    def swap_after_check(*args, **kwargs):
        n = real_count(*args, **kwargs)
        marked.write_bytes(swapped.read_bytes())
        return n

    monkeypatch.setattr(render, "_page_count", swap_after_check)
    res = E.render_pages(marked, tmp_path / "p", settings, dpi=72)
    assert len(res["files"]) == 3  # the original three pages, not the swapped file


def test_pdfium_render_failure_is_a_user_error(marked, settings, monkeypatch):
    def broken(self, *args, **kwargs):
        raise render.pdfium.PdfiumError("Failed to render")

    monkeypatch.setattr(render.pdfium.PdfPage, "render", broken)
    with pytest.raises(PdfUserError, match="Could not render page 1"):
        E.view_pages(marked, settings)


def test_failed_save_leaves_no_temp_file(marked, tmp_path, settings, monkeypatch):
    from PIL import Image as PILImage

    def full_disk(self, fp, *args, **kwargs):
        fp.write(b"partial")
        raise OSError("No space left on device")

    monkeypatch.setattr(PILImage.Image, "save", full_disk)
    with pytest.raises(OSError, match="No space left"):
        E.render_pages(marked, tmp_path / "p", settings, pages="1")
    assert list((tmp_path / "p").iterdir()) == []  # neither the target nor a temp file
