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
from paperboy.engine.base import (
    drop_copied_signatures,
    has_annotations,
    require_no_restrictions,
    signature_state,
)
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


def _signed_field(pdf):
    """A signature the way signing software writes it: a signature field whose widget
    is on page 1, its value a signature dictionary."""
    sig = pdf.make_indirect(Dictionary(Type=Name.Sig, Filter=Name("/Adobe.PPKLite"),
                                       ByteRange=[0, 1, 2, 3],
                                       Contents=pikepdf.String(b"\x30\x82PKCS7")))
    field = pdf.make_indirect(Dictionary(FT=Name.Sig, T=pikepdf.String("Signature1"),
                                         V=sig))
    widget = pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Widget,
                                          Rect=[72, 72, 272, 122], F=4, Parent=field))
    field.Kids = pikepdf.Array([widget])
    pdf.pages[0].obj.Annots = pikepdf.Array([widget])
    pdf.Root.AcroForm = Dictionary(Fields=pikepdf.Array([field]), SigFlags=3)


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


def _photo_pdf(path):
    from PIL import Image
    from reportlab.pdfgen import canvas
    img = path.with_suffix(".png")
    Image.new("RGB", (1200, 1200), (200, 30, 30)).save(img)
    c = canvas.Canvas(str(path))
    c.drawImage(str(img), 72, 400, 144, 144)  # 600 dpi
    c.showPage()
    c.save()
    return path


@pytest.mark.parametrize("args", [{"max_image_dpi": 72}, {"target_size_mb": 0.01}])
def test_lowering_picture_resolution_needs_permission_to_change_the_pages(args, lock,
                                                                          tmp_path, settings):
    src = lock(NO_CHANGES, src=_photo_pdf(tmp_path / "photo.pdf"))
    with pytest.raises(PdfUserError, match="does not allow lowering the resolution"):
        E.optimize_pdf(src, tmp_path / "o.pdf", settings, password="user", **args)
    assert not (tmp_path / "o.pdf").exists()
    E.optimize_pdf(src, tmp_path / "o.pdf", settings, password="user")  # lossless: fine
    res = E.optimize_pdf(src, tmp_path / "o.pdf", settings, password="owner", **args)
    if "max_image_dpi" in args:  # (the target is met before any picture needs lowering)
        assert res["images_resampled"] == 1


@pytest.mark.parametrize("revision", [2, 3, 4])
def test_older_encryption_keeps_its_restrictions(revision, make_pdf, tmp_path, settings):
    # R2 has no assembly bit: changing the document (bit 4) covers it.
    out = tmp_path / "old.pdf"
    with pikepdf.open(make_pdf("p.pdf", 2)) as pdf:
        pdf.save(out, encryption=pikepdf.Encryption(
            user="user", owner="owner", R=revision, aes=False, metadata=False,
            allow=pikepdf.Permissions(modify_assembly=False, modify_other=False)))
    with pytest.raises(PdfUserError, match="does not allow deleting pages"):
        E.delete_pages(out, tmp_path / "d.pdf", "1", settings, "user")
    with pytest.raises(PdfUserError, match="does not allow cropping"):
        E.crop_pages(out, tmp_path / "c.pdf", settings, margins=[5, 5, 5, 5], password="user")
    with pytest.raises(PdfUserError, match="restricts what can be done"):
        E.decrypt_pdf(out, tmp_path / "plain.pdf", "user", settings)
    E.delete_pages(out, tmp_path / "d.pdf", "1", settings, "owner")
    E.decrypt_pdf(out, tmp_path / "plain.pdf", "owner", settings)


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


