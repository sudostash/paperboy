"""End-to-end test over the real MCP stdio protocol.

Launches the server as a subprocess (``python -m paperboy``) and drives it with
the MCP client SDK, the way Claude Desktop / Cursor / Claude Code would.
"""
from __future__ import annotations

import json
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from reportlab.pdfgen import canvas

EXPECTED_TOOLS = {
    "pdf_info", "get_metadata", "check_pdf", "extract_text",
    "encrypt_pdf", "decrypt_pdf",
    "rearrange_pages", "delete_pages", "extract_pages", "rotate_pages",
    "merge_pdfs", "split_pdf",
    "optimize_pdf", "repair_pdf", "flatten_pdf", "set_metadata",
    "convert_to_pdf", "images_to_pdf",
    "view_pages", "render_pages", "find_blank_pages",
    "list_form_fields", "fill_form",
    "add_watermark", "add_page_numbers",
    "inspect_hidden_data", "sanitize_pdf", "scan_pdf",
    "get_outline", "set_outline",
    "list_attachments", "extract_attachments", "extract_images",
}


def _is_error(result) -> bool:
    return bool(getattr(result, "is_error", getattr(result, "isError", False)))


def _payload(result) -> dict:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured.get("result", structured)
    text = result.content[0].text
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return {"text": text}


@pytest.fixture
def server_env(tmp_path):
    env = dict(os.environ)
    env["PAPERBOY_ROOT"] = str(tmp_path)
    # Make the in-tree package importable without an install, for local runs.
    src = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    return env


async def _session(env):
    params = StdioServerParameters(command=sys.executable, args=["-m", "paperboy"], env=env)
    return stdio_client(params)


async def test_lists_all_tools(server_env):
    async with await _session(server_env) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            assert {t.name for t in tools.tools} == EXPECTED_TOOLS


READ_ONLY_TOOLS = {"pdf_info", "get_metadata", "check_pdf", "extract_text", "view_pages",
                   "find_blank_pages", "list_form_fields", "inspect_hidden_data",
                   "scan_pdf", "get_outline",
                   "list_attachments"}


async def test_tool_annotations(server_env):
    async with await _session(server_env) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
    for tool in tools.tools:
        ann = tool.annotations.model_dump(by_alias=True, exclude_none=True)
        assert ann.get("title"), tool.name
        assert ann["openWorldHint"] is False, tool.name
        assert ann["idempotentHint"] is True, tool.name
        if tool.name in READ_ONLY_TOOLS:
            assert ann["readOnlyHint"] is True, tool.name
        else:
            # Writers never modify their input, but can replace an existing
            # output when overwrite=true, so clients should still confirm.
            assert ann["readOnlyHint"] is False, tool.name
            assert ann["destructiveHint"] is True, tool.name


async def test_encrypt_then_info(server_env, tmp_path):
    src = tmp_path / "a.pdf"
    c = canvas.Canvas(str(src))
    c.drawString(72, 720, "HELLO")
    c.showPage()
    c.save()
    async with await _session(server_env) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("encrypt_pdf", {"input_path": str(src),
                                                    "user_password": "pw"})
            assert not _is_error(res)
            out = _payload(res)["output"]

            res = await s.call_tool("pdf_info", {"input_path": out})
            assert _payload(res)["is_encrypted"] is True

            res = await s.call_tool("pdf_info", {"input_path": out, "password": "pw"})
            assert _payload(res)["pages"] == 1


async def test_path_confinement_is_error(server_env):
    async with await _session(server_env) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("pdf_info", {"input_path": "/etc/passwd"})
            assert _is_error(res)
            assert "root" in res.content[0].text.lower()


async def test_missing_file_is_error(server_env, tmp_path):
    async with await _session(server_env) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("pdf_info", {"input_path": str(tmp_path / "nope.pdf")})
            assert _is_error(res)
