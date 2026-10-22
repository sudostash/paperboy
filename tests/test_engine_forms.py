"""Form fields: list_form_fields (structure and values) and fill_form."""
from __future__ import annotations

import dataclasses

import pikepdf
import pytest
from reportlab.pdfgen import canvas

from paperboy import engine as E
from paperboy.errors import PdfUserError

from conftest import page_labels


def _form(path, *, max_len=None):
    """Text, checkbox, radio group, combo box and list box on page 1; a notes field on 2."""
    c = canvas.Canvas(str(path))
    f = c.acroForm
    f.textfield(name="name", value="", x=72, y=700, width=200, height=20,
                maxlen=max_len or 100)
    f.checkbox(name="agree", x=72, y=660, buttonStyle="check", checked=False)
    f.radio(name="size", value="small", x=72, y=620, selected=False)
    f.radio(name="size", value="large", x=120, y=620, selected=False)
    f.choice(name="colour", value="red", options=["red", "green", "blue"], x=72, y=580,
             width=120, height=20)
    f.listbox(name="fruit", value="apple", options=["apple", "pear"], x=72, y=500,
              width=120, height=60)
    c.showPage()
    f.textfield(name="notes", value="first draft", x=72, y=700, width=300, height=20)
    c.showPage()
    c.save()
    return path


def _field(pdf, name):
    return next(f for f in pdf.Root.AcroForm.Fields if str(f.get("/T")) == name)


def _edit(path, change):
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        change(pdf)
        pdf.save(path)
    return path


@pytest.fixture
def form(tmp_path):
    return _form(tmp_path / "form.pdf")


def _by_name(result):
    return {f["name"]: f for f in result["fields"]}


# --- list_form_fields ------------------------------------------------------------------


def test_lists_every_field_with_its_type_value_and_options(form, settings):
    res = E.list_form_fields(form, settings)
    assert res["path"] == str(form)
    assert res["has_form"] is True and res["values_hidden"] is False
    fields = _by_name(res)
    assert list(fields) == ["name", "agree", "size", "colour", "fruit", "notes"]
    assert res["count"] == 6
    assert fields["name"] == {
        "name": "name", "type": "text", "value": "", "options": [], "option_labels": [],
        "required": False, "read_only": False, "max_length": 100, "page": 1}
    assert fields["agree"]["type"] == "checkbox"
    assert fields["agree"]["value"] == "Off" and fields["agree"]["options"] == ["Yes"]
    assert fields["size"]["type"] == "radio"
    assert fields["size"]["value"] is None and fields["size"]["options"] == ["small", "large"]
    assert fields["colour"]["type"] == "choice"
    assert fields["colour"]["options"] == ["red", "green", "blue"]
    assert fields["colour"]["value"] == "red"
    assert fields["fruit"]["type"] == "choice" and fields["fruit"]["options"] == ["apple", "pear"]
    assert fields["notes"]["page"] == 2 and fields["notes"]["value"] == "first draft"


def test_flags_required_and_read_only(form, settings):
    def flag(pdf):
        _field(pdf, "name").Ff = 2       # required
        _field(pdf, "notes").Ff = 1      # read-only
    fields = _by_name(E.list_form_fields(_edit(form, flag), settings))
    assert fields["name"]["required"] is True and fields["name"]["read_only"] is False
    assert fields["notes"]["read_only"] is True


def test_hierarchical_fields_get_dotted_names(tmp_path, settings):
    path = _form(tmp_path / "f.pdf")

    def nest(pdf):
        notes = _field(pdf, "notes")
        parent = pdf.make_indirect(pikepdf.Dictionary(T=pikepdf.String("address"),
                                                      Kids=pikepdf.Array([notes])))
        notes.Parent = parent
        notes.T = pikepdf.String("city")
        pdf.Root.AcroForm.Fields = pikepdf.Array(
            [f for f in pdf.Root.AcroForm.Fields if f.objgen != notes.objgen] + [parent])
    names = [f["name"] for f in E.list_form_fields(_edit(path, nest), settings)["fields"]]
    assert "address.city" in names and "notes" not in names