def test_redacting_text_found_in_a_comment_needs_permission_to_change_it(lock, tmp_path,
                                                                        settings):
    from reportlab.pdfgen import canvas
    plain = tmp_path / "secret.pdf"
    c = canvas.Canvas(str(plain))
    c.drawString(72, 720, "code SECRET42")
    c.showPage()
    c.drawString(72, 720, "page two")
    c.showPage()
    c.save()

    def note(pdf):  # on page 2, outside the pages searched; its text isn't drawn
        pdf.pages[1].obj.Annots = pikepdf.Array([pdf.make_indirect(Dictionary(
            Type=Name.Annot, Subtype=Name.Text, Rect=[300, 300, 320, 320],
            Contents=pikepdf.String("reminder: SECRET42")))])
    src = lock(NO_ANNOTATIONS, src=_edit(plain, note))
    with pytest.raises(PdfUserError, match="replacing the text in its comments"):
        E.redact_pdf(src, tmp_path / "r.pdf", settings, texts=["SECRET42"], pages="1",
                     password="user")
    assert not (tmp_path / "r.pdf").exists()
    E.redact_pdf(src, tmp_path / "r.pdf", settings, texts=["page two"], password="user")
    E.redact_pdf(src, tmp_path / "r.pdf", settings, texts=["SECRET42"], password="owner")
    with pikepdf.open(tmp_path / "r.pdf", password="owner") as pdf:
        assert str(pdf.pages[1].obj.Annots[0].Contents) == "reminder: [redacted]"


def _strings_with_secret(pdf):
    """The text to redact, only in annotation and form places that aren't drawn."""
    link = pdf.make_indirect(Dictionary(
        Type=Name.Annot, Subtype=Name.Link, Rect=[10, 10, 20, 20],
        A=Dictionary(S=Name.URI, URI=pikepdf.String("mailto:SECRET42@example.com"))))
    parent = pdf.make_indirect(Dictionary(T=pikepdf.String("SECRET42 group")))
    field = pdf.make_indirect(Dictionary(
        Type=Name.Annot, Subtype=Name.Widget, FT=Name.Tx, Rect=[30, 30, 40, 40],
        Parent=parent, T=pikepdf.String("f"),
        AA=Dictionary(K=Dictionary(S=Name.JavaScript, JS=pikepdf.String("x='SECRET42'")))))
    parent.Kids = pikepdf.Array([field])
    pdf.pages[1].obj.Annots = pikepdf.Array([link, field])
    pdf.Root.AcroForm = Dictionary(Fields=pikepdf.Array([parent]))


@pytest.mark.parametrize("pattern", [False, True])
def test_redacting_text_anywhere_in_annotations_needs_permission(pattern, lock, tmp_path,
                                                                 settings):
    from reportlab.pdfgen import canvas
    plain = tmp_path / "secret.pdf"
    c = canvas.Canvas(str(plain))
    c.drawString(72, 720, "code SECRET42")
    c.showPage()
    c.showPage()
    c.save()
    src = lock(NO_ANNOTATIONS, src=_edit(plain, _strings_with_secret))
    what = {"patterns": ["SECRET\\d+"]} if pattern else {"texts": ["SECRET42"]}
    with pytest.raises(PdfUserError, match="replacing the text in its comments"):
        E.redact_pdf(src, tmp_path / "r.pdf", settings, pages="1", password="user", **what)
    E.redact_pdf(src, tmp_path / "r.pdf", settings, pages="1", password="owner", **what)


def _two_pages_with_secret(tmp_path, edit):
    from reportlab.pdfgen import canvas
    plain = tmp_path / "secret.pdf"
    c = canvas.Canvas(str(plain))
    c.drawString(72, 720, "code SECRET42")
    c.showPage()
    c.showPage()
    c.save()
    return _edit(plain, edit)


def test_redact_looks_into_the_text_streams_of_annotations(lock, tmp_path, settings):
    def edit(pdf):  # a link whose URI matches, beside a comment with rich text that doesn't
        link = Dictionary(Type=Name.Annot, Subtype=Name.Link, Rect=[10, 10, 20, 20],
                          A=Dictionary(S=Name.URI, URI=pikepdf.String("x/SECRET42")))
        note = Dictionary(Type=Name.Annot, Subtype=Name.FreeText, Rect=[30, 30, 90, 50],
                          RC=pdf.make_stream(b"<body>hello</body>"))
        pdf.pages[1].obj.Annots = pikepdf.Array([pdf.make_indirect(note),
                                                 pdf.make_indirect(link)])
    src = lock(NO_ANNOTATIONS, src=_two_pages_with_secret(tmp_path, edit))
    with pytest.raises(PdfUserError, match="replacing the text in its comments"):
        E.redact_pdf(src, tmp_path / "r.pdf", settings, texts=["SECRET42"], pages="1",
                     password="user")


