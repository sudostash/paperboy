"""OCR: give scanned pages a searchable text layer with Tesseract.

Each page is rendered by PDFium as it is displayed, read by the ``tesseract``
command (an optional system dependency, Apache-2.0), and Tesseract's text-only PDF
of that page is placed over the original as invisible text (render mode 3). The
page's own content is kept as it was: pictures are never re-rendered or
re-compressed lossily, so a scan looks the same and gains selectable, searchable words.
"""
from __future__ import annotations

import functools
import hashlib
import io
import json
import math
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pikepdf

from ..config import Settings
from ..errors import DependencyError, PdfUserError
from ..pagespec import parse_pages
from ..results import OcrPage, OcrResult
from .base import (
    inherit_protection,
    open_pdf,
    require_copy_permission,
    require_modify_permission,
    signature_state,
)
from .images import (
    _DELIMITERS,
    _WHITESPACE,
    MAX_CONTENT,
    MAX_STREAM_TOKENS,
    MAX_TOKENS,
    MAX_TOTAL_CONTENT,
    _streams,
    _Unknown,
)
from .stamp import _displayed_size, _isolate, _own_resources, _placement, _rotation
from .streams import decode

DPI_RANGE = (150, 600)
OCR_MAX_PIXELS = 25_000_000  # per page (300 dpi A3 is 17.4M); a larger page gets fewer dpi
MAX_LANGUAGES = 5
WORKERS = 4                  # Tesseract processes per call, and across all calls
WORKER_MEMORY = 4 * 2**30    # the rendering process (enforced on Linux only)
SLOT_WAIT_S = 0.25           # how often a page waiting for a Tesseract slot checks for a stop
PAGE_TIME_LIMIT_S = 120      # per page; Tesseract takes 1-5 s on a normal page
OCR_TIME_LIMIT_S = 600       # per call
MAX_OUTPUT_BYTES = 64 * 1024 * 1024  # what Tesseract writes for one page
SIZE_TOLERANCE = 0.5        # points PDFium's and pikepdf's page sizes may differ
MIN_TESSERACT = (4, 1)       # text-only PDFs (textonly_pdf) and TSV output
_LANGUAGE = re.compile(r"[A-Za-z0-9_/-]{1,64}")
_FINGERPRINT_DEPTH = 8
_SLOTS = threading.BoundedSemaphore(WORKERS)  # parallel tool calls share these


def _probe(exe: str) -> tuple[tuple[int, int], frozenset[str]]:
    """Tesseract's (major, minor) version and installed languages."""
    return _probe_cached(exe, _mtime(exe))


def _mtime(exe: str) -> float:
    try:
        return os.stat(exe).st_mtime
    except OSError:
        return 0.0


@functools.lru_cache(maxsize=8)
def _probe_cached(exe: str, _mtime: float) -> tuple[tuple[int, int], frozenset[str]]:
    def run(*args: str) -> str:
        try:
            proc = subprocess.run([exe, *args], capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DependencyError(f"Could not run Tesseract ({exe}): {exc}") from None
        # Versions differ in which stream they print to.
        return (proc.stdout + proc.stderr).decode("utf-8", "replace")

    found = re.search(r"tesseract\s+v?(\d+)\.(\d+)", run("--version"))
    if not found:
        raise DependencyError(f"{exe} doesn't look like Tesseract (no version found)")
    version = (int(found.group(1)), int(found.group(2)))
    listing = run("--list-langs").splitlines()
    languages = frozenset(line.strip() for line in listing[1:]
                          if _LANGUAGE.fullmatch(line.strip()))
    return version, languages


def _tesseract(settings: Settings, language: str) -> str:
    """The Tesseract executable, checked to be new enough and to know *language*."""
    exe = settings.tesseract_path
    if not exe:
        raise DependencyError("OCR needs the tesseract command on PATH (install Tesseract "
                              "OCR, or set PAPERBOY_TESSERACT)")
    version, installed = _probe(exe)
    if version < MIN_TESSERACT:
        raise DependencyError(f"OCR needs Tesseract {'.'.join(map(str, MIN_TESSERACT))} or "
                              f"later; {exe} is {'.'.join(map(str, version))}")
    installed = installed - {"osd"}  # orientation detection, not a language to read
    missing = [name for name in language.split("+") if name not in installed]
    if missing:
        have = ", ".join(sorted(installed)) or "none"
        raise PdfUserError(f"Tesseract has no language data for {', '.join(missing)} "
                           f"(installed: {have}). Install the language pack, e.g. "
                           "tesseract-lang on Homebrew or tesseract-ocr-<lang> on Debian")
    return exe


def _check_language(language: str) -> str:
    names = language.split("+")
    if (not 1 <= len(names) <= MAX_LANGUAGES
            or not all(_LANGUAGE.fullmatch(n) and not n.startswith("-") for n in names)
            or any(".." in n for n in names)):
        raise PdfUserError(f"language must be Tesseract language codes joined by +, e.g. "
                           f"\"eng\" or \"eng+fra\" (at most {MAX_LANGUAGES})")
    return language


def _plain(pdf: pikepdf.Pdf) -> bytes:
    """The bytes PDFium renders: the document as pikepdf reads it, decrypted, so both
    libraries see the same pages. XMP is left as it is (pikepdf would otherwise rewrite
    it in the open document)."""
    out = io.BytesIO()
    pdf.save(out, fix_metadata_version=False)
    return out.getvalue()


def _kill(proc: subprocess.Popen) -> None:
    """Kill a process and anything it started (a wrapper script's real Tesseract)."""
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)  # its own session: see start_new_session
        else:  # pragma: no cover - Windows
            proc.kill()
    except (ProcessLookupError, PermissionError):  # already gone
        pass


