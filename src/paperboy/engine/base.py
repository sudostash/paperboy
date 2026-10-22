"""Shared engine helpers: opening, limit enforcement, metadata copy."""
from __future__ import annotations

import io
import secrets
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pikepdf
import pikepdf.codec  # noqa: F401  registers the "pdfdoc" codec

from ..config import Settings
from ..errors import PdfUserError


@contextmanager
def open_pdf(path: Path | bytes, settings: Settings, password: str | None = None):
    """Open a PDF (a path, or its bytes) with pikepdf, enforcing the page-count cap.

    Raises PdfUserError for a missing/incorrect password or an oversized document.
    """
    source = io.BytesIO(path) if isinstance(path, bytes) else str(path)
    try:
        pdf = pikepdf.open(source, password=password or "")
    except pikepdf.PasswordError:
        if password:
            raise PdfUserError("Incorrect password") from None
        raise PdfUserError("This PDF is password protected; supply its password") from None
    except pikepdf.PdfError as exc:
        raise PdfUserError(f"Could not open PDF: {exc}") from exc
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
    if src.user_password_matched:
        # The owner password is unknown; a random one keeps the permissions enforceable.
        user, owner, changed = password or "", secrets.token_urlsafe(32), False
    else:  # opened with the owner password
        owner = password or ""
        if src.encryption.R <= 4:  # qpdf recovers the user password, in PDFDocEncoding
            user, changed = src.encryption.user_password.decode("pdfdoc"), False
        else:  # AES-256 cannot be recovered: never fall back to "opens without a password"
            user, changed = password or secrets.token_urlsafe(32), True
    return Protection(pikepdf.Encryption(user=user, owner=owner, R=6, allow=src.allow), changed)


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
