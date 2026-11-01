"""Every tool that changes a PDF respects its permissions and its signatures.

A PDF opened without its owner password keeps the restrictions its author set:
document assembly (adding, removing, reordering or rotating pages), changing the
document, and changing annotations. Signed PDFs are refused by tools that would
rewrite the signed document, and tools that would lift the restrictions altogether
(decrypting, re-encrypting, PDF/A) need the owner password.
"""
from __future__ import annotations

import pikepdf
import pytest
from pikepdf import Dictionary, Name

from paperboy import engine as E
from paperboy.engine.base import has_annotations, require_no_restrictions
from paperboy.errors import PdfUserError

from conftest import HAS_QPDF, page_labels

OPEN = pikepdf.Permissions(modify_assembly=True)  # pikepdf's default forbids assembly
NO_ASSEMBLY = OPEN._replace(modify_assembly=False)
NO_CHANGES = OPEN._replace(modify_other=False)
NO_ANNOTATIONS = OPEN._replace(modify_annotation=False)


def _lock(src, out, allow, user="user", owner="owner"):
    with pikepdf.open(src) as pdf:
        pdf.save(out, encryption=pikepdf.Encryption(user=user, owner=owner, R=6, allow=allow))
    return out


@pytest.fixture
def lock(make_pdf, tmp_path):
    def factory(allow, name="locked.pdf", pages=3, user="user", src=None):
        return _lock(src or make_pdf(f"plain-{name}", pages), tmp_path / name, allow, user)
    return factory


def _edit(path, change):
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        change(pdf)
        pdf.save(path)
    return path


def _note(pdf, page=0, rect=(100, 100, 200, 140)):
    """A square comment with an appearance on *page*."""
    ap = pdf.make_stream(b"0 0 1 rg 0 0 100 40 re f", Type=Name.XObject, Subtype=Name.Form,
                         BBox=[0, 0, 100, 40])
    note = pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Square,
                                        Rect=list(rect), F=4, AP=Dictionary(N=ap)))
    pdf.pages[page].obj.Annots = pikepdf.Array([note])


def _signed(pdf):
    pdf.Root.Perms = Dictionary(DocMDP=pdf.make_indirect(Dictionary(
        Type=Name.Sig, ByteRange=[0, 1, 2, 3])))


def _usage_rights(pdf):
    pdf.Root.Perms = Dictionary(UR3=pdf.make_indirect(Dictionary(
        Type=Name.Sig, ByteRange=[0, 1, 2, 3])))


# --- document assembly -------------------------------------------------------------------

ASSEMBLY = {
    "rearrange_pages": lambda src, out, s, pw: E.rearrange_pages(src, out, "2,1", s,
                                                                 password=pw),
    "delete_pages": lambda src, out, s, pw: E.delete_pages(src, out, "1", s, pw),
    "extract_pages": lambda src, out, s, pw: E.extract_pages(src, out, "1-2", s, pw),
    "rotate_pages": lambda src, out, s, pw: E.rotate_pages(src, out, 90, s, password=pw),
    "insert_blank_pages": lambda src, out, s, pw: E.insert_blank_pages(src, out, s, [1],
                                                                       password=pw),
    "n_up": lambda src, out, s, pw: E.n_up(src, out, s, password=pw),
    "split_pdf": lambda src, out, s, pw: E.split_pdf(src, out.with_suffix(""), s,
                                                     password=pw),
}


@pytest.mark.parametrize("tool", sorted(ASSEMBLY))
def test_assembly_needs_the_owner_password_when_forbidden(tool, lock, tmp_path, settings):
    src = lock(NO_ASSEMBLY)
    out = tmp_path / "out.pdf"
    with pytest.raises(PdfUserError, match="does not allow .*owner password"):
        ASSEMBLY[tool](src, out, settings, "user")
    assert not out.exists() and not out.with_suffix("").exists()  # nothing written
    res = ASSEMBLY[tool](src, out, settings, "owner")
    assert res["password_protected"] is True  # still protected, restrictions and all


@pytest.mark.parametrize("tool", sorted(ASSEMBLY))
def test_assembly_works_with_the_open_password_when_allowed(tool, lock, tmp_path, settings):
    ASSEMBLY[tool](lock(OPEN), tmp_path / "out.pdf", settings, "user")


