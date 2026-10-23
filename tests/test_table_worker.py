"""table_worker, run in this process: what the extract_tables child process does."""
from __future__ import annotations

import io
import json
import sys

import pikepdf
import pytest
from test_engine_tables import REGIONS, _doc, _grid, _table

from paperboy.engine import table_worker as W


def _request(path, **overrides):
    request = {"path": str(path), "page_count": 1, "pages": [0], "strategy": "lines",
               "max_tables": 10, "max_cells": 1000, "max_cell": 50,
               "max_intersections": W.MAX_INTERSECTIONS}
    return {**request, **overrides}


def _records(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_tables_are_sent_one_page_per_line(tmp_path, capsys):
    doc = _doc(tmp_path / "d.pdf", [_table(REGIONS)], [_table([["a", "b"]])])
    W.run(_request(doc, page_count=2, pages=[1, 0]))
    assert _records(capsys) == [{"page": 1, "tables": [[["a", "b"]]]},
                                {"page": 0, "tables": [REGIONS]}]


def test_text_strategy(tmp_path, capsys):
    doc = _doc(tmp_path / "d.pdf", [_table(REGIONS, grid=False)])
    W.run(_request(doc, strategy="text"))
    (record,) = _records(capsys)
    assert ["North", "12,400", "98.1%"] in record["tables"][0]


def test_limits_stop_with_a_truncated_record(tmp_path, capsys):
    doc = _doc(tmp_path / "d.pdf", [_table(REGIONS), _table([["a", "b"]])])
    W.run(_request(doc, max_tables=1))
    assert _records(capsys) == [{"page": 0, "tables": [REGIONS], "truncated": True}]
    W.run(_request(doc, max_cells=5))
    assert _records(capsys) == [{"page": 0, "tables": [], "truncated": True}]


def test_a_file_that_cannot_be_opened(tmp_path, capsys):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.7\nnothing")
    W.run(_request(bad))
    (record,) = _records(capsys)
    assert set(record) == {"error"} and len(record["error"]) <= W.MAX_ERROR


def test_a_page_count_that_disagrees_stops_everything(tmp_path, capsys):
    doc = _doc(tmp_path / "d.pdf", [_table(REGIONS)])
    W.run(_request(doc, page_count=2))
    assert _records(capsys) == [{"error": "the page tree can't be read consistently"}]


def test_dense_grids_are_skipped(tmp_path, capsys):
    W.run(_request(_grid(tmp_path / "g.pdf", 50), max_intersections=100))
    assert _records(capsys) == [{"page": 0, "skipped": "too many ruled lines to analyse"}]


def test_a_failing_page_is_skipped_with_a_short_reason(tmp_path, capsys, monkeypatch):
    import pdfplumber.page

    def broken(self, settings=None):
        raise ValueError("x" * 1000)
    monkeypatch.setattr(pdfplumber.page.Page, "find_tables", broken)
    W.run(_request(_doc(tmp_path / "d.pdf", [_table(REGIONS)])))
    (record,) = _records(capsys)
    assert record["skipped"].startswith("it can't be read: xxx")
    assert len(record["skipped"]) <= len("it can't be read: ") + W.MAX_ERROR


def test_errors_without_a_message_are_named():
    assert W._short(RecursionError()) == "RecursionError"


def test_cells_are_cleaned_and_cut():
    assert W.clean_cell(" a \n\t b ", 10) == "a b"
    assert W.clean_cell(None, 10) == ""
    assert W.clean_cell("x" * 20, 10) == "x" * 9 + "…"


def test_limits_are_set_where_the_system_enforces_them(monkeypatch):
    import resource
    calls = []
    monkeypatch.setattr(resource, "setrlimit", lambda kind, values: calls.append(kind))
    monkeypatch.setattr(sys, "platform", "linux")
    W._limit(10, 2**30)
    assert calls == [resource.RLIMIT_CPU, resource.RLIMIT_DATA]
    calls.clear()
    monkeypatch.setattr(sys, "platform", "darwin")
    W._limit(10, 2**30)
    assert calls == [resource.RLIMIT_CPU]


def test_without_the_resource_module_only_the_deadline_applies(monkeypatch):
    monkeypatch.setitem(sys.modules, "resource", None)    # as on Windows
    W._limit(10, 2**30)


def test_main_reads_the_request_from_stdin(tmp_path, capsys, monkeypatch):
    doc = _doc(tmp_path / "d.pdf", [_table([["a", "b"]])])
    request = {**_request(doc), "cpu_seconds": 30, "memory_bytes": 2**31}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    limits = []
    monkeypatch.setattr(W, "_limit", lambda cpu, memory: limits.append((cpu, memory)))
    W.main()
    assert limits == [(30, 2**31)]
    assert _records(capsys) == [{"page": 0, "tables": [[["a", "b"]]]}]


def test_the_rewritten_copy_has_the_same_pages(tmp_path):
    from paperboy.engine.tables import _plain_copy
    doc = _doc(tmp_path / "d.pdf", [_table(REGIONS)], [])
    with pikepdf.open(doc) as pdf:
        pdf.pages[0].obj.TrimBox = pikepdf.Array([1, 1, 2, 2])
        pdf.pages[1].obj.MediaBox = pikepdf.String("broken")
        data = _plain_copy(pdf)
    with pikepdf.open(io.BytesIO(data)) as copy:
        assert "/Info" not in copy.trailer and len(copy.pages) == 2
        assert "/TrimBox" not in copy.pages[0].obj
        assert [float(v) for v in copy.pages[1].obj.MediaBox] == [0, 0, 612, 792]
        assert int(copy.pages[0].obj.Rotate) == 0


@pytest.mark.parametrize("value", [-90, 450])
def test_odd_rotations_are_written_plainly(tmp_path, value):
    from paperboy.engine.tables import _plain_copy
    doc = _doc(tmp_path / "d.pdf", [_table(REGIONS)])
    with pikepdf.open(doc) as pdf:
        pdf.pages[0].obj.Rotate = value
        data = _plain_copy(pdf)
    with pikepdf.open(io.BytesIO(data)) as copy:
        assert int(copy.pages[0].obj.Rotate) == {-90: 270, 450: 90}[value]
