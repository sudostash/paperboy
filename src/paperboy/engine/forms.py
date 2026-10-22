"""Interactive form (AcroForm) fields: list them, and fill them in.

Works on the PDF's field tree directly, so it needs nothing beyond the pikepdf
floor; qpdf (via ``generate_appearance_streams``) draws the filled values so
every viewer shows them. Malformed structures are skipped, never trusted:
form trees come from untrusted files.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pikepdf

from ..config import Settings
from ..errors import PdfUserError
from ..results import FillFormResult, FormFieldsResult
from .base import inherit_protection, open_pdf

# Field flags, PDF 32000-1:2008 sections 12.7.3.1 and 12.7.4.
_READ_ONLY = 1
_REQUIRED = 1 << 1
_NO_TOGGLE_TO_OFF = 1 << 14
_RADIO = 1 << 15
_PUSHBUTTON = 1 << 16
_COMBO = 1 << 17
_EDIT = 1 << 18
_MAX_DEPTH = 32             # field trees deeper than this are malformed (or hostile)
_MAX_VALUE_ITEMS = 100      # multi-value fields: report at most this many
FLATTEN_MAX_WIDGETS = 2000  # in-process flattening slows quadratically with widget count


def _is_dict(obj) -> bool:
    return isinstance(obj, pikepdf.Dictionary)


def _positive_int(obj) -> int | None:
    return obj if isinstance(obj, int) and not isinstance(obj, bool) and obj > 0 else None


def _text(obj) -> str | None:
    return str(obj) if isinstance(obj, pikepdf.String) else None


@dataclass
class _Field:
    name: str                               # fully qualified, e.g. "address.city"
    node: pikepdf.Dictionary                # the terminal field: holds /V
    widgets: list[pikepdf.Dictionary]       # its widget annotations (may be node itself)

    def get(self, key: str):
        """Look *key* up on the field, then its ancestors (inheritable attributes)."""
        node, depth = self.node, 0
        while _is_dict(node) and depth <= _MAX_DEPTH:
            if key in node:
                return node[key]
            node, depth = node.get("/Parent"), depth + 1
        return None

    @property
    def flags(self) -> int:
        flags = self.get("/Ff")
        return flags if isinstance(flags, int) and not isinstance(flags, bool) else 0

    @property
    def kind(self) -> str:
        ft, flags = self.get("/FT"), self.flags
        if ft == pikepdf.Name.Tx:
            return "text"
        if ft == pikepdf.Name.Ch:
            return "choice"
        if ft == pikepdf.Name.Sig:
            return "signature"
        if ft == pikepdf.Name.Btn:
            if flags & _PUSHBUTTON:
                return "button"
            return "radio" if flags & _RADIO else "checkbox"
        return "other"

    def _choices(self) -> list[tuple[str, str]]:
        """(export value, display label) for each valid /Opt entry."""
        opts = self.get("/Opt")
        if not isinstance(opts, pikepdf.Array):
            return []
        choices = []
        for opt in opts:
            if isinstance(opt, pikepdf.String):
                choices.append((str(opt), str(opt)))
            elif isinstance(opt, pikepdf.Array) and len(opt) and _text(opt[0]) is not None:
                choices.append((str(opt[0]), _text(opt[-1]) or str(opt[0])))
        return choices

    @property
    def options(self) -> list[str]:
        if self.kind == "choice":
            return [value for value, _ in self._choices()]
        if self.kind in ("checkbox", "radio"):
            states: list[str] = []
            for widget in self.widgets:
                ap = widget.get("/AP")
                normal = ap.get("/N") if _is_dict(ap) else None
                if _is_dict(normal):
                    for key in normal.keys():
                        state = str(key)[1:]
                        if state != "Off" and state not in states:
                            states.append(state)
            return states
        return []

    @property
    def labels(self) -> list[str]:
        return [label for _, label in self._choices()] if self.kind == "choice" \
            else self.options

    @property
    def value(self) -> str | None:
        v = self.get("/V")
        if isinstance(v, pikepdf.Name):
            return str(v)[1:]
        if isinstance(v, pikepdf.String):
            return str(v)
        if isinstance(v, pikepdf.Array):
            items = [str(x)[1:] if isinstance(x, pikepdf.Name) else str(x)
                     for x in list(v)[:_MAX_VALUE_ITEMS]
                     if isinstance(x, (pikepdf.String, pikepdf.Name))]
            return ", ".join(items)
        return None  # missing, or not a type a field value can be


def _fields(pdf: pikepdf.Pdf) -> list[_Field]:
    acro = pdf.Root.get("/AcroForm")
    if not _is_dict(acro) or not isinstance(acro.get("/Fields"), pikepdf.Array):
        return []
    found: list[_Field] = []
    seen: set[tuple[int, int]] = set()

    def visit(node: pikepdf.Dictionary, prefix: str, depth: int) -> None:
        if depth > _MAX_DEPTH:
            raise PdfUserError("The form's field tree is too deeply nested to read")
        if node.is_indirect:
            if node.objgen in seen:  # a cycle, or a field listed twice
                return
            seen.add(node.objgen)
        name = ".".join(p for p in (prefix, _text(node.get("/T")) or "") if p)
        raw_kids = node.get("/Kids")
        kids = [k for k in raw_kids if _is_dict(k)] \
            if isinstance(raw_kids, pikepdf.Array) else []
        child_fields = [k for k in kids if "/T" in k]
        if child_fields:
            for kid in child_fields:
                visit(kid, name, depth + 1)
        elif name:  # kids without /T are this field's widgets
            found.append(_Field(name, node, kids or [node]))

    for field in acro.Fields:
        if _is_dict(field):
            visit(field, "", 0)
    return found


def _pages_by_widget(pdf: pikepdf.Pdf) -> dict[tuple[int, int], int]:
    pages = {}
    for number, page in enumerate(pdf.pages, start=1):
        annots = page.obj.get("/Annots")
        for annot in annots if isinstance(annots, pikepdf.Array) else []:
            if _is_dict(annot) and annot.is_indirect:
                pages.setdefault(annot.objgen, number)
    return pages


def list_form_fields(path: Path, settings: Settings,
                     password: str | None = None) -> FormFieldsResult:
    """Every fillable field: full name, type, value, options, flags and page.

    Values are document content: when the PDF forbids copying and the owner
    password wasn't given, they are withheld (``values_hidden``) but the
    structure is still listed, so the form can be filled.
    """
    with open_pdf(path, settings, password) as pdf:
        hide = pdf.is_encrypted and not pdf.owner_password_matched and not pdf.allow.extract
        pages = _pages_by_widget(pdf)
        fields = []
        for f in _fields(pdf):
            page = next((pages[w.objgen] for w in f.widgets
                         if w.is_indirect and w.objgen in pages), None)
            fields.append({
                "name": f.name, "type": f.kind,
                "value": None if hide else f.value,
                "options": f.options,
                "option_labels": f.labels,
                "required": bool(f.flags & _REQUIRED),
                "read_only": bool(f.flags & _READ_ONLY),
                "max_length": _positive_int(f.get("/MaxLen")) if f.kind == "text" else None,
                "page": page,
            })
    return {"path": str(path), "has_form": bool(fields), "values_hidden": hide and bool(fields),
            "fields": fields, "count": len(fields)}


def _check(field: _Field, value: str | bool) -> str:
    """Validate one value for its field; return it normalised."""
    name, kind = field.name, field.kind
    if field.flags & _READ_ONLY:
        raise PdfUserError(f"{name} is read-only")
    if kind in ("signature", "button", "other"):
        raise PdfUserError(f"{name} is a {kind} field and cannot be filled")
    if kind == "checkbox":
        if value is False or value == "Off":
            return "Off"
        if not field.options:
            raise PdfUserError(f"{name} has no 'checked' appearance, so it can't be checked")
        on = field.options[0]
        if value is True or value == on:
            return on
        raise PdfUserError(f"{name}: use one of: true, false, {on}")
    if not isinstance(value, str):
        raise PdfUserError(f"{name}: {kind} fields take text, not {value!r}")
    if kind == "radio":
        clearable = not field.flags & _NO_TOGGLE_TO_OFF
        if value not in field.options and not (value == "Off" and clearable):
            raise PdfUserError(f"{name}: use one of: {', '.join(field.options)}")
    if kind == "choice" and value not in field.options and not field.flags & _EDIT:
        raise PdfUserError(f"{name}: use one of: {', '.join(field.options)}")
    if kind == "text":
        max_len = _positive_int(field.get("/MaxLen"))
        if max_len is not None and len(value) > max_len:
            raise PdfUserError(f"{name}: at most {max_len} characters")
    return value


def _apply(field: _Field, value: str) -> None:
    if field.kind in ("checkbox", "radio"):
        state = pikepdf.Name("/" + value)
        field.node.V = state
        for widget in field.widgets:  # show the matching widget "on", the rest "Off"
            ap = widget.get("/AP")
            normal = ap.get("/N") if _is_dict(ap) else None
            widget.AS = state if _is_dict(normal) and state in normal else pikepdf.Name.Off
        return
    field.node.V = pikepdf.String(value)
    if field.kind == "choice" and not field.flags & _COMBO:  # list box: selected index
        if value in field.options:
            field.node.I = pikepdf.Array([field.options.index(value)])
        elif "/I" in field.node:
            del field.node.I


def _signatures(pdf: pikepdf.Pdf) -> tuple[bool, bool]:
    """(signed, has Reader usage rights). Any signature dictionary counts, wherever it is."""
    perms = pdf.Root.get("/Perms")
    ur3 = perms.get("/UR3") if _is_dict(perms) else None
    ur3_id = ur3.objgen if _is_dict(ur3) and ur3.is_indirect else None
    signed = _is_dict(perms) and "/DocMDP" in perms
    for obj in pdf.objects:
        if _is_dict(obj) and "/ByteRange" in obj and obj.objgen != ur3_id:
            signed = True
            break
    return signed, ur3 is not None


def _copy_appearance(pdf: pikepdf.Pdf, obj, depth: int = 0):
    """Deep-copy an appearance (/AP and its streams): qpdf redraws in place."""
    if isinstance(obj, pikepdf.Stream):
        copy = pikepdf.Stream(pdf, obj.read_raw_bytes())
        for key, value in obj.stream_dict.items():
            if key != "/Length":
                copy[key] = value  # including /Filter: the raw bytes are still encoded
        return copy
    if _is_dict(obj) and depth < 3:  # /AP -> /N, /R, /D -> states -> streams
        return pikepdf.Dictionary({k: _copy_appearance(pdf, v, depth + 1)
                                   for k, v in obj.items()})
    return obj


def _appearance(widget: pikepdf.Dictionary):
    ap = widget.get("/AP")
    return ap.get("/N") if _is_dict(ap) else None


def fill_form(
    path: Path,
    out: Path,
    values: dict[str, str | bool],
    settings: Settings,
    password: str | None = None,
    flatten: bool = False,
) -> FillFormResult:
    """Fill form fields by full name; every value is checked before anything is written."""
    if not values:
        raise PdfUserError("Give at least one field to fill")
    with open_pdf(path, settings, password) as pdf:
        fields = _fields(pdf)
        if not fields:
            raise PdfUserError("This PDF has no form fields")
        restricted = pdf.is_encrypted and not pdf.owner_password_matched
        if restricted and not (pdf.allow.modify_form or pdf.allow.modify_annotation):
            raise PdfUserError(
                "This PDF does not allow filling in its form; that needs its owner password"
            )
        if flatten and restricted and not (pdf.allow.modify_annotation or pdf.allow.modify_other):
            raise PdfUserError(
                "This PDF does not allow flattening its form (only filling it in); that "
                "needs its owner password"
            )
        signed, usage_rights = _signatures(pdf)
        if signed or any(f.kind == "signature" and f.get("/V") is not None for f in fields):
            raise PdfUserError(
                "This PDF is digitally signed; filling it would invalidate the signature"
            )
        if flatten and sum(len(f.widgets) for f in fields) > FLATTEN_MAX_WIDGETS:
            raise PdfUserError(
                f"This form has too many fields to flatten here (over {FLATTEN_MAX_WIDGETS} "
                "widgets); fill it without flatten, then use flatten_pdf"
            )
        by_name: dict[str, list[_Field]] = {}
        for f in fields:
            by_name.setdefault(f.name, []).append(f)
        unknown = sorted(set(values) - set(by_name))
        if unknown:
            known = ", ".join(list(by_name)[:20]) + (", ..." if len(by_name) > 20 else "")
            raise PdfUserError(f"Unknown field(s): {', '.join(unknown)}. Fields: {known}")
        checked = {name: [(f, _check(f, value)) for f in by_name[name]]
                   for name, value in values.items()}

        # qpdf redraws every text/choice field, in place; keep copies of the appearances
        # of fields we don't fill, since its redraw is basic (one line, and only the
        # characters it can encode for the field's font).
        kept = [(w, _copy_appearance(pdf, w.get("/AP")))
                for f in fields if f.name not in values for w in f.widgets]
        labelled = []  # choice fields drawn with their display label, stored by export value
        for pairs in checked.values():
            for field, value in pairs:
                _apply(field, value)
                label = dict(field._choices()).get(value, value) \
                    if field.kind == "choice" else value
                if label != value:
                    field.node.V = pikepdf.String(label)
                    labelled.append((field, value))
        acro = pdf.Root.AcroForm
        acro.NeedAppearances = True
        pdf.generate_appearance_streams()
        if "/NeedAppearances" in acro:
            del acro.NeedAppearances
        # qpdf 11 applies the new appearance lazily, on the stream's first read only;
        # read it once now and store it, so later reads and the save all see it.
        drawn: dict[str, list[bytes]] = {}
        for name, pairs in checked.items():
            for field, _ in pairs:
                if field.kind in ("text", "choice"):
                    for widget in field.widgets:
                        stream = _appearance(widget)
                        if isinstance(stream, pikepdf.Stream):
                            data = stream.read_bytes()
                            stream.write(data)
                            drawn.setdefault(name, []).append(data)
        for field, value in labelled:
            field.node.V = pikepdf.String(value)
        for widget, ap in kept:
            if ap is not None:
                widget.AP = ap
            elif "/AP" in widget:
                del widget.AP

        # qpdf draws characters the field's font can't encode as "?".
        unrenderable = []
        for name, pairs in checked.items():
            shown = [dict(f._choices()).get(v, v) if f.kind == "choice" else v
                     for f, v in pairs if f.kind in ("text", "choice")]
            if any(data.count(b"?") > text.count("?")
                   for data in drawn.get(name, []) for text in shown):
                unrenderable.append(name)

        if unrenderable:
            # Ask viewers to redraw fields themselves; those that do (most) can draw the
            # stored value correctly, where qpdf's appearance shows "?".
            acro.NeedAppearances = True

        xfa_removed = "/XFA" in acro
        if xfa_removed:  # XFA-aware viewers would otherwise show the old XFA data
            del acro.XFA
        if usage_rights:  # Reader extensions break once the file changes; drop them
            del pdf.Root.Perms
        if flatten:
            if unrenderable:
                raise PdfUserError(
                    "Can't flatten: the form's font can't draw the value of "
                    f"{', '.join(unrenderable)}; fill without flatten (the value is still "
                    "stored), or use characters the font supports"
                )
            bare = sorted({f.name for f in fields for w in f.widgets
                           if _appearance(w) is None})
            if bare:
                raise PdfUserError(f"Can't flatten: {', '.join(bare)} has no appearance to "
                                   "flatten and would be left behind")
            pdf.flatten_annotations("all")
        protection = inherit_protection(pdf, password)
        pdf.save(str(out), encryption=protection.encryption)
    return protection.report({
        "output": str(out), "filled": sorted(values), "flattened": flatten,
        "unrenderable_fields": sorted(unrenderable), "xfa_removed": xfa_removed,
        "usage_rights_removed": usage_rights,
    })