def _finish(proc: subprocess.Popen) -> None:
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover - a child that ignores SIGKILL
        pass


class _Run:
    """The Tesseract processes of one call, so all can be stopped at the deadline."""

    def __init__(self, exe: str, language: str, deadline: float):
        self.exe, self.language, self.deadline = exe, language, deadline
        self.running: set[subprocess.Popen] = set()
        self.lock = threading.Lock()

    def stop(self) -> None:
        """Stop now: pages not started yet don't start, running ones are killed."""
        with self.lock:
            self.deadline = 0
            for proc in self.running:
                if proc.returncode is None:  # a reaped one's pid could be reused
                    _kill(proc)

    def page(self, number: int, png: str, dpi: float) -> tuple[bytes, int, float | None]:
        """Tesseract's text-only PDF for one page image, its words and their mean
        confidence."""
        # In short waits, so a stopped call doesn't wait for another call's pages.
        while not _SLOTS.acquire(timeout=SLOT_WAIT_S):
            if time.monotonic() >= self.deadline:
                raise _OutOfTime
        try:
            return self._page(number, png, dpi)
        finally:
            _SLOTS.release()

    def _page(self, number: int, png: str, dpi: float) -> tuple[bytes, int, float | None]:
        base = png[:-4]
        # Settings, not the "pdf" and "tsv" config files: Tesseract looks for those in
        # the current directory too.
        cmd = [self.exe, png, base, "--dpi", str(round(dpi)), "-l", self.language,
               "-c", "textonly_pdf=1", "-c", "tessedit_create_pdf=1",
               "-c", "tessedit_create_tsv=1"]
        # One thread per process: pages already run in parallel, and OpenMP builds
        # otherwise oversubscribe the CPU.
        env = dict(os.environ, OMP_THREAD_LIMIT="1")
        with self.lock:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise _OutOfTime
            try:
                proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, env=env,
                                        cwd=os.path.dirname(png), start_new_session=True)
            except OSError as exc:
                raise DependencyError(f"Could not run Tesseract: {exc}") from None
            self.running.add(proc)
        try:
            _, stderr = proc.communicate(timeout=min(PAGE_TIME_LIMIT_S, remaining))
        except subprocess.TimeoutExpired:
            _kill(proc)
            _finish(proc)
            if time.monotonic() >= self.deadline:
                raise _OutOfTime from None
            raise PdfUserError(f"Tesseract took over {PAGE_TIME_LIMIT_S} s on page "
                               f"{number}") from None
        finally:
            with self.lock:
                self.running.discard(proc)
        if self.deadline == 0:  # stopped while it ran
            raise _OutOfTime
        if proc.returncode != 0:
            detail = stderr.decode("utf-8", "replace").strip().splitlines()
            raise PdfUserError(f"Tesseract failed on page {number}: "
                               f"{detail[-1][:200] if detail else proc.returncode}")
        words, confidence = _words(_read(base + ".tsv", number).decode("utf-8", "replace"))
        return _read(base + ".pdf", number), words, confidence


