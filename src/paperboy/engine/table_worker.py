"""The part of extract_tables that runs pdfplumber, in a separate process.

pdfplumber's work on a hostile page can't be bounded from outside (finding the
cells of a dense grid of lines grows with the fourth power of its lines), and a
thread can't be stopped. So the server runs this module as a child process with CPU
and memory limits, and kills it at the deadline; results stream back one page per
line, so pages finished before a kill still count.

Run as ``python -I -m paperboy.engine.table_worker``: the request is JSON on stdin,
and each result is one JSON line on stdout.
"""
from __future__ import annotations

import json
import re
import sys

MAX_INTERSECTIONS = 15_000   # ruled lines crossing on one page; past this, cells take minutes
MAX_ERROR = 200              # characters of an error message passed back


def _limit(cpu_seconds: int, memory_bytes: int) -> None:
    """Cap this process's CPU time and (where the system enforces it) its memory."""
    try:
        import resource
    except ImportError:  # Windows: the parent's deadline still applies
        return
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    if sys.platform.startswith("linux"):  # macOS doesn't enforce memory limits
        resource.setrlimit(resource.RLIMIT_DATA, (memory_bytes, memory_bytes))


def _emit(record: dict) -> None:
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()


def _short(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    return text[:MAX_ERROR]


def clean_cell(value, limit: int) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _intersections(page) -> int:
    """How many places the page's ruled lines cross, after joining the pieces that
    make up each line, as the table finder does."""
    from pdfplumber.table import merge_edges

    edges = merge_edges(page.edges, 3, 3, 3, 3)
    horizontal = sum(e["orientation"] == "h" for e in edges)
    return horizontal * (len(edges) - horizontal)


def run(request: dict) -> None:
    import pdfplumber

    settings = {"lines": {"vertical_strategy": "lines", "horizontal_strategy": "lines"},
                "text": {"vertical_strategy": "text", "horizontal_strategy": "text"},
                }[request["strategy"]]
    tables_left, cells_left = request["max_tables"], request["max_cells"]
    try:
        pdf = pdfplumber.open(request["path"])
    except Exception as exc:  # noqa: BLE001 - any failure to read the file at all
        _emit({"error": _short(exc)})
        return
    with pdf:
        if len(pdf.pages) != request["page_count"]:
            _emit({"error": "the page tree can't be read consistently"})
            return
        for index in request["pages"]:
            page = pdf.pages[index]
            try:
                if request["strategy"] == "lines" \
                        and _intersections(page) > request["max_intersections"]:
                    _emit({"page": index, "skipped": "too many ruled lines to analyse"})
                    continue
                found = []
                for table in page.find_tables(settings):
                    rows = [[clean_cell(c, request["max_cell"]) for c in row]
                            for row in table.extract()]
                    rows = [row for row in rows if any(row)]  # drop empty spacer rows
                    if not rows:
                        continue
                    size = sum(map(len, rows))
                    if tables_left <= 0 or size > cells_left:
                        _emit({"page": index, "tables": found, "truncated": True})
                        return
                    tables_left, cells_left = tables_left - 1, cells_left - size
                    found.append(rows)
                _emit({"page": index, "tables": found})
            except Exception as exc:  # noqa: BLE001 - a damaged page skips only that page
                _emit({"page": index, "skipped": f"it can't be read: {_short(exc)}"})
            finally:
                page.close()  # pdfplumber caches each page's objects until closed


def main() -> None:
    request = json.loads(sys.stdin.read())
    _limit(request["cpu_seconds"], request["memory_bytes"])
    sys.setrecursionlimit(5_000)  # deep but finite content nesting is an error, not a crash
    run(request)


if __name__ == "__main__":
    main()
