"""Shared engine helpers: opening, limit enforcement, metadata copy."""
from __future__ import annotations

import io
import re
import secrets
import subprocess
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

import pikepdf
import pikepdf.codec  # noqa: F401  registers the "pdfdoc" codec

from ..config import Settings
from ..errors import PdfUserError


def open_with_password(source: str | io.BytesIO, password: str | None) -> pikepdf.Pdf:
    """``pikepdf.open`` that also accepts non-ASCII passwords for RC4/AES-128 files.

    Revisions 2-4 store passwords in PDFDocEncoding, but pikepdf sends a str as
    UTF-8, which only matches AES-256 (revision 6). On a mismatch, retry with the
    PDFDocEncoding bytes. Raises pikepdf.PasswordError if neither form opens it.
    """
    try:
        return pikepdf.open(source, password=password or "")
    except pikepdf.PasswordError:
        if not password or password.isascii():
            raise
        try:
            legacy = password.encode("pdfdoc")
        except UnicodeEncodeError:  # not expressible in PDFDocEncoding at all
            raise pikepdf.PasswordError("invalid password") from None
        if isinstance(source, io.BytesIO):
            source.seek(0)
        pdf = pikepdf.open(source, password=legacy)
        if pdf.encryption.R >= 5:  # AES-256 uses UTF-8; don't accept a mis-encoded alias
            pdf.close()
            raise pikepdf.PasswordError("invalid password") from None
        return pdf


def require_copy_permission(pdf: pikepdf.Pdf, action: str) -> None:
    """Refuse *action* (getting content out) when the PDF forbids copying.

    Only the owner password lifts that restriction; the user ("open") password,
    or no password for an owner-only file, is not enough.
    """
    if pdf.is_encrypted and not pdf.owner_password_matched and not pdf.allow.extract:
        raise PdfUserError(
            f"This PDF does not allow copying its content; {action} needs its owner password"
        )


@contextmanager
def open_pdf(path: Path | bytes, settings: Settings, password: str | None = None):
    """Open a PDF (a path, or its bytes) with pikepdf, enforcing the page-count cap.

    Raises PdfUserError for a missing/incorrect password or an oversized document.
    """
    source = io.BytesIO(path) if isinstance(path, bytes) else str(path)
    try:
        pdf = open_with_password(source, password)
    except pikepdf.PasswordError:
        if password:
            raise PdfUserError("Incorrect password") from None
        raise PdfUserError("This PDF is password protected; supply its password") from None
    except pikepdf.PdfError as exc:
        # qpdf names an in-memory file by its Python repr, a memory address; drop it.
        detail = re.sub(r"^stream <_io\.BytesIO object at 0x[0-9a-fA-F]+>:?\s*", "",
                        str(exc))
        raise PdfUserError(f"Could not open PDF: {detail}") from exc
    try:
        n = len(pdf.pages)
        if n > settings.max_pages:
            raise PdfUserError(
                f"Document has {n} pages, over the {settings.max_pages} page limit"
            )
        yield pdf
    finally:
        pdf.close()


@dataclass(frozen=True)
class Protection:
    """How an output derived from protected input(s) is protected."""

    encryption: pikepdf.Encryption | bool = False
    user_password_changed: bool = False

    def report(self, result: dict) -> dict:
        """Add what the caller needs to know about the output's protection.

        All three flags are always present (see :mod:`paperboy.results`).
        """
        encrypted = bool(self.encryption)
        result["password_protected"] = encrypted and bool(self.encryption.user)
        # Opens without a password, but keeps its restrictions.
        result["permissions_restricted"] = encrypted and not self.encryption.user
        result["user_password_changed"] = self.user_password_changed
        return result


def inherit_protection(src: pikepdf.Pdf, password: str | None) -> Protection:
    """Protection for an output derived from *src*.

    Outputs of a protected input stay protected (AES-256, same user password and
    permissions); only decrypt_pdf removes protection. Decided from the open
    document alone, never by reopening its file.
    """
    if not src.is_encrypted:
        return Protection()
    if not src.owner_password_matched:
        # The owner password is unknown; a random one keeps the permissions enforceable.
        user, owner, changed = password or "", secrets.token_urlsafe(32), False
    else:  # opened with the owner password: keep it, so it still lifts the restrictions
        owner = password or ""
        known = KNOWN_USER_PASSWORD.get()
        if src.user_password_matched:  # the same password opens and owns it
            user, changed = password or "", False
        elif src.encryption.R <= 4:  # qpdf recovers the user password, in PDFDocEncoding
            user, changed = src.encryption.user_password.decode("pdfdoc"), False
        elif known is not None:  # a pipeline recovered it from the file it started with
            user, changed = known, False
        else:  # AES-256 cannot be recovered: never fall back to "opens without a password"
            user, changed = password or secrets.token_urlsafe(32), True
    return Protection(pikepdf.Encryption(user=user, owner=owner, R=6, allow=src.allow), changed)


# The user password of the document a run_pipeline started from, when known. Each step
# writes AES-256, from which the user password can't be recovered, so later steps opened
# with the owner password need it to keep the original user password.
KNOWN_USER_PASSWORD: ContextVar[str | None] = ContextVar("known_user_password",
                                                         default=None)