class _Pages:
    """The child process that checks and renders pages (see ocr_worker)."""

    def __init__(self, data: bytes, request: dict):
        self.request = request
        request = dict(request, size=len(data), cpu_seconds=int(OCR_TIME_LIMIT_S) + 5,
                       memory_bytes=WORKER_MEMORY)
        self.log = os.path.join(request["work"], "renderer.log")
        with open(self.log, "wb") as log:
            self.proc = subprocess.Popen(
                [sys.executable, "-I", "-m", "paperboy.engine.ocr_worker"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                start_new_session=True)
        self.started = False
        self.lines: queue.Queue = queue.Queue()
        self.outbox: queue.Queue = queue.Queue()
        self.outbox.put(json.dumps(request).encode() + b"\n")
        self.outbox.put(data)
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._write, daemon=True).start()

    def _read(self) -> None:
        with self.proc.stdout:
            for line in self.proc.stdout:
                self.lines.put(line)
        self.lines.put(None)

    def _write(self) -> None:
        try:
            while (chunk := self.outbox.get()) is not None:
                self.proc.stdin.write(chunk)
                self.proc.stdin.flush()
        except (OSError, ValueError):  # the worker exited or was killed
            pass
        try:
            self.proc.stdin.close()
        except OSError:  # unwritten data, to a worker that is gone
            pass

    def ready(self, pages: int = 1) -> None:
        """Let the worker render *pages* more."""
        self.outbox.put(b"\n" * pages)

    def _line(self, deadline: float):
        try:
            line = self.lines.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            raise _OutOfTime from None
        if line is None:
            return None
        try:
            return json.loads(line)
        except ValueError:
            raise _Unexpected from None

    def next(self, position: int, deadline: float) -> dict:
        """The record of request["pages"][position], checked, so nothing the worker
        says can make the server touch another page or another file."""
        if not self.started:
            if self._line(deadline) != {"ready": True}:
                self._stopped(deadline, "Could not start the page renderer")
            self.started = True
        record = self._line(deadline)
        if record is None:
            self._stopped(deadline, "Could not render the pages: the renderer stopped "
                                    "(the PDF may be too complex)")
        if not isinstance(record, dict):
            raise _Unexpected
        if set(record) == {"error"} and isinstance(record["error"], str):
            raise PdfUserError(record["error"][:300])
        if record.get("index") != self.request["pages"][position] \
                or isinstance(record["index"], bool):
            raise _Unexpected
        if record == {"index": record["index"], "text": True}:
            return record
        numbers = [record.get(k) for k in ("width", "height", "scale")]
        if set(record) != {"index", "width", "height", "rotation", "png", "scale"} \
                or record["rotation"] not in (0, 90, 180, 270) \
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                           and math.isfinite(v) and v > 0 for v in numbers) \
                or record["png"] != os.path.join(self.request["work"], f"p{position}.png"):
            raise _Unexpected
        return record

    def _stopped(self, deadline: float, message: str) -> None:
        if time.monotonic() >= deadline:
            raise _OutOfTime
        try:
            self.proc.wait(timeout=5)
            with open(self.log, "rb") as log:
                detail = log.read()[-4000:].decode("utf-8", "replace").strip().splitlines()
        except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - defensive
            detail = []
        if not self.started and detail:
            message += f": {detail[-1][:200]}"
        raise PdfUserError(message)

    def close(self) -> None:
        _kill(self.proc)
        self.outbox.put(None)  # the writer stops; the reader stops at end of output
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - ignores SIGKILL
            pass


class _OutOfTime(Exception):
    pass


class _Unexpected(PdfUserError):
    def __init__(self) -> None:
        super().__init__("Could not render the pages: the renderer returned something "
                         "unexpected")


def _read(path: str, number: int) -> bytes:
    try:
        if os.path.getsize(path) > MAX_OUTPUT_BYTES:
            raise PdfUserError(f"Tesseract's output for page {number} is too large")
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        raise PdfUserError(f"Tesseract wrote no output for page {number}") from None


def _words(tsv: str) -> tuple[int, float | None]:
    """(words recognised, their mean confidence 0-100) from Tesseract's TSV."""
    confidences = []
    for line in tsv.splitlines()[1:]:
        cells = line.split("\t")
        if len(cells) < 12 or cells[0] != "5" or not cells[11].strip():
            continue
        try:
            confidence = float(cells[10])
        except ValueError:
            continue
        if confidence >= 0:
            confidences.append(confidence)
    if not confidences:
        return 0, None
    return len(confidences), round(sum(confidences) / len(confidences), 1)


