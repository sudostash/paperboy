"""The bounded decoders shared by the tools (here: LZW, used to measure before qpdf)."""
from __future__ import annotations

import random

import pikepdf
import pytest

from paperboy.engine.streams import lzw


def encode(data: bytes, early: int = 1) -> bytes:
    """A plain LZW encoder (as PDF's LZWDecode reads), to test against."""
    table = {bytes([i]): i for i in range(256)}
    nxt, width, out, bits, acc = 258, 9, bytearray(), 0, 0

    def put(code):
        nonlocal bits, acc
        acc = (acc << width) | code
        bits += width
        while bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xFF)
            acc &= (1 << bits) - 1

    put(256)
    word = b""
    for byte in data:
        grown = word + bytes([byte])
        if grown in table:
            word = grown
            continue
        put(table[word])
        if nxt < 4096:
            table[grown] = nxt
            nxt += 1
            if nxt + early > (1 << width) and width < 12:
                width += 1
        else:
            put(256)
            table = {bytes([i]): i for i in range(256)}
            nxt, width = 258, 9
        word = bytes([byte])
    if word:
        put(table[word])
    put(257)
    if bits:
        out.append((acc << (8 - bits)) & 0xFF)
    return bytes(out)


def qpdf(data: bytes, early: int = 1) -> bytes:
    pdf = pikepdf.new()  # kept alive while the stream is read
    stream = pdf.make_stream(data)
    stream.Filter = pikepdf.Name.LZWDecode
    if early != 1:
        stream.DecodeParms = pikepdf.Dictionary(EarlyChange=early)
    return stream.read_bytes()


@pytest.mark.parametrize("early", [1, 0])
def test_matches_qpdf_through_table_resets(early):
    rnd = random.Random(1)
    data = bytes(rnd.choice(b"abcdefgh") for _ in range(120_000))  # fills the table
    coded = encode(data, early)
    assert lzw(coded, 10**7, early) == data == qpdf(coded, early)


def test_the_tricky_code_that_isnt_in_the_table_yet():
    data = b"aaaaaaaaaaaaaa"  # the encoder emits a code defined by its own use
    assert lzw(encode(data), 100) == data


def test_stops_at_the_limit_and_on_bad_codes():
    assert lzw(encode(b"x" * 1000), 999) is None
    assert lzw(bytes.fromhex("807fc0"), 100) is None   # clear, then code 511
    assert lzw(b"", 10) == b""
    assert lzw(encode(b"abc")[:-1], 10) == b"abc"       # no end code: what was read


def test_stops_at_the_deadline():
    import time
    assert lzw(encode(b"x" * 1000), 10**6, deadline=time.monotonic() - 1) is None
    assert lzw(encode(b"x" * 1000), 10**6, deadline=time.monotonic() + 60) == b"x" * 1000