def test_pdf_without_a_form(doc, settings):
    res = E.list_form_fields(doc, settings)
    assert res == {"path": str(doc), "has_form": False, "values_hidden": False,
                   "fields": [], "count": 0}


def test_values_are_hidden_when_copying_is_forbidden(form, tmp_path, settings):
    locked = tmp_path / "locked.pdf"
    with pikepdf.open(form) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(
            user="u", owner="o", R=6, allow=pikepdf.Permissions(extract=False)))
    hidden = E.list_form_fields(locked, settings, password="u")
    assert hidden["values_hidden"] is True
    assert all(f["value"] is None for f in hidden["fields"])
    assert _by_name(hidden)["colour"]["options"] == ["red", "green", "blue"]  # structure stays
    shown = E.list_form_fields(locked, settings, password="o")
    assert shown["values_hidden"] is False and _by_name(shown)["notes"]["value"] == "first draft"


def test_list_needs_the_password(form, tmp_path, settings, make_encrypted):
    with pytest.raises(PdfUserError, match="supply its password"):
        E.list_form_fields(make_encrypted(), settings)


# --- fill_form ----------------------------------------------------------------------------


VALUES = {"name": "Ada Lovelace", "agree": True, "size": "large", "colour": "blue",
          "fruit": "pear", "notes": "Second draft"}


def test_fills_every_kind_of_field(form, tmp_path, settings):
    out = tmp_path / "filled.pdf"
    res = E.fill_form(form, out, VALUES, settings)
    assert res["output"] == str(out)
    assert res["filled"] == sorted(VALUES)
    assert res["flattened"] is False and res["xfa_removed"] is False
    assert res["password_protected"] is False
    fields = _by_name(E.list_form_fields(out, settings))
    assert {n: fields[n]["value"] for n in VALUES} == {
        "name": "Ada Lovelace", "agree": "Yes", "size": "large", "colour": "blue",
        "fruit": "pear", "notes": "Second draft"}


def test_appearances_are_generated_so_every_viewer_shows_the_values(form, tmp_path, settings):
    out = tmp_path / "filled.pdf"
    E.fill_form(form, out, VALUES, settings)
    with pikepdf.open(out) as pdf:
        assert b"Ada Lovelace" in _field(pdf, "name").AP.N.read_bytes()
        assert "/NeedAppearances" not in pdf.Root.AcroForm or \
            not pdf.Root.AcroForm.NeedAppearances
        assert _field(pdf, "agree").AS == pikepdf.Name.Yes
        states = {str(k.AS) for k in _field(pdf, "size").Kids}
        assert states == {"/large", "/Off"}


def test_checkbox_accepts_booleans_and_state_names(form, tmp_path, settings):
    on = E.fill_form(form, tmp_path / "on.pdf", {"agree": "Yes"}, settings)
    assert _by_name(E.list_form_fields(on["output"], settings))["agree"]["value"] == "Yes"
    off = E.fill_form(on["output"], tmp_path / "off.pdf", {"agree": False}, settings)
    assert _by_name(E.list_form_fields(off["output"], settings))["agree"]["value"] == "Off"


def test_unfilled_fields_keep_their_values(form, tmp_path, settings):
    out = tmp_path / "filled.pdf"
    E.fill_form(form, out, {"name": "Ada"}, settings)
    assert _by_name(E.list_form_fields(out, settings))["notes"]["value"] == "first draft"


@pytest.mark.parametrize(("values", "message"), [
    ({"nmae": "x"}, "Unknown field.*nmae"),
    ({"agree": "maybe"}, "agree.*one of: true, false, Yes"),
    ({"size": "medium"}, "size.*one of: small, large"),
    ({"colour": "purple"}, "colour.*one of: red, green, blue"),
    ({"name": True}, "name.*text"),
    ({"name": "x" * 101}, "name.*at most 100 characters"),
], ids=["unknown", "checkbox", "radio", "choice", "text-type", "max-length"])
def test_invalid_values_are_rejected_before_writing(form, tmp_path, settings, values, message):
    with pytest.raises(PdfUserError, match=message):
        E.fill_form(form, tmp_path / "out.pdf", values, settings)
    assert not (tmp_path / "out.pdf").exists()


