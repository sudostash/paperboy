"""Path confinement, size caps, and overwrite policy."""
from __future__ import annotations

import pytest

from paperboy.config import Settings
from paperboy.errors import PdfUserError
from paperboy.paths import prepare_output, require_input, resolve


def test_outside_root_rejected(tmp_path):
    s = Settings(root=tmp_path.resolve())
    with pytest.raises(PdfUserError, match="outside the allowed root"):
        resolve("/etc/passwd", s)


def test_inside_root_ok(doc, settings):
    assert require_input(str(doc), settings) == doc


def test_missing_file(settings, tmp_path):
    with pytest.raises(PdfUserError, match="File not found"):
        require_input(str(tmp_path / "nope.pdf"), settings)


def test_size_cap(doc, tmp_path):
    s = Settings(root=tmp_path.resolve(), max_file_bytes=10)
    with pytest.raises(PdfUserError, match="over the"):
        require_input(str(doc), s)


def test_no_overwrite(doc, settings, tmp_path):
    existing = tmp_path / "out.pdf"
    existing.write_bytes(b"%PDF-1.4\n")
    with pytest.raises(PdfUserError, match="already exists"):
        prepare_output(doc, str(existing), "_x", False, settings)


def test_overwrite_allowed(doc, settings, tmp_path):
    existing = tmp_path / "out.pdf"
    existing.write_bytes(b"%PDF-1.4\n")
    assert prepare_output(doc, str(existing), "_x", True, settings) == existing


def test_output_same_as_input_rejected(doc, settings):
    with pytest.raises(PdfUserError, match="same as the input"):
        prepare_output(doc, str(doc), "_x", True, settings)