def test_a_file_that_opens_without_a_password_still_keeps_its_restrictions(lock, tmp_path,
                                                                           settings):
    src = lock(NO_ASSEMBLY, user="")
    with pytest.raises(PdfUserError, match="does not allow deleting pages"):
        E.delete_pages(src, tmp_path / "out.pdf", "1", settings)
    E.delete_pages(src, tmp_path / "out.pdf", "1", settings, "owner")
    # AES-256 hides the (empty) open password from the owner, so the owner's opens it.
    assert page_labels(tmp_path / "out.pdf", "owner") == ["PAGE-2", "PAGE-3"]


@pytest.mark.parametrize("restricted", ["first", "second"])
def test_merge_checks_every_input(restricted, lock, make_pdf, tmp_path, settings):
    plain = make_pdf("plain.pdf", 1)
    locked = lock(NO_ASSEMBLY)
    inputs = [locked, plain] if restricted == "first" else [plain, locked]
    with pytest.raises(PdfUserError, match=r"does not allow merging its pages \(locked.pdf\)"):
        E.merge_pdfs(inputs, tmp_path / "m.pdf", settings, {str(locked): "user"})
    assert not (tmp_path / "m.pdf").exists()
    E.merge_pdfs(inputs, tmp_path / "m.pdf", settings, {str(locked): "owner"})


@pytest.mark.parametrize("side", ["fronts", "backs"])
def test_interleave_checks_both_scans(side, lock, make_pdf, tmp_path, settings):
    plain = make_pdf("plain.pdf", 3)
    locked = lock(NO_ASSEMBLY)
    fronts, backs = (locked, plain) if side == "fronts" else (plain, locked)
    pw = {"front_password" if side == "fronts" else "back_password": "user"}
    with pytest.raises(PdfUserError, match=f"combining its pages \\(the {side}\\)"):
        E.interleave_pdfs(fronts, backs, tmp_path / "i.pdf", settings, **pw)
    pw = {k: "owner" for k in pw}
    assert E.interleave_pdfs(fronts, backs, tmp_path / "i.pdf", settings, **pw)["pages"] == 6


def test_n_up_also_needs_permission_to_change_the_pages(lock, tmp_path, settings):
    with pytest.raises(PdfUserError, match="does not allow changing its pages"):
        E.n_up(lock(NO_CHANGES), tmp_path / "n.pdf", settings, password="user")


# --- changing the document ---------------------------------------------------------------


def test_cropping_needs_permission_to_change_the_pages(lock, tmp_path, settings):
    src = lock(NO_CHANGES)
    with pytest.raises(PdfUserError, match="does not allow cropping its pages"):
        E.crop_pages(src, tmp_path / "c.pdf", settings, margins=[10, 10, 10, 10],
                     password="user")
    E.crop_pages(src, tmp_path / "c.pdf", settings, margins=[10, 10, 10, 10],
                 password="owner")
    E.crop_pages(lock(OPEN, "open.pdf"), tmp_path / "c.pdf", settings,
                 margins=[10, 10, 10, 10], password="user")


def test_metadata_needs_permission_to_change_the_document(lock, tmp_path, settings):
    src = lock(NO_CHANGES)
    with pytest.raises(PdfUserError, match="does not allow changing its document information"):
        E.set_metadata(src, tmp_path / "m.pdf", {"title": "T"}, settings, "user")
    assert not (tmp_path / "m.pdf").exists()
    E.set_metadata(src, tmp_path / "m.pdf", {"title": "T"}, settings, "owner")
    E.set_metadata(lock(OPEN, "open.pdf"), tmp_path / "m.pdf", {"title": "T"}, settings, "user")


def test_rotating_is_assembly_not_a_change_to_the_pages(lock, tmp_path, settings):
    # The PDF spec lists rotating pages under assembly, which is allowed here.
    E.rotate_pages(lock(NO_CHANGES), tmp_path / "r.pdf", 90, settings, password="user")


# --- annotations -------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_QPDF, reason="qpdf CLI not installed")
@pytest.mark.parametrize("allow,message", [
    (NO_ANNOTATIONS, "does not allow flattening"),
    (NO_CHANGES, "does not allow flattening"),
])
def test_flattening_needs_permission_to_change_annotations_and_pages(allow, message, lock,
                                                                     tmp_path, settings):
    src = lock(allow)
    with pytest.raises(PdfUserError, match=message):
        E.flatten_pdf(src, tmp_path / "f.pdf", settings, "user")
    assert not (tmp_path / "f.pdf").exists()
    E.flatten_pdf(src, tmp_path / "f.pdf", settings, "owner")