def test_editable_combo_box_accepts_other_values(form, tmp_path, settings):
    _edit(form, lambda pdf: setattr(_field(pdf, "colour"), "Ff", (1 << 17) | (1 << 18)))
    out = E.fill_form(form, tmp_path / "out.pdf", {"colour": "teal"}, settings)["output"]
    assert _by_name(E.list_form_fields(out, settings))["colour"]["value"] == "teal"


def test_read_only_fields_are_refused(form, tmp_path, settings):
    _edit(form, lambda pdf: setattr(_field(pdf, "notes"), "Ff", 1))
    with pytest.raises(PdfUserError, match="notes.*read-only"):
        E.fill_form(form, tmp_path / "out.pdf", {"notes": "x"}, settings)


def test_needs_at_least_one_value(form, tmp_path, settings):
    with pytest.raises(PdfUserError, match="at least one field"):
        E.fill_form(form, tmp_path / "out.pdf", {}, settings)


def test_pdf_without_a_form_is_refused(doc, tmp_path, settings):
    with pytest.raises(PdfUserError, match="has no form fields"):
        E.fill_form(doc, tmp_path / "out.pdf", {"x": "y"}, settings)


def test_flatten_burns_values_into_the_page(form, tmp_path, settings):
    out = tmp_path / "flat.pdf"
    res = E.fill_form(form, out, {"name": "Ada Lovelace"}, settings, flatten=True)
    assert res["flattened"] is True
    with pikepdf.open(out) as pdf:
        assert all(not p.obj.get("/Annots") for p in pdf.pages)
        assert not pdf.Root.get("/AcroForm") or not pdf.Root.AcroForm.get("/Fields")
    assert "Ada Lovelace" in page_labels(out)[0]
    assert E.list_form_fields(out, settings)["has_form"] is False


def test_input_is_unchanged(form, tmp_path, settings):
    before = form.read_bytes()
    E.fill_form(form, tmp_path / "out.pdf", VALUES, settings)
    assert form.read_bytes() == before


# --- protection, permissions and signatures ---------------------------------------------------


def _protect(form, tmp_path, allow=None):
    locked = tmp_path / "locked.pdf"
    with pikepdf.open(form) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(
            user="u", owner="o", R=6, allow=allow or pikepdf.Permissions()))
    return locked


def test_protected_form_stays_protected(form, tmp_path, settings):
    out = tmp_path / "out.pdf"
    res = E.fill_form(_protect(form, tmp_path), out, {"name": "Ada"}, settings, password="u")
    assert res["password_protected"] is True
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(out)
    assert _by_name(E.list_form_fields(out, settings, password="u"))["name"]["value"] == "Ada"


def test_form_filling_permission_is_honoured(form, tmp_path, settings):
    locked = _protect(form, tmp_path, pikepdf.Permissions(modify_form=False,
                                                          modify_annotation=False))
    with pytest.raises(PdfUserError, match="does not allow filling in its form.*owner"):
        E.fill_form(locked, tmp_path / "out.pdf", {"name": "Ada"}, settings, password="u")
    E.fill_form(locked, tmp_path / "out.pdf", {"name": "Ada"}, settings, password="o")


def test_signed_documents_are_refused(form, tmp_path, settings):
    def sign(pdf):
        sig = pdf.make_indirect(pikepdf.Dictionary(
            FT=pikepdf.Name.Sig, T=pikepdf.String("signature"),
            V=pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Sig))))
        pdf.Root.AcroForm.Fields.append(sig)
        pdf.Root.AcroForm.SigFlags = 3
    _edit(form, sign)
    assert _by_name(E.list_form_fields(form, settings))["signature"]["type"] == "signature"
    with pytest.raises(PdfUserError, match="digitally signed.*invalidate"):
        E.fill_form(form, tmp_path / "out.pdf", {"name": "Ada"}, settings)


