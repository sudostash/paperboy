"""Structured output contract, over the real stdio protocol.

Every tool declares an ``outputSchema`` and returns ``structuredContent`` that:
  * validates against that schema (strict clients reject results that don't);
  * equals the JSON text content (the SDK silently drops undeclared keys, and
    fills absent optional keys with null, so a mismatch means a lossy schema).
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys

import jsonschema
import pikepdf
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image
from reportlab.pdfgen import canvas
from test_server_stdio import EXPECTED_TOOLS

from conftest import HAS_QPDF


def _pdf(path, pages=3):
    c = canvas.Canvas(str(path))
    for n in range(pages):
        c.drawString(72, 720, f"PAGE-{n + 1}")
        c.showPage()
    c.save()
    return str(path)


def _locked(path, user="pw", owner="owner"):
    plain = _pdf(path.with_name("plain-" + path.name))
    with pikepdf.open(plain) as pdf:
        pdf.save(path, encryption=pikepdf.Encryption(
            user=user, owner=owner, R=6, allow=pikepdf.Permissions(print_highres=False)))
    return str(path)


def _owner_only(path):
    return _locked(path, user="", owner="boss")


def _blank_middle(path):
    c = canvas.Canvas(str(path))
    c.drawString(72, 720, "ONE")
    c.showPage()
    c.showPage()  # blank
    c.drawString(72, 720, "THREE")
    c.showPage()
    c.save()
    return str(path)


def _formpdf(path):
    c = canvas.Canvas(str(path))
    c.acroForm.textfield(name="name", x=72, y=700, width=200, height=20)
    c.acroForm.checkbox(name="ok", x=72, y=660, checked=False)
    c.showPage()
    c.save()
    return str(path)


def _image(path):
    Image.new("RGB", (20, 20), "red").save(path)
    return str(path)


def _attached(path):
    _pdf(path)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.attachments["notes.txt"] = pikepdf.AttachedFileSpec(pdf, b"hello")
        pdf.save(path)
    return str(path)


def _pictured(path):
    img = path.with_suffix(".png")
    Image.new("RGB", (20, 20), "red").save(img)
    c = canvas.Canvas(str(path))
    c.drawImage(str(img), 72, 600, 20, 20)
    c.save()
    return str(path)


def _tabled(path):
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle
    table = Table([["a", "b"], ["1", "=2"]])
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
    SimpleDocTemplate(str(path)).build([table])
    return str(path)


def _text(path):
    path.write_text("hello\n  indented\n")
    return str(path)


# (case id, tool, function of tmp_path returning the arguments)
CASES = [
    ("pdf_info", "pdf_info", lambda d: {"input_path": _pdf(d / "a.pdf")}),
    ("pdf_info-locked-no-password", "pdf_info", lambda d: {"input_path": _locked(d / "l.pdf")}),
    ("pdf_info-locked", "pdf_info",
     lambda d: {"input_path": _locked(d / "l.pdf"), "password": "pw"}),
    ("get_metadata", "get_metadata", lambda d: {"input_path": _pdf(d / "a.pdf")}),
    ("check_pdf", "check_pdf", lambda d: {"input_path": _pdf(d / "a.pdf")}),
    ("extract_text", "extract_text", lambda d: {"input_path": _pdf(d / "a.pdf"), "pages": "2"}),
    ("encrypt_pdf", "encrypt_pdf",
     lambda d: {"input_path": _pdf(d / "a.pdf"), "user_password": "x"}),
    ("decrypt_pdf", "decrypt_pdf",
     lambda d: {"input_path": _locked(d / "l.pdf"), "password": "pw"}),
    ("rearrange_pages", "rearrange_pages",
     lambda d: {"input_path": _pdf(d / "a.pdf"), "page_order": "3,1"}),
    ("delete_pages", "delete_pages", lambda d: {"input_path": _pdf(d / "a.pdf"), "pages": "1"}),
    ("extract_pages", "extract_pages", lambda d: {"input_path": _pdf(d / "a.pdf"), "pages": "2"}),
    ("rotate_pages", "rotate_pages", lambda d: {"input_path": _pdf(d / "a.pdf"), "degrees": 90}),
    ("rotate_pages-protected", "rotate_pages",
     lambda d: {"input_path": _locked(d / "l.pdf"), "degrees": 90, "password": "pw"}),
    ("rotate_pages-owner-password", "rotate_pages",
     lambda d: {"input_path": _locked(d / "l.pdf"), "degrees": 90, "password": "owner"}),
    ("rotate_pages-owner-only", "rotate_pages",
     lambda d: {"input_path": _owner_only(d / "o.pdf"), "degrees": 90}),
    ("merge_pdfs", "merge_pdfs",
     lambda d: {"input_paths": [_pdf(d / "a.pdf"), _pdf(d / "b.pdf")],
                "output_path": str(d / "m.pdf")}),
    ("split_pdf", "split_pdf", lambda d: {"input_path": _pdf(d / "a.pdf"), "ranges": "1;2-"}),
    ("optimize_pdf", "optimize_pdf", lambda d: {"input_path": _pdf(d / "a.pdf")}),
    ("repair_pdf", "repair_pdf", lambda d: {"input_path": _pdf(d / "a.pdf")}),
    ("set_metadata", "set_metadata",
     lambda d: {"input_path": _pdf(d / "a.pdf"), "fields": {"title": "T"}}),
    ("convert_to_pdf", "convert_to_pdf", lambda d: {"input_path": _text(d / "n.txt")}),
    ("images_to_pdf", "images_to_pdf",
     lambda d: {"input_paths": [_image(d / "i.png")], "output_path": str(d / "i.pdf")}),
    ("view_pages", "view_pages", lambda d: {"input_path": _pdf(d / "a.pdf"), "pages": "1-2"}),
    ("view_pages-locked", "view_pages",
     lambda d: {"input_path": _locked(d / "l.pdf"), "password": "pw"}),
    ("render_pages", "render_pages", lambda d: {"input_path": _pdf(d / "a.pdf"), "dpi": 72}),
    ("find_blank_pages", "find_blank_pages", lambda d: {"input_path": _blank_middle(d / "b.pdf")}),
    ("list_form_fields", "list_form_fields", lambda d: {"input_path": _formpdf(d / "f.pdf")}),
    ("add_watermark", "add_watermark", lambda d: {"input_path": _pdf(d / "a.pdf"),
                                                  "text": "DRAFT"}),
    ("add_watermark-image", "add_watermark",
     lambda d: {"input_path": _pdf(d / "a.pdf"), "image_path": _image(d / "logo.png")}),
    ("add_page_numbers", "add_page_numbers", lambda d: {"input_path": _pdf(d / "a.pdf")}),
    ("inspect_hidden_data", "inspect_hidden_data", lambda d: {"input_path": _pdf(d / "a.pdf")}),
    ("scan_pdf", "scan_pdf", lambda d: {"input_path": _pdf(d / "a.pdf")}),
    ("get_outline", "get_outline", lambda d: {"input_path": _pdf(d / "a.pdf")}),
    ("search_text", "search_text", lambda d: {"input_path": _pdf(d / "a.pdf"), "query": "a"}),
    ("extract_tables", "extract_tables", lambda d: {"input_path": _tabled(d / "t.pdf")}),
    ("export_tables", "export_tables", lambda d: {"input_path": _tabled(d / "t.pdf")}),
    ("crop_pages", "crop_pages",
     lambda d: {"input_path": _pdf(d / "a.pdf"), "margins": [10, 10, 10, 10]}),
    ("resize_pages", "resize_pages", lambda d: {"input_path": _pdf(d / "a.pdf"), "size": "a4"}),
    ("n_up", "n_up", lambda d: {"input_path": _pdf(d / "a.pdf"), "per_sheet": 4}),
    ("insert_blank_pages", "insert_blank_pages",
     lambda d: {"input_path": _pdf(d / "a.pdf"), "after": [0, 3]}),
    ("interleave_pdfs", "interleave_pdfs",
     lambda d: {"front_path": _pdf(d / "f.pdf"), "back_path": _pdf(d / "b.pdf"),
                "output_path": str(d / "i.pdf")}),
    ("list_attachments", "list_attachments", lambda d: {"input_path": _attached(d / "t.pdf")}),
    ("extract_attachments", "extract_attachments",
     lambda d: {"input_path": _attached(d / "t.pdf")}),
    ("extract_images", "extract_images", lambda d: {"input_path": _pictured(d / "p.pdf")}),
    ("set_outline", "set_outline",
     lambda d: {"input_path": _pdf(d / "a.pdf"),
                "entries": [{"title": "A", "page": 1}, {"title": "B", "page": 2, "level": 2}]}),
    ("set_outline-protected", "set_outline",
     lambda d: {"input_path": _locked(d / "l.pdf"), "password": "owner",
                "entries": [{"title": "A", "page": 1}]}),
    ("scan_pdf-locked", "scan_pdf",
     lambda d: {"input_path": _locked(d / "l.pdf"), "password": "pw", "antivirus": False}),
    ("sanitize_pdf", "sanitize_pdf", lambda d: {"input_path": _pdf(d / "a.pdf"),
                                                "remove_comments": True}),
    ("add_page_numbers-bates", "add_page_numbers",
     lambda d: {"input_path": _pdf(d / "a.pdf"), "format": "X-{n:04}", "pages": "2-"}),
    ("list_form_fields-none", "list_form_fields", lambda d: {"input_path": _pdf(d / "a.pdf")}),
    ("fill_form", "fill_form",
     lambda d: {"input_path": _formpdf(d / "f.pdf"), "values": {"name": "Ada", "ok": True}}),
    ("fill_form-flatten", "fill_form",
     lambda d: {"input_path": _formpdf(d / "f.pdf"), "values": {"name": "Ada"},
                "flatten": True}),
    ("find_blank_pages-none", "find_blank_pages",
     lambda d: {"input_path": _pdf(d / "a.pdf"), "ink_threshold_percent": 0}),
    ("render_pages-jpeg", "render_pages",
     lambda d: {"input_path": _pdf(d / "a.pdf"), "pages": "2", "format": "jpeg"}),
]
if HAS_QPDF:
    CASES.append(("flatten_pdf", "flatten_pdf", lambda d: {"input_path": _pdf(d / "a.pdf")}))
    CASES.append(("check_pdf-qpdf-locked", "check_pdf",
                  lambda d: {"input_path": _locked(d / "l.pdf"), "password": "pw"}))


@pytest.fixture
def server_env(tmp_path):
    env = dict(os.environ, PAPERBOY_ROOT=str(tmp_path))
    src = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    if not HAS_QPDF:
        env.pop("PAPERBOY_QPDF", None)
    return env


def _session(env):
    return stdio_client(StdioServerParameters(command=sys.executable,
                                              args=["-m", "paperboy"], env=env))


def _field(obj, snake, camel):
    return getattr(obj, snake, getattr(obj, camel, None))


async def _tools(env) -> dict:
    async with _session(env) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        return {t.name: t for t in (await s.list_tools()).tools}


async def test_every_tool_declares_a_closed_output_schema(server_env):
    tools = await _tools(server_env)
    assert set(tools) == EXPECTED_TOOLS
    for name, tool in tools.items():
        schema = _field(tool, "output_schema", "outputSchema")
        assert schema, f"{name} has no outputSchema"
        assert schema["type"] == "object"
        # No optional keys: an absent optional key comes back as null on mcp 1.x,
        # which violates a non-nullable schema type.
        assert set(schema["required"]) == set(schema["properties"]), name


def test_every_tool_has_a_contract_case():
    covered = {tool for _, tool, _ in CASES}
    expected = EXPECTED_TOOLS if HAS_QPDF else EXPECTED_TOOLS - {"flatten_pdf"}
    assert covered == expected


@pytest.mark.parametrize(("tool", "make_args"), [(t, a) for _, t, a in CASES],
                         ids=[c for c, _, _ in CASES])
async def test_structured_result_is_valid_and_lossless(server_env, tmp_path, tool, make_args):
    args = make_args(tmp_path)
    async with _session(server_env) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        schema = _field(next(t for t in (await s.list_tools()).tools if t.name == tool),
                        "output_schema", "outputSchema")
        result = await s.call_tool(tool, args)

    assert not _field(result, "is_error", "isError"), result.content
    structured = _field(result, "structured_content", "structuredContent")
    assert isinstance(structured, dict)
    jsonschema.validate(structured, schema)
    assert json.loads(result.content[0].text) == structured
    images = [c for c in result.content if c.type == "image"]
    if tool == "view_pages":  # one PNG per page listed in the result
        assert len(images) == len(structured["pages"])
        for item, page in zip(images, structured["pages"], strict=True):
            assert _field(item, "mime_type", "mimeType") == "image/png"
            with Image.open(io.BytesIO(base64.b64decode(item.data))) as im:
                assert im.size == (page["width_px"], page["height_px"])
    else:
        assert images == []
