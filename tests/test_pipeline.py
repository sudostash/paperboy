"""run_pipeline: several tools in one call, only the final result saved."""
from __future__ import annotations

import os

import pikepdf
import pytest
from PIL import Image

import paperboy.server as S

from conftest import page_labels


@pytest.fixture(autouse=True)
def sandbox(settings, monkeypatch):
    monkeypatch.setattr(S, "SETTINGS", settings)
    return settings


@pytest.fixture
def outside(tmp_path_factory):
    d = tmp_path_factory.mktemp("outside")
    (d / "logo.png").write_bytes(b"")
    return d


def _names(folder):
    return sorted(p.name for p in folder.iterdir())


def test_a_chain_runs_in_order_and_saves_only_the_result(doc):
    before = _names(doc.parent)
    res = S.run_pipeline(str(doc), [
        {"tool": "delete_pages", "args": {"pages": "1"}},
        {"tool": "rotate_pages", "args": {"degrees": 90, "pages": "1"}},
        {"tool": "add_page_numbers"},
        {"tool": "optimize_pdf"},
    ])
    out = doc.with_name("doc_processed.pdf")
    assert res["output"] == str(out)
    assert [s["tool"] for s in res["steps"]] == ["delete_pages", "rotate_pages",
                                                 "add_page_numbers", "optimize_pdf"]
    assert all("output" not in s["result"] for s in res["steps"])   # no temp paths
    assert _names(doc.parent) == sorted([*before, out.name])          # nothing else written
    labels = page_labels(out)
    assert len(labels) == 4 and labels[0].startswith("PAGE-2")
    with pikepdf.open(out) as pdf:
        assert int(pdf.pages[0].obj.get("/Rotate", 0)) == 90


def test_decrypt_work_encrypt(make_encrypted, tmp_path):
    locked = make_encrypted("in.pdf", password="old", pages=3)
    out = tmp_path / "result.pdf"
    res = S.run_pipeline(str(locked), [
        {"tool": "decrypt_pdf"},
        {"tool": "delete_pages", "args": {"pages": "2"}},
        {"tool": "encrypt_pdf", "args": {"user_password": "new", "allow_copy": False}},
    ], output_path=str(out), password="old")
    assert res["password_protected"] is True and res["steps"][-1]["tool"] == "encrypt_pdf"
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(out, password="old")
    with pikepdf.open(out, password="new") as pdf:
        assert len(pdf.pages) == 2 and not pdf.allow.extract


def test_a_protected_input_stays_protected_through_the_steps(make_encrypted, tmp_path):
    locked = make_encrypted("in.pdf", password="pw", pages=3)
    res = S.run_pipeline(str(locked), [{"tool": "rotate_pages", "args": {"degrees": 180}},
                                       {"tool": "add_watermark", "args": {"text": "DRAFT"}}],
                         password="pw")
    assert res["password_protected"] is True
    with pikepdf.open(res["output"], password="pw") as pdf:
        assert len(pdf.pages) == 3


@pytest.mark.parametrize(("steps", "message"), [
    ([], "Give 1 to 20 steps"),
    ([{"tool": "optimize_pdf"}] * 21, "Give 1 to 20 steps"),
    (["optimize_pdf"], "Step 1 must be an object with a tool name"),
    ([{"args": {}}], "Step 1 must be an object with a tool name"),
    ([{"tool": "merge_pdfs"}], "can't run in a pipeline. Steps can be: add_page_numbers"),
    ([{"tool": "pdf_info"}], "can't run in a pipeline"),
    ([{"tool": "delete_pages"}], r"Step 1 \(delete_pages\): pages: Field required"),
    ([{"tool": "rotate_pages", "args": {"degrees": "ninety"}}], "degrees:"),
    ([{"tool": "optimize_pdf", "args": {"input_path": "x.pdf"}}], "input_path: Extra inputs"),
    ([{"tool": "optimize_pdf", "args": {"password": "x"}}], "password: Extra inputs"),
    ([{"tool": "optimize_pdf", "args": "fast"}], "args must be an object"),
    ([{"tool": "encrypt_pdf", "args": {"user_password": "x"}}, {"tool": "optimize_pdf"}],
     "Step 1: encrypt_pdf must be the last step"),
])
def test_steps_are_all_checked_before_any_runs(doc, steps, message):
    before = _names(doc.parent)
    with pytest.raises(S.ToolError, match=message):
        S.run_pipeline(str(doc), steps)
    assert _names(doc.parent) == before