def test_signature_and_push_buttons_cannot_be_filled(form, tmp_path, settings):
    def add(pdf):
        pdf.Root.AcroForm.Fields.append(pdf.make_indirect(pikepdf.Dictionary(
            FT=pikepdf.Name.Sig, T=pikepdf.String("sig"))))      # unsigned
        pdf.Root.AcroForm.Fields.append(pdf.make_indirect(pikepdf.Dictionary(
            FT=pikepdf.Name.Btn, Ff=1 << 16, T=pikepdf.String("submit"))))
    _edit(form, add)
    for name in ("sig", "submit"):
        with pytest.raises(PdfUserError, match=f"{name}.*cannot be filled"):
            E.fill_form(form, tmp_path / "out.pdf", {name: "x"}, settings)


def test_xfa_is_removed_so_viewers_use_the_filled_fields(form, tmp_path, settings):
    _edit(form, lambda pdf: setattr(pdf.Root.AcroForm, "XFA", pikepdf.Array()))
    out = tmp_path / "out.pdf"
    assert E.fill_form(form, out, {"name": "Ada"}, settings)["xfa_removed"] is True
    with pikepdf.open(out) as pdf:
        assert "/XFA" not in pdf.Root.AcroForm


def test_page_cap(form, tmp_path, settings):
    with pytest.raises(PdfUserError, match="page limit"):
        E.fill_form(form, tmp_path / "o.pdf", {"name": "x"},
                    dataclasses.replace(settings, max_pages=1))


# --- malformed and unusual forms ---------------------------------------------------------


def test_field_tree_cycles_are_survived(form, settings):
    def loop(pdf):
        notes = _field(pdf, "notes")
        parent = pdf.make_indirect(pikepdf.Dictionary(T=pikepdf.String("group")))
        parent.Kids = pikepdf.Array([notes, parent])  # the group contains itself
        notes.Parent = parent
        pdf.Root.AcroForm.Fields = pikepdf.Array(
            [f for f in pdf.Root.AcroForm.Fields if f.objgen != notes.objgen] + [parent])
    names = [f["name"] for f in E.list_form_fields(_edit(form, loop), settings)["fields"]]
    assert "group.notes" in names


def test_deeply_nested_field_trees_are_refused(form, settings):
    def deep(pdf):
        node = _field(pdf, "notes")
        for depth in range(40):
            node = pdf.make_indirect(pikepdf.Dictionary(T=pikepdf.String(f"l{depth}"),
                                                        Kids=pikepdf.Array([node])))
        pdf.Root.AcroForm.Fields = pikepdf.Array([node])
    with pytest.raises(PdfUserError, match="too deeply nested"):
        E.list_form_fields(_edit(form, deep), settings)


def test_export_display_option_pairs_use_the_export_value(form, tmp_path, settings):
    def pairs(pdf):
        _field(pdf, "colour").Opt = pikepdf.Array([
            pikepdf.Array([pikepdf.String("r"), pikepdf.String("Red")]),
            pikepdf.Array([pikepdf.String("g"), pikepdf.String("Green")])])
        _field(pdf, "colour").V = pikepdf.String("r")
    _edit(form, pairs)
    assert _by_name(E.list_form_fields(form, settings))["colour"]["options"] == ["r", "g"]
    out = E.fill_form(form, tmp_path / "o.pdf", {"colour": "g"}, settings)["output"]
    assert _by_name(E.list_form_fields(out, settings))["colour"]["value"] == "g"


def test_multi_value_and_unusual_types_are_reported(form, settings):
    def odd(pdf):
        _field(pdf, "fruit").V = pikepdf.Array([pikepdf.String("apple"), pikepdf.String("pear")])
        pdf.Root.AcroForm.Fields.append(pdf.make_indirect(pikepdf.Dictionary(
            T=pikepdf.String("mystery"), FT=pikepdf.Name("/Xx"))))
    fields = _by_name(E.list_form_fields(_edit(form, odd), settings))
    assert fields["fruit"]["value"] == "apple, pear"
    assert fields["mystery"]["type"] == "other" and fields["mystery"]["page"] is None


