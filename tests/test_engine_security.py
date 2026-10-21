"""Encryption and decryption."""
from __future__ import annotations

import dataclasses

import pikepdf
import pytest

from paperboy import engine as E
from paperboy.errors import PdfUserError

from conftest import page_labels


@pytest.mark.parametrize(("algorithm", "revision", "aes"), [
    ("AES-256", 6, True),
    ("AES-128", 4, True),
    ("RC4-128", 4, False),
    ("RC4-40", 2, False),
])
def test_encrypt_uses_requested_algorithm(doc, tmp_path, settings, algorithm, revision, aes):
    out = tmp_path / "e.pdf"
    res = E.encrypt_pdf(doc, out, "pw", settings, algorithm=algorithm)
    assert res == {"output": str(out), "algorithm": algorithm, "pages": 5}
    with pikepdf.open(out, password="pw") as pdf:
        assert pdf.is_encrypted
        assert pdf.encryption.R == revision
        assert (pdf.encryption.stream_method.name.startswith("aes")) is aes


def test_encrypted_output_requires_the_password(doc, tmp_path, settings):
    out = tmp_path / "e.pdf"
    E.encrypt_pdf(doc, out, "pw", settings)
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(out)
    assert page_labels(out, "pw") == [f"PAGE-{n}" for n in range(1, 6)]


def test_owner_password_differs_from_user_password(doc, tmp_path, settings):
    out = tmp_path / "e.pdf"
    E.encrypt_pdf(doc, out, "user", settings, owner_password="owner")
    with pikepdf.open(out, password="owner") as pdf:
        assert pdf.owner_password_matched
    with pikepdf.open(out, password="user") as pdf:
        assert pdf.user_password_matched and not pdf.owner_password_matched


def test_owner_password_defaults_to_user_password(doc, tmp_path, settings):
    out = tmp_path / "e.pdf"
    E.encrypt_pdf(doc, out, "only", settings)
    with pikepdf.open(out, password="only") as pdf:
        assert pdf.owner_password_matched


@pytest.mark.parametrize(("flags", "denied"), [
    ({"allow_print": False}, {"print_lowres", "print_highres"}),
    ({"allow_copy": False}, {"extract"}),
    ({"allow_modify": False}, {"modify_annotation", "modify_assembly",
                               "modify_form", "modify_other"}),
])
def test_permissions_are_restricted(doc, tmp_path, settings, flags, denied):
    out = tmp_path / "e.pdf"
    E.encrypt_pdf(doc, out, "user", settings, owner_password="owner", **flags)
    with pikepdf.open(out, password="user") as pdf:
        allow = pdf.allow
        for field in pikepdf.Permissions._fields:
            assert getattr(allow, field) is (field not in denied), field


# RC4-40 (revision 2) has no separate accessibility bit; it follows the copy bit.
@pytest.mark.parametrize("algorithm", ["AES-256", "AES-128", "RC4-128"])
def test_copy_protection_never_blocks_screen_readers(doc, tmp_path, settings, algorithm):
    out = tmp_path / "e.pdf"
    E.encrypt_pdf(doc, out, "u", settings, owner_password="o", algorithm=algorithm,
                  allow_print=False, allow_copy=False, allow_modify=False)
    with pikepdf.open(out, password="u") as pdf:
        assert pdf.allow.accessibility is True
        assert pdf.allow.extract is False


def test_encrypt_rejects_empty_password(doc, tmp_path, settings):
    with pytest.raises(PdfUserError, match="must not be empty"):
        E.encrypt_pdf(doc, tmp_path / "e.pdf", "", settings)
    assert not (tmp_path / "e.pdf").exists()


def test_encrypt_rejects_unknown_algorithm(doc, tmp_path, settings):
    with pytest.raises(PdfUserError, match="algorithm must be one of"):
        E.encrypt_pdf(doc, tmp_path / "e.pdf", "pw", settings, algorithm="aes-256")


def test_reencrypt_with_current_password(make_encrypted, tmp_path, settings):
    old = make_encrypted(password="old")
    out = tmp_path / "new.pdf"
    E.encrypt_pdf(old, out, "new", settings, current_password="old")
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(out, password="old")
    assert page_labels(out, "new")[0] == "PAGE-1"


def test_encrypt_encrypted_input_needs_current_password(make_encrypted, tmp_path, settings):
    with pytest.raises(PdfUserError, match="supply its password"):
        E.encrypt_pdf(make_encrypted(), tmp_path / "e.pdf", "new", settings)


def test_encrypt_enforces_page_cap(doc, tmp_path, settings):
    with pytest.raises(PdfUserError, match="page limit"):
        E.encrypt_pdf(doc, tmp_path / "e.pdf", "pw", dataclasses.replace(settings, max_pages=1))


def test_decrypt_removes_protection_and_keeps_content(make_encrypted, tmp_path, settings):
    out = tmp_path / "plain.pdf"
    res = E.decrypt_pdf(make_encrypted(password="pw"), out, "pw", settings)
    assert res == {"output": str(out), "pages": 3}
    with pikepdf.open(out) as pdf:
        assert not pdf.is_encrypted
    assert page_labels(out) == ["PAGE-1", "PAGE-2", "PAGE-3"]


def test_decrypt_with_owner_password(make_encrypted, tmp_path, settings):
    locked = make_encrypted(password="user", owner="owner")
    E.decrypt_pdf(locked, tmp_path / "plain.pdf", "owner", settings)
    with pikepdf.open(tmp_path / "plain.pdf") as pdf:
        assert not pdf.is_encrypted


@pytest.mark.parametrize("password", ["wrong", ""])
def test_decrypt_wrong_or_missing_password(make_encrypted, tmp_path, settings, password):
    with pytest.raises(PdfUserError, match="Incorrect password"):
        E.decrypt_pdf(make_encrypted(), tmp_path / "p.pdf", password, settings)
    assert not (tmp_path / "p.pdf").exists()


def test_decrypt_unencrypted_input(doc, tmp_path, settings):
    with pytest.raises(PdfUserError, match="not encrypted"):
        E.decrypt_pdf(doc, tmp_path / "p.pdf", "pw", settings)


def test_decrypt_non_pdf(tmp_path, settings):
    junk = tmp_path / "junk.pdf"
    junk.write_text("nope")
    with pytest.raises(PdfUserError, match="Could not open PDF"):
        E.decrypt_pdf(junk, tmp_path / "p.pdf", "pw", settings)


def test_decrypt_enforces_page_cap(make_encrypted, tmp_path, settings):
    with pytest.raises(PdfUserError, match="page limit"):
        E.decrypt_pdf(make_encrypted(), tmp_path / "p.pdf", "pw",
                      dataclasses.replace(settings, max_pages=2))
