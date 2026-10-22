"""Password handling across tools: non-ASCII passwords, and the copy permission.

Two passwords protect a PDF: the user ("open") password and the owner password,
which lifts restrictions. When a PDF forbids copying, getting its content out
(extract_text, decrypt_pdf, re-encrypting) needs the owner password.
"""
from __future__ import annotations

import pikepdf
import pytest

from paperboy import engine as E
from paperboy.errors import PdfUserError

from conftest import HAS_QPDF, page_labels

# (revision, aes) for every encryption scheme Paperboy can read or write.
SCHEMES = [(2, False), (3, False), (4, False), (4, True), (6, True)]
SCHEME_IDS = ["RC4-40", "RC4-128-R3", "RC4-128", "AES-128", "AES-256"]


def _encrypt(make_pdf, tmp_path, name, user, owner, revision=6, aes=True, allow=None):
    out = tmp_path / name
    with pikepdf.open(make_pdf(f"plain-{name}", 2)) as pdf:
        pdf.save(out, encryption=pikepdf.Encryption(
            user=user, owner=owner, R=revision, aes=aes, metadata=aes,
            allow=allow or pikepdf.Permissions()))
    return out


# --- non-ASCII passwords ------------------------------------------------------------


@pytest.fixture(params=SCHEMES, ids=SCHEME_IDS)
def accented(request, make_pdf, tmp_path):
    revision, aes = request.param
    return _encrypt(make_pdf, tmp_path, "accented.pdf", "café", "propriétaire", revision, aes)


@pytest.mark.parametrize("password", ["café", "propriétaire"])
def test_non_ascii_passwords_open_every_scheme(accented, settings, password):
    assert E.pdf_info(accented, settings, password)["pages"] == 2
    assert E.get_metadata(accented, settings, password)["path"] == str(accented)
    assert "PAGE-1" in E.extract_text(accented, settings, password=password)["text"]


def test_non_ascii_password_works_for_writing_tools(accented, tmp_path, settings):
    rotated = tmp_path / "rotated.pdf"
    res = E.rotate_pages(accented, rotated, 90, settings, password="café")
    assert res["password_protected"] is True
    assert page_labels(rotated, "café")[0] == "PAGE-1"  # output opens with the same password
    E.repair_pdf(accented, tmp_path / "repaired.pdf", settings, "café")
    E.decrypt_pdf(accented, tmp_path / "plain.pdf", "propriétaire", settings)
    with pikepdf.open(tmp_path / "plain.pdf") as pdf:
        assert not pdf.is_encrypted


@pytest.mark.skipif(not HAS_QPDF, reason="qpdf CLI not installed")
def test_non_ascii_password_works_with_qpdf_cli(accented, settings):
    assert E.check_pdf(accented, settings, "café")["healthy"] is True


def test_wrong_non_ascii_password_is_still_rejected(accented, settings):
    with pytest.raises(PdfUserError, match="Incorrect password"):
        E.pdf_info(accented, settings, "cafe")
    with pytest.raises(PdfUserError, match="Incorrect password"):
        E.pdf_info(accented, settings, "cafè")


def test_password_outside_pdfdoc_encoding_is_rejected_cleanly(make_pdf, tmp_path, settings):
    # "密码" can't be expressed in PDFDocEncoding; retrying must not crash.
    rc4 = _encrypt(make_pdf, tmp_path, "rc4.pdf", "user", "owner", 4, False)
    with pytest.raises(PdfUserError, match="Incorrect password"):
        E.pdf_info(rc4, settings, "密码")


def test_aes256_unicode_password_beyond_latin1(make_pdf, tmp_path, settings):
    aes = _encrypt(make_pdf, tmp_path, "aes.pdf", "密码", "owner")
    assert E.pdf_info(aes, settings, "密码")["pages"] == 2


# --- copy permission -------------------------------------------------------------------


NO_COPY = pikepdf.Permissions(extract=False)


@pytest.fixture
def no_copy(make_pdf, tmp_path):
    return _encrypt(make_pdf, tmp_path, "nocopy.pdf", "user", "owner", allow=NO_COPY)


@pytest.fixture
def copy_ok(make_pdf, tmp_path):
    return _encrypt(make_pdf, tmp_path, "copyok.pdf", "user", "owner")


def test_extract_text_needs_owner_password_when_copying_is_forbidden(no_copy, settings):
    with pytest.raises(PdfUserError, match="does not allow copying.*owner password"):
        E.extract_text(no_copy, settings, password="user")
    assert "PAGE-1" in E.extract_text(no_copy, settings, password="owner")["text"]


