"""Engine tests. These exercise PDF behavior directly, without the MCP layer."""
from __future__ import annotations

import shutil

import pikepdf
import pytest
from PIL import Image

from paperboy import engine as E
from paperboy.errors import DependencyError, PdfUserError

from conftest import HAS_QPDF, HAS_SOFFICE, page_labels


# --- inspection -----------------------------------------------------------


def test_pdf_info(doc, settings):
    info = E.pdf_info(doc, settings)
    assert info["pages"] == 5
    assert info["is_encrypted"] is False


def test_extract_text(doc, settings):
    out = E.extract_text(doc, settings, pages="2")
    assert "PAGE-2" in out["text"]
    assert out["truncated"] is False


def test_check_pdf(doc, settings):
    assert E.check_pdf(doc, settings)["healthy"] is True


# --- security -------------------------------------------------------------


@pytest.mark.parametrize("algorithm", ["AES-256", "AES-128", "RC4-128", "RC4-40"])
def test_encrypt_each_algorithm(doc, settings, tmp_path, algorithm):
    out = tmp_path / f"{algorithm}.pdf"
    E.encrypt_pdf(doc, out, "pw", settings, algorithm=algorithm, allow_copy=False)
    assert pikepdf.open(str(out), password="pw").is_encrypted
    info = E.pdf_info(out, settings, password="pw")
    assert info["pages"] == 5
    assert info["permissions"]["extract"] is False


def test_info_on_encrypted_without_password(doc, settings, tmp_path):
    enc = tmp_path / "enc.pdf"
    E.encrypt_pdf(doc, enc, "pw", settings)
    info = E.pdf_info(enc, settings)
    assert info["is_encrypted"] is True
    assert info["pages"] is None  # needs password


def test_decrypt_roundtrip(doc, settings, tmp_path):
    enc = tmp_path / "enc.pdf"
    E.encrypt_pdf(doc, enc, "pw", settings)
    dec = E.decrypt_pdf(enc, tmp_path / "dec.pdf", "pw", settings)
    assert pikepdf.open(dec["output"]).is_encrypted is False


def test_decrypt_wrong_password(doc, settings, tmp_path):
    enc = tmp_path / "enc.pdf"
    E.encrypt_pdf(doc, enc, "pw", settings)
    with pytest.raises(PdfUserError, match="Incorrect password"):
        E.decrypt_pdf(enc, tmp_path / "x.pdf", "nope", settings)


def test_decrypt_unencrypted_errors(doc, settings, tmp_path):
    with pytest.raises(PdfUserError, match="not encrypted"):
        E.decrypt_pdf(doc, tmp_path / "x.pdf", "pw", settings)


# --- pages ----------------------------------------------------------------


def test_rearrange(doc, settings, tmp_path):
    out = E.rearrange_pages(doc, tmp_path / "r.pdf", "3,1,last", settings)["output"]
    assert page_labels(out) == ["PAGE-3", "PAGE-1", "PAGE-5"]


def test_rearrange_reverse(doc, settings, tmp_path):
    out = E.rearrange_pages(doc, tmp_path / "r.pdf", "5-1", settings)["output"]
    assert page_labels(out) == [f"PAGE-{i}" for i in range(5, 0, -1)]


def test_rearrange_append_unlisted(doc, settings, tmp_path):
    out = E.rearrange_pages(doc, tmp_path / "r.pdf", "4,2", settings,
                            append_unlisted=True)["output"]
    assert page_labels(out) == ["PAGE-4", "PAGE-2", "PAGE-1", "PAGE-3", "PAGE-5"]


def test_delete(doc, settings, tmp_path):
    out = E.delete_pages(doc, tmp_path / "d.pdf", "2,4-5", settings)["output"]
    assert page_labels(out) == ["PAGE-1", "PAGE-3"]


def test_delete_all_errors(doc, settings, tmp_path):
    with pytest.raises(PdfUserError, match="every page"):
        E.delete_pages(doc, tmp_path / "d.pdf", "1-5", settings)


def test_extract(doc, settings, tmp_path):
    out = E.extract_pages(doc, tmp_path / "e.pdf", "2-3", settings)["output"]
    assert page_labels(out) == ["PAGE-2", "PAGE-3"]


def test_rotate(doc, settings, tmp_path):
    out = E.rotate_pages(doc, tmp_path / "r.pdf", 90, settings, pages="1")["output"]
    with pikepdf.open(out) as pdf:
        assert int(pdf.pages[0].obj.Rotate) == 90
        assert int(pdf.pages[1].obj.get("/Rotate", 0)) == 0


def test_rotate_bad_angle(doc, settings, tmp_path):
    with pytest.raises(PdfUserError, match="multiple of 90"):
        E.rotate_pages(doc, tmp_path / "r.pdf", 45, settings)