def test_list_box_free_text_clears_the_selected_index(form, tmp_path, settings):
    _edit(form, lambda pdf: setattr(_field(pdf, "fruit"), "Ff", 1 << 18))  # editable list
    E.fill_form(form, tmp_path / "a.pdf", {"fruit": "pear"}, settings)
    with pikepdf.open(tmp_path / "a.pdf") as pdf:
        assert list(_field(pdf, "fruit").I) == [1]
    E.fill_form(tmp_path / "a.pdf", tmp_path / "b.pdf", {"fruit": "plum"}, settings)
    with pikepdf.open(tmp_path / "b.pdf") as pdf:
        assert "/I" not in _field(pdf, "fruit")


def test_large_forms_list_a_few_names_in_errors(tmp_path, settings):
    c = canvas.Canvas(str(tmp_path / "big.pdf"))
    for n in range(25):
        c.acroForm.textfield(name=f"f{n}", x=72, y=760 - n * 28, width=100, height=20)
    c.showPage()
    c.save()
    with pytest.raises(PdfUserError, match=r"Fields: f0, f1, .*f19, \.\.\.$"):
        E.fill_form(tmp_path / "big.pdf", tmp_path / "o.pdf", {"nope": "x"}, settings)


def test_direct_field_objects_and_buttons_without_appearances(form, tmp_path, settings):
    def odd(pdf):
        # A checkbox stored inline (not an indirect object) with no appearance states.
        pdf.Root.AcroForm.Fields.append(pikepdf.Dictionary(
            T=pikepdf.String("inline"), FT=pikepdf.Name.Btn, V=pikepdf.Name.Off))
    fields = _by_name(E.list_form_fields(_edit(form, odd), settings))
    assert fields["inline"]["type"] == "checkbox" and fields["inline"]["options"] == []
    out = E.fill_form(form, tmp_path / "o.pdf", {"inline": False}, settings)["output"]
    assert _by_name(E.list_form_fields(out, settings))["inline"]["value"] == "Off"


def test_need_appearances_flag_is_never_left_behind(form, tmp_path, settings, monkeypatch):
    # If appearance generation ever left the flag set, viewers would redraw fields
    # themselves; the fill must clear it either way.
    monkeypatch.setattr(pikepdf.Pdf, "generate_appearance_streams", lambda self: None)
    E.fill_form(form, tmp_path / "o.pdf", {"name": "Ada"}, settings)
    with pikepdf.open(tmp_path / "o.pdf") as pdf:
        assert "/NeedAppearances" not in pdf.Root.AcroForm


# --- review findings: what fill_form writes ---------------------------------------------


def _ap(pdf, name):
    return _field(pdf, name).AP.N.read_bytes()


def test_untouched_fields_keep_their_appearance(form, tmp_path, settings):
    # Filling one field must not redraw the others (qpdf's redraw is single-line and
    # can't draw every character, so it would damage good appearances).
    with pikepdf.open(form) as pdf:
        before = _ap(pdf, "notes")
    E.fill_form(form, tmp_path / "o.pdf", {"name": "Ada"}, settings)
    with pikepdf.open(tmp_path / "o.pdf") as pdf:
        assert _ap(pdf, "notes") == before
        assert b"Ada" in _ap(pdf, "name")


def test_values_the_font_cannot_draw_are_reported(form, tmp_path, settings):
    res = E.fill_form(form, tmp_path / "o.pdf", {"name": "Łódź 東京", "notes": "plain"},
                      settings)
    assert res["unrenderable_fields"] == ["name"]
    stored = _by_name(E.list_form_fields(tmp_path / "o.pdf", settings))
    assert stored["name"]["value"] == "Łódź 東京"  # the value itself is kept intact
    with pikepdf.open(tmp_path / "o.pdf") as pdf:  # viewers are asked to redraw
        assert pdf.Root.AcroForm.NeedAppearances is True


def test_flatten_refuses_values_the_font_cannot_draw(form, tmp_path, settings):
    with pytest.raises(PdfUserError, match="Can't flatten.*name"):
        E.fill_form(form, tmp_path / "o.pdf", {"name": "東京"}, settings, flatten=True)
    assert not (tmp_path / "o.pdf").exists()