def test_redact_counts_an_unreadable_script_in_an_annotation_as_a_change(lock, tmp_path,
                                                                        settings):
    def edit(pdf):  # scrubbing removes streams it can't read
        js = pdf.make_stream(b"not flate at all", Filter=Name.FlateDecode)
        pdf.pages[1].obj.Annots = pikepdf.Array([pdf.make_indirect(Dictionary(
            Type=Name.Annot, Subtype=Name.Link, Rect=[10, 10, 20, 20],
            A=Dictionary(S=Name.JavaScript, JS=js)))])
    src = lock(NO_ANNOTATIONS, src=_two_pages_with_secret(tmp_path, edit))
    with pytest.raises(PdfUserError, match="replacing the text in its comments"):
        E.redact_pdf(src, tmp_path / "r.pdf", settings, texts=["SECRET42"], pages="1",
                     password="user")


def _mouse_down_script(pdf):  # /D in /AA is the mouse-down action
    pdf.pages[1].obj.Annots = pikepdf.Array([pdf.make_indirect(Dictionary(
        Type=Name.Annot, Subtype=Name.Widget, FT=Name.Tx, Rect=[10, 10, 90, 30],
        AA=Dictionary(D=Dictionary(S=Name.JavaScript,
                                   JS=pikepdf.String("app.alert('SECRET42')")))))])


def _claims_to_be_a_page(pdf):  # an annotation can't hide behind /Type /Page
    pdf.pages[1].obj.Annots = pikepdf.Array([pdf.make_indirect(Dictionary(
        Type=Name.Page, Subtype=Name.Link, Rect=[10, 10, 20, 20],
        A=Dictionary(S=Name.URI, URI=pikepdf.String("x/SECRET42"))))])


@pytest.mark.parametrize("edit", [_mouse_down_script, _claims_to_be_a_page],
                         ids=["mouse-down", "typed-page"])
def test_redact_finds_text_in_every_part_of_an_annotation(edit, lock, tmp_path, settings):
    src = lock(NO_ANNOTATIONS, src=_two_pages_with_secret(tmp_path, edit))
    with pytest.raises(PdfUserError, match="replacing the text in its comments"):
        E.redact_pdf(src, tmp_path / "r.pdf", settings, texts=["SECRET42"], pages="1",
                     password="user")


def test_a_layer_named_like_the_text_is_not_an_annotation_change(lock, tmp_path, settings):
    def edit(pdf):
        layer = pdf.make_indirect(Dictionary(Type=Name.OCG, Name=pikepdf.String("SECRET42")))
        pdf.pages[1].obj.Annots = pikepdf.Array([pdf.make_indirect(Dictionary(
            Type=Name.Annot, Subtype=Name.Square, Rect=[10, 10, 20, 20], OC=layer))])
    src = lock(NO_ANNOTATIONS, src=_two_pages_with_secret(tmp_path, edit))
    E.redact_pdf(src, tmp_path / "r.pdf", settings, texts=["SECRET42"], pages="1",
                 password="user")


def test_redact_refuses_when_annotations_are_too_many_to_check(lock, tmp_path, settings,
                                                               monkeypatch):
    from paperboy.engine import redact
    monkeypatch.setattr(redact, "MAX_ANNOTATION_OBJECTS", 3)
    def edit(pdf):
        pdf.pages[1].obj.Annots = pikepdf.Array([pdf.make_indirect(Dictionary(
            Type=Name.Annot, Subtype=Name.Square, Rect=[n, 0, n + 5, 5])) for n in range(5)])
    src = lock(NO_ANNOTATIONS, src=_two_pages_with_secret(tmp_path, edit))
    with pytest.raises(PdfUserError, match="too large to check"):
        E.redact_pdf(src, tmp_path / "r.pdf", settings, texts=["SECRET42"], pages="1",
                     password="user")
    assert not (tmp_path / "r.pdf").exists()


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
    "optimize_pdf": lambda src, out, s: E.optimize_pdf(src, out, s),
    "repair_pdf": lambda src, out, s: E.repair_pdf(src, out, s),
    "encrypt_pdf": lambda src, out, s: E.encrypt_pdf(src, out, "new", s),
}
if HAS_QPDF:
    IN_PLACE["flatten_pdf"] = lambda src, out, s: E.flatten_pdf(src, out, s)


