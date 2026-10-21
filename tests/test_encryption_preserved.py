"""Outputs of a password-protected input stay protected; only decrypt_pdf removes it."""
from __future__ import annotations

import pikepdf
import pytest

from paperboy import engine as E
from paperboy.engine.base import inherit_protection
from paperboy.errors import PdfUserError

from conftest import HAS_QPDF, page_labels

RESTRICTED = pikepdf.Permissions(print_lowres=False, print_highres=False, modify_other=False)


@pytest.fixture
def locked(make_encrypted):
    return make_encrypted("locked.pdf", password="userpw", owner="ownerpw", pages=3,
                          allow=RESTRICTED)


def _assert_protected_like_input(path, user="userpw", allow=RESTRICTED):
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(path)
    with pikepdf.open(path, password=user) as pdf:
        assert pdf.is_encrypted
        assert pdf.encryption.R == 6  # AES-256
        assert pdf.user_password_matched
        # Exactly the input's permissions: none lifted, none added.
        assert pdf.allow == allow._replace(accessibility=True)


# Every engine function that writes a PDF from a single encrypted input.
SINGLE_INPUT_TOOLS = {
    "rearrange_pages": lambda src, out, s, pw: E.rearrange_pages(src, out, "2,1", s, password=pw),
    "delete_pages": lambda src, out, s, pw: E.delete_pages(src, out, "1", s, pw),
    "extract_pages": lambda src, out, s, pw: E.extract_pages(src, out, "1-2", s, pw),
    "rotate_pages": lambda src, out, s, pw: E.rotate_pages(src, out, 90, s, password=pw),
    "optimize_pdf": lambda src, out, s, pw: E.optimize_pdf(src, out, s, password=pw),
    "repair_pdf": lambda src, out, s, pw: E.repair_pdf(src, out, s, pw),
    "set_metadata": lambda src, out, s, pw: E.set_metadata(src, out, {"title": "T"}, s, pw),
}


@pytest.mark.parametrize("tool", sorted(SINGLE_INPUT_TOOLS))
def test_output_keeps_user_password_and_permissions(tool, locked, tmp_path, settings):
    out = tmp_path / "out.pdf"
    res = SINGLE_INPUT_TOOLS[tool](locked, out, settings, "userpw")
    assert res["password_protected"] is True
    _assert_protected_like_input(out)


@pytest.mark.parametrize("tool", sorted(SINGLE_INPUT_TOOLS))
def test_unprotected_input_gives_unprotected_output(tool, doc, tmp_path, settings):
    out = tmp_path / "out.pdf"
    res = SINGLE_INPUT_TOOLS[tool](doc, out, settings, None)
    assert res["password_protected"] is False
    with pikepdf.open(out) as pdf:
        assert not pdf.is_encrypted


def test_split_parts_are_each_protected(locked, tmp_path, settings):
    res = E.split_pdf(locked, tmp_path / "parts", settings, password="userpw")
    assert res["password_protected"] is True
    for part in res["files"]:
        _assert_protected_like_input(part)


def test_merge_with_one_encrypted_input_keeps_its_protection(make_pdf, locked, tmp_path,
                                                             settings):
    out = tmp_path / "m.pdf"
    res = E.merge_pdfs([make_pdf("plain.pdf", 1), locked], out, settings,
                       {str(locked): "userpw"})
    assert res["password_protected"] is True
    _assert_protected_like_input(out)
    assert len(page_labels(out, "userpw")) == 1 + 3


def test_merge_with_a_shared_password_uses_the_strictest_permissions(make_encrypted,
                                                                     tmp_path, settings):
    a = make_encrypted("a.pdf", password="same", allow=pikepdf.Permissions(extract=False))
    b = make_encrypted("b.pdf", password="same", allow=pikepdf.Permissions(modify_form=False))
    out = tmp_path / "m.pdf"
    E.merge_pdfs([a, b], out, settings, {str(a): "same", str(b): "same"})
    with pikepdf.open(out, password="same") as pdf:
        assert pdf.allow.extract is False and pdf.allow.modify_form is False