def test_a_failing_step_saves_nothing(doc):
    before = _names(doc.parent)
    with pytest.raises(S.ToolError, match=r"Step 2 \(delete_pages\) failed: .*every page"):
        S.run_pipeline(str(doc), [{"tool": "optimize_pdf"},
                                  {"tool": "delete_pages", "args": {"pages": "1-5"}}])
    assert _names(doc.parent) == before


def test_each_step_keeps_its_own_rules(make_encrypted):
    restricted = make_encrypted("r.pdf", password="pw", owner="owner",
                                allow=pikepdf.Permissions(modify_other=False))
    with pytest.raises(S.ToolError, match=r"Step 1 \(add_watermark\) failed: .*changing"):
        S.run_pipeline(str(restricted), [{"tool": "add_watermark", "args": {"text": "X"}}],
                       password="pw")


def test_every_runnable_step_works(doc, tmp_path):
    logo = tmp_path / "logo.png"
    Image.new("RGB", (20, 20), "red").save(logo)
    steps = [
        {"tool": "rearrange_pages", "args": {"page_order": "2,1", "append_unlisted": True}},
        {"tool": "extract_pages", "args": {"pages": "1-4"}},
        {"tool": "repair_pdf"},
        {"tool": "set_metadata", "args": {"fields": {"title": "Piped"}}},
        {"tool": "sanitize_pdf", "args": {"remove_comments": True}},
        {"tool": "add_watermark", "args": {"image_path": str(logo), "position": "top-right"}},
        {"tool": "crop_pages", "args": {"margins": [10, 10, 10, 10]}},
        {"tool": "resize_pages", "args": {"size": "a4"}},
        {"tool": "insert_blank_pages", "args": {"after": [0]}},
        {"tool": "set_outline", "args": {"entries": [{"title": "Start", "page": 2}]}},
        {"tool": "flatten_pdf"},
        {"tool": "n_up", "args": {"per_sheet": 2}},
    ]
    res = S.run_pipeline(str(doc), steps)
    assert [s["tool"] for s in res["steps"]] == [s["tool"] for s in steps]
    with pikepdf.open(res["output"]) as pdf:
        assert len(pdf.pages) == 3          # 5 pages, blank added: 5 sheets of 2 -> 3


def test_forms_can_be_filled_in_a_pipeline(tmp_path):
    from test_engine_forms import _form
    form = _form(tmp_path / "form.pdf")
    res = S.run_pipeline(str(form), [{"tool": "fill_form",
                                      "args": {"values": {"name": "Ada"}, "flatten": True}}])
    assert res["steps"][0]["result"]["filled"] == ["name"]


def test_inputs_outside_the_root_are_refused(doc, outside):
    with pytest.raises(S.ToolError, match="outside the allowed root"):
        S.run_pipeline(str(outside / "secret.pdf"), [{"tool": "optimize_pdf"}])
    with pytest.raises(S.ToolError, match="outside the allowed root"):
        S.run_pipeline(str(doc), [{"tool": "optimize_pdf"}],
                       output_path=str(outside / "o.pdf"))
    with pytest.raises(S.ToolError, match=r"Step 1 \(add_watermark\) failed: .*outside"):
        S.run_pipeline(str(doc), [{"tool": "add_watermark",
                                   "args": {"image_path": str(outside / "logo.png")}}])
    assert not (outside / "o.pdf").exists()


def test_the_output_is_never_the_input_or_an_existing_file(doc):
    with pytest.raises(S.ToolError, match="same as the input|overwrite"):
        S.run_pipeline(str(doc), [{"tool": "optimize_pdf"}], output_path=str(doc),
                       overwrite=True)
    S.run_pipeline(str(doc), [{"tool": "optimize_pdf"}])
    with pytest.raises(S.ToolError, match="already exists"):
        S.run_pipeline(str(doc), [{"tool": "optimize_pdf"}])
    S.run_pipeline(str(doc), [{"tool": "optimize_pdf"}], overwrite=True)


def test_the_result_gets_the_usual_file_mode(doc):
    res = S.run_pipeline(str(doc), [{"tool": "optimize_pdf"}])
    plain = doc.with_name("plain.pdf")
    S.optimize_pdf(str(doc), output_path=str(plain))
    assert os.stat(res["output"]).st_mode == os.stat(plain).st_mode


