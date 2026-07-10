"""Render a receipt's transaction-number barcode.

Costco warehouse receipts encode the transaction number as an Interleaved 2 of 5
(ITF) barcode — the same all-numeric symbology seen on the printed receipt in
myaccount.pdf. ITF requires an even number of digits, so odd-length numbers are
left-padded with a zero (standard practice). Falls back to Code 128 for any
non-ITF-encodable value. Output is inline SVG (no external image deps).
"""
from __future__ import annotations

import io
import re


def _svg_body(svg: str) -> str:
    """Strip XML prolog/doctype so the <svg> can be embedded inline in HTML/MD."""
    m = re.search(r"<svg\b.*</svg>", svg, re.S)
    return m.group(0) if m else svg


def barcode_svg(number: str, symbology: str = "itf") -> str | None:
    """Return inline SVG for the barcode of `number`, or None if not renderable."""
    number = re.sub(r"\D", "", str(number or ""))
    if not number:
        return None
    try:
        import barcode
        from barcode.writer import SVGWriter
    except ImportError:
        return None

    opts = {"module_height": 8.0, "module_width": 0.28,
            "font_size": 8, "text_distance": 3.0, "quiet_zone": 2.0}

    candidates = []
    if symbology == "itf":
        candidates.append(("itf", number if len(number) % 2 == 0 else "0" + number))
    candidates.append(("code128", number))

    for sym, val in candidates:
        try:
            buf = io.BytesIO()
            barcode.get(sym, val, writer=SVGWriter()).write(buf, options=opts)
            return _svg_body(buf.getvalue().decode())
        except Exception:
            continue
    return None
