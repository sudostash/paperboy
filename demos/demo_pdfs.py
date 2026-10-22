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


SCAN_PAGES = [
    ("Field notes", ["Route 14: two failed first attempts.", "Depot B: van 7 in for service."]),
    None,                                   # a page the scanner fed through empty
    ("Field notes (cont.)", ["Customer feedback: 4.6 / 5 average.", "Next audit: 3 Nov."]),
    "dust",                                 # an empty page with a few specks of dust
    ("Sign-off", ["Reviewed by the regional lead."]),
]


def build_scan(path: Path) -> Path:
    """A five-page 'scan' with a truly blank page 2 and a dusty but empty page 4."""
    import random

    c = canvas.Canvas(str(path), pagesize=letter)
    width, height = letter
    for page in SCAN_PAGES:
        c.setFillColor(colors.HexColor("#fbfaf6"))  # off-white scanner paper
        c.rect(0, 0, width, height, stroke=0, fill=1)
        if page == "dust":
            rnd = random.Random(7)
            c.setFillGray(0.35)
            for _ in range(20):
                c.circle(rnd.uniform(40, width - 40), rnd.uniform(40, height - 40), 0.7,
                         stroke=0, fill=1)
        elif page:
            title, lines = page
            c.setFillColor(INK)
            c.setFont("Helvetica-Bold", 24)
            c.drawString(72, height - 110, title)
            c.setFont("Helvetica", 14)
            for i, line in enumerate(lines):
                c.drawString(72, height - 160 - i * 26, line)
        c.showPage()
    c.save()
    return path


def build_delivery_form(path: Path) -> Path:
    """A one-page delivery request form: text fields, a checkbox, radio buttons, a drop-down."""
    c = canvas.Canvas(str(path), pagesize=letter)
    width, height = letter
    form = c.acroForm
    c.setFillColor(ACCENT)
    c.rect(0, height - 14, width, 14, stroke=0, fill=1)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 26)
    c.drawString(72, height - 100, "Delivery request")
    c.setFont("Helvetica", 12)
    c.setFillColor(MUTED)
    c.drawString(72, height - 124, "Paperboy Demo Co. \u00b7 please complete every field")

    def label(y, text):
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 12)
        c.drawString(72, y + 6, text)

    common = {"x": 200, "width": 300, "height": 24, "fontSize": 12, "borderColor": RULE,
              "fillColor": colors.white, "textColor": INK, "borderWidth": 1}
    label(height - 190, "Full name")
    form.textfield(name="name", y=height - 194, **common)
    label(height - 240, "Address")
    form.textfield(name="address", y=height - 244, **common)
    label(height - 290, "Express")
    form.checkbox(name="express", x=200, y=height - 294, size=20, buttonStyle="check",
                  borderColor=RULE, fillColor=colors.white, textColor=ACCENT, checked=False)
    label(height - 340, "Parcel size")
    for i, size in enumerate(["small", "medium", "large"]):
        form.radio(name="size", value=size, x=200 + i * 100, y=height - 344, size=20,
                   borderColor=RULE, fillColor=colors.white, textColor=ACCENT, selected=False)
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 11)
        c.drawString(226 + i * 100, height - 338, size)
    label(height - 390, "Depot")
    form.choice(name="depot", value="Choose...",
                options=["Choose...", "Leeds", "Porto", "Lyon"],
                **{**common, "y": height - 394, "width": 160})
    c.showPage()
    c.save()
    return path


