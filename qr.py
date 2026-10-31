"""Server-side QR code generation for Questionable Intel.

Wraps the vendored ``qrcodegen.py`` (Project Nayuki, MIT — see
THIRD_PARTY_LICENSES.md) to produce a scannable PNG with a quiet zone,
rendered entirely server-side with the standard library (``zlib`` + ``struct``).
No external image service, no network call at scan time.
"""
from __future__ import annotations

import struct
import zlib

import qrcodegen


def qr_matrix(text: str) -> qrcodegen.QrCode:
    """Encode *text* as a QR Code symbol (byte mode, ECC medium, version auto)."""
    qr = qrcodegen.QrCode.encode_text(text, qrcodegen.QrCode.Ecc.MEDIUM)
    return qr


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    chunk = struct.pack(">I", len(data)) + tag + data
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return chunk + struct.pack(">I", crc)


def qr_png(text: str, scale: int = 10, quiet: int = 4) -> bytes:
    """Return a PNG (RGBA, black modules on white) for *text*.

    *scale* is the pixel size of each module; *quiet* the quiet-zone width in
    modules (scanners need a quiet zone on a light tile).
    """
    qr = qr_matrix(text)
    size = qr.get_size()
    dim = (size + 2 * quiet) * scale

    # Build raw scanlines (each row: 1 filter byte + RGBA per pixel).
    rows = []
    white = b"\xff\xff\xff\xff"
    black = b"\x00\x00\x00\xff"
    for y in range(dim):
        row = bytearray(b"\x00")
        py = (y // scale) - quiet
        for x in range(dim):
            px = (x // scale) - quiet
            if 0 <= py < size and 0 <= px < size and qr.get_module(px, py):
                row += black
            else:
                row += white
        rows.append(bytes(row))

    raw = b"".join(rows)
    ihdr = struct.pack(">IIBBBBB", dim, dim, 8, 6, 0, 0, 0)  # 8-bit RGBA
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(raw, 9))
        + _png_chunk(b"IEND", b"")
    )
    return png