@pytest.mark.skipif(not HAS_QPDF, reason="qpdf CLI not installed")
def test_flattening_works_with_the_open_password_when_allowed(lock, tmp_path, settings):
    E.flatten_pdf(lock(OPEN), tmp_path / "f.pdf", settings, "user")


def _form(path):
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path))
    c.acroForm.textfield(name="name", value="", x=72, y=700, width=200, height=20)
    c.showPage()
    c.save()
    return path


@pytest.mark.parametrize("allow", [NO_ANNOTATIONS._replace(modify_form=True), NO_CHANGES])
def test_filling_and_flattening_needs_both_permissions(allow, lock, tmp_path, settings):
    src = lock(allow, src=_form(tmp_path / "form.pdf"))
    E.fill_form(src, tmp_path / "filled.pdf", {"name": "Ada"}, settings, "user")  # filling: fine
    with pytest.raises(PdfUserError, match="does not allow flattening its form"):
        E.fill_form(src, tmp_path / "flat.pdf", {"name": "Ada"}, settings, "user",
                    flatten=True)
    E.fill_form(src, tmp_path / "flat.pdf", {"name": "Ada"}, settings, "owner", flatten=True)


def test_resizing_pages_with_annotations_needs_permission_to_move_them(make_pdf, lock,
                                                                       tmp_path, settings):
    noted = _edit(make_pdf("noted.pdf", 2), _note)
    src = lock(NO_ANNOTATIONS, src=noted)
    with pytest.raises(PdfUserError, match="does not allow moving its comments"):
        E.resize_pages(src, tmp_path / "r.pdf", settings, size="a4", password="user")
    # Page 2 has none, so resizing only it changes no annotation.
    E.resize_pages(src, tmp_path / "r.pdf", settings, size="a4", pages="2", password="user")
    E.resize_pages(src, tmp_path / "r.pdf", settings, size="a4", password="owner")


def test_redacting_where_an_annotation_is_needs_permission_to_remove_it(make_pdf, lock,
                                                                        tmp_path, settings):
    src = lock(NO_ANNOTATIONS, src=_edit(make_pdf("noted.pdf", 1), _note))
    with pytest.raises(PdfUserError, match="does not allow removing the comments"):
        E.redact_pdf(src, tmp_path / "r.pdf", settings,
                     areas=[{"page": 1, "rect": [90, 90, 210, 150]}], password="user")
    assert not (tmp_path / "r.pdf").exists()
    # Elsewhere on the page, no annotation is touched.
    res = E.redact_pdf(src, tmp_path / "r.pdf", settings,
                       areas=[{"page": 1, "rect": [300, 500, 400, 600]}], password="user")
    assert res["pages"][0]["annotations_removed"] == 0
    res = E.redact_pdf(src, tmp_path / "r.pdf", settings,
                       areas=[{"page": 1, "rect": [90, 90, 210, 150]}], password="owner")
    assert res["pages"][0]["annotations_removed"] == 1


def test_sanitizing_annotations_needs_permission_to_change_them(make_pdf, lock, tmp_path,
                                                                settings):
    src = lock(NO_ANNOTATIONS, src=_edit(make_pdf("noted.pdf", 1), _note))
    with pytest.raises(PdfUserError, match="does not allow cleaning its comments"):
        E.sanitize_pdf(src, tmp_path / "s.pdf", settings, password="user")
    # With every option that touches annotations off, there is nothing to ask.
    E.sanitize_pdf(src, tmp_path / "s.pdf", settings, remove_metadata=False,
                   remove_scripts=False, remove_attachments=False, password="user")
    E.sanitize_pdf(src, tmp_path / "s.pdf", settings, password="owner")
    # Without annotations, the permission doesn't matter.
    E.sanitize_pdf(lock(NO_ANNOTATIONS, "bare.pdf"), tmp_path / "s.pdf", settings,
                   password="user")


def test_has_annotations(make_pdf):
    with pikepdf.open(_edit(make_pdf("noted.pdf", 2), _note)) as pdf:
        assert has_annotations(pdf)
        assert has_annotations(pdf, [0]) and not has_annotations(pdf, [1])
        pdf.pages[0].obj.Annots = pikepdf.Array()
        assert not has_annotations(pdf)


# --- signed PDFs -------------------------------------------------------------------------

