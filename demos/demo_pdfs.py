"""Build the sample PDFs the demo scenarios operate on.

Kept deterministic and dependency-free beyond reportlab (already a runtime
dependency) so every recording starts from identical input.
"""
from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

INK = colors.HexColor("#1f2933")
MUTED = colors.HexColor("#616e7c")
ACCENT = colors.HexColor("#2f6fde")
RULE = colors.HexColor("#d9e2ec")

REPORT_PAGES = [
    ("Q3 Field Report", "Summary", [
        "Deliveries rose 18% quarter over quarter across all regions.",
        "Average route time fell from 41 to 36 minutes.",
        "Two new depots opened in Leeds and Porto.",
    ]),
    ("Regional Breakdown", "North, South, East, West", [
        "North: 12,400 deliveries, 98.1% on time.",
        "South: 10,950 deliveries, 97.4% on time.",
        "East: 8,210 deliveries, 96.9% on time.",
        "West: 9,775 deliveries, 98.6% on time.",
    ]),
    ("Next Quarter", "Priorities", [
        "Pilot electric vans on three urban routes.",
        "Cut failed first-attempt deliveries below 2%.",
        "Roll out live tracking to all customers.",
    ]),
]


def _bars(c: canvas.Canvas, x: float, y: float, values: list[int]) -> None:
    top = max(values)
    for i, v in enumerate(values):
        h = 120 * v / top
        c.setFillColor(ACCENT if i == len(values) - 1 else RULE)
        c.rect(x + i * 46, y, 30, h, stroke=0, fill=1)


def build_report(path: Path) -> Path:
    """A three-page, letter-size report with a heading, bullets and a chart."""
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setTitle("Q3 Field Report")
    c.setAuthor("Paperboy Demo Co.")
    width, height = letter
    for n, (title, subtitle, bullets) in enumerate(REPORT_PAGES, start=1):
        c.setFillColor(ACCENT)
        c.rect(0, height - 14, width, 14, stroke=0, fill=1)
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 30)
        c.drawString(72, height - 110, title)
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 15)
        c.drawString(72, height - 138, subtitle)
        c.setStrokeColor(RULE)
        c.setLineWidth(1.5)
        c.line(72, height - 158, width - 72, height - 158)
        c.setFillColor(INK)
        c.setFont("Helvetica", 14)
        for i, line in enumerate(bullets):
            c.drawString(90, height - 200 - i * 28, f"•  {line}")
        _bars(c, 90, 180, [42, 55, 61, 58, 72][: 3 + n - 1] + [80])
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 10)
        c.drawString(72, 48, "Paperboy Demo Co. · Internal")
        c.drawRightString(width - 72, 48, f"Page {n} of {len(REPORT_PAGES)}")
        c.showPage()
    c.save()
    return path


BUILDERS = {"report.pdf": build_report}


def build(names: list[str], workspace: Path) -> None:
    """Write each named sample file into *workspace*."""
    for name in names:
        BUILDERS[name](workspace / name)