def test_choice_appearance_shows_the_label_not_the_export_value(form, tmp_path, settings):
    def labels(pdf):
        _field(pdf, "colour").Opt = pikepdf.Array([
            pikepdf.Array([pikepdf.String("US"), pikepdf.String("United States")]),
            pikepdf.Array([pikepdf.String("FR"), pikepdf.String("France")])])
    _edit(form, labels)
    options = _by_name(E.list_form_fields(form, settings))["colour"]["options"]
    assert options == ["US", "FR"]
    assert _by_name(E.list_form_fields(form, settings))["colour"]["option_labels"] == [
        "United States", "France"]
    E.fill_form(form, tmp_path / "o.pdf", {"colour": "FR"}, settings)
    with pikepdf.open(tmp_path / "o.pdf") as pdf:
        assert b"France" in _ap(pdf, "colour")
        assert _field(pdf, "colour").V == "FR"  # the export value is what's stored


def test_radio_group_can_be_cleared_unless_no_toggle_to_off(form, tmp_path, settings):
    on = E.fill_form(form, tmp_path / "on.pdf", {"size": "large"}, settings)["output"]
    _edit(pathlib_path(on), lambda pdf: setattr(_field(pdf, "size"), "Ff", 1 << 15))
    off = E.fill_form(on, tmp_path / "off.pdf", {"size": "Off"}, settings)["output"]
    assert _by_name(E.list_form_fields(off, settings))["size"]["value"] == "Off"
    _edit(pathlib_path(off), lambda pdf: setattr(_field(pdf, "size"), "Ff",
                                                 (1 << 15) | (1 << 14)))
    with pytest.raises(PdfUserError, match="size: use one of: small, large"):
        E.fill_form(off, tmp_path / "x.pdf", {"size": "Off"}, settings)


def test_checkbox_without_an_on_appearance_cannot_be_checked(form, tmp_path, settings):
    _edit(form, lambda pdf: pdf.Root.AcroForm.Fields.append(pikepdf.Dictionary(
        T=pikepdf.String("bare"), FT=pikepdf.Name.Btn, V=pikepdf.Name.Off)))
    with pytest.raises(PdfUserError, match="bare has no 'checked' appearance"):
        E.fill_form(form, tmp_path / "o.pdf", {"bare": True}, settings)
    E.fill_form(form, tmp_path / "o.pdf", {"bare": False}, settings)  # unchecking is fine


# --- review findings: signatures and usage rights ----------------------------------------


def _add_signature_dict(pdf, where):
    sig = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Sig, ByteRange=pikepdf.Array([0, 10, 20, 30])))
    if where == "docmdp":
        pdf.Root.Perms = pikepdf.Dictionary(DocMDP=sig)
    elif where == "widget-only":  # a signed widget on the page, not in /Fields
        widget = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Widget, FT=pikepdf.Name.Sig,
            V=sig, Rect=pikepdf.Array([0, 0, 0, 0])))
        pdf.pages[0].Annots.append(widget)
    return sig


@pytest.mark.parametrize("where", ["docmdp", "widget-only"])
def test_any_signature_in_the_file_is_detected(form, tmp_path, settings, where):
    _edit(form, lambda pdf: _add_signature_dict(pdf, where))
    with pytest.raises(PdfUserError, match="digitally signed"):
        E.fill_form(form, tmp_path / "o.pdf", {"name": "Ada"}, settings)


def test_reader_usage_rights_are_removed_and_reported(form, tmp_path, settings):
    def ur3(pdf):
        sig = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Sig, ByteRange=pikepdf.Array([0, 10, 20, 30])))
        pdf.Root.Perms = pikepdf.Dictionary(UR3=sig)
    _edit(form, ur3)
    res = E.fill_form(form, tmp_path / "o.pdf", {"name": "Ada"}, settings)
    assert res["usage_rights_removed"] is True
    with pikepdf.open(tmp_path / "o.pdf") as pdf:
        assert "/Perms" not in pdf.Root


# --- review findings: flatten -------------------------------------------------------------