def test_out_of_range(doc, settings, tmp_path):
    with pytest.raises(PdfUserError, match="out of range"):
        E.extract_pages(doc, tmp_path / "e.pdf", "9", settings)


def test_merge(doc, settings, make_pdf, tmp_path):
    other = make_pdf("other.pdf", 2)
    enc = tmp_path / "enc.pdf"
    E.encrypt_pdf(doc, enc, "pw", settings)
    res = E.merge_pdfs([doc, enc, other], tmp_path / "m.pdf", settings,
                       passwords={str(enc): "pw"})
    assert res["pages"] == 12


def test_merge_too_few(doc, settings, tmp_path):
    with pytest.raises(PdfUserError, match="at least two"):
        E.merge_pdfs([doc], tmp_path / "m.pdf", settings)


def test_split_ranges(doc, settings, tmp_path):
    res = E.split_pdf(doc, tmp_path / "split", settings, ranges="1-2;3-")
    assert [len(pikepdf.open(f).pages) for f in res["files"]] == [2, 3]


def test_split_each_page(doc, settings, tmp_path):
    res = E.split_pdf(doc, tmp_path / "each", settings)
    assert res["count"] == 5


# --- optimization ---------------------------------------------------------


def test_optimize(doc, settings, tmp_path):
    res = E.optimize_pdf(doc, tmp_path / "o.pdf", settings)
    assert res["linearized"] is True
    assert pikepdf.open(res["output"])


def test_repair(doc, settings, tmp_path):
    assert E.repair_pdf(doc, tmp_path / "r.pdf", settings)["pages"] == 5


@pytest.mark.skipif(not HAS_QPDF, reason="qpdf CLI not installed")
def test_flatten(doc, settings, tmp_path):
    assert E.flatten_pdf(doc, tmp_path / "f.pdf", settings)["pages"] == 5


def test_flatten_without_qpdf(doc, tmp_path):
    from paperboy.config import Settings
    s = Settings(root=tmp_path.resolve(), qpdf_path=None)
    with pytest.raises(DependencyError, match="qpdf"):
        E.flatten_pdf(doc, tmp_path / "f.pdf", s)


# --- metadata -------------------------------------------------------------


def test_metadata_roundtrip(doc, settings, tmp_path):
    E.set_metadata(doc, tmp_path / "m.pdf", {"title": "Hello", "author": "Himan"}, settings)
    md = E.get_metadata(tmp_path / "m.pdf", settings)["metadata"]
    assert md["title"] == "Hello"
    assert "Himan" in md["author"]


def test_metadata_bad_field(doc, settings, tmp_path):
    with pytest.raises(PdfUserError, match="Unknown metadata"):
        E.set_metadata(doc, tmp_path / "m.pdf", {"nope": "x"}, settings)


# --- creation -------------------------------------------------------------


def _img(path, color):
    Image.new("RGBA", (300, 200), color).save(path)
    return path


def test_image_to_pdf(settings, tmp_path):
    img = _img(tmp_path / "i.png", "red")
    assert E.convert_to_pdf(img, tmp_path / "i.pdf", settings)["pages"] == 1


def test_images_to_pdf(settings, tmp_path):
    imgs = [_img(tmp_path / f"i{n}.png", c) for n, c in enumerate(("red", "green", "blue"))]
    assert E.images_to_pdf(imgs, tmp_path / "i.pdf", settings)["pages"] == 3


def test_text_to_pdf(settings, tmp_path):
    txt = tmp_path / "n.txt"
    txt.write_text("\n".join(f"line {i} " + "word " * 40 for i in range(120)))
    assert E.convert_to_pdf(txt, tmp_path / "t.pdf", settings)["pages"] >= 2


def test_convert_already_pdf(doc, settings, tmp_path):
    with pytest.raises(PdfUserError, match="already a PDF"):
        E.convert_to_pdf(doc, tmp_path / "x.pdf", settings)


def test_convert_unsupported(settings, tmp_path):
    f = tmp_path / "data.xyz"
    f.write_text("x")
    with pytest.raises(PdfUserError, match="Unsupported"):
        E.convert_to_pdf(f, tmp_path / "x.pdf", settings)


@pytest.mark.skipif(not HAS_SOFFICE, reason="LibreOffice not installed")
def test_html_to_pdf(settings, tmp_path):
    from pypdf import PdfReader
    html = tmp_path / "p.html"
    html.write_text("<html><body><h1>Hello Office</h1></body></html>")
    out = E.convert_to_pdf(html, tmp_path / "h.pdf", settings)["output"]
    assert "Hello Office" in (PdfReader(out).pages[0].extract_text() or "")