def _fingerprint(obj, depth: int = 0) -> bytes:
    """Bytes that are equal for equal objects, to share one copy of Tesseract's font."""
    if depth > _FINGERPRINT_DEPTH:
        raise ValueError("too deep")
    if isinstance(obj, pikepdf.Stream):
        inner = _fingerprint(pikepdf.Dictionary({k: v for k, v in obj.items()}), depth + 1)
        return b"S" + inner + hashlib.sha256(obj.read_raw_bytes()).digest()
    if isinstance(obj, pikepdf.Dictionary):
        return b"D" + b"".join(k.encode() + _fingerprint(obj[k], depth + 1)
                               for k in sorted(obj.keys())) + b"E"
    if isinstance(obj, pikepdf.Array):
        return b"A" + b"".join(_fingerprint(v, depth + 1) for v in obj) + b"E"
    return b"V" + repr(obj).encode()


def _share_fonts(form: pikepdf.Stream, fonts: dict[bytes, pikepdf.Object]) -> None:
    """Point the form's fonts at an identical font already in the document, if any."""
    resources = form.get("/Resources")
    table = resources.get("/Font") if isinstance(resources, pikepdf.Dictionary) else None
    if not isinstance(table, pikepdf.Dictionary):
        return
    for name in list(table.keys()):
        font = table[name]
        try:
            key = _fingerprint(font)
        except ValueError:
            continue
        if key in fonts:
            table[name] = fonts[key]
        else:
            fonts[key] = font


def _measure(page: pikepdf.Page, number: int, budget: list[int]) -> None:
    """Refuse a page whose content is too big to read back safely: placing the text
    layer parses it (to close what it leaves open). *budget* is [bytes, tokens] left."""
    try:
        streams = _streams(page.obj.get("/Contents"))
    except _Unknown:
        streams = None
    size = tokens = 0
    for stream in streams or []:
        data = decode(stream, MAX_CONTENT + 1)
        if data is None:
            streams = None
            break
        size += len(data)
        tokens += 1 + sum(data.count(c) for c in _WHITESPACE) \
            + 2 * sum(data.count(c) for c in _DELIMITERS)
    if streams is None or size > MAX_CONTENT or tokens > MAX_STREAM_TOKENS \
            or size > budget[0] or tokens > budget[1]:
        raise PdfUserError(f"Page {number} has too much content (or content that can't be "
                           "measured) to add a text layer safely; leave it out with pages")
    budget[0] -= size
    budget[1] -= tokens


def _overlay(pdf: pikepdf.Pdf, index: int, text_pdf: bytes,
             fonts: dict[bytes, pikepdf.Object]) -> None:
    """Place Tesseract's text-only page over page *index*, as the page is displayed."""
    page = pdf.pages[index]
    with pikepdf.open(io.BytesIO(text_pdf)) as layer:
        source = layer.pages[0]
        x0, y0, x1, y1 = (float(v) for v in source.mediabox)
        form = pdf.copy_foreign(source.as_form_xobject())
    _share_fonts(form, fonts)
    width, height = _displayed_size(page)
    scale = f"{width / (x1 - x0):.6f} 0 0 {height / (y1 - y0):.6f} {-x0:.6f} {-y0:.6f}"
    _own_resources(page)
    name = page.add_resource(form, pikepdf.Name.XObject, prefix="Ocr")
    matrix = " ".join(f"{v:.6f}" for v in _placement(page))
    _isolate(pdf, page)  # the page's own graphics state can't move the text
    page.contents_add(pikepdf.Stream(pdf, f"q {matrix} cm {scale} cm {name} Do Q\n".encode()))


def _check(pdf: pikepdf.Pdf, record: dict, budget: list[int]) -> None:
    """Refuse a page whose text couldn't be placed where PDFium showed its words."""
    index = record["index"]
    page = pdf.pages[index]
    shown = _displayed_size(page)
    width, height = record["width"], record["height"]
    if record["rotation"] != _rotation(page) \
            or not max(abs(width - shown[0]), abs(height - shown[1])) <= SIZE_TOLERANCE:
        raise PdfUserError(f"Page {index + 1}'s page boxes or rotation are malformed, so the "
                           "text couldn't be placed where it's seen; leave the page out "
                           "with pages")
    _measure(page, index + 1, budget)