def known_user_password(pdf: pikepdf.Pdf, password: str | None) -> str | None:
    """The user password of an open document, if it can be told: the password given if
    it opens the document as a user, or the one qpdf recovers from RC4/AES-128."""
    if not pdf.is_encrypted:
        return None
    if pdf.user_password_matched:
        return password or ""
    if pdf.owner_password_matched and pdf.encryption.R <= 4:
        return pdf.encryption.user_password.decode("pdfdoc")
    return None


def combine_protections(protections: list[Protection]) -> Protection:
    """Protection for a merge: the strictest of its protected inputs.

    Inputs that need different passwords are refused rather than letting one
    input's password (or none) open another input's pages.
    """
    encrypted = [p.encryption for p in protections if p.encryption]
    if not encrypted:
        return Protection()
    users = {e.user for e in encrypted if e.user}
    if len(users) > 1:
        raise PdfUserError(
            "The encrypted inputs have different passwords; decrypt them, or re-encrypt them "
            "with the same password, before merging"
        )
    owners = {e.owner for e in encrypted}
    allow = pikepdf.Permissions(**{
        field: all(getattr(e.allow, field) for e in encrypted)
        for field in pikepdf.Permissions._fields
    })
    return Protection(
        pikepdf.Encryption(
            user=users.pop() if users else "",
            owner=owners.pop() if len(owners) == 1 else secrets.token_urlsafe(32),
            R=6, allow=allow,
        ),
        any(p.user_password_changed for p in protections),
    )


def signature_state(pdf: pikepdf.Pdf) -> tuple[bool, bool]:
    """(digitally signed, has Adobe Reader usage rights).

    Any signature dictionary counts, wherever it is in the file, as does a
    certification (DocMDP) entry. Usage rights (UR3) are a signature too, but one
    that only grants Reader features; callers drop it rather than refuse.
    """
    perms = pdf.Root.get("/Perms")
    perms = perms if isinstance(perms, pikepdf.Dictionary) else None
    ur3 = perms.get("/UR3") if perms is not None else None
    ur3_id = ur3.objgen if isinstance(ur3, pikepdf.Dictionary) and ur3.is_indirect else None
    signed = perms is not None and "/DocMDP" in perms or _signed_field(pdf)
    for obj in pdf.objects:
        if isinstance(obj, pikepdf.Dictionary) and "/ByteRange" in obj and obj.objgen != ur3_id:
            signed = True
            break
    return signed, ur3 is not None


def _signed_field(pdf: pikepdf.Pdf) -> bool:
    """A signature field with a value, even one stored inline (not as its own object)."""
    acro = pdf.Root.get("/AcroForm")
    fields = acro.get("/Fields") if isinstance(acro, pikepdf.Dictionary) else None
    stack = list(fields) if isinstance(fields, pikepdf.Array) else []
    seen: set[tuple[int, int]] = set()
    while stack and len(seen) < 100_000:
        field = stack.pop()
        if not isinstance(field, pikepdf.Dictionary):
            continue
        if field.is_indirect:
            if field.objgen in seen:
                continue
            seen.add(field.objgen)
        if field.get("/FT") == pikepdf.Name.Sig and isinstance(field.get("/V"),
                                                               pikepdf.Dictionary):
            return True
        kids = field.get("/Kids")
        if isinstance(kids, pikepdf.Array):
            stack.extend(kids)
    return False


def require_modify_permission(pdf: pikepdf.Pdf) -> None:
    """Refuse changing page content when the PDF forbids it, unless opened as owner."""
    if pdf.is_encrypted and not pdf.owner_password_matched and not pdf.allow.modify_other:
        raise PdfUserError(
            "This PDF does not allow changing its pages; that needs its owner password"
        )


def is_encrypted(path: Path) -> bool:
    """True if the PDF is encrypted, without needing its password."""
    try:
        with pikepdf.open(str(path)) as pdf:
            return pdf.is_encrypted
    except pikepdf.PasswordError:
        return True  # needs a user password to open, so it is encrypted


def copy_docinfo(src: pikepdf.Pdf, dst: pikepdf.Pdf) -> None:
    """Carry basic document info to a freshly built PDF."""
    try:
        with dst.open_metadata(set_pikepdf_as_editor=False) as dst_meta, \
                src.open_metadata() as src_meta:
            for key in src_meta:
                dst_meta[key] = src_meta[key]
    except Exception:
        # Metadata copy is best-effort; never fail an operation over it.
        pass


def run_qpdf(
    settings: Settings, args: list[str], password: str | None = None, timeout: int = 120
) -> subprocess.CompletedProcess:
    """Run the qpdf CLI and return the completed process.

    The password is sent on stdin (``--password-file=-``), never in argv, so
    other local users cannot read it from the process list. A timeout becomes a
    PdfUserError rather than an internal crash. Output is decoded from bytes:
    text mode would turn a "\r" into a line break and fail on non-UTF-8 output.
    """
    if password and ("\n" in password or "\r" in password):
        # qpdf reads only the first line of the password file and ignores the rest.
        raise PdfUserError("Passwords containing a line break are not supported here")
    cmd = [settings.qpdf_path]
    if password:
        cmd.append("--password-file=-")
    cmd += args
    try:
        proc = subprocess.run(cmd, input=(password or "").encode("utf-8"),
                              capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise PdfUserError(f"qpdf timed out after {timeout}s") from exc
    return subprocess.CompletedProcess(
        proc.args, proc.returncode,
        proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace"),
    )
