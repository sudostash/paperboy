"""Reading stream data from untrusted PDFs without letting it expand without bound.

A few kilobytes of compressed data can inflate to gigabytes. These helpers decode
through the general-purpose filters with a byte budget, so a hostile stream is cut
off or refused instead of exhausting memory.
"""
from __future__ import annotations

import base64
import binascii
import re
import zlib

import pikepdf

IMAGE_FILTERS = {"/DCTDecode", "/JPXDecode", "/JBIG2Decode", "/CCITTFaxDecode"}
# Worst-case growth of filters only pikepdf decodes, and only as a whole (LZW codes of
# 12 bits can emit 4,096 bytes; run-length at most 128 from 2).
_RATIO = {"/LZWDecode": 3000, "/RunLengthDecode": 64}
_CHUNK = 1 << 20
# ASCII85 is decoded in Python (slowly before 3.13) and "z" makes 4 bytes of 1
# character: more text than this per stream is not decoded here.
A85_MAX_TEXT = 32 * 2**20
_DAMAGED = (zlib.error, binascii.Error, ValueError)


def filters_of(stream) -> list[str]:
    filters = stream.get("/Filter")
    filters = list(filters) if isinstance(filters, pikepdf.Array) else \
        ([] if filters is None else [filters])
    return [str(f) for f in filters]


def parms_of(stream) -> list[pikepdf.Dictionary]:
    """The /DecodeParms dictionaries of a stream (one per filter, or one for all)."""
    parms = stream.get("/DecodeParms")
    parms = list(parms) if isinstance(parms, pikepdf.Array) else [parms]
    return [p for p in parms if isinstance(p, pikepdf.Dictionary)]


def _has_predictor(stream) -> bool:
    return any(p.get("/Predictor", 1) != 1 for p in parms_of(stream))


def _stage(name: str, data: bytes, limit: int, strict: bool) -> bytes | None:
    """One filter, output cut at *limit*. None if unsupported or (strict) damaged."""
    if name == "/FlateDecode":
        inflater = zlib.decompressobj()
        out = inflater.decompress(data, limit)
        if strict and not inflater.eof and not inflater.unconsumed_tail:
            return None  # the data stops before the end of the compressed stream
        return out
    if name == "/ASCIIHexDecode":
        digits = re.sub(rb"[^0-9A-Fa-f]", b"", data.split(b">")[0])[:2 * limit]
        return binascii.unhexlify(digits + b"0" * (len(digits) % 2))
    if name == "/ASCII85Decode":
        # Everything after the end marker is ignored, as readers do. At most 5
        # characters make 4 bytes, so a few more than 5/4 of the limit is enough.
        text = re.sub(rb"\s", b"", data.split(b"~>")[0])
        need = (limit // 4 + 2) * 5
        if need > A85_MAX_TEXT and len(text) > A85_MAX_TEXT:
            return None
        return base64.a85decode(text[:need], adobe=False)[:limit]
    return None


def _run(filters: list[str], data: bytes, limit: int, strict: bool) -> bytes | None:
    try:
        for name in filters:
            data = _stage(name, data, limit, strict)
            if data is None:
                return None
    except _DAMAGED:
        return None
    return data[:limit]


def decode(stream, limit: int, strict: bool = False) -> bytes | None:
    """A stream's data through its general filters, cut at *limit* bytes. None if it
    can't be decoded here (image filters, predictors, LZW, run-length) or, when
    *strict*, if the data is damaged or cut short. A result of exactly *limit* bytes
    may have been cut: ask for one byte more than you will accept."""
    filters = filters_of(stream)
    if len(filters) > 4 or _has_predictor(stream):
        return None
    return _run(filters, stream.read_raw_bytes(), limit, strict)


def encoded_image(stream, limit: int) -> bytes | None:
    """The data an image codec (JPEG, JPEG 2000, ...) receives: the stream with only
    the general filters in front of the codec undone, at most *limit* bytes."""
    filters = filters_of(stream)
    if not filters or filters[-1] not in IMAGE_FILTERS or len(filters) > 4:
        return None
    return _run(filters[:-1], stream.read_raw_bytes(), limit, strict=False)


def _inflated_size(data: bytes, cap: int) -> int:
    """How many bytes Flate data inflates to, counted in chunks, stopping past *cap*."""
    inflater, total = zlib.decompressobj(), 0
    while total <= cap:
        chunk = inflater.decompress(data, _CHUNK)
        data = inflater.unconsumed_tail
        if not chunk and not data:  # all input used and no output held back
            break
        total += len(chunk)
    return total


def decoded_size(stream, cap: int) -> int | None:
    """How many bytes the stream's general filters (not image codecs, and not
    predictors, which callers check separately) decode to, counted no further than
    just past *cap*: a result over *cap* means "too big". None if it can't be measured:
    damaged data (another decoder may read more of it than Python's), a layer cut at
    the cap (the next would see too little), and LZW or run-length anywhere but last."""
    general = [f for f in filters_of(stream) if f not in IMAGE_FILTERS]
    data = stream.read_raw_bytes()
    if len(general) > 4:
        return None
    try:
        for i, name in enumerate(general):
            last = i == len(general) - 1
            if name in _RATIO:  # decoded whole by pikepdf: bound it by its worst case
                return len(data) * _RATIO[name] if last else None
            if last and name == "/FlateDecode":
                return _inflated_size(data, cap)
            data = _stage(name, data, cap + 1, strict=False)
            if data is None or len(data) > cap:
                return None
    except _DAMAGED:
        return None
    return len(data)


def within(stream, cap: int) -> bool:
    """Whether decoding the stream's general filters stays within *cap* bytes; anything
    that can't be measured is refused (see decoded_size)."""
    size = decoded_size(stream, cap)
    return size is not None and size <= cap