def test_flatten_needs_more_than_a_fill_only_permission(form, tmp_path, settings):
    locked = _protect(form, tmp_path, pikepdf.Permissions(
        modify_form=True, modify_annotation=False, modify_other=False))
    E.fill_form(locked, tmp_path / "filled.pdf", {"name": "Ada"}, settings, password="u")
    with pytest.raises(PdfUserError, match="does not allow flattening.*owner"):
        E.fill_form(locked, tmp_path / "flat.pdf", {"name": "Ada"}, settings, password="u",
                    flatten=True)
    E.fill_form(locked, tmp_path / "flat.pdf", {"name": "Ada"}, settings, password="o",
                flatten=True)


def test_flatten_is_refused_for_very_large_forms(tmp_path, settings, monkeypatch):
    from paperboy.engine import forms
    monkeypatch.setattr(forms, "FLATTEN_MAX_WIDGETS", 3)
    path = _form(tmp_path / "f.pdf")  # 7 widgets
    with pytest.raises(PdfUserError, match="too many fields to flatten"):
        E.fill_form(path, tmp_path / "o.pdf", {"name": "Ada"}, settings, flatten=True)


def test_flatten_refuses_fields_that_would_be_left_behind(form, tmp_path, settings):
    _edit(form, lambda pdf: pdf.Root.AcroForm.Fields.append(pikepdf.Dictionary(
        T=pikepdf.String("bare"), FT=pikepdf.Name.Btn, V=pikepdf.Name.Off)))
    with pytest.raises(PdfUserError, match="Can't flatten.*bare.*no appearance"):
        E.fill_form(form, tmp_path / "o.pdf", {"name": "Ada"}, settings, flatten=True)


# --- review findings: malformed structures are user errors or ignored ---------------------


MALFORMED = {
    "acroform-array": lambda pdf: setattr(pdf.Root, "AcroForm", pikepdf.Array([1])),
    "fields-name": lambda pdf: setattr(pdf.Root.AcroForm, "Fields", pikepdf.Name.Bad),
    "fields-item-int": lambda pdf: pdf.Root.AcroForm.Fields.append(7),
    "kids-int": lambda pdf: setattr(_field(pdf, "notes"), "Kids", 5),
    "kids-item-name": lambda pdf: setattr(_field(pdf, "size"), "Kids",
                                          pikepdf.Array([pikepdf.Name.X])),
    "parent-array": lambda pdf: setattr(_field(pdf, "notes"), "Parent", pikepdf.Array()),
    "ff-string": lambda pdf: setattr(_field(pdf, "name"), "Ff", pikepdf.String("x")),
    "maxlen-name": lambda pdf: setattr(_field(pdf, "name"), "MaxLen", pikepdf.Name.X),
    "opt-int": lambda pdf: setattr(_field(pdf, "colour"), "Opt", 3),
    "opt-empty-pair": lambda pdf: setattr(_field(pdf, "colour"), "Opt",
                                          pikepdf.Array([pikepdf.Array()])),
    "annots-int": lambda pdf: pdf.pages[0].Annots.append(9),
    "title-array": lambda pdf: setattr(_field(pdf, "notes"), "T", pikepdf.Array([1])),
    "value-dict": lambda pdf: setattr(_field(pdf, "notes"), "V", pikepdf.Dictionary(A=1)),
}


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_malformed_forms_never_cause_internal_errors(form, tmp_path, settings, case):
    _edit(form, MALFORMED[case])
    try:
        listed = E.list_form_fields(form, settings)
    except PdfUserError:
        return  # a clear error is fine
    for f in listed["fields"]:
        assert f["value"] is None or "pikepdf" not in f["value"]
        assert "pikepdf" not in f["name"]
    try:
        E.fill_form(form, tmp_path / "o.pdf", {listed["fields"][0]["name"]: "x"}
                    if listed["fields"] else {"x": "y"}, settings)
    except PdfUserError:
        pass


def test_non_positive_max_length_is_ignored(form, tmp_path, settings):
    _edit(form, lambda pdf: setattr(_field(pdf, "name"), "MaxLen", -1))
    assert _by_name(E.list_form_fields(form, settings))["name"]["max_length"] is None
    E.fill_form(form, tmp_path / "o.pdf", {"name": "Ada"}, settings)


def pathlib_path(p):
    from pathlib import Path
    return Path(p)
