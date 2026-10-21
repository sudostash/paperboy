"""Page operations: rearrange, delete, extract, rotate, merge, split."""
from __future__ import annotations

import dataclasses

import pikepdf
import pytest

from paperboy import engine as E
from paperboy.errors import PdfUserError

from conftest import UNPROTECTED, page_labels


def _titled(doc, tmp_path, title="Kept Title"):
    out = tmp_path / "titled.pdf"
    with pikepdf.open(doc) as pdf:
        with pdf.open_metadata() as m:
            m["dc:title"] = title
        pdf.save(out)
    return out


def _title(path):
    with pikepdf.open(path) as pdf, pdf.open_metadata() as m:
        return m.get("dc:title")


def _rotations(path):
    with pikepdf.open(path) as pdf:
        return [int(p.obj.get("/Rotate", 0)) % 360 for p in pdf.pages]


# --- rearrange ---------------------------------------------------------------


def test_rearrange_reorders_and_reports(doc, tmp_path, settings):
    out = tmp_path / "r.pdf"
    res = E.rearrange_pages(doc, out, "3,1,2", settings)
    assert res == {"output": str(out), "pages": 3, "order": [3, 1, 2], **UNPROTECTED}
    assert page_labels(out) == ["PAGE-3", "PAGE-1", "PAGE-2"]


def test_rearrange_duplicates_pages(doc, tmp_path, settings):
    out = tmp_path / "r.pdf"
    E.rearrange_pages(doc, out, "1,1,2", settings)
    assert page_labels(out) == ["PAGE-1", "PAGE-1", "PAGE-2"]


def test_rearrange_append_unlisted_keeps_original_order(doc, tmp_path, settings):
    out = tmp_path / "r.pdf"
    res = E.rearrange_pages(doc, out, "4", settings, append_unlisted=True)
    assert res["order"] == [4, 1, 2, 3, 5]


def test_rearrange_keeps_metadata(doc, tmp_path, settings):
    out = tmp_path / "r.pdf"
    E.rearrange_pages(_titled(doc, tmp_path), out, "2,1", settings)
    assert _title(out) == "Kept Title"


def test_rearrange_bad_spec_writes_nothing(doc, tmp_path, settings):
    with pytest.raises(PdfUserError):
        E.rearrange_pages(doc, tmp_path / "r.pdf", "9", settings)
    assert not (tmp_path / "r.pdf").exists()


def test_rearrange_encrypted_input(make_encrypted, tmp_path, settings):
    with pytest.raises(PdfUserError, match="supply its password"):
        E.rearrange_pages(make_encrypted(), tmp_path / "r.pdf", "1", settings)


# --- delete / extract ----------------------------------------------------------


def test_delete_removes_pages_and_reports_them(doc, tmp_path, settings):
    out = tmp_path / "d.pdf"
    res = E.delete_pages(doc, out, "5,2,2", settings)
    assert res == {"output": str(out), "pages": 3, "deleted": [2, 5], **UNPROTECTED}
    assert page_labels(out) == ["PAGE-1", "PAGE-3", "PAGE-4"]


def test_delete_every_page_is_refused(doc, tmp_path, settings):
    with pytest.raises(PdfUserError, match="Cannot delete every page"):
        E.delete_pages(doc, tmp_path / "d.pdf", "1-last", settings)
    assert not (tmp_path / "d.pdf").exists()


def test_delete_keeps_metadata(doc, tmp_path, settings):
    out = tmp_path / "d.pdf"
    E.delete_pages(_titled(doc, tmp_path), out, "1", settings)
    assert _title(out) == "Kept Title"


def test_extract_keeps_requested_order_and_duplicates(doc, tmp_path, settings):
    out = tmp_path / "x.pdf"
    res = E.extract_pages(doc, out, "5,1,5", settings)
    assert res == {"output": str(out), "pages": 3, **UNPROTECTED}
    assert page_labels(out) == ["PAGE-5", "PAGE-1", "PAGE-5"]


def test_extract_out_of_range(doc, tmp_path, settings):
    with pytest.raises(PdfUserError, match="out of range"):
        E.extract_pages(doc, tmp_path / "x.pdf", "0", settings)


# --- rotate --------------------------------------------------------------------


def test_rotate_all_pages_by_default(doc, tmp_path, settings):
    out = tmp_path / "r.pdf"
    res = E.rotate_pages(doc, out, 90, settings)
    assert res["rotated_pages"] == [1, 2, 3, 4, 5]
    assert _rotations(out) == [90] * 5


def test_rotate_selected_pages_only(doc, tmp_path, settings):
    out = tmp_path / "r.pdf"
    res = E.rotate_pages(doc, out, 180, settings, pages="2,last,2")
    assert res["rotated_pages"] == [2, 5]
    assert _rotations(out) == [0, 180, 0, 0, 180]


@pytest.mark.parametrize(("degrees", "expected"), [(-90, 270), (270, 270), (360, 0), (450, 90)])
def test_rotate_normalizes_any_multiple_of_90(doc, tmp_path, settings, degrees, expected):
    out = tmp_path / "r.pdf"
    E.rotate_pages(doc, out, degrees, settings, pages="1")
    assert _rotations(out)[0] == expected