def test_extract_text_with_user_password_when_copying_is_allowed(copy_ok, settings):
    assert "PAGE-1" in E.extract_text(copy_ok, settings, password="user")["text"]


def test_decrypt_needs_owner_password_when_copying_is_forbidden(no_copy, tmp_path, settings):
    with pytest.raises(PdfUserError, match="does not allow copying.*owner password"):
        E.decrypt_pdf(no_copy, tmp_path / "plain.pdf", "user", settings)
    assert not (tmp_path / "plain.pdf").exists()
    E.decrypt_pdf(no_copy, tmp_path / "plain.pdf", "owner", settings)


def test_decrypt_with_user_password_when_copying_is_allowed(copy_ok, tmp_path, settings):
    E.decrypt_pdf(copy_ok, tmp_path / "plain.pdf", "user", settings)
    with pikepdf.open(tmp_path / "plain.pdf") as pdf:
        assert not pdf.is_encrypted


def test_reencrypting_needs_owner_password_when_copying_is_forbidden(no_copy, tmp_path,
                                                                     settings):
    with pytest.raises(PdfUserError, match="does not allow copying.*owner password"):
        E.encrypt_pdf(no_copy, tmp_path / "re.pdf", "new", settings, current_password="user")
    assert not (tmp_path / "re.pdf").exists()
    E.encrypt_pdf(no_copy, tmp_path / "re.pdf", "new", settings, current_password="owner")


def test_owner_only_copy_restricted_file_needs_owner_password(make_pdf, tmp_path, settings):
    # Opens with no password at all, but still forbids copying.
    restricted = _encrypt(make_pdf, tmp_path, "oo.pdf", "", "boss", allow=NO_COPY)
    with pytest.raises(PdfUserError, match="does not allow copying"):
        E.extract_text(restricted, settings)
    with pytest.raises(PdfUserError, match="does not allow copying"):
        E.decrypt_pdf(restricted, tmp_path / "plain.pdf", "", settings)
    assert "PAGE-1" in E.extract_text(restricted, settings, password="boss")["text"]


def test_unencrypted_files_are_unaffected(doc, settings):
    assert "PAGE-1" in E.extract_text(doc, settings)["text"]


@pytest.mark.parametrize("tool", ["rotate", "optimize"])
def test_tools_that_keep_protection_still_work_with_user_password(no_copy, tmp_path, settings,
                                                                  tool):
    # Their output stays copy-restricted, so nothing is copied out.
    out = tmp_path / "out.pdf"
    if tool == "rotate":
        E.rotate_pages(no_copy, out, 90, settings, password="user")
    else:
        E.optimize_pdf(no_copy, out, settings, password="user")
    with pikepdf.open(out, password="user") as pdf:
        assert pdf.allow.extract is False


# --- review findings: passwords where latin-1, UTF-8 and PDFDocEncoding disagree ---------


@pytest.mark.parametrize(("revision", "aes"), [(2, False), (4, False), (4, True)],
                         ids=["RC4-40", "RC4-128", "AES-128"])
def test_euro_and_curly_quotes_work_in_every_tool(make_pdf, tmp_path, settings, revision,
                                                  aes):
    # "€" and "“" are where latin-1 and PDFDocEncoding differ.
    locked = _encrypt(make_pdf, tmp_path, "euro.pdf", "p€ss“", "own€r", revision, aes)
    assert "PAGE-1" in E.extract_text(locked, settings, password="p€ss“")["text"]
    assert E.view_pages(locked, settings, password="p€ss“")[0]["pages"][0]["page"] == 1
    assert len(E.render_pages(locked, tmp_path / "imgs", settings, dpi=72,
                              password="own€r")["files"]) == 2


def test_aes256_does_not_accept_a_mis_encoded_alias(make_pdf, tmp_path, settings):
    # "Ã©" in PDFDocEncoding is the UTF-8 bytes of "é"; only "é" may open the file.
    aes = _encrypt(make_pdf, tmp_path, "aes.pdf", "é", "owner")
    assert E.pdf_info(aes, settings, "é")["pages"] == 2
    with pytest.raises(PdfUserError, match="Incorrect password"):
        E.pdf_info(aes, settings, "Ã©")


def test_extract_text_reports_wrong_passwords_cleanly(make_pdf, tmp_path, settings):
    # A soft hyphen is latin-1 but not PDFDocEncoding: pypdf would have accepted it.
    rc4 = _encrypt(make_pdf, tmp_path, "rc4.pdf", "pass", "owner", 4, False)
    for wrong in ("pa\u00adss", "nope"):
        with pytest.raises(PdfUserError, match="Incorrect password"):
            E.extract_text(rc4, settings, password=wrong)