def build_proposal(path: Path) -> Path:
    """A client proposal still carrying what it shouldn't: an author and software in
    its metadata, a script that runs on open, an internal attachment, a reviewer's
    comment, and an earlier saved version with a revealing title."""
    import re

    import pikepdf

    c = canvas.Canvas(str(path), pagesize=letter)
    c.setTitle("Delivery services proposal")
    c.setAuthor("Jane Smith (Sales)")
    c.setCreator("Microsoft Word for Mac")
    width, height = letter
    for heading, line in [("Delivery services proposal", "Prepared for Northwind Traders"),
                          ("Pricing", "Fixed monthly fee, see schedule A.")]:
        c.setFillColor(ACCENT)
        c.rect(0, height - 14, width, 14, stroke=0, fill=1)
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 26)
        c.drawString(72, height - 110, heading)
        c.setFont("Helvetica", 14)
        c.setFillColor(MUTED)
        c.drawString(72, height - 140, line)
        c.showPage()
    c.save()
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        pdf.Root.OpenAction = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Action, S=pikepdf.Name.JavaScript,
            JS=pikepdf.String("app.alert('Tracking open');")))
        spreadsheet = pdf.make_indirect(pikepdf.Stream(pdf, b"cost,margin\n100,40%"))
        filespec = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Filespec, F=pikepdf.String("pricing-internal.xlsx"),
            EF=pikepdf.Dictionary(F=spreadsheet)))
        pdf.Root.Names = pikepdf.Dictionary(EmbeddedFiles=pikepdf.Dictionary(
            Names=pikepdf.Array([pikepdf.String("pricing-internal.xlsx"), filespec])))
        page.obj.Annots = pdf.make_indirect(pikepdf.Array([pdf.make_indirect(
            pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Text,
                               Rect=pikepdf.Array([500, 700, 520, 720]),
                               T=pikepdf.String("Jane Smith"),
                               Contents=pikepdf.String("Don't mention the 40% margin")))]))
        pdf.save(path)
    # Make the first save's title revealing, then append an incremental update with
    # the final metadata: the revealing title stays in the file as an earlier version.
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        final = {k: v for k, v in pdf.trailer.Info.items()}
        pdf.trailer.Info = pdf.make_indirect(pikepdf.Dictionary(
            Title=pikepdf.String("Proposal v1 - internal margin 40%")))
        pdf.save(path, object_stream_mode=pikepdf.ObjectStreamMode.disable,
                 compress_streams=False)
    data = path.read_bytes()
    with pikepdf.open(path) as pdf:
        root, size = pdf.Root.objgen[0], int(pdf.trailer.Size)
        info = pikepdf.Dictionary(final).unparse()
    prev = int(re.findall(rb"startxref\s+(\d+)", data)[-1])
    body = b"\n%d 0 obj\n%s\nendobj\n" % (size, info)
    xref_at = len(data) + len(body)
    path.write_bytes(data + body + (
        b"xref\n%d 1\n%010d 00000 n \ntrailer\n<< /Size %d /Root %d 0 R /Info %d 0 R "
        b"/Prev %d >>\nstartxref\n%d\n%%%%EOF\n" % (size, len(data) + 1, size + 1, root, size,
                                                     prev, xref_at)))
    return path


def build_invoice(path: Path) -> Path:
    """An "invoice" from an unknown sender that does what phishing PDFs do: runs a
    script as soon as it opens, carries a program disguised as a PDF attachment, and
    links out to a look-alike payment site. The attachment is a harmless stub that
    only starts like a Windows program; nothing here can run."""
    import pikepdf

    c = canvas.Canvas(str(path), pagesize=letter)
    c.setTitle("Invoice 20931")
    width, height = letter
    c.setFillColor(ACCENT)
    c.rect(0, height - 14, width, 14, stroke=0, fill=1)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 26)
    c.drawString(72, height - 110, "Invoice #20931 - payment overdue")
    c.setFont("Helvetica", 14)
    c.setFillColor(MUTED)
    c.drawString(72, height - 140, "Open the attached statement to avoid a late fee.")
    c.setFillColor(ACCENT)
    c.drawString(72, height - 180, "Pay now: secure-billing-portal.example")
    c.showPage()
    c.save()
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        pdf.Root.OpenAction = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Action, S=pikepdf.Name.JavaScript,
            JS=pikepdf.String("this.exportDataObject({cName: 'statement.pdf.exe', nLaunch: 2});")))
        stub = pdf.make_indirect(pikepdf.Stream(pdf, b"MZ" + b"\0" * 62 + b"demo stub"))
        filespec = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Filespec, F=pikepdf.String("statement.pdf.exe"),
            EF=pikepdf.Dictionary(F=stub)))
        pdf.Root.Names = pikepdf.Dictionary(EmbeddedFiles=pikepdf.Dictionary(
            Names=pikepdf.Array([pikepdf.String("statement.pdf.exe"), filespec])))
        page.obj.Annots = pdf.make_indirect(pikepdf.Array([pdf.make_indirect(
            pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Link,
                               Rect=pikepdf.Array([72, height - 186, 360, height - 166]),
                               Border=pikepdf.Array([0, 0, 0]),
                               A=pikepdf.Dictionary(
                                   S=pikepdf.Name.URI,
                                   URI=pikepdf.String("https://secure-billing-portal.example/pay"))))]))
        pdf.save(path)
    return path