def test_merge_refuses_inputs_with_different_passwords(make_encrypted, tmp_path, settings):
    a = make_encrypted("a.pdf", password="first")
    b = make_encrypted("b.pdf", password="second")
    with pytest.raises(PdfUserError, match="different passwords"):
        E.merge_pdfs([a, b], tmp_path / "m.pdf", settings, {str(a): "first", str(b): "second"})
    assert not (tmp_path / "m.pdf").exists()


def test_merge_never_exposes_a_password_protected_input(make_pdf, locked, tmp_path, settings):
    # Review finding B1: an owner-only (no open password) file first must not make
    # the merged output, which contains a password-protected file, open freely.
    owner_only = tmp_path / "owner-only.pdf"
    with pikepdf.open(make_pdf("o.pdf", 1)) as pdf:
        pdf.save(owner_only, encryption=pikepdf.Encryption(user="", owner="boss", R=6))
    out = tmp_path / "m.pdf"
    res = E.merge_pdfs([owner_only, locked], out, settings, {str(locked): "userpw"})
    assert res["password_protected"] is True
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(out)
    assert len(page_labels(out, "userpw")) == 1 + 3


def test_merge_of_unprotected_inputs_is_unprotected(make_pdf, tmp_path, settings):
    out = tmp_path / "m.pdf"
    res = E.merge_pdfs([make_pdf("a.pdf", 1), make_pdf("b.pdf", 1)], out, settings)
    assert res["password_protected"] is False


@pytest.mark.skipif(not HAS_QPDF, reason="qpdf CLI not installed")
def test_flatten_keeps_protection(locked, tmp_path, settings):
    out = tmp_path / "f.pdf"
    res = E.flatten_pdf(locked, out, settings, "userpw")
    assert res["password_protected"] is True
    _assert_protected_like_input(out)


@pytest.mark.skipif(not HAS_QPDF, reason="qpdf CLI not installed")
def test_flatten_upgrades_old_encryption_to_aes256(make_pdf, tmp_path, settings):
    rc4 = tmp_path / "rc4.pdf"
    with pikepdf.open(make_pdf("p.pdf", 1)) as pdf:
        pdf.save(rc4, encryption=pikepdf.Encryption(user="u", owner="o", R=3, aes=False,
                                                    metadata=False, allow=RESTRICTED))
    out = tmp_path / "f.pdf"
    E.flatten_pdf(rc4, out, settings, "u")
    _assert_protected_like_input(out, user="u")


def test_decrypt_still_removes_protection(locked, tmp_path, settings):
    E.decrypt_pdf(locked, tmp_path / "plain.pdf", "userpw", settings)
    with pikepdf.open(tmp_path / "plain.pdf") as pdf:
        assert not pdf.is_encrypted


# --- inherited_encryption: which passwords the output gets ------------------------


def _encryption_for(path, password):
    with pikepdf.open(path, password=password or "") as pdf:
        return inherit_protection(pdf, password).encryption


def test_unencrypted_input_needs_no_encryption(doc):
    with pikepdf.open(doc) as pdf:
        assert inherit_protection(pdf, None).encryption is False


def test_opened_with_user_password_gets_random_owner_password(locked):
    first = _encryption_for(locked, "userpw")
    second = _encryption_for(locked, "userpw")
    assert first.user == "userpw"
    assert first.owner not in ("userpw", "ownerpw", "")
    assert len(first.owner) >= 32
    assert first.owner != second.owner  # not predictable


def test_opened_with_owner_password_keeps_both(locked):
    enc = _encryption_for(locked, "ownerpw")  # AES-256: user password not recoverable
    assert enc.owner == "ownerpw"
    assert enc.user == "ownerpw"


@pytest.mark.parametrize(("revision", "aes"), [(4, False), (4, True)])
def test_older_revisions_recover_the_user_password(make_pdf, tmp_path, revision, aes):
    out = tmp_path / "old.pdf"
    with pikepdf.open(make_pdf("p.pdf", 1)) as pdf:
        pdf.save(out, encryption=pikepdf.Encryption(user="userpw", owner="ownerpw", R=revision,
                                                    aes=aes, metadata=aes))
    enc = _encryption_for(out, "ownerpw")
    assert (enc.user, enc.owner, enc.R) == ("userpw", "ownerpw", 6)


