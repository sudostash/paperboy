"""The part of ocr_pdf that uses PDFium, in a separate process.

A tiny hostile PDF (forms that draw each other ten times over, nested deep) can keep
PDFium busy for hours on one page, and a PDFium call can't be interrupted from another
thread. So pages are checked and rendered in a child process with a CPU limit, which
the server kills at its deadline; the server's own PDFium lock is never held.

Run as ``python -I -m paperboy.engine.ocr_worker``. stdin: one JSON line (the request),
the PDF's bytes (``size`` of them), then one line per page the server is ready for.
stdout: ``{"ready": true}`` once started, then one JSON line per page, in order:
``{"index", "text": true}`` for a page with text that is skipped, or ``{"index",
"width", "height", "rotation", "png", "scale"}`` for a page rendered to a PNG in the
server's private folder; ``{"error"}`` and exit on a failure.
"""
from __future__ import annotations

import json
import os
import sys

from .table_worker import _limit


def _emit(record: dict) -> None:
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()


def _scale(width: float, height: float, dpi: int, max_pixels: int) -> float:
    """Pixels per point for a page shown *width* x *height* points, at most *dpi*."""
    scale = dpi / 72
    pixels = width * scale * height * scale
    if pixels > max_pixels:
        scale *= (max_pixels / pixels) ** 0.5
    return scale


def run(request: dict, data: bytes, ready) -> None:
    """Check, then render, each requested page; *ready* blocks until the server wants
    the next rendering (so PNGs never pile up on disk)."""
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_raw

    _emit({"ready": True})  # started: a failure from here on is the PDF's
    try:
        doc = pdfium.PdfDocument(data)
        doc.init_forms()  # filled-in form fields are part of what is seen
    except pdfium.PdfiumError as exc:
        _emit({"error": f"Could not open PDF: {exc}"})
        return
    for n, index in enumerate(request["pages"]):
        try:
            page = doc[index]
            try:
                if request["skip"]:
                    found = page.get_objects(filter=[pdfium_raw.FPDF_PAGEOBJ_TEXT],
                                             max_depth=15)
                    if next(found, None) is not None:
                        _emit({"index": index, "text": True})
                        continue
                width, height = page.get_size()
                rotation = page.get_rotation()
                if not ready():
                    return
                scale = _scale(width, height, request["dpi"], request["max_pixels"])
                image = page.render(scale=scale, grayscale=True).to_pil()
                png = os.path.join(request["work"], f"p{n}.png")
                image.save(png, "PNG", compress_level=1)
            finally:
                page.close()
        except Exception as exc:  # noqa: BLE001 - PDFium's, or pypdfium2's on odd sizes
            _emit({"error": f"Could not render page {index + 1}: {exc}"})
            return
        _emit({"index": index, "width": width, "height": height,
               "rotation": rotation, "png": png, "scale": scale})


def main() -> None:
    stdin = sys.stdin.buffer
    request = json.loads(stdin.readline())
    _limit(request["cpu_seconds"], request["memory_bytes"])
    data = stdin.read(request["size"])
    run(request, data, lambda: stdin.readline() == b"\n")


if __name__ == "__main__":  # pragma: no cover - run as a module by ocr.py
    main()
