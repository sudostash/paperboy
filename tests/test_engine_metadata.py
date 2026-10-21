"""Metadata write: set_metadata (read side is covered in test_engine_info)."""
from __future__ import annotations

import pikepdf
import pytest

from paperboy import engine as E
from paperboy.errors import PdfUserError

ALL_FIELDS = {
    "title": "Annual Report",
    "author": "Ann Lee",
    "subject": "Finances",
    "keywords": "money, growth",
    "creator": "Paperboy Tests",
    "producer": "Test Producer",
}


def test_set_every_field_roundtrips(doc, tmp_path, settings):
    out = tmp_path / "m.pdf"
    res = E.set_metadata(doc, out, ALL_FIELDS, settings)
    assert res == {"output": str(out), "updated": sorted(ALL_FIELDS)}
    assert E.get_metadata(out, settings)["metadata"] == ALL_FIELDS


def test_author_is_stored_as_ordered_list(doc, tmp_path, settings):
    out = tmp_path / "m.pdf"
    E.set_metadata(doc, out, {"author": " Ann ,Bob,, Cy "}, settings)
    with pikepdf.open(out) as pdf, pdf.open_metadata() as m:
        assert m["dc:creator"] == ["Ann", "Bob", "Cy"]


def test_unset_fields_are_left_alone(doc, tmp_path, settings):
    first, second = tmp_path / "1.pdf", tmp_path / "2.pdf"
    E.set_metadata(doc, first, {"title": "T", "subject": "S"}, settings)
    E.set_metadata(first, second, {"title": "New"}, settings)
    meta = E.get_metadata(second, settings)["metadata"]
    assert meta["title"] == "New"
    assert meta["subject"] == "S"


def test_unknown_fields_are_rejected_before_writing(doc, tmp_path, settings):
    with pytest.raises(PdfUserError, match="Unknown metadata field"):
        E.set_metadata(doc, tmp_path / "m.pdf", {"title": "ok", "color": "red"}, settings)
    assert not (tmp_path / "m.pdf").exists()


def test_set_metadata_on_encrypted_input(make_encrypted, tmp_path, settings):
    with pytest.raises(PdfUserError, match="supply its password"):
        E.set_metadata(make_encrypted(), tmp_path / "m.pdf", {"title": "x"}, settings)


def test_unicode_values_survive(doc, tmp_path, settings):
    out = tmp_path / "m.pdf"
    E.set_metadata(doc, out, {"title": "Résumé — 履歴書 ✓"}, settings)
    assert E.get_metadata(out, settings)["metadata"]["title"] == "Résumé — 履歴書 ✓"