def _photo(color: str, accent: str, size=(480, 320)) -> bytes:
    """A simple product "photo": a backdrop, a shape and a floor shadow."""
    import io

    from PIL import Image, ImageDraw

    img = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(img)
    w, h = size
    draw.ellipse([w * 0.25, h * 0.78, w * 0.75, h * 0.9], fill="#c9d1db")
    draw.rounded_rectangle([w * 0.3, h * 0.2, w * 0.7, h * 0.8], radius=30, fill=accent)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return buf.getvalue()


def build_brochure(path: Path) -> Path:
    """A two-page product brochure with two photos, a small logo, and two attached
    files: a spec sheet and a price list."""
    import io

    import pikepdf
    from reportlab.lib.utils import ImageReader

    c = canvas.Canvas(str(path), pagesize=letter)
    c.setTitle("Courier bags 2026")
    width, height = letter
    logo = io.BytesIO()
    from PIL import Image
    Image.new("RGB", (24, 24), "#2f6fde").save(logo, "PNG")
    for heading, line, photo in [
        ("Metro courier bag", "Waterproof, 28 L, reflective strap.", _photo("#eef2f7", "#2f6fde")),
        ("Cargo messenger", "Expands to 40 L for bulk rounds.", _photo("#f7f1ea", "#d9822b")),
    ]:
        c.setFillColor(ACCENT)
        c.rect(0, height - 14, width, 14, stroke=0, fill=1)
        c.drawImage(ImageReader(io.BytesIO(logo.getvalue())), width - 96, height - 70, 24, 24)
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 26)
        c.drawString(72, height - 110, heading)
        c.setFont("Helvetica", 14)
        c.setFillColor(MUTED)
        c.drawString(72, height - 140, line)
        c.drawImage(ImageReader(io.BytesIO(photo)), 72, height - 480, 360, 240)
        c.showPage()
    c.save()
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.attachments["spec-sheet.csv"] = pikepdf.AttachedFileSpec(
            pdf, b"model,volume_l,weight_kg\nMetro,28,1.1\nCargo,40,1.6\n",
            description="Technical specifications", mime_type="text/csv")
        pdf.attachments["price-list.txt"] = pikepdf.AttachedFileSpec(
            pdf, b"Metro courier bag  EUR 129\nCargo messenger   EUR 159\n",
            description="2026 prices", mime_type="text/plain")
        pdf.save(path)
    return path


BUILDERS = {"report.pdf": build_report, "scan.pdf": build_scan,
            "delivery-form.pdf": build_delivery_form, "proposal.pdf": build_proposal,
            "invoice.pdf": build_invoice, "brochure.pdf": build_brochure}


def build(names: list[str], workspace: Path) -> None:
    """Write each named sample file into *workspace*."""
    for name in names:
        BUILDERS[name](workspace / name)