def test_publishing_never_leaves_a_half_written_file(doc, monkeypatch):
    def fail(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(S.ToolError, match="Internal error"):
        S.run_pipeline(str(doc), [{"tool": "optimize_pdf"}])
    assert _names(doc.parent) == ["doc.pdf"]       # no output, no leftover temporary file



def test_step_errors_do_not_show_temporary_paths(doc, monkeypatch):
    import paperboy.engine as engine
    real = engine.repair_pdf

    def fails(path, out, settings, password=None):
        raise S.PdfUserError(f"Could not open PDF: {path}: damaged")
    monkeypatch.setattr(engine, "repair_pdf", fails)
    with pytest.raises(S.ToolError) as caught:
        S.run_pipeline(str(doc), [{"tool": "optimize_pdf"}, {"tool": "repair_pdf"}])
    message = str(caught.value)
    assert "step01" not in message and "Could not open PDF: the PDF: damaged" in message
    monkeypatch.setattr(engine, "repair_pdf", real)


# --- review findings ---------------------------------------------------------------------------


def _locked(make_pdf, tmp_path, R, allow=None, name="in.pdf"):
    plain = make_pdf(f"plain-{name}", 3)
    out = tmp_path / name
    with pikepdf.open(plain) as pdf:
        pdf.save(out, encryption=pikepdf.Encryption(
            user="user", owner="owner", R=R, aes=R >= 4, metadata=R >= 4,
            allow=allow or pikepdf.Permissions(extract=False)))
    return out


def _opens(path, password):
    try:
        with pikepdf.open(path, password=password) as pdf:
            return "owner" if pdf.owner_password_matched else "user"
    except pikepdf.PasswordError:
        return None


@pytest.mark.parametrize("R", [4, 6])
def test_owner_password_chains_keep_the_owner_password(make_pdf, tmp_path, R):
    locked = _locked(make_pdf, tmp_path, R)
    res = S.run_pipeline(str(locked), [{"tool": "rotate_pages", "args": {"degrees": 90}},
                                       {"tool": "optimize_pdf"},
                                       {"tool": "add_page_numbers"}], password="owner")
    out = res["output"]
    assert _opens(out, "owner") == "owner"          # it still lifts the restrictions
    if R == 4:                                       # the user password can be recovered
        assert _opens(out, "user") == "user" and res["user_password_changed"] is False
    else:                                            # AES-256: it can't, and that's reported
        assert _opens(out, "user") is None and res["user_password_changed"] is True


@pytest.mark.parametrize("R", [4, 6])
def test_owner_password_chains_can_still_decrypt(make_pdf, tmp_path, R):
    locked = _locked(make_pdf, tmp_path, R)
    res = S.run_pipeline(str(locked), [{"tool": "rotate_pages", "args": {"degrees": 90}},
                                       {"tool": "optimize_pdf"}, {"tool": "decrypt_pdf"}],
                         password="owner")
    with pikepdf.open(res["output"]) as pdf:
        assert not pdf.is_encrypted


def test_a_user_password_chain_cannot_lift_copy_restrictions(make_pdf, tmp_path):
    locked = _locked(make_pdf, tmp_path, 6)
    for last in ("decrypt_pdf", "encrypt_pdf"):
        args = {"user_password": "new"} if last == "encrypt_pdf" else {}
        with pytest.raises(S.ToolError, match=f"Step 3 \\({last}\\) failed: .*owner password"):
            S.run_pipeline(str(locked), [{"tool": "optimize_pdf"}, {"tool": "repair_pdf"},
                                         {"tool": last, "args": args}], password="user")


def test_intermediate_files_must_stay_under_the_size_cap(doc, sandbox, monkeypatch):
    import dataclasses
    monkeypatch.setattr(S, "SETTINGS", dataclasses.replace(sandbox,
                                                           max_file_bytes=doc.stat().st_size))
    before = _names(doc.parent)
    with pytest.raises(S.ToolError, match=r"Step 1 \(add_watermark\) made a file over"):
        S.run_pipeline(str(doc), [{"tool": "add_watermark", "args": {"text": "X" * 40}},
                                  {"tool": "optimize_pdf"}])
    assert _names(doc.parent) == before


def test_each_step_file_is_removed_once_used(doc, monkeypatch):
    seen = []
    real = S._step_runners

    def watching():
        runners = real()

        def optimize(src, out, args, pw):
            seen.append(len(list(out.parent.iterdir())))
            return runners["optimize_pdf"](src, out, args, pw)
        return {**runners, "optimize_pdf": optimize}
    monkeypatch.setattr(S, "_step_runners", watching)
    S.run_pipeline(str(doc), [{"tool": "optimize_pdf"}] * 4)
    assert max(seen) <= 1                            # only the file the step reads


def test_overwriting_keeps_the_files_permissions(doc):
    out = doc.with_name("private.pdf")
    out.write_bytes(b"old")
    os.chmod(out, 0o600)
    S.run_pipeline(str(doc), [{"tool": "optimize_pdf"}], output_path=str(out), overwrite=True)
    assert os.stat(out).st_mode & 0o777 == 0o600 and out.read_bytes() != b"old"


def test_read_only_outputs_and_links_are_refused(doc, tmp_path):
    readonly = doc.with_name("ro.pdf")
    readonly.write_bytes(b"keep")
    os.chmod(readonly, 0o444)
    with pytest.raises(S.ToolError, match="read-only"):
        S.run_pipeline(str(doc), [{"tool": "optimize_pdf"}], output_path=str(readonly),
                       overwrite=True)
    assert readonly.read_bytes() == b"keep"
    folder = doc.with_name("folder.pdf")
    folder.mkdir()
    with pytest.raises(S.ToolError):
        S.run_pipeline(str(doc), [{"tool": "optimize_pdf"}], output_path=str(folder),
                       overwrite=True)
    assert folder.is_dir()


def test_a_file_that_appears_during_the_run_is_not_replaced(doc, monkeypatch):
    out = doc.with_name("doc_processed.pdf")
    real = S._step_runners

    def racing():
        runners = real()

        def optimize(src, made, args, pw):
            out.write_bytes(b"someone else's")
            return runners["optimize_pdf"](src, made, args, pw)
        return {**runners, "optimize_pdf": optimize}
    monkeypatch.setattr(S, "_step_runners", racing)
    with pytest.raises(S.ToolError, match="already exists"):
        S.run_pipeline(str(doc), [{"tool": "optimize_pdf"}])
    assert out.read_bytes() == b"someone else's"


def test_unknown_step_keys_are_refused(doc):
    with pytest.raises(S.ToolError, match=r"Step 1: unknown key 'arguments'"):
        S.run_pipeline(str(doc), [{"tool": "sanitize_pdf",
                                   "arguments": {"remove_metadata": False}}])


def test_set_metadata_needs_a_field_as_the_tool_does(doc):
    with pytest.raises(S.ToolError, match="at least one metadata field"):
        S.run_pipeline(str(doc), [{"tool": "set_metadata", "args": {"fields": {}}}])


def test_the_pipeline_has_a_time_limit(doc, monkeypatch):
    monkeypatch.setattr(S, "PIPELINE_TIME_LIMIT_S", -1)
    with pytest.raises(S.ToolError, match=r"stopped before step 2 \(repair_pdf\), nothing"):
        S.run_pipeline(str(doc), [{"tool": "optimize_pdf"}, {"tool": "repair_pdf"}])
    assert not doc.with_name("doc_processed.pdf").exists()


def test_no_fragment_of_a_temporary_path_is_shown(doc, monkeypatch):
    import paperboy.engine as engine

    def fails(path, out, settings, password=None):
        raise S.PdfUserError(f"qpdf: {path.parent}/.step01.pdf.flattening: write failed")
    monkeypatch.setattr(engine, "flatten_pdf", fails)
    with pytest.raises(S.ToolError) as caught:
        S.run_pipeline(str(doc), [{"tool": "optimize_pdf"}, {"tool": "flatten_pdf"}])
    assert "step01" not in str(caught.value) and "the PDF: write failed" in str(caught.value)


def test_the_schema_lists_the_steps_and_their_number():
    import asyncio
    import json
    tools = asyncio.run(S.mcp.list_tools())
    tool = next(t for t in tools if t.name == "run_pipeline")
    schema = getattr(tool, "inputSchema", None) or tool.input_schema
    steps = schema["properties"]["steps"]
    assert (steps["minItems"], steps["maxItems"]) == (1, 20)
    assert '"n_up"' in json.dumps(schema) and '"merge_pdfs"' not in json.dumps(schema)


def test_every_step_passes_on_all_its_arguments():
    """A step argument the runner leaves out would be accepted and silently ignored."""
    import inspect
    import re
    source = inspect.getsource(S._step_runners)
    blocks = re.split(r'\n\s+"(\w+)": ', source)
    runners = dict(zip(blocks[1::2], blocks[2::2], strict=True))
    passed_otherwise = {"encrypt_pdf": {"current_password"},   # the pipeline's password
                        "set_metadata": {"fields"}}            # through _fields(a)
    for name, body in runners.items():
        params = set(inspect.signature(getattr(S, name)).parameters) - {
            "input_path", "output_path", "password", "overwrite"}
        used = set(re.findall(r'a\["(\w+)"\]', body))
        assert params - used == passed_otherwise.get(name, set()), name
    assert len(runners) == len(S._step_runners())