IN_PLACE = {
    "rotate_pages": lambda src, out, s: E.rotate_pages(src, out, 90, s),
    "crop_pages": lambda src, out, s: E.crop_pages(src, out, s, margins=[5, 5, 5, 5]),
    "resize_pages": lambda src, out, s: E.resize_pages(src, out, s, size="a4"),
    "insert_blank_pages": lambda src, out, s: E.insert_blank_pages(src, out, s, [1]),
    "set_metadata": lambda src, out, s: E.set_metadata(src, out, {"title": "T"}, s),
    "add_watermark": lambda src, out, s: E.add_watermark(src, out, s, text="DRAFT"),
    "add_page_numbers": lambda src, out, s: E.add_page_numbers(src, out, s),
}
if HAS_QPDF:
    IN_PLACE["flatten_pdf"] = lambda src, out, s: E.flatten_pdf(src, out, s)


@pytest.mark.parametrize("tool", sorted(IN_PLACE))
def test_tools_that_change_a_signed_pdf_refuse_it(tool, make_pdf, tmp_path, settings):
    src = _edit(make_pdf("signed.pdf", 2), _signed)
    with pytest.raises(PdfUserError, match="digitally signed.*invalidate the signature"):
        IN_PLACE[tool](src, tmp_path / "out.pdf", settings)
    assert not (tmp_path / "out.pdf").exists()


@pytest.mark.parametrize("tool", sorted(IN_PLACE))
def test_tools_that_change_a_pdf_drop_its_reader_usage_rights(tool, make_pdf, tmp_path,
                                                              settings):
    src = _edit(make_pdf("extended.pdf", 2), _usage_rights)
    IN_PLACE[tool](src, tmp_path / "out.pdf", settings)
    with pikepdf.open(tmp_path / "out.pdf") as pdf:
        assert "/Perms" not in pdf.Root  # Reader would call the changed file damaged


@pytest.mark.parametrize("tool", ["extract_pages", "delete_pages", "rearrange_pages"])
def test_new_documents_made_from_a_signed_pdfs_pages_are_allowed(tool, make_pdf, tmp_path,
                                                                 settings):
    # The signature isn't carried over: the output is a new, unsigned document.
    src = _edit(make_pdf("signed.pdf", 3), _signed)
    ASSEMBLY[tool](src, tmp_path / "out.pdf", settings, None)
    with pikepdf.open(tmp_path / "out.pdf") as pdf:
        assert "/Perms" not in pdf.Root


# --- lifting the restrictions ------------------------------------------------------------


@pytest.mark.parametrize("allow", [NO_ASSEMBLY, NO_CHANGES, NO_ANNOTATIONS,
                                   OPEN._replace(print_lowres=False, print_highres=False)])
def test_removing_the_password_needs_the_owner_password_when_anything_is_restricted(
        allow, lock, tmp_path, settings):
    src = lock(allow)
    with pytest.raises(PdfUserError, match="restricts what can be done.*owner password"):
        E.decrypt_pdf(src, tmp_path / "plain.pdf", "user", settings)
    with pytest.raises(PdfUserError, match="restricts what can be done.*owner password"):
        E.encrypt_pdf(src, tmp_path / "re.pdf", "new", settings, current_password="user")
    assert not (tmp_path / "plain.pdf").exists() and not (tmp_path / "re.pdf").exists()
    E.decrypt_pdf(src, tmp_path / "plain.pdf", "owner", settings)
    E.encrypt_pdf(src, tmp_path / "re.pdf", "new", settings, current_password="owner")


def test_removing_the_password_of_an_unrestricted_file_needs_only_the_open_password(
        lock, tmp_path, settings):
    E.decrypt_pdf(lock(OPEN), tmp_path / "plain.pdf", "user", settings)
    with pikepdf.open(tmp_path / "plain.pdf") as pdf:
        assert not pdf.is_encrypted


def test_the_accessibility_bit_restricts_nothing(make_pdf, tmp_path):
    # PDF 2.0 tells readers to ignore it (qpdf only clears it with RC4-128, R3).
    out = tmp_path / "a.pdf"
    with pikepdf.open(make_pdf("p.pdf", 1)) as pdf:
        pdf.save(out, encryption=pikepdf.Encryption(
            user="user", owner="owner", R=3, aes=False, metadata=False,
            allow=OPEN._replace(accessibility=False)))
    with pikepdf.open(out, password="user") as pdf:
        assert not pdf.allow.accessibility
        require_no_restrictions(pdf, "anything")


def test_unencrypted_files_have_no_restrictions(doc):
    with pikepdf.open(doc) as pdf:
        require_no_restrictions(pdf, "anything")