def ocr_pdf(
    path: Path,
    out: Path,
    settings: Settings,
    language: str = "eng",
    pages: str | None = None,
    skip_pages_with_text: bool = True,
    dpi: int = 300,
    password: str | None = None,
) -> OcrResult:
    """Add an invisible text layer to the pages (default: those without text)."""
    _check_language(language)
    if isinstance(dpi, bool) or not isinstance(dpi, int) \
            or not DPI_RANGE[0] <= dpi <= DPI_RANGE[1]:
        raise PdfUserError(f"dpi must be a whole number from {DPI_RANGE[0]} to {DPI_RANGE[1]}")
    exe = _tesseract(settings, language)
    deadline = time.monotonic() + OCR_TIME_LIMIT_S
    with open_pdf(path.read_bytes(), settings, password) as pdf:
        require_modify_permission(pdf)
        # The text layer makes words in pictures copyable by any tool that ignores
        # permissions, so this asks for what extracting text does.
        require_copy_permission(pdf, "making its text searchable")
        signed, usage_rights = signature_state(pdf)
        if signed:
            raise PdfUserError("This PDF is digitally signed; adding a text layer would "
                               "invalidate the signature")
        total = len(pdf.pages)
        targets = sorted(parse_pages(pages, total, dedupe=True)) if pages \
            else list(range(total))
        work = tempfile.mkdtemp(prefix="paperboy-ocr-")  # private (0700), removed below
        try:
            request = {"pages": targets, "dpi": dpi, "skip": skip_pages_with_text,
                       "max_pixels": OCR_MAX_PIXELS, "work": work}
            run = _Run(exe, language, deadline)
            with_text, done = _recognise(pdf, _plain(pdf), request, run)
            report: list[OcrPage] = []
            fonts: dict[bytes, pikepdf.Object] = {}
            for i, scale, (text_pdf, words, confidence) in done:
                if time.monotonic() >= deadline:
                    raise _OutOfTime
                if words:
                    try:
                        _overlay(pdf, i, text_pdf, fonts)
                    except pikepdf.PdfError as exc:
                        raise PdfUserError(f"Could not read Tesseract's output for page "
                                           f"{i + 1}: {exc}") from None
                report.append({"page": i + 1, "words": words,
                               "mean_confidence": confidence, "dpi": round(scale * 72)})
        except _OutOfTime:
            raise PdfUserError(f"OCR took over {OCR_TIME_LIMIT_S} s; OCR fewer pages at a "
                               "time with pages, or lower dpi. Nothing was written") from None
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if usage_rights:  # Reader extensions break once the file changes; drop them
            del pdf.Root.Perms
        protection = inherit_protection(pdf, password)
        pdf.save(str(out), encryption=protection.encryption)
    return protection.report({
        "output": str(out), "language": language, "pages": report,
        "pages_with_text": with_text,
        "words": sum(p["words"] for p in report),
    })


def _recognise(pdf: pikepdf.Pdf, data: bytes, request: dict,
               run: _Run) -> tuple[list[int], list]:
    """Check and render each page in the worker, and read the rendered ones with
    Tesseract, several at once. Returns the pages skipped for having text, and
    (index, scale, Tesseract's result) for the others, in page order."""
    workers = max(1, min(WORKERS, os.cpu_count() or 1))
    ahead = 2 * workers  # pages rendered but not yet read; never the whole file on disk
    budget = [MAX_TOTAL_CONTENT, MAX_TOKENS]
    with_text: list[int] = []
    done: list = []
    renderer = _Pages(data, request)
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending: list = []

            def collect(limit: int) -> None:
                while len(pending) > limit:
                    i, scale, png, future = pending.pop(0)
                    try:
                        done.append((i, scale, future.result()))
                    finally:
                        _remove(png)
                    renderer.ready()

            try:
                renderer.ready(ahead)
                for position in range(len(request["pages"])):
                    record = renderer.next(position, run.deadline)
                    if record.get("text"):
                        with_text.append(record["index"] + 1)
                        continue
                    png = record["png"]
                    try:
                        _check(pdf, record, budget)
                    except PdfUserError:
                        _remove(png)
                        raise
                    pending.append((record["index"], record["scale"], png,
                                    pool.submit(run.page, record["index"] + 1, png,
                                                record["scale"] * 72)))
                    collect(ahead - 1)
                collect(0)
            except BaseException:
                run.stop()
                for *_, future in pending:
                    future.cancel()
                raise
    finally:
        renderer.close()
    return with_text, done


def _remove(path: str) -> None:
    for name in (path, path[:-4] + ".pdf", path[:-4] + ".tsv"):
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass
