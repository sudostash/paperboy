"""scan_pdf: a risk report for a PDF you didn't make.

It reports what it finds and grades it; it never calls a file safe, because a
scan can't prove that: attacks on viewers through malformed fonts, images or
data may look like ordinary content. Optional ClamAV adds known-malware
signatures.
"""
from __future__ import annotations

import base64
import binascii
import math
import re
import subprocess
import tempfile
import zlib
from pathlib import Path
from urllib.parse import urlsplit

import pikepdf

from ..config import Settings
from ..errors import PdfUserError
from ..results import ScanResult
from .base import open_pdf
from .sanitize import RISKY, _action_kind, _dictionaries, _is_annotation, _subtype, _threat

LIMITS = (
    "A scan reports what it finds; it can't prove a file is safe. Attacks on PDF viewers "
    "through malformed fonts, images or data may not be detected, nor attachments in "
    "unusual encodings, nor files beyond ClamAV's size limits. For files you don't trust, "
    "use sanitize_pdf, and open them in an up-to-date or sandboxed viewer."
)
MAGIC_BYTES = 4096            # how much of an attachment is decoded to identify it
OBJSTM_BUDGET = 64 * 2**20    # object-stream bytes decoded per file, against bombs
XFA_BUDGET = 16 * 2**20       # XFA form bytes decoded per file
MAX_ACTION_STEPS = 100_000    # action-chain links followed per file
MAX_NESTED_DEPTH = 2          # PDFs inside PDFs inside the scanned PDF
MAX_NESTED = 10               # attached PDFs scanned per file
TRAILING_SLACK = 32           # bytes after %%EOF that tools commonly leave behind
MB = 2**20
RICH_MEDIA = {"/RichMedia", "/3D", "/Movie"}
# Actions that run without a click but only move around the document.
HARMLESS_AUTOMATIC = {"/GoTo", "/GoToDp", "/Thread", "/SetOCGState", "/Trans", "/Hide"}
CLICKS = {"/D", "/U"}  # an annotation's mouse down and up: a click, not automatic
# Names attackers disguise with #xx escapes to slip past simple scanners.
SENSITIVE_NAMES = {"JavaScript", "JS", "OpenAction", "AA", "Launch", "EmbeddedFile",
                   "EmbeddedFiles", "RichMedia", "XFA", "JBIG2Decode", "AcroForm", "URI",
                   "SubmitForm", "ImportData", "GoToR", "GoToE", "Rendition", "ObjStm",
                   "FlateDecode", "Names", "Action", "S"}
_ESCAPED_NAME = re.compile(rb"/([^\s/<>\[\]()%{}]*#[0-9A-Fa-f]{2}[^\s/<>\[\]()%{}]*)")
# Plainly written keys, counted in the raw bytes to compare with what the parser reached.
_RAW_KEY = re.compile(rb"/(JS|Launch)(?=[\s/<>\[\]()%{}]|$)")
_BYTESIO = re.compile(r"<_io\.BytesIO object at 0x[0-9a-fA-F]+>")
SIGNATURES = [  # (leading bytes, what it is, can it run)
    (b"MZ", "Windows program", True),
    (b"\x7fELF", "Linux program", True),
    (b"\xcf\xfa\xed\xfe", "macOS program", True),
    (b"\xce\xfa\xed\xfe", "macOS program", True),
    (b"\xfe\xed\xfa\xcf", "macOS program", True),
    (b"\xfe\xed\xfa\xce", "macOS program", True),
    (b"\xca\xfe\xba\xbe", "macOS program or Java class", True),
    (b"#!", "script", True),
    (b"L\x00\x00\x00\x01\x14\x02\x00", "Windows shortcut", True),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "Office document (may contain macros)", False),
    (b"PK\x03\x04", "ZIP archive (or modern Office file)", False),
    (b"%PDF-", "PDF document", False),
    (b"{\\rtf", "RTF document", False),
]
# Opening any of these can run code: programs, scripts, shortcuts, disk images that
# mount programs, Excel add-ins, help files, web pages and macro-enabled Office files.
RUNNABLE_EXTENSIONS = {"exe", "dll", "scr", "com", "bat", "cmd", "ps1", "vbs", "vbe", "js",
                       "jse", "wsf", "wsh", "hta", "msi", "jar", "lnk", "sh", "app", "apk",
                       "pif", "cpl", "reg", "iso", "img", "vhd", "vhdx", "xll", "one", "chm",
                       "url", "svg", "html", "htm", "msc", "appx", "msix", "docm", "dotm",
                       "xlsm", "xltm", "xlam", "pptm", "potm", "ppam"}
DOCUMENT_EXTENSIONS = {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "rtf",
                       "jpg", "jpeg", "png", "gif", "csv", "zip"}


def _indicator(code: str, severity: str, count: int, detail: str) -> dict:
    return {"code": code, "severity": severity, "count": count, "detail": detail}


def _verdict(indicators: list[dict]) -> str:
    severities = {i["severity"] for i in indicators}
    return ("suspicious" if "high" in severities
            else "active_content" if "warning" in severities else "no_active_content_found")


# --- decoding, bounded ---------------------------------------------------------------------


def _decode(stream, limit: int) -> bytes | None:
    """A stream's data through its general filters, cut at *limit* bytes so a small
    compressed stream can't expand into gigabytes. None if it can't be decoded here
    (image filters, predictors, damaged data)."""
    filters = stream.get("/Filter")
    filters = list(filters) if isinstance(filters, pikepdf.Array) else \
        ([] if filters is None else [filters])
    parms = stream.get("/DecodeParms")
    parms = list(parms) if isinstance(parms, pikepdf.Array) else [parms]
    if len(filters) > 4 or any(isinstance(p, pikepdf.Dictionary) and p.get("/Predictor", 1) != 1
                               for p in parms):
        return None
    data = stream.read_raw_bytes()
    try:
        for name in (str(f) for f in filters):
            if name == "/FlateDecode":
                data = zlib.decompressobj().decompress(data, limit)
            elif name == "/ASCIIHexDecode":
                digits = re.sub(rb"[^0-9A-Fa-f]", b"", data.split(b">")[0])[:2 * limit]
                data = binascii.unhexlify(digits + b"0" * (len(digits) % 2))
            elif name == "/ASCII85Decode":
                data = base64.a85decode(data.strip().removesuffix(b"~>"), adobe=False)
            else:
                return None
    except (zlib.error, binascii.Error, ValueError):
        return None
    return data[:limit]


# --- attachments -------------------------------------------------------------------------


def _identify(head: bytes | None) -> tuple[str, bool]:
    if head is None:
        return "unknown", False
    for magic, kind, runs in SIGNATURES:
        if head.startswith(magic):
            return kind, runs
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        return "binary data", False
    return ("text", False) if all(c.isprintable() or c.isspace() for c in text) \
        else ("binary data", False)


def _attachments(dicts: list) -> list[tuple[dict, pikepdf.Stream]]:
    """Each embedded file's report, with its stream."""
    found = []
    for spec in dicts:
        ef = spec.get("/EF") if isinstance(spec, pikepdf.Dictionary) else None
        if not isinstance(ef, pikepdf.Dictionary):
            continue
        keys = ("/UF", "/F", "/DOS", "/Mac", "/Unix")
        stream = next((ef[k] for k in keys if isinstance(ef.get(k), pikepdf.Stream)), None)
        if stream is None:
            continue
        name = next((str(spec[k]) for k in keys if isinstance(spec.get(k), pikepdf.String)),
                    "unnamed")
        params = stream.get("/Params")
        size = params.get("/Size") if isinstance(params, pikepdf.Dictionary) else None
        kind, runs = _identify(_decode(stream, MAGIC_BYTES))
        # Windows drops trailing dots and spaces, so "invoice.exe." runs as invoice.exe.
        base = re.split(r"[/\\]", name.lower())[-1].rstrip(". ")
        parts = base.split(".")
        ext = parts[-1] if len(parts) > 1 else ""
        runnable_name = ext in RUNNABLE_EXTENSIONS
        double = len(parts) > 2 and parts[-2] in DOCUMENT_EXTENSIONS and runnable_name
        found.append(({
            "name": name,
            "size": size if isinstance(size, int) and not isinstance(size, bool) else None,
            "detected_type": kind,
            "executable": runs or runnable_name,
            # Disguised: "invoice.pdf.exe", or a program named like a document.
            "extension_mismatch": double or (runs and bool(ext) and not runnable_name),
        }, stream))
    return found


def _nested(attachments: list, settings: Settings, depth: int) -> list[dict]:
    """Scan attached PDFs: a PDF can open one it carries, and run what's inside."""
    active, unscanned, worst = [], [], "warning"
    pdfs = [(info, stream) for info, stream in attachments
            if info["detected_type"] == "PDF document"]
    unscanned += [info["name"] for info, _ in pdfs[MAX_NESTED:]]
    for info, stream in pdfs[:MAX_NESTED]:
        data = _decode(stream, settings.max_file_bytes + 1)
        if data is None or len(data) > settings.max_file_bytes:
            unscanned.append(info["name"])
            continue
        try:
            with open_pdf(data, settings, None) as inner:
                inner_found, _ = _analyse(inner, data, settings, depth + 1)
        except (PdfUserError, pikepdf.PdfError):  # damaged or locked: can't be checked
            unscanned.append(info["name"])
            continue
        serious = [i for i in inner_found if i["severity"] != "info"]
        if serious:
            if any(i["severity"] == "high" for i in serious):
                worst = "high"
            active.append(f"{info['name']} ({', '.join(i['code'] for i in serious)})")
    found = []
    if active:
        found.append(_indicator("attachment_active_content", worst, len(active),
                                "attached PDFs with active content: " + "; ".join(active[:5])))
    if unscanned:
        found.append(_indicator("attachment_not_scanned", "warning", len(unscanned),
                                "attached PDFs that couldn't be scanned: "
                                + ", ".join(unscanned[:5])))
    return found


# --- actions -----------------------------------------------------------------------------


def _scripted(d) -> bool:
    """A dictionary that carries JavaScript, whatever kind of action it is (a Rendition
    action's /JS runs too)."""
    return _threat(d) == "/JavaScript" or (isinstance(d, pikepdf.Dictionary) and "/JS" in d)


class _Actions:
    """Follows action chains (/Next) with one step budget for the whole file, so many
    triggers sharing one long chain can't make the scan run for minutes."""

    def __init__(self) -> None:
        self.steps = 0
        self.exhausted = False

    def kinds(self, action) -> set[str]:
        """The action kinds in this action and its /Next chain; "/JavaScript" for any
        script."""
        found, seen, stack = set(), set(), [action]
        while stack:
            if self.steps >= MAX_ACTION_STEPS:
                self.exhausted = True
                break
            self.steps += 1
            a = stack.pop()
            if isinstance(a, pikepdf.Array):
                stack.extend(a)
                continue
            if not isinstance(a, pikepdf.Dictionary):
                continue
            if a.is_indirect:
                if a.objgen in seen:
                    continue
                seen.add(a.objgen)
            if _scripted(a):
                found.add("/JavaScript")
            kind = _action_kind(a)
            if kind is not None:
                found.add(kind)
            stack.append(a.get("/Next"))
        return found


def _name_tree_values(tree) -> list:
    """The values of a name tree (here, document-level scripts), cycle-safe."""
    values, seen, stack = [], set(), [tree]
    while stack and len(seen) < MAX_ACTION_STEPS:
        node = stack.pop()
        if not isinstance(node, pikepdf.Dictionary):
            continue
        if node.is_indirect:
            if node.objgen in seen:
                continue
            seen.add(node.objgen)
        names = node.get("/Names")
        if isinstance(names, pikepdf.Array):
            values += [names[i] for i in range(1, len(names), 2)]
        kids = node.get("/Kids")
        if isinstance(kids, pikepdf.Array):
            stack.extend(kids)
    return values


def _automatic(pdf: pikepdf.Pdf, dicts: list,
               walker: _Actions) -> list[tuple[set[str], bool]]:
    """What runs on its own: the open action, document-level scripts (run when the file
    opens) and event triggers (/AA) on the document, pages, fields and annotations.
    One (action kinds, needs a click) pair per trigger."""
    triggered = [(walker.kinds(pdf.Root.get("/OpenAction")), False)]
    names = pdf.Root.get("/Names")
    if isinstance(names, pikepdf.Dictionary):
        triggered += [(walker.kinds(v) | {"/JavaScript"}, False)
                      for v in _name_tree_values(names.get("/JavaScript"))]
    for d in dicts:
        events = d.get("/AA") if isinstance(d, pikepdf.Dictionary) else None
        if isinstance(events, pikepdf.Dictionary):
            triggered += [(walker.kinds(action), event in CLICKS)
                          for event, action in events.items()]
    return [(kinds, click) for kinds, click in triggered if kinds]


# --- structure ----------------------------------------------------------------------------------


def _object_streams(pdf: pikepdf.Pdf) -> list[bytes]:
    """Object streams decoded, within one budget per file; other filter chains are skipped
    (and reported as unusual chains)."""
    decoded, budget = [], OBJSTM_BUDGET
    for obj in pdf.objects:
        if budget <= 0:
            break
        if isinstance(obj, pikepdf.Stream) and obj.get("/Type") == pikepdf.Name.ObjStm:
            data = _decode(obj, budget)
            if data:
                decoded.append(data)
                budget -= len(data)
    return decoded


def _escaped_names(sources: list[bytes]) -> list[str]:
    """Sensitive names written with #xx escapes."""
    hits = set()
    for source in sources:
        for match in _ESCAPED_NAME.finditer(source):
            decoded = re.sub(rb"#([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]),
                             match.group(1)).decode("latin-1")
            if decoded in SENSITIVE_NAMES:
                hits.add(decoded)
    return sorted(hits)


def _filter_chains(dicts: list) -> int:
    unusual = 0
    for d in dicts:
        filters = d.get("/Filter") if isinstance(d, pikepdf.Stream) else None
        if isinstance(filters, pikepdf.Array):
            names = [str(f) for f in filters]
            if len(names) > 2 or len(set(names)) < len(names):
                unusual += 1
    return unusual


def _hidden_after_end(data: bytes) -> int:
    """Bytes after the end of the file, including those slipped in before a final
    startxref that points back to an earlier cross-reference table."""
    last = data.rfind(b"startxref")
    # Measured from the end marker that closes the last revision, so a second, fake
    # %%EOF after the payload doesn't hide it.
    end = data.find(b"%%EOF", max(last, 0))
    trailing = len(data[end + 5:].strip()) if end >= 0 else 0
    match = re.match(rb"startxref\s+(\d+)", data[last:last + 40]) if last >= 0 else None
    if match and int(match.group(1)) < last:
        inner = data.rfind(b"%%EOF", int(match.group(1)), last)
        hidden = data[inner + 5:last] if inner >= 0 else b""
        # A linearized file also ends an early section with %%EOF, but its main
        # cross-reference table follows; bytes with no structure at all were slipped in.
        if not any(token in hidden for token in (b"xref", b"trailer", b"endobj")):
            trailing += len(hidden.strip())
    return trailing


def _structure_indicators(pdf: pikepdf.Pdf, data: bytes, dicts: list) -> list[dict]:
    found = []
    header = data.find(b"%PDF-")
    if header > 0:
        found.append(_indicator("data_before_header", "warning", header,
                                f"{header} bytes before the PDF header: the file may also be "
                                "another kind of file (a polyglot)"))
    trailing = _hidden_after_end(data)
    if trailing > TRAILING_SLACK:
        found.append(_indicator("data_after_end", "warning", trailing,
                                f"{trailing} bytes after the end of the PDF"))
    sources = [data, *_object_streams(pdf)]
    escaped = _escaped_names(sources)
    if escaped:
        found.append(_indicator("obfuscated_names", "high", len(escaped),
                                "names disguised with hex escapes: " + ", ".join(escaped)))
    # Scripts or launches in the bytes that the document's structure doesn't reach: an
    # earlier version, or objects another reader may pick up where this parser didn't.
    raw = {"JS": 0, "Launch": 0}
    for source in sources:
        for match in _RAW_KEY.finditer(source):
            raw[match.group(1).decode()] += 1
    reached = {"JS": sum(isinstance(d, pikepdf.Dictionary) and "/JS" in d for d in dicts),
               "Launch": sum(_action_kind(d) == "/Launch" for d in dicts)}
    unreached = sum(max(0, raw[k] - reached[k]) for k in raw)
    if unreached:
        found.append(_indicator("unreferenced_active_content", "warning", unreached,
                                "scripts or launch actions in the file that the document "
                                "doesn't use: an earlier version, or aimed at another reader"))
    chains = _filter_chains(dicts)
    if chains:
        found.append(_indicator("unusual_filter_chains", "warning", chains,
                                "streams compressed in unusual layers, which can hide content"))
    warnings = list(pdf.get_warnings())
    if warnings:
        found.append(_indicator("damaged_structure", "info", len(warnings),
                                "; ".join(_BYTESIO.sub("file", str(w)) for w in warnings[:3])))
    return found


def _xfa_scripts(acro) -> bool:
    xfa = acro.get("/XFA")
    parts = list(xfa) if isinstance(xfa, pikepdf.Array) else [xfa]
    budget = XFA_BUDGET
    for part in parts:
        if isinstance(part, pikepdf.Stream) and budget > 0:
            data = _decode(part, budget) or b""
            budget -= len(data)
            if b"<script" in data.lower():
                return True
    return False


def _link_label(uri: str) -> str:
    try:
        parts = urlsplit(uri)
    except ValueError:
        return "an unreadable link"
    if parts.hostname:
        return parts.hostname
    return f"{parts.scheme}: links" if parts.scheme else "an unreadable link"


def _content_indicators(pdf: pikepdf.Pdf, dicts: list, files: list[dict]) -> list[dict]:
    found = []
    walker = _Actions()
    automatic = _automatic(pdf, dicts, walker)
    on_open = sum("/JavaScript" in kinds for kinds, _ in automatic)
    if on_open:
        found.append(_indicator("javascript_on_open", "high", on_open,
                                "JavaScript that runs when the file opens or on page events"))
    unclicked = [kinds - HARMLESS_AUTOMATIC - {"/JavaScript"}
                 for kinds, click in automatic if not click]
    others = sorted(set().union(*unclicked))
    if others:
        found.append(_indicator("automatic_actions", "warning", sum(map(bool, unclicked)),
                                "actions that run without a click: "
                                + ", ".join(k.lstrip("/") for k in others)))
    if walker.exhausted:
        found.append(_indicator("action_chains_too_long", "high", walker.steps,
                                "chains of actions too long to check completely"))
    scripts = sum(_scripted(d) for d in dicts)
    if scripts:
        found.append(_indicator("javascript", "warning", scripts, "JavaScript actions"))
    threats = [_threat(d) for d in dicts]
    launches = threats.count("/Launch")
    if launches:
        found.append(_indicator("launch_actions", "high", launches,
                                "actions that start programs or open files on the computer"))
    risky = sum(t in RISKY - {"/Launch"} for t in threats)
    if risky:
        found.append(_indicator("risky_actions", "warning", risky,
                                "actions that submit data or open other files or media"))
    acro = pdf.Root.get("/AcroForm")
    if isinstance(acro, pikepdf.Dictionary) and "/XFA" in acro:
        if _xfa_scripts(acro):
            found.append(_indicator("xfa_form", "high", 1,
                                    "an XFA form with scripts, which can run when it opens"))
        else:
            found.append(_indicator("xfa_form", "warning", 1,
                                    "an XFA form, which can carry its own scripts"))
    media = sum(_is_annotation(d) and _subtype(d) in RICH_MEDIA for d in dicts)
    if media:
        found.append(_indicator("rich_media", "warning", media,
                                "Flash, 3D or multimedia content, historically exploited"))
    jbig2 = sum(isinstance(d, pikepdf.Stream) and "JBIG2Decode" in str(d.get("/Filter", ""))
                for d in dicts)
    if jbig2:
        found.append(_indicator("jbig2_images", "info", jbig2,
                                "JBIG2 images: common in scans, also a known exploit route"))
    if files:
        found.append(_indicator("attachments", "warning", len(files),
                                "embedded files: " + ", ".join(f["name"] for f in files[:5])))
        runnable = [f["name"] for f in files if f["executable"]]
        if runnable:
            found.append(_indicator("executable_attachments", "high", len(runnable),
                                    "attachments that can run: " + ", ".join(runnable[:5])))
        disguised = [f["name"] for f in files if f["extension_mismatch"]]
        if disguised:
            found.append(_indicator("disguised_attachments", "high", len(disguised),
                                    "names that hide what the file is: "
                                    + ", ".join(disguised[:5])))
    uris = [str(d.get("/URI")) for d in dicts
            if _action_kind(d) == "/URI" and isinstance(d.get("/URI"), pikepdf.String)]
    if uris:
        labels = sorted({_link_label(u) for u in uris})
        found.append(_indicator("external_links", "info", len(uris),
                                "links to: " + ", ".join(labels[:10])))
    return found


def _analyse(pdf: pikepdf.Pdf, data: bytes, settings: Settings,
             depth: int = 0) -> tuple[list[dict], list[dict]]:
    """The indicators and attachments of an open PDF, attached PDFs included."""
    dicts = _dictionaries(pdf)
    attachments = _attachments(dicts)
    files = [info for info, _ in attachments]
    indicators = _content_indicators(pdf, dicts, files) + \
        _structure_indicators(pdf, data, dicts)
    if depth < MAX_NESTED_DEPTH:
        indicators += _nested(attachments, settings, depth)
    return indicators, files


# --- antivirus -----------------------------------------------------------------------------------


def _clamscan(settings: Settings, files: list[Path], tmp: str) -> dict:
    result = {"available": True, "engine": None, "database": None, "infected": None,
              "findings": [], "error": None}
    try:
        version = subprocess.run([settings.clamscan_path, "--version"], capture_output=True,
                                 text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        result["error"] = f"ClamAV could not start: {exc}"
        return result
    engine, _, database = version.partition("/")  # "ClamAV 1.4.1/27450/Mon Oct ..."
    number, _, date = database.partition("/")
    result["engine"] = engine or None
    result["database"] = f"{number} ({date})" if number and date else (number or None)
    # ClamAV's defaults (25 MB per file, 100 MB scanned) would skip larger files and still
    # call them clean; raise them past our own cap and have it say when it stops short.
    size = min(math.ceil(settings.max_file_bytes / MB) + 1, 4000)
    limits = [f"--max-filesize={size}M", f"--max-scansize={min(size * 4, 4000)}M",
              "--alert-exceeds-max=yes"]
    infected, errors = False, []
    for file in files:
        try:
            proc = subprocess.run([settings.clamscan_path, "--no-summary", *limits, str(file)],
                                  capture_output=True, text=True,
                                  timeout=settings.clamscan_timeout)
        except subprocess.TimeoutExpired:
            errors.append(f"ClamAV timed out after {settings.clamscan_timeout}s")
            continue
        if proc.returncode == 1:  # 0 clean, 1 found, 2 error
            names = [line.rsplit(": ", 1)[-1].removesuffix(" FOUND")
                     for line in proc.stdout.splitlines() if line.endswith(" FOUND")]
            if any(n.startswith("Heuristics.Limits.Exceeded") for n in names):
                errors.append("the file is too large or too deeply packed for ClamAV "
                              "to scan completely")
            names = [n for n in names if not n.startswith("Heuristics.Limits.Exceeded")]
            infected = infected or bool(names)
            result["findings"] += names
        elif proc.returncode != 0:
            message = (proc.stderr or proc.stdout).strip().replace(tmp, "<temp>")
            errors.append(message[-300:] or "ClamAV failed")
    result["infected"] = True if infected else (None if errors else False)
    result["error"] = "; ".join(errors) or None
    return result


# --- the scan ------------------------------------------------------------------------------------


def scan_pdf(path: Path, settings: Settings, password: str | None = None,
             antivirus: bool = True) -> ScanResult:
    """Grade what a PDF contains: nothing active found, active content, or suspicious."""
    data = path.read_bytes()
    with open_pdf(data, settings, password) as pdf:
        indicators, files = _analyse(pdf, data, settings)
        av = {"available": False, "engine": None, "database": None, "infected": None,
              "findings": [], "error": None}
        if antivirus and settings.clamscan_path:
            # ClamAV scans the same bytes checked above (not the path, which could change),
            # and a decrypted copy of an encrypted file, since it can't see inside one. The
            # copies stay in a private temporary folder that is deleted afterwards.
            with tempfile.TemporaryDirectory() as tmp:
                original = Path(tmp) / "input.pdf"
                original.write_bytes(data)
                targets, failed = [original], None
                if pdf.is_encrypted:
                    plain = Path(tmp) / "decrypted.pdf"
                    try:
                        pdf.save(plain)
                        targets.append(plain)
                    except (pikepdf.PdfError, OSError):
                        failed = "couldn't decrypt a copy for ClamAV; only the encrypted " \
                                 "file was scanned"
                av = _clamscan(settings, targets, tmp)
                if failed:
                    av["error"] = "; ".join(filter(None, [av["error"], failed]))
                    if av["infected"] is False:
                        av["infected"] = None
    if av["infected"]:
        indicators.insert(0, _indicator("antivirus_detection", "high", len(av["findings"]),
                                        "ClamAV: " + ", ".join(av["findings"])))
    return {"path": str(path), "verdict": _verdict(indicators), "indicators": indicators,
            "embedded_files": files, "antivirus": av, "limits": LIMITS}
