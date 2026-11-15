"""batch_process: the same steps on many PDFs, each file on its own."""
from __future__ import annotations

import json
import os
import sys

import pikepdf
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import paperboy.server as S

from conftest import _make_pdf, page_labels


@pytest.fixture(autouse=True)
def sandbox(settings, monkeypatch):
    monkeypatch.setattr(S, "SETTINGS", settings)
    return settings


@pytest.fixture
def folder(tmp_path):
    """Two PDFs, one in a subfolder, a hidden one, and a file that isn't a PDF."""
    d = tmp_path / "invoices"
    (d / "2025").mkdir(parents=True)
    _make_pdf(d / "march.pdf", 2)
    _make_pdf(d / "april.pdf", 3)
    _make_pdf(d / "2025" / "dec.pdf", 1)
    _make_pdf(d / ".draft.pdf", 1)
    (d / "notes.txt").write_text("not a PDF")
    return d


NUMBERS = [{"tool": "add_page_numbers", "args": {"format": "N{n}"}}]


def _rel(paths, base):
    return sorted(os.path.relpath(p, base) for p in paths)


def _junk(path):
    path.write_bytes(b"not a PDF at all")
    return path


def test_a_folder_runs_every_pdf_and_saves_results_beside_it(folder):
    before = {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    res = S.batch_process(NUMBERS, folder=str(folder))
    dest = folder / "processed"
    assert res["output_dir"] == str(dest)
    assert (res["done"], res["failed"], res["not_run"], res["stopped"]) == (2, 0, 0, None)
    assert [os.path.basename(f["input"]) for f in res["files"]] == ["april.pdf", "march.pdf"]
    assert _rel(dest.rglob("*.pdf"), dest) == ["april.pdf", "march.pdf"]  # not subfolders
    for f in res["files"]:
        assert f["status"] == "done" and f["error"] is None
        assert f["steps"][0]["tool"] == "add_page_numbers"
        assert "output" not in f["steps"][0]["result"]
        assert f["password_protected"] is False
    assert page_labels(dest / "april.pdf")[2].endswith("N3")
    assert all(p.read_bytes() == data for p, data in before.items())  # inputs untouched


def test_recursive_keeps_subfolders_and_a_rerun_leaves_results_out(folder):
    res = S.batch_process(NUMBERS, folder=str(folder), recursive=True)
    dest = folder / "processed"
    assert res["done"] == 3
    assert _rel(dest.rglob("*.pdf"), dest) == ["2025/dec.pdf", "april.pdf", "march.pdf"]
    again = S.batch_process(NUMBERS, folder=str(folder), recursive=True, overwrite=True)
    assert again["done"] == 3                         # not processed/*.pdf as well
    assert not (dest / "processed").exists()


def test_input_paths_are_named_from_the_folder_they_share(folder, tmp_path):
    a, b = folder / "march.pdf", folder / "2025" / "dec.pdf"
    res = S.batch_process(NUMBERS, input_paths=[str(b), str(a)])
    dest = folder / "processed"
    assert res["output_dir"] == str(dest)
    assert [f["output"] for f in res["files"]] == [str(dest / "2025" / "dec.pdf"),
                                                   str(dest / "march.pdf")]
    out = tmp_path / "out"
    res = S.batch_process(NUMBERS, input_paths=[str(a)], output_dir=str(out))
    assert res["files"][0]["output"] == str(out / "march.pdf")
    assert page_labels(out / "march.pdf")[0].endswith("N1")


def test_one_bad_file_fails_alone(folder):
    _junk(folder / "broken.pdf")
    res = S.batch_process(NUMBERS, folder=str(folder))
    assert (res["done"], res["failed"]) == (2, 1)
    bad = next(f for f in res["files"] if f["input"].endswith("broken.pdf"))
    assert bad["status"] == "failed" and bad["output"] is None and bad["steps"] == []
    assert "Could not open PDF" in bad["error"]
    assert not (folder / "processed" / "broken.pdf").exists()


def test_a_failing_step_saves_nothing_for_that_file(folder):
    res = S.batch_process([{"tool": "delete_pages", "args": {"pages": "3"}}],
                          folder=str(folder))
    status = {os.path.basename(f["input"]): f["status"] for f in res["files"]}
    assert status == {"april.pdf": "done", "march.pdf": "failed"}   # march has 2 pages
    march = next(f for f in res["files"] if f["input"].endswith("march.pdf"))
    assert march["error"].startswith("Step 1 (delete_pages) failed")
    assert not (folder / "processed" / "march.pdf").exists()


def test_stop_on_error_leaves_the_rest_not_run(folder):
    _junk(folder / "aaa.pdf")   # first in name order
    res = S.batch_process(NUMBERS, folder=str(folder), stop_on_error=True)
    assert [f["status"] for f in res["files"]] == ["failed", "not_run", "not_run"]
    assert (res["done"], res["failed"], res["not_run"]) == (0, 1, 2)
    assert res["stopped"] == "“aaa.pdf” failed (stop_on_error)"
    assert all(f["error"] is None for f in res["files"][1:])
    assert not list((folder / "processed").glob("*.pdf"))


def test_the_time_limit_stops_starting_files(folder, monkeypatch):
    monkeypatch.setattr(S, "BATCH_TIME_LIMIT_S", 0)
    res = S.batch_process(NUMBERS, folder=str(folder))
    assert [f["status"] for f in res["files"]] == ["done", "not_run"]  # the first runs
    assert res["stopped"] == "The batch took over 0 minutes"


def test_an_unexpected_error_fails_that_file_only(folder, monkeypatch, caplog):
    real = S._run_plan

    def flaky(src, *args):
        if src.name == "april.pdf":
            raise RuntimeError("/secret/internal/detail")
        return real(src, *args)
    monkeypatch.setattr(S, "_run_plan", flaky)
    res = S.batch_process(NUMBERS, folder=str(folder))
    april, march = res["files"]
    assert april["error"] == "Internal error; details were logged by the server"
    assert "/secret" not in json.dumps(res)
    assert march["status"] == "done"
    assert "Unexpected error in batch_process" in caplog.text


def test_existing_results_stop_the_batch_before_anything_runs(folder):
    dest = folder / "processed"
    dest.mkdir()
    (dest / "march.pdf").write_bytes(b"keep me")
    with pytest.raises(S.ToolError, match=r"1 of the results already exist .*\(march\.pdf\); "
                                          "pass overwrite=true"):
        S.batch_process(NUMBERS, folder=str(folder))
    assert (dest / "march.pdf").read_bytes() == b"keep me"
    assert not (dest / "april.pdf").exists()
    res = S.batch_process(NUMBERS, folder=str(folder), overwrite=True)
    assert res["done"] == 2 and (dest / "march.pdf").read_bytes().startswith(b"%PDF")


def test_many_existing_results_are_summarised(tmp_path):
    files = [str(_make_pdf(tmp_path / f"f{n}.pdf", 1)) for n in range(5)]
    (tmp_path / "processed").mkdir()
    for n in range(5):
        (tmp_path / "processed" / f"f{n}.pdf").write_bytes(b"x")
    with pytest.raises(S.ToolError, match=r"5 of the results .*\(f0\.pdf, f1\.pdf, f2\.pdf, "
                                          r"\.\.\.\)"):
        S.batch_process(NUMBERS, input_paths=files)


def test_protected_inputs_open_with_the_password_and_stay_protected(make_encrypted, tmp_path):
    a = make_encrypted("a.pdf", password="pw", pages=2)
    b = make_encrypted("b.pdf", password="pw", pages=2)
    res = S.batch_process([{"tool": "rotate_pages", "args": {"degrees": 90}}],
                          input_paths=[str(a), str(b)], password="pw")
    assert res["done"] == 2 and all(f["password_protected"] for f in res["files"])
    with pikepdf.open(tmp_path / "processed" / "a.pdf", password="pw") as pdf:
        assert int(pdf.pages[0].obj.get("/Rotate", 0)) == 90
    missing = S.batch_process([{"tool": "rotate_pages", "args": {"degrees": 90}}],
                              input_paths=[str(a)], output_dir=str(tmp_path / "o"))
    assert "password protected" in missing["files"][0]["error"]


def test_encrypting_a_batch_protects_every_copy(doc, make_pdf, tmp_path):
    other = make_pdf("other.pdf", 2)
    res = S.batch_process([{"tool": "encrypt_pdf", "args": {"user_password": "new"}}],
                          input_paths=[str(doc), str(other)])
    assert all(f["password_protected"] for f in res["files"])
    for name in ("doc.pdf", "other.pdf"):
        with pytest.raises(pikepdf.PasswordError):
            pikepdf.open(tmp_path / "processed" / name)
        pikepdf.open(tmp_path / "processed" / name, password="new").close()


@pytest.mark.parametrize(("kwargs", "message"), [
    ({}, "Give either folder or input_paths"),
    ({"folder": ".", "input_paths": ["a.pdf"]}, "Give either folder or input_paths"),
    ({"input_paths": []}, "input_paths is empty"),
    ({"input_paths": ["a.pdf"] * 101}, "at most 100 PDFs"),
    ({"input_paths": ["{tmp}/nope.pdf"]}, "File not found"),
])
def test_what_to_process_is_checked(tmp_path, kwargs, message):
    if kwargs.get("input_paths"):
        kwargs["input_paths"] = [p.format(tmp=tmp_path) for p in kwargs["input_paths"]]
    with pytest.raises(S.ToolError, match=message):
        S.batch_process(NUMBERS, **kwargs)


def test_a_file_given_twice_is_refused(doc, tmp_path):
    with pytest.raises(S.ToolError, match="names “doc.pdf” twice"):
        S.batch_process(NUMBERS, input_paths=[str(doc), str(tmp_path / "." / "doc.pdf")])


def test_folders_are_checked(folder, tmp_path, monkeypatch):
    with pytest.raises(S.ToolError, match="Not a folder"):
        S.batch_process(NUMBERS, folder=str(folder / "march.pdf"))
    (tmp_path / "empty" / "sub").mkdir(parents=True)
    _make_pdf(tmp_path / "empty" / "sub" / "x.pdf", 1)
    with pytest.raises(S.ToolError, match=r"No PDFs to process .*recursive=true includes"):
        S.batch_process(NUMBERS, folder=str(tmp_path / "empty"))
    (tmp_path / "empty" / "sub" / "x.pdf").unlink()
    with pytest.raises(S.ToolError, match=r"No PDFs to process in \S+$"):
        S.batch_process(NUMBERS, folder=str(tmp_path / "empty"), recursive=True)
    monkeypatch.setattr(S, "BATCH_MAX_FILES", 2)
    with pytest.raises(S.ToolError, match="has 3 PDFs; a batch can process at most 2"):
        S.batch_process(NUMBERS, folder=str(folder), recursive=True)


def test_a_folder_that_cant_be_listed_in_full_is_refused(folder, monkeypatch):
    real = S.engine.list_pdfs
    monkeypatch.setattr(S.engine, "list_pdfs",
                        lambda *a: {**real(*a), "truncated": True})
    with pytest.raises(S.ToolError, match="Couldn't list every PDF"):
        S.batch_process(NUMBERS, folder=str(folder))


def test_a_folder_batch_needs_a_root(folder, monkeypatch, settings):
    import dataclasses
    monkeypatch.setattr(S, "SETTINGS", dataclasses.replace(settings, root=None))
    with pytest.raises(S.ToolError, match="needs PAPERBOY_ROOT"):
        S.batch_process(NUMBERS, folder=str(folder))
    res = S.batch_process(NUMBERS, input_paths=[str(folder / "march.pdf")])
    assert res["done"] == 1                    # a list of files works without one


def test_everything_stays_inside_the_root(folder, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    _make_pdf(outside / "x.pdf", 1)
    for kwargs in ({"folder": str(outside)}, {"input_paths": [str(outside / "x.pdf")]},
                   {"folder": str(folder), "output_dir": str(outside)}):
        with pytest.raises(S.ToolError, match="outside the allowed root"):
            S.batch_process(NUMBERS, **kwargs)
    assert sorted(p.name for p in outside.iterdir()) == ["x.pdf"]


def test_the_output_folder_must_be_a_folder(folder):
    with pytest.raises(S.ToolError, match="exists and is not a folder"):
        S.batch_process(NUMBERS, folder=str(folder), output_dir=str(folder / "notes.txt"))


def test_a_result_may_never_replace_an_input(folder):
    with pytest.raises(S.ToolError, match="“march.pdf” would replace an input"):
        S.batch_process(NUMBERS, input_paths=[str(folder / "march.pdf")],
                        output_dir=str(folder), overwrite=True)


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_links_in_the_output_folder_cant_redirect_results(folder, tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "april.pdf").symlink_to(folder / "april.pdf")        # onto an input
    with pytest.raises(S.ToolError, match="would replace an input"):
        S.batch_process(NUMBERS, folder=str(folder), output_dir=str(dest), overwrite=True)
    (dest / "april.pdf").unlink()
    (dest / "2025").symlink_to(dest)                             # two results, one file
    _make_pdf(folder / "dec.pdf", 1)
    with pytest.raises(S.ToolError, match="Two results would be saved as"):
        S.batch_process(NUMBERS, folder=str(folder), output_dir=str(dest), recursive=True)
    (dest / "2025").unlink()
    (folder / "dec.pdf").unlink()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _make_pdf(elsewhere / "dec.pdf", 1)
    (dest / "2025").symlink_to(elsewhere)                        # out of the output folder
    with pytest.raises(S.ToolError, match=r"already exist .*\(\S+elsewhere/dec\.pdf\)"):
        S.batch_process(NUMBERS, folder=str(folder), output_dir=str(dest), recursive=True)


@pytest.mark.parametrize(("steps", "message"), [
    ([], "Give 1 to 20 steps"),
    ("optimize_pdf", "Give 1 to 20 steps"),
    ([{"tool": "merge_pdfs"}], "can't run in a pipeline"),
    ([{"tool": "encrypt_pdf", "args": {"user_password": "x"}}, {"tool": "optimize_pdf"}],
     "encrypt_pdf must be the last step"),
    ([{"tool": "add_watermark", "args": {"image_path": "{folder}/missing.png"}}],
     "File not found"),
])
def test_steps_are_checked_before_any_file(folder, steps, message):
    steps = json.loads(json.dumps(steps).replace("{folder}", str(folder)))
    with pytest.raises(S.ToolError, match=message):
        S.batch_process(steps, folder=str(folder))
    assert not (folder / "processed").exists()


# --- progress -------------------------------------------------------------------------------


class _Ctx:
    def __init__(self, fail=False):
        self.reports, self.fail = [], fail

    async def report_progress(self, progress, total, message):
        if self.fail:
            raise RuntimeError("the client went away")
        self.reports.append((progress, total, message))


async def test_progress_is_reported_after_each_file(folder):
    ctx = _Ctx()
    tool = S._asking(S.batch_process)
    res = await tool(steps=NUMBERS, folder=str(folder), paperboy_context=ctx)
    assert res["done"] == 2
    assert ctx.reports == [(0, 2, "1 of 2: “april.pdf”"),
                           (1, 2, "2 of 2: “march.pdf”"), (2, 2, "Done")]
    assert S._PROGRESS.get() is None             # only while the tool runs


async def test_a_client_that_fails_progress_doesnt_fail_the_batch(folder):
    res = await S._asking(S.batch_process)(steps=NUMBERS, folder=str(folder),
                                           paperboy_context=_Ctx(fail=True))
    assert res["done"] == 2


async def test_over_stdio_progress_reaches_the_client(folder, tmp_path):
    env = dict(os.environ, PAPERBOY_ROOT=str(tmp_path))
    src = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    seen = []

    async def progress(value, total, message):
        seen.append((value, total, message))
    params = StdioServerParameters(command=sys.executable, args=["-m", "paperboy"], env=env)
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        result = await s.call_tool("batch_process", {"folder": str(folder), "steps": NUMBERS},
                                   progress_callback=progress)
    assert not getattr(result, "isError", False), result.content
    assert seen[-1] == (2, 2, "Done") and len(seen) == 3


def test_an_output_folder_around_the_folder_still_takes_every_pdf(folder):
    res = S.batch_process(NUMBERS, folder=str(folder), output_dir=str(folder.parent))
    assert res["done"] == 2
    assert (folder.parent / "march.pdf").exists()


def test_folders_made_for_a_failed_file_are_removed(folder):
    _junk(folder / "2025" / "broken.pdf")
    res = S.batch_process(NUMBERS, input_paths=[str(folder / "2025" / "broken.pdf"),
                                                str(folder / "march.pdf")],
                          output_dir=str(folder / "out" / "deep"))
    assert [f["status"] for f in res["files"]] == ["failed", "done"]
    assert sorted(p.name for p in (folder / "out" / "deep").iterdir()) == ["march.pdf"]


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_a_folder_swapped_for_a_link_during_the_batch_gets_nothing(folder, tmp_path,
                                                                  monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    real = S.require_input

    def swap(path, settings):
        if path.endswith("dec.pdf"):   # just before this file runs, its folder becomes a link
            (folder / "processed").mkdir(exist_ok=True)
            (folder / "processed" / "2025").symlink_to(elsewhere)
        return real(path, settings)
    monkeypatch.setattr(S, "require_input", swap)
    res = S.batch_process(NUMBERS, folder=str(folder), recursive=True)
    dec = next(f for f in res["files"] if f["input"].endswith("dec.pdf"))
    assert dec["status"] == "failed" and "changed during the batch" in dec["error"]
    assert list(elsewhere.iterdir()) == []


def test_pdfs_on_different_drives_are_refused(folder, monkeypatch):
    def no_common(paths):
        raise ValueError("Paths don't have the same drive")
    monkeypatch.setattr(S.os.path, "commonpath", no_common)
    with pytest.raises(S.ToolError, match="on different drives"):
        S.batch_process(NUMBERS, input_paths=[str(folder / "march.pdf")])


def test_a_bound_batch_runs_only_on_its_files(folder):
    token = S._BATCH_BOUND.set(json.dumps([str(folder / "march.pdf")]))
    try:
        with pytest.raises(S.ToolError, match="changed after the password was typed"):
            S.batch_process(NUMBERS, folder=str(folder))
        assert S.batch_process(NUMBERS, input_paths=[str(folder / "march.pdf")])["done"] == 1
    finally:
        S._BATCH_BOUND.reset(token)