@pytest.mark.parametrize("sign", [_signed, _signed_field], ids=["certified", "field"])
@pytest.mark.parametrize("tool", sorted(IN_PLACE))
def test_tools_that_change_a_signed_pdf_refuse_it(tool, sign, make_pdf, tmp_path, settings):
    src = _edit(make_pdf("signed.pdf", 2), sign)
    with pytest.raises(PdfUserError, match="digitally signed.*invalidate the signature"):
        IN_PLACE[tool](src, tmp_path / "out.pdf", settings)
    assert not (tmp_path / "out.pdf").exists()


@pytest.mark.parametrize("tool", sorted(IN_PLACE))
def test_tools_that_change_a_pdf_drop_its_reader_usage_rights(tool, make_pdf, tmp_path,
                                                              settings):
    src = _edit(make_pdf("extended.pdf", 2), _usage_rights)
    IN_PLACE[tool](src, tmp_path / "out.pdf", settings)
    with pikepdf.open(tmp_path / "out.pdf", password="new") as pdf:  # (encrypt_pdf's)
        assert "/Perms" not in pdf.Root  # Reader would call the changed file damaged


def test_removing_the_password_of_a_signed_pdf_is_refused(make_pdf, tmp_path, settings):
    signed = _edit(make_pdf("signed.pdf", 1), _signed_field)
    src = _lock(signed, tmp_path / "locked.pdf", OPEN)
    with pytest.raises(PdfUserError, match="digitally signed"):
        E.decrypt_pdf(src, tmp_path / "plain.pdf", "user", settings)
    extended = _lock(_edit(make_pdf("ur.pdf", 1), _usage_rights), tmp_path / "ur-l.pdf", OPEN)
    E.decrypt_pdf(extended, tmp_path / "plain.pdf", "user", settings)
    with pikepdf.open(tmp_path / "plain.pdf") as pdf:
        assert "/Perms" not in pdf.Root


NEW_DOCUMENT = {
    **{tool: ASSEMBLY[tool] for tool in ("extract_pages", "delete_pages", "rearrange_pages")},
    "merge_pdfs": lambda src, out, s, pw: E.merge_pdfs([src, src], out, s),
    "interleave_pdfs": lambda src, out, s, pw: E.interleave_pdfs(src, src, out, s),
    "split_pdf": lambda src, out, s, pw: E.split_pdf(src, out.with_suffix(""), s,
                                                     ranges="1-2"),
}


@pytest.mark.parametrize("tool", sorted(NEW_DOCUMENT))
def test_new_documents_made_from_a_signed_pdfs_pages_are_not_signed(tool, make_pdf, tmp_path,
                                                                    settings):
    # The signature is left behind: the signer's data would only show as broken.
    src = _edit(make_pdf("signed.pdf", 3), _signed_field)
    out = tmp_path / "out.pdf"
    NEW_DOCUMENT[tool](src, out, settings, None)
    out = out if out.exists() else next(out.with_suffix("").iterdir())
    with pikepdf.open(out) as pdf:
        assert signature_state(pdf) == (False, False)
        assert all(b"PKCS7" not in bytes(o.get("/Contents", b"")) for o in pdf.objects
                   if isinstance(o, Dictionary))
    with pikepdf.open(src) as pdf:  # the input stays signed
        assert signature_state(pdf)[0]


def _sig_value(pdf):
    return pdf.make_indirect(Dictionary(Type=Name.Sig, ByteRange=[0, 1, 2, 3],
                                        Contents=pikepdf.String(b"\x30\x82PKCS7")))


def _inherited_type(pdf):
    """/FT on a parent field, /V on its kid (both are inherited, so this is allowed)."""
    parent = pdf.make_indirect(Dictionary(FT=Name.Sig, T=pikepdf.String("sigs")))
    widget = pdf.make_indirect(Dictionary(
        Type=Name.Annot, Subtype=Name.Widget, Rect=[72, 72, 272, 122], F=4, Parent=parent,
        T=pikepdf.String("one"), V=_sig_value(pdf),
        AP=Dictionary(N=pdf.make_stream(b"BT ET", Type=Name.XObject, Subtype=Name.Form,
                                        BBox=[0, 0, 200, 50]))))
    parent.Kids = pikepdf.Array([widget])
    pdf.pages[0].obj.Annots = pikepdf.Array([widget])
    pdf.Root.AcroForm = Dictionary(Fields=pikepdf.Array([parent]), SigFlags=3)