def test_owner_only_file_opened_without_password_stays_open_but_restricted(make_pdf, tmp_path):
    out = tmp_path / "owner-only.pdf"
    with pikepdf.open(make_pdf("p.pdf", 1)) as pdf:
        pdf.save(out, encryption=pikepdf.Encryption(user="", owner="boss", R=6, allow=RESTRICTED))
    enc = _encryption_for(out, None)
    assert enc.user == ""
    assert enc.owner not in ("", "boss")
    assert enc.allow.print_highres is False


def test_aes256_owner_only_file_opened_with_owner_password_fails_safe(make_pdf, tmp_path,
                                                                      settings):
    # AES-256 hides whether the user password is empty; without reopening the file the
    # safe choice is to require the owner password, and to say so.
    src = tmp_path / "owner-only.pdf"
    with pikepdf.open(make_pdf("p.pdf", 1)) as pdf:
        pdf.save(src, encryption=pikepdf.Encryption(user="", owner="boss", R=6))
    res = E.rotate_pages(src, tmp_path / "r.pdf", 90, settings, password="boss")
    assert res["password_protected"] is True
    assert res["user_password_changed"] is True
    with pikepdf.open(tmp_path / "r.pdf", password="boss") as pdf:
        assert pdf.user_password_matched


# --- review findings: real tool outputs, not just Encryption objects -----------------


@pytest.mark.parametrize("revision", [2, 3, 4])
def test_owner_password_with_non_ascii_user_password_keeps_it_usable(make_pdf, tmp_path,
                                                                     settings, revision):
    src = tmp_path / "old.pdf"
    with pikepdf.open(make_pdf("p.pdf", 2)) as pdf:
        pdf.save(src, encryption=pikepdf.Encryption(user="café", owner="ownerpw", R=revision,
                                                    aes=False, metadata=False))
    out = tmp_path / "rot.pdf"
    res = E.rotate_pages(src, out, 90, settings, password="ownerpw")
    assert res["password_protected"] is True
    assert res["user_password_changed"] is False
    with pikepdf.open(out, password="café") as pdf:  # the original user password still works
        assert pdf.user_password_matched


def test_aes256_opened_with_owner_password_reports_the_changed_user_password(locked,
                                                                           tmp_path, settings):
    out = tmp_path / "rot.pdf"
    res = E.rotate_pages(locked, out, 90, settings, password="ownerpw")
    assert res["password_protected"] is True
    assert res["user_password_changed"] is True  # caller must be told the old one won't work
    _assert_protected_like_input(out, user="ownerpw")


def test_empty_owner_password_is_not_mistaken_for_no_open_password(make_pdf, tmp_path,
                                                                   settings):
    # Review finding S1: user "secret", owner "" opens with "" via the owner password.
    src = tmp_path / "weird.pdf"
    with pikepdf.open(make_pdf("p.pdf", 1)) as pdf:
        pdf.save(src, encryption=pikepdf.Encryption(user="secret", owner="", R=6))
    out = tmp_path / "rot.pdf"
    res = E.rotate_pages(src, out, 90, settings)
    assert res["password_protected"] is True
    with pikepdf.open(out) as pdf:  # opens only via the (empty) owner password, as before
        assert not pdf.user_password_matched  # so viewers still ask for a password


def test_owner_only_input_reports_restrictions_not_a_password(make_pdf, tmp_path, settings):
    src = tmp_path / "owner-only.pdf"
    with pikepdf.open(make_pdf("p.pdf", 1)) as pdf:
        pdf.save(src, encryption=pikepdf.Encryption(user="", owner="boss", R=6,
                                                    allow=RESTRICTED))
    out = tmp_path / "rot.pdf"
    res = E.rotate_pages(src, out, 90, settings)
    assert res["password_protected"] is False
    assert res["permissions_restricted"] is True
    with pikepdf.open(out) as pdf:  # still opens freely, still restricted
        assert pdf.allow == RESTRICTED._replace(accessibility=True)


def test_inherit_protection_never_reopens_the_file(locked, monkeypatch):
    # Review finding S2: decide from the open document, not by reopening its path.
    with pikepdf.open(locked, password="ownerpw") as pdf:
        monkeypatch.setattr(pikepdf, "open", lambda *a, **k: pytest.fail("reopened"))
        assert inherit_protection(pdf, "ownerpw").encryption