def test_rotate_is_relative_to_existing_rotation(doc, tmp_path, settings):
    once, twice = tmp_path / "1.pdf", tmp_path / "2.pdf"
    E.rotate_pages(doc, once, 90, settings)
    E.rotate_pages(once, twice, 90, settings)
    assert _rotations(twice) == [180] * 5


@pytest.mark.parametrize("degrees", [45, 1, -30])
def test_rotate_rejects_non_right_angles(doc, tmp_path, settings, degrees):
    with pytest.raises(PdfUserError, match="multiple of 90"):
        E.rotate_pages(doc, tmp_path / "r.pdf", degrees, settings)


def test_rotate_leaves_input_untouched(doc, tmp_path, settings):
    before = doc.read_bytes()
    E.rotate_pages(doc, tmp_path / "r.pdf", 90, settings)
    assert doc.read_bytes() == before


# --- merge ------------------------------------------------------------------------


def test_merge_in_given_order(make_pdf, tmp_path, settings):
    a, b = make_pdf("a.pdf", 2), make_pdf("b.pdf", 1)
    out = tmp_path / "m.pdf"
    res = E.merge_pdfs([b, a], out, settings)
    assert res == {"output": str(out), "pages": 3, "sources": 2, **UNPROTECTED}
    assert page_labels(out) == ["PAGE-1", "PAGE-1", "PAGE-2"]


def test_merge_same_file_twice(make_pdf, tmp_path, settings):
    a = make_pdf("a.pdf", 2)
    out = tmp_path / "m.pdf"
    assert E.merge_pdfs([a, a], out, settings)["pages"] == 4


def test_merge_with_passwords(make_pdf, make_encrypted, tmp_path, settings):
    locked = make_encrypted("l.pdf", password="pw", pages=2)
    out = tmp_path / "m.pdf"
    E.merge_pdfs([make_pdf("a.pdf", 1), locked], out, settings, {str(locked): "pw"})
    assert len(page_labels(out, "pw")) == 3  # protected by the encrypted input's password


def test_merge_missing_password(make_pdf, make_encrypted, tmp_path, settings):
    with pytest.raises(PdfUserError, match="supply its password"):
        E.merge_pdfs([make_pdf("a.pdf", 1), make_encrypted()], tmp_path / "m.pdf", settings)
    assert not (tmp_path / "m.pdf").exists()


@pytest.mark.parametrize("count", [0, 1])
def test_merge_needs_two_inputs(make_pdf, tmp_path, settings, count):
    inputs = [make_pdf("a.pdf", 1)][:count]
    with pytest.raises(PdfUserError, match="at least two"):
        E.merge_pdfs(inputs, tmp_path / "m.pdf", settings)


def test_merge_enforces_combined_page_cap(make_pdf, tmp_path, settings):
    a, b = make_pdf("a.pdf", 3), make_pdf("b.pdf", 3)
    with pytest.raises(PdfUserError, match="exceed the 5 page limit"):
        E.merge_pdfs([a, b], tmp_path / "m.pdf", dataclasses.replace(settings, max_pages=5))
    assert not (tmp_path / "m.pdf").exists()


# --- split -----------------------------------------------------------------------


def test_split_by_ranges(doc, tmp_path, settings):
    out_dir = tmp_path / "parts"
    res = E.split_pdf(doc, out_dir, settings, ranges="1-2; 3 ;4-")
    assert res["count"] == 3
    assert res["output_dir"] == str(out_dir)
    assert [page_labels(f) for f in res["files"]] == [
        ["PAGE-1", "PAGE-2"], ["PAGE-3"], ["PAGE-4", "PAGE-5"]]
    assert res["files"][0].endswith("doc_part001.pdf")


def test_split_one_file_per_page_by_default(doc, tmp_path, settings):
    res = E.split_pdf(doc, tmp_path / "parts", settings)
    assert res["count"] == 5
    assert [page_labels(f) for f in res["files"]] == [[f"PAGE-{n}"] for n in range(1, 6)]


def test_split_creates_output_directory(doc, tmp_path, settings):
    out_dir = tmp_path / "a" / "b"
    E.split_pdf(doc, out_dir, settings, ranges="1")
    assert out_dir.is_dir()


def test_split_with_only_separators_is_rejected(doc, tmp_path, settings):
    with pytest.raises(PdfUserError, match="No ranges"):
        E.split_pdf(doc, tmp_path / "parts", settings, ranges=" ; ;")


def test_split_refuses_to_overwrite(doc, tmp_path, settings):
    E.split_pdf(doc, tmp_path / "parts", settings, ranges="1")
    with pytest.raises(PdfUserError, match="already exists"):
        E.split_pdf(doc, tmp_path / "parts", settings, ranges="2")
    assert page_labels(tmp_path / "parts" / "doc_part001.pdf") == ["PAGE-1"]


def test_split_overwrites_when_asked(doc, tmp_path, settings):
    E.split_pdf(doc, tmp_path / "parts", settings, ranges="1")
    E.split_pdf(doc, tmp_path / "parts", settings, ranges="2", overwrite=True)
    assert page_labels(tmp_path / "parts" / "doc_part001.pdf") == ["PAGE-2"]


def test_split_keeps_metadata(doc, tmp_path, settings):
    res = E.split_pdf(_titled(doc, tmp_path), tmp_path / "parts", settings, ranges="1")
    assert _title(res["files"][0]) == "Kept Title"