def _signed_sibling(pdf):
    """A text field on page 1 whose parent also holds a signed field shown on page 2."""
    form = pdf.make_indirect(Dictionary(T=pikepdf.String("form")))
    text = pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Widget, FT=Name.Tx,
                                        T=pikepdf.String("name"), Rect=[72, 600, 272, 620],
                                        Parent=form))
    sig = pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Widget, FT=Name.Sig,
                                       T=pikepdf.String("sig"), Rect=[72, 72, 272, 122],
                                       Parent=form, V=_sig_value(pdf)))
    form.Kids = pikepdf.Array([text, sig])
    pdf.pages[0].obj.Annots = pikepdf.Array([text])
    pdf.pages[1].obj.Annots = pikepdf.Array([sig])
    pdf.Root.AcroForm = Dictionary(Fields=pikepdf.Array([form]), SigFlags=3)


@pytest.mark.parametrize("sign", [_signed_field, _inherited_type, _signed_sibling],
                         ids=["field", "inherited-type", "sibling"])
def test_no_signature_travels_with_extracted_pages(sign, make_pdf, tmp_path, settings):
    src = _edit(make_pdf("signed.pdf", 2), sign)
    E.extract_pages(src, tmp_path / "out.pdf", "1", settings)
    with pikepdf.open(tmp_path / "out.pdf") as pdf:
        assert signature_state(pdf) == (False, False)
        assert not any("/ByteRange" in o for o in pdf.objects if isinstance(o, Dictionary))
        for annot in pdf.pages[0].obj.get("/Annots", []):
            if annot.get("/FT") == Name.Sig or annot.get("/Parent", {}).get("/FT") == Name.Sig:
                assert "/AP" not in annot  # no "Digitally signed by ..." left showing


def test_every_reference_to_a_signature_is_removed():
    with pikepdf.new() as pdf:
        pdf.add_blank_page()
        sig = _sig_value(pdf)
        field = pdf.make_indirect(Dictionary(
            Type=Name.Annot, Subtype=Name.Widget, FT=Name.Sig, Rect=[0, 0, 9, 9], V=sig,
            Lock=Dictionary(Action=Name.All), SV=Dictionary(Ff=0)))
        stray = pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Square,
                                             Rect=[0, 0, 9, 9], Keep=sig,
                                             List=pikepdf.Array([1, sig, 2])))
        loose = pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Widget,
                                             V=pikepdf.String("x"), Parent=5))
        pdf.pages[0].obj.Annots = pikepdf.Array([field, stray, loose])
        drop_copied_signatures(pdf)
        assert not {"/V", "/Lock", "/SV"} & set(field.keys())
        assert "/Keep" not in stray and list(stray.List) == [1, 2]
        assert str(loose.V) == "x"  # not a signature field: its value stays


def test_too_many_objects_to_check_for_signatures_is_refused(make_pdf, tmp_path, settings,
                                                             monkeypatch):
    from paperboy.engine import base
    monkeypatch.setattr(base, "MAX_SIGNATURE_SCAN", 5)
    with pytest.raises(PdfUserError, match="too many objects"):
        E.extract_pages(_edit(make_pdf("signed.pdf", 2), _signed_field), tmp_path / "o.pdf",
                        "1", settings)
    assert not (tmp_path / "o.pdf").exists()


def test_a_field_type_is_inherited_from_at_most_32_parents():
    with pikepdf.new() as pdf:
        pdf.add_blank_page()
        top = parent = pdf.make_indirect(Dictionary(FT=Name.Sig))
        for _ in range(40):
            parent = pdf.make_indirect(Dictionary(Parent=parent))
        widget = pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Widget,
                                              Rect=[0, 0, 9, 9], Parent=parent,
                                              V=pikepdf.String("x")))
        pdf.pages[0].obj.Annots = pikepdf.Array([widget])
        drop_copied_signatures(pdf)
        assert "/V" in widget and "/FT" in top  # too far up to count as its type


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
