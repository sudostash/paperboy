"""Path confinement, size caps, and overwrite policy."""
from __future__ import annotations

import pytest

from paperboy.config import Settings
from paperboy.errors import PdfUserError
from paperboy.paths import prepare_output, prepare_output_dir, require_input, resolve


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


# --- confinement: every way out of the sandbox must be refused ------------------


@pytest.fixture
def sandbox(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.pdf").write_bytes(b"%PDF-1.4 secret")
    return Settings(root=root.resolve()), root, outside


def test_dotdot_escape_is_refused(sandbox):
    s, root, _ = sandbox
    with pytest.raises(PdfUserError, match="outside the allowed root"):
        resolve(str(root / ".." / "outside" / "secret.pdf"), s)


def test_relative_escape_is_refused(sandbox, monkeypatch):
    s, root, _ = sandbox
    monkeypatch.chdir(root)
    with pytest.raises(PdfUserError, match="outside the allowed root"):
        resolve("../outside/secret.pdf", s)


def test_symlinked_file_pointing_outside_is_refused(sandbox):
    s, root, outside = sandbox
    (root / "link.pdf").symlink_to(outside / "secret.pdf")
    with pytest.raises(PdfUserError, match="outside the allowed root"):
        require_input(str(root / "link.pdf"), s)


def test_symlinked_directory_pointing_outside_is_refused(sandbox):
    s, root, outside = sandbox
    (root / "dir").symlink_to(outside)
    with pytest.raises(PdfUserError, match="outside the allowed root"):
        resolve(str(root / "dir" / "secret.pdf"), s)


def test_sibling_with_shared_prefix_is_refused(tmp_path):
    # "/x/root-evil" starts with "/x/root" as a string but is not inside it.
    (tmp_path / "root").mkdir()
    (tmp_path / "root-evil").mkdir()
    s = Settings(root=(tmp_path / "root").resolve())
    with pytest.raises(PdfUserError, match="outside the allowed root"):
        resolve(str(tmp_path / "root-evil" / "a.pdf"), s)


def test_tilde_is_expanded_then_confined(sandbox, monkeypatch, tmp_path):
    s, _, _ = sandbox
    monkeypatch.setenv("HOME", str(tmp_path / "outside"))
    with pytest.raises(PdfUserError, match="outside the allowed root"):
        resolve("~/secret.pdf", s)


def test_symlink_inside_root_is_allowed(sandbox):
    s, root, _ = sandbox
    (root / "real.pdf").write_bytes(b"%PDF-1.4")
    (root / "alias.pdf").symlink_to(root / "real.pdf")
    assert resolve(str(root / "alias.pdf"), s) == (root / "real.pdf").resolve()


def test_root_itself_resolves(sandbox):
    s, root, _ = sandbox
    assert resolve(str(root), s) == root.resolve()


def test_no_root_means_no_confinement(tmp_path):
    assert resolve(str(tmp_path / "anywhere.pdf"), Settings(root=None)) == (
        tmp_path / "anywhere.pdf").resolve()


@pytest.mark.parametrize("path", ["", "   "])
def test_empty_path_is_refused(settings, path):
    with pytest.raises(PdfUserError, match="must not be empty"):
        resolve(path, settings)


# --- require_input ----------------------------------------------------------------


def test_directory_is_not_an_input(settings, tmp_path):
    (tmp_path / "folder").mkdir()
    with pytest.raises(PdfUserError, match="Not a file"):
        require_input(str(tmp_path / "folder"), settings)


def test_file_exactly_at_size_cap_is_allowed(tmp_path):
    f = tmp_path / "ten.pdf"
    f.write_bytes(b"x" * 10)
    assert require_input(str(f), Settings(root=tmp_path.resolve(), max_file_bytes=10)) == f


def test_require_input_also_confines(sandbox):
    s, _, outside = sandbox
    with pytest.raises(PdfUserError, match="outside the allowed root"):
        require_input(str(outside / "secret.pdf"), s)


# --- prepare_output -----------------------------------------------------------------


def test_default_output_name_uses_suffix(doc, settings):
    out = prepare_output(doc, None, "_rotated", False, settings)
    assert out == doc.with_name("doc_rotated.pdf")


def test_default_output_name_with_custom_extension(tmp_path, settings):
    src = tmp_path / "photo.png"
    src.write_bytes(b"x")
    assert prepare_output(src, None, "", False, settings) == tmp_path / "photo.pdf"


def test_output_parent_directories_are_created(doc, settings, tmp_path):
    out = prepare_output(doc, str(tmp_path / "a" / "b" / "o.pdf"), "_x", False, settings)
    assert out.parent.is_dir()


def test_output_outside_root_is_refused(doc, sandbox):
    s, _, outside = sandbox
    with pytest.raises(PdfUserError, match="outside the allowed root"):
        prepare_output(doc, str(outside / "o.pdf"), "_x", True, s)


def test_output_via_symlink_to_input_is_refused(doc, settings, tmp_path):
    (tmp_path / "same.pdf").symlink_to(doc)
    with pytest.raises(PdfUserError, match="same as the input"):
        prepare_output(doc, str(tmp_path / "same.pdf"), "_x", True, settings)


def test_existing_default_output_is_protected(doc, settings):
    doc.with_name("doc_x.pdf").write_bytes(b"keep me")
    with pytest.raises(PdfUserError, match="already exists"):
        prepare_output(doc, None, "_x", False, settings)
    assert doc.with_name("doc_x.pdf").read_bytes() == b"keep me"


# --- prepare_output_dir ---------------------------------------------------------------


def test_prepare_output_dir_creates_it(settings, tmp_path):
    d = prepare_output_dir(str(tmp_path / "x" / "y"), settings)
    assert d.is_dir()


def test_prepare_output_dir_is_confined(sandbox):
    s, _, outside = sandbox
    with pytest.raises(PdfUserError, match="outside the allowed root"):
        prepare_output_dir(str(outside / "parts"), s)
    assert not (outside / "parts").exists()


# --- review findings: same file under a different name -----------------------------


def test_output_hard_link_to_input_is_refused(doc, settings, tmp_path):
    import os
    os.link(doc, tmp_path / "hard.pdf")
    with pytest.raises(PdfUserError, match="same as the input"):
        prepare_output(doc, str(tmp_path / "hard.pdf"), "_x", True, settings)


def test_output_case_variant_of_input_is_refused_on_case_insensitive_fs(doc, settings):
    upper = doc.with_name("DOC.PDF")
    if not upper.exists():
        pytest.skip("case-sensitive filesystem: DOC.PDF is a different file")
    with pytest.raises(PdfUserError, match="same as the input"):
        prepare_output(doc, str(upper), "_x", True, settings)
