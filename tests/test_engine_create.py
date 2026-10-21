"""Creating PDFs from images, text, and Office/HTML documents."""
from __future__ import annotations

import dataclasses

import pytest
from PIL import Image
from pypdf import PdfReader

from paperboy import engine as E
from paperboy.engine import create
from paperboy.errors import DependencyError, PdfUserError

from conftest import page_labels


def _image(path, mode="RGB", size=(40, 30), color=None):
    color = color if color is not None else {"RGB": (200, 30, 30), "RGBA": (0, 0, 255, 0),
                                             "L": 128, "LA": (128, 0), "P": 3,
                                             "CMYK": (0, 0, 0, 255), "1": 1}[mode]
    Image.new(mode, size, color).save(path)
    return path


def _first_page_pixel(pdf_path):
    """Decode the first page's image and return its centre pixel (RGB)."""
    page = PdfReader(str(pdf_path)).pages[0]
    img = page.images[0].image.convert("RGB")
    return img.getpixel((img.width // 2, img.height // 2))


# --- images ----------------------------------------------------------------------


@pytest.mark.parametrize(("mode", "ext"), [
    ("RGB", ".png"), ("RGB", ".jpg"), ("L", ".png"), ("P", ".gif"),
    ("RGBA", ".png"), ("LA", ".png"), ("CMYK", ".tiff"), ("1", ".bmp"), ("RGB", ".webp"),
])
def test_convert_image_modes(tmp_path, settings, mode, ext):
    src = _image(tmp_path / f"img{ext}", mode)
    out = tmp_path / "img.pdf"
    res = E.convert_to_pdf(src, out, settings)
    assert res == {"output": str(out), "pages": 1, "source_kind": "image"}


def test_transparent_pixels_become_white(tmp_path, settings):
    src = _image(tmp_path / "clear.png", "RGBA", color=(0, 0, 255, 0))
    out = tmp_path / "clear.pdf"
    E.convert_to_pdf(src, out, settings)
    assert _first_page_pixel(out) == (255, 255, 255)


def test_opaque_colors_are_kept(tmp_path, settings):
    src = _image(tmp_path / "red.png", "RGB", color=(200, 30, 30))
    out = tmp_path / "red.pdf"
    E.convert_to_pdf(src, out, settings)
    r, g, b = _first_page_pixel(out)
    assert r > 180 and g < 60 and b < 60


def test_uppercase_extension_is_recognised(tmp_path, settings):
    src = _image(tmp_path / "PHOTO.JPG")
    assert E.convert_to_pdf(src, tmp_path / "p.pdf", settings)["source_kind"] == "image"


def test_images_to_pdf_one_page_per_image_in_order(tmp_path, settings):
    paths = [_image(tmp_path / f"{i}.png", size=(10 + i, 10)) for i in range(3)]
    out = tmp_path / "all.pdf"
    res = E.images_to_pdf(paths, out, settings)
    assert res == {"output": str(out), "pages": 3}
    widths = [float(p.mediabox.width) for p in PdfReader(str(out)).pages]
    assert widths == sorted(widths)  # same order as given


def test_images_to_pdf_needs_images(tmp_path, settings):
    with pytest.raises(PdfUserError, match="at least one image"):
        E.images_to_pdf([], tmp_path / "x.pdf", settings)


def test_images_to_pdf_rejects_non_images(tmp_path, settings):
    txt = tmp_path / "notes.txt"
    txt.write_text("hi")
    with pytest.raises(PdfUserError, match="Not image files: notes.txt"):
        E.images_to_pdf([_image(tmp_path / "a.png"), txt], tmp_path / "x.pdf", settings)


def test_corrupt_image_is_a_user_error(tmp_path, settings):
    bad = tmp_path / "broken.png"
    bad.write_bytes(b"\x89PNG\r\n\x1a\n this is not really a png")
    with pytest.raises(PdfUserError, match="broken.png"):
        E.convert_to_pdf(bad, tmp_path / "b.pdf", settings)
    assert not (tmp_path / "b.pdf").exists()


def test_decompression_bomb_is_a_user_error(tmp_path, settings, monkeypatch):
    src = _image(tmp_path / "big.png", size=(100, 100))
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)  # 10,000 px is > 2x this
    with pytest.raises(PdfUserError, match="big.png"):
        E.images_to_pdf([src], tmp_path / "b.pdf", settings)


# --- text ------------------------------------------------------------------------


def test_text_file_becomes_pdf(tmp_path, settings):
    src = tmp_path / "notes.md"
    src.write_text("# Title\nhello world\n")
    out = tmp_path / "notes.pdf"
    res = E.convert_to_pdf(src, out, settings)
    assert res == {"output": str(out), "pages": 1, "source_kind": "text"}
    text = page_labels(out)[0]
    assert "# Title" in text and "hello world" in text


def test_empty_text_file_gives_one_blank_page(tmp_path, settings):
    src = tmp_path / "empty.txt"
    src.write_text("")
    assert E.convert_to_pdf(src, tmp_path / "e.pdf", settings)["pages"] == 1


def test_long_text_flows_onto_more_pages(tmp_path, settings):
    src = tmp_path / "long.txt"
    src.write_text("\n".join(f"line {n}" for n in range(200)))
    out = tmp_path / "long.pdf"
    assert E.convert_to_pdf(src, out, settings)["pages"] > 1
    assert "line 199" in "".join(page_labels(out))


def test_long_lines_wrap_instead_of_running_off_the_page(tmp_path, settings):
    src = tmp_path / "wide.txt"
    src.write_text("word " * 200)
    out = tmp_path / "wide.pdf"
    E.convert_to_pdf(src, out, settings)
    assert len(page_labels(out)[0].splitlines()) > 5


def test_invalid_utf8_is_replaced_not_fatal(tmp_path, settings):
    src = tmp_path / "latin1.txt"
    src.write_bytes(b"caf\xe9 ok")
    out = tmp_path / "l.pdf"
    E.convert_to_pdf(src, out, settings)
    assert "ok" in page_labels(out)[0]


def test_indentation_and_spacing_are_preserved(tmp_path, settings):
    src = tmp_path / "config.yaml"
    src.write_text("server:\n    port:  8080\n")
    out = tmp_path / "c.pdf"
    E.convert_to_pdf(src, out, settings)
    content = PdfReader(str(out)).pages[0].get_contents().get_data()
    assert b"(    port:  8080)" in content


def test_tabs_are_expanded(tmp_path, settings):
    src = tmp_path / "tabs.txt"
    src.write_text("a\tb")
    out = tmp_path / "t.pdf"
    E.convert_to_pdf(src, out, settings)
    content = PdfReader(str(out)).pages[0].get_contents().get_data()
    assert b"(a    b)" in content  # text extraction would collapse the spaces


# --- dispatch ----------------------------------------------------------------------


def test_pdf_input_is_rejected(doc, tmp_path, settings):
    with pytest.raises(PdfUserError, match="already a PDF"):
        E.convert_to_pdf(doc, tmp_path / "x.pdf", settings)


@pytest.mark.parametrize("name", ["archive.zip", "noext", "song.mp3"])
def test_unsupported_types_are_rejected(tmp_path, settings, name):
    src = tmp_path / name
    src.write_bytes(b"data")
    with pytest.raises(PdfUserError, match="Unsupported file type"):
        E.convert_to_pdf(src, tmp_path / "x.pdf", settings)


# --- Office via LibreOffice (exercised with a stand-in soffice script) --------------


# The engine runs: soffice -env:... --headless --convert-to pdf --outdir <dir> <src>
SOFFICE_OK = """\
echo "$@" > "$(dirname "$0")/args.txt"
src="$7"; name=$(basename "$src"); stem="${name%.*}"
cp "$SAMPLE_PDF" "$6/$stem.pdf"
"""


@pytest.fixture
def office_doc(tmp_path):
    src = tmp_path / "letter.docx"
    src.write_bytes(b"fake docx")
    return src


def test_office_conversion_runs_soffice_headless(office_doc, doc, tmp_path, settings,
                                                 fake_binary, monkeypatch):
    monkeypatch.setenv("SAMPLE_PDF", str(doc))
    soffice = fake_binary("soffice", SOFFICE_OK)
    out = tmp_path / "letter.pdf"
    res = E.convert_to_pdf(office_doc, out, dataclasses.replace(settings, soffice_path=soffice))
    assert res == {"output": str(out), "pages": 5, "source_kind": "office"}
    args = (tmp_path / "bin" / "args.txt").read_text().split()
    assert args[0].startswith("-env:UserInstallation=file://")  # isolated profile
    assert args[1:4] == ["--headless", "--convert-to", "pdf"]
    assert args[-1] == str(office_doc)


def test_office_conversion_without_libreoffice(office_doc, tmp_path, settings):
    with pytest.raises(DependencyError, match="needs LibreOffice"):
        E.convert_to_pdf(office_doc, tmp_path / "x.pdf",
                         dataclasses.replace(settings, soffice_path=None))


def test_office_conversion_failure_reports_stderr(office_doc, tmp_path, settings, fake_binary):
    soffice = fake_binary("soffice", 'echo "source file could not be loaded" >&2\nexit 1\n')
    with pytest.raises(PdfUserError, match="could not be loaded"):
        E.convert_to_pdf(office_doc, tmp_path / "x.pdf",
                         dataclasses.replace(settings, soffice_path=soffice))


def test_office_conversion_that_produces_nothing(office_doc, tmp_path, settings, fake_binary):
    soffice = fake_binary("soffice", "exit 0\n")
    with pytest.raises(PdfUserError, match="LibreOffice conversion failed"):
        E.convert_to_pdf(office_doc, tmp_path / "x.pdf",
                         dataclasses.replace(settings, soffice_path=soffice))
    assert not (tmp_path / "x.pdf").exists()


def test_office_conversion_timeout(office_doc, tmp_path, settings, fake_binary):
    soffice = fake_binary("soffice", "sleep 5\n")
    with pytest.raises(PdfUserError, match="timed out after 1s"):
        E.convert_to_pdf(office_doc, tmp_path / "x.pdf",
                         dataclasses.replace(settings, soffice_path=soffice, soffice_timeout=1))


@pytest.mark.parametrize("ext", sorted(create.OFFICE_EXTS))
def test_every_office_extension_routes_to_libreoffice(tmp_path, settings, ext):
    src = tmp_path / f"file{ext}"
    src.write_bytes(b"x")
    with pytest.raises(DependencyError):
        E.convert_to_pdf(src, tmp_path / "x.pdf", dataclasses.replace(settings, soffice_path=None))


# --- review findings: resource limits ------------------------------------------------


def test_failed_image_is_closed(tmp_path, settings, monkeypatch):
    closed = []
    real_close = Image.Image.close
    monkeypatch.setattr(Image.Image, "close", lambda self: (closed.append(1), real_close(self)))

    def broken_load(self):
        raise OSError("truncated")

    from PIL import ImageFile
    monkeypatch.setattr(ImageFile.ImageFile, "load", broken_load)
    with pytest.raises(PdfUserError, match="not a readable image"):
        E.images_to_pdf([_image(tmp_path / "a.png")], tmp_path / "x.pdf", settings)
    assert closed


def test_image_over_the_pixel_limit_is_refused_not_just_warned(tmp_path, settings,
                                                               monkeypatch):
    src = _image(tmp_path / "big.png", size=(100, 100))
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 6000)  # 10,000 px: warning zone (< 2x)
    with pytest.raises(PdfUserError, match="too large"):
        E.images_to_pdf([src], tmp_path / "b.pdf", settings)


def test_images_to_pdf_enforces_page_cap(tmp_path, settings):
    paths = [_image(tmp_path / f"{n}.png") for n in range(3)]
    with pytest.raises(PdfUserError, match="over the 2 page limit"):
        E.images_to_pdf(paths, tmp_path / "x.pdf", dataclasses.replace(settings, max_pages=2))
    assert not (tmp_path / "x.pdf").exists()


def test_text_conversion_enforces_page_cap(tmp_path, settings):
    src = tmp_path / "huge.txt"
    src.write_text("x" * 84 * 60 * 5)  # about five pages of wrapped text
    with pytest.raises(PdfUserError, match="over the 2 page limit"):
        E.convert_to_pdf(src, tmp_path / "h.pdf", dataclasses.replace(settings, max_pages=2))
    assert not (tmp_path / "h.pdf").exists()
