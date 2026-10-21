"""Encryption and decryption via pikepdf (qpdf), with permission control."""
from __future__ import annotations

from pathlib import Path

import pikepdf

from ..config import Settings
from ..errors import PdfUserError
from .base import open_pdf

# algorithm -> (revision R, use AES)
_ALGORITHMS = {
    "AES-256": (6, True),
    "AES-128": (4, True),
    "RC4-128": (4, False),
    "RC4-40": (2, False),
}


def _permissions(allow_print: bool, allow_copy: bool, allow_modify: bool) -> pikepdf.Permissions:
    return pikepdf.Permissions(
        accessibility=allow_copy,
        extract=allow_copy,
        print_lowres=allow_print,
        print_highres=allow_print,
        modify_annotation=allow_modify,
        modify_assembly=allow_modify,
        modify_form=allow_modify,
        modify_other=allow_modify,
    )


def encrypt_pdf(
    path: Path,
    out: Path,
    user_password: str,
    settings: Settings,
    owner_password: str | None = None,
    algorithm: str = "AES-256",
    allow_print: bool = True,
    allow_copy: bool = True,
    allow_modify: bool = True,
    current_password: str | None = None,
) -> dict:
    """Encrypt (password-protect) a PDF.

    ``user_password`` opens the file; ``owner_password`` (defaults to the user
    password) controls permissions. ``allow_*`` are enforced by compliant viewers.
    """
    if not user_password:
        raise PdfUserError("user_password must not be empty")
    if algorithm not in _ALGORITHMS:
        raise PdfUserError(
            f"algorithm must be one of {', '.join(_ALGORITHMS)}; got {algorithm!r}"
        )
    r, use_aes = _ALGORITHMS[algorithm]
    encryption = pikepdf.Encryption(
        user=user_password,
        owner=owner_password or user_password,
        R=r,
        aes=use_aes,
        metadata=use_aes,  # RC4 cannot encrypt the metadata stream
        allow=_permissions(allow_print, allow_copy, allow_modify),
    )
    with open_pdf(path, settings, current_password) as pdf:
        pages = len(pdf.pages)
        pdf.save(str(out), encryption=encryption)
    return {"output": str(out), "algorithm": algorithm, "pages": pages}


def decrypt_pdf(path: Path, out: Path, password: str, settings: Settings) -> dict:
    """Remove the password from a PDF you can open. Not a password cracker."""
    try:
        pdf = pikepdf.open(str(path), password=password or "")
    except pikepdf.PasswordError:
        raise PdfUserError("Incorrect password") from None
    except pikepdf.PdfError as exc:
        raise PdfUserError(f"Could not open PDF: {exc}") from exc
    with pdf:
        if not pdf.is_encrypted:
            raise PdfUserError("This PDF is not encrypted")
        if len(pdf.pages) > settings.max_pages:
            raise PdfUserError(
                f"Document has {len(pdf.pages)} pages, over the "
                f"{settings.max_pages} page limit"
            )
        pages = len(pdf.pages)
        pdf.save(str(out))  # saving without encryption= drops the protection
    return {"output": str(out), "pages": pages}
