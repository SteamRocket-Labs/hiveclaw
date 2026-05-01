#!/usr/bin/env python3
"""
render_cover.py -- Render a PDF cover page using reportlab canvas.

Usage:
    python3 render_cover.py --type proposal --title "Q3 Strategy" \
        --author "Team" --date "Oct 2025" --accent "#2D5F8A" --out cover.pdf

Or import directly:
    from render_cover import render_cover
    from palette import generate_palette
    tokens = generate_palette("proposal", accent="#2D5F8A")
    render_cover(tokens, "cover.pdf", title="...", author="...", date_text="...")
"""

import argparse
import logging
import os
import sys

from reportlab.lib.pagesizes import A4
from reportlab.lib.colors import HexColor, Color
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# ---------------------------------------------------------------------------
# Font registration
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

FONT_PATHS = {
    "LiberationSans": "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "LiberationSans-Bold": "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "LiberationSans-Italic": "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf",
    "LiberationMono": "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "NotoSansCJK": "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
}


def _register_fonts():
    for name, path in FONT_PATHS.items():
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont(name, path))
            except Exception as exc:
                logger.warning("Failed to register font %s from %s: %s", name, path, exc)


_register_fonts()

PAGE_W, PAGE_H = A4  # 595.27 x 841.89


# ---------------------------------------------------------------------------
# Helper: draw wrapped text on canvas
# ---------------------------------------------------------------------------

def _draw_wrapped_text(c, text, x, y, max_width, font_name, font_size, color, leading=None, align="left"):
    """Draw text with word-wrapping. Returns the Y position after the last line."""
    if leading is None:
        leading = font_size * 1.3
    c.setFont(font_name, font_size)
    c.setFillColor(HexColor(color))

    words = text.split()
    lines = []
    current_line = ""
    for word in words:
        test = f"{current_line} {word}".strip()
        if pdfmetrics.stringWidth(test, font_name, font_size) <= max_width:
            current_line = test
        else:
            if current_line:
                lines.append(current_line)
            current_line = word
    if current_line:
        lines.append(current_line)

    for line in lines:
        if align == "center":
            lw = pdfmetrics.stringWidth(line, font_name, font_size)
            c.drawString(x + (max_width - lw) / 2, y, line)
        elif align == "right":
            lw = pdfmetrics.stringWidth(line, font_name, font_size)
            c.drawString(x + max_width - lw, y, line)
        else:
            c.drawString(x, y, line)
        y -= leading
    return y


# ---------------------------------------------------------------------------
# Cover pattern renderers
# ---------------------------------------------------------------------------

def _cover_fullbleed(c, tokens, title, author, date_text, **kw):
    """Full-bleed dark background with optional dot grid."""
    bg = HexColor(tokens["cover_bg"])
    accent = HexColor(tokens["accent"])
    text_color = tokens["cover_text"]

    # Background
    c.setFillColor(bg)
    c.rect(0, 0, PAGE_W, PAGE_H, fill=True, stroke=False)

    # Dot grid pattern
    c.setFillColor(Color(1, 1, 1, alpha=0.04))
    for gx in range(0, int(PAGE_W), 20):
        for gy in range(0, int(PAGE_H), 20):
            c.circle(gx, gy, 1, fill=True, stroke=False)

    # Accent line
    c.setStrokeColor(accent)
    c.setLineWidth(3)
    c.line(65, PAGE_H - 200, PAGE_W - 55, PAGE_H - 200)

    # Title
    y = PAGE_H - 280
    y = _draw_wrapped_text(c, title, 65, y, PAGE_W - 120, "LiberationSans-Bold", 36, text_color, leading=44)

    # Author + date
    y -= 30
    if author:
        _draw_wrapped_text(c, author, 65, y, PAGE_W - 120, "LiberationSans", 14, text_color)
        y -= 22
    if date_text:
        _draw_wrapped_text(c, date_text, 65, y, PAGE_W - 120, "LiberationSans", 12, tokens.get("muted", "#888888"))


def _cover_split(c, tokens, title, author, date_text, **kw):
    """Left panel + right geometric."""
    accent = HexColor(tokens["accent"])
    bg = HexColor(tokens["cover_bg"])
    text_color = tokens["cover_text"]

    # Left panel (40% width)
    panel_w = PAGE_W * 0.4
    c.setFillColor(bg)
    c.rect(0, 0, panel_w, PAGE_H, fill=True, stroke=False)

    # Right side - lighter
    c.setFillColor(HexColor(tokens["accent_lt"]))
    c.rect(panel_w, 0, PAGE_W - panel_w, PAGE_H, fill=True, stroke=False)

    # Geometric circles on right
    c.setFillColor(Color(accent.red, accent.green, accent.blue, alpha=0.15))
    c.circle(PAGE_W - 80, PAGE_H - 150, 120, fill=True, stroke=False)
    c.circle(PAGE_W - 200, 200, 80, fill=True, stroke=False)

    # Title on left panel
    y = PAGE_H - 300
    y = _draw_wrapped_text(c, title, 40, y, panel_w - 60, "LiberationSans-Bold", 28, text_color, leading=36)

    # Accent bar
    y -= 20
    c.setFillColor(accent)
    c.rect(40, y, 60, 4, fill=True, stroke=False)

    # Author + date
    y -= 30
    if author:
        _draw_wrapped_text(c, author, 40, y, panel_w - 60, "LiberationSans", 12, text_color)
        y -= 20
    if date_text:
        _draw_wrapped_text(c, date_text, 40, y, panel_w - 60, "LiberationSans", 11, tokens.get("muted", "#888888"))


def _cover_typographic(c, tokens, title, author, date_text, **kw):
    """Oversized first word, elegant typography."""
    bg = HexColor(tokens["cover_bg"])
    text_color = tokens["cover_text"]
    accent = HexColor(tokens["accent"])

    c.setFillColor(bg)
    c.rect(0, 0, PAGE_W, PAGE_H, fill=True, stroke=False)

    # Oversized first word
    words = title.split()
    first_word = words[0] if words else title
    rest = " ".join(words[1:]) if len(words) > 1 else ""

    y = PAGE_H - 300
    _draw_wrapped_text(c, first_word, 65, y, PAGE_W - 120, "LiberationSans-Bold", 72, text_color)
    y -= 80
    if rest:
        y = _draw_wrapped_text(c, rest, 65, y, PAGE_W - 120, "LiberationSans-Bold", 28, text_color, leading=36)

    # Accent underline
    y -= 20
    c.setFillColor(accent)
    c.rect(65, y, 80, 3, fill=True, stroke=False)

    y -= 40
    if author:
        _draw_wrapped_text(c, author, 65, y, PAGE_W - 120, "LiberationSans", 13, text_color)
        y -= 22
    if date_text:
        _draw_wrapped_text(c, date_text, 65, y, PAGE_W - 120, "LiberationSans", 11, tokens.get("muted", "#888888"))


def _cover_minimal(c, tokens, title, author, date_text, **kw):
    """White background + single accent bar."""
    bg = HexColor(tokens["cover_bg"])
    accent = HexColor(tokens["accent"])
    text_color = tokens["cover_text"]

    c.setFillColor(bg)
    c.rect(0, 0, PAGE_W, PAGE_H, fill=True, stroke=False)

    # Single accent bar at top
    c.setFillColor(accent)
    c.rect(0, PAGE_H - 8, PAGE_W, 8, fill=True, stroke=False)

    y = PAGE_H - 350
    y = _draw_wrapped_text(c, title, 80, y, PAGE_W - 160, "LiberationSans-Bold", 32, text_color, leading=40)

    y -= 30
    if author:
        _draw_wrapped_text(c, author, 80, y, PAGE_W - 160, "LiberationSans", 13, text_color)
        y -= 22
    if date_text:
        _draw_wrapped_text(c, date_text, 80, y, PAGE_W - 160, "LiberationSans", 11, tokens.get("muted", "#888888"))


def _cover_stripe(c, tokens, title, author, date_text, **kw):
    """Three bold horizontal color bands."""
    accent = HexColor(tokens["accent"])
    accent_lt = HexColor(tokens["accent_lt"])
    accent_dk = HexColor(tokens["accent_dk"])
    text_color = tokens["cover_text"]

    band_h = PAGE_H / 3
    c.setFillColor(accent_dk)
    c.rect(0, band_h * 2, PAGE_W, band_h, fill=True, stroke=False)
    c.setFillColor(accent)
    c.rect(0, band_h, PAGE_W, band_h, fill=True, stroke=False)
    c.setFillColor(accent_lt)
    c.rect(0, 0, PAGE_W, band_h, fill=True, stroke=False)

    y = PAGE_H - 280
    y = _draw_wrapped_text(c, title, 65, y, PAGE_W - 120, "LiberationSans-Bold", 36, text_color, leading=44)
    y -= 30
    if author:
        _draw_wrapped_text(c, author, 65, y, PAGE_W - 120, "LiberationSans", 14, text_color)
        y -= 22
    if date_text:
        _draw_wrapped_text(c, date_text, 65, y, PAGE_W - 120, "LiberationSans", 12, "#FFFFFF")


def _cover_diagonal(c, tokens, title, author, date_text, **kw):
    """Angled cut -- dark top-left, light bottom-right."""
    bg_dark = HexColor(tokens["cover_bg"])
    HexColor(tokens["accent"])
    accent_lt = HexColor(tokens["accent_lt"])
    text_color = tokens["cover_text"]

    # Dark upper triangle
    c.setFillColor(bg_dark)
    path = c.beginPath()
    path.moveTo(0, PAGE_H)
    path.lineTo(PAGE_W, PAGE_H)
    path.lineTo(PAGE_W, PAGE_H * 0.4)
    path.lineTo(0, PAGE_H * 0.65)
    path.close()
    c.drawPath(path, fill=True, stroke=False)

    # Light lower triangle
    c.setFillColor(accent_lt)
    path = c.beginPath()
    path.moveTo(0, PAGE_H * 0.65)
    path.lineTo(PAGE_W, PAGE_H * 0.4)
    path.lineTo(PAGE_W, 0)
    path.lineTo(0, 0)
    path.close()
    c.drawPath(path, fill=True, stroke=False)

    y = PAGE_H - 250
    y = _draw_wrapped_text(c, title, 65, y, PAGE_W - 120, "LiberationSans-Bold", 34, text_color, leading=42)
    y -= 30
    if author:
        _draw_wrapped_text(c, author, 65, y, PAGE_W - 120, "LiberationSans", 13, text_color)
        y -= 22
    if date_text:
        _draw_wrapped_text(c, date_text, 65, y, PAGE_W - 120, "LiberationSans", 11, "#FFFFFF")


def _cover_frame(c, tokens, title, author, date_text, **kw):
    """Inset border with corner ornaments."""
    bg = HexColor(tokens["cover_bg"])
    accent = HexColor(tokens["accent"])
    text_color = tokens["cover_text"]

    c.setFillColor(bg)
    c.rect(0, 0, PAGE_W, PAGE_H, fill=True, stroke=False)

    # Inset frame
    inset = 40
    c.setStrokeColor(accent)
    c.setLineWidth(2)
    c.rect(inset, inset, PAGE_W - 2 * inset, PAGE_H - 2 * inset, fill=False, stroke=True)

    # Inner frame
    c.setLineWidth(0.5)
    c.rect(inset + 8, inset + 8, PAGE_W - 2 * (inset + 8), PAGE_H - 2 * (inset + 8), fill=False, stroke=True)

    # Corner ornaments (small squares)
    orn = 6
    for cx, cy in [(inset, inset), (inset, PAGE_H - inset), (PAGE_W - inset, inset), (PAGE_W - inset, PAGE_H - inset)]:
        c.setFillColor(accent)
        c.rect(cx - orn / 2, cy - orn / 2, orn, orn, fill=True, stroke=False)

    y = PAGE_H - 340
    y = _draw_wrapped_text(c, title, 80, y, PAGE_W - 160, "LiberationSans-Bold", 30, text_color, leading=38, align="center")
    y -= 25
    if author:
        _draw_wrapped_text(c, author, 80, y, PAGE_W - 160, "LiberationSans", 13, text_color, align="center")
        y -= 22
    if date_text:
        _draw_wrapped_text(c, date_text, 80, y, PAGE_W - 160, "LiberationSans", 11, tokens.get("muted", "#888888"), align="center")


def _cover_editorial(c, tokens, title, author, date_text, **kw):
    """Ghost letter behind all-caps title."""
    bg = HexColor(tokens["cover_bg"])
    accent = HexColor(tokens["accent"])
    text_color = tokens["cover_text"]

    c.setFillColor(bg)
    c.rect(0, 0, PAGE_W, PAGE_H, fill=True, stroke=False)

    # Ghost letter
    first_char = title[0].upper() if title else "A"
    c.setFillColor(Color(0.9, 0.9, 0.9, alpha=1))
    c.setFont("LiberationSans-Bold", 400)
    c.drawString(100, PAGE_H - 550, first_char)

    # All-caps title
    y = PAGE_H - 350
    y = _draw_wrapped_text(c, title.upper(), 65, y, PAGE_W - 120, "LiberationSans-Bold", 40, text_color, leading=50)

    # Accent line
    y -= 15
    c.setFillColor(accent)
    c.rect(65, y, 100, 4, fill=True, stroke=False)

    y -= 30
    if author:
        _draw_wrapped_text(c, author, 65, y, PAGE_W - 120, "LiberationSans", 13, text_color)
        y -= 22
    if date_text:
        _draw_wrapped_text(c, date_text, 65, y, PAGE_W - 120, "LiberationSans", 11, tokens.get("muted", "#888888"))


def _cover_magazine(c, tokens, title, author, date_text, **kw):
    """Warm cream bg, centered stack, optional hero image."""
    bg = HexColor(tokens["cover_bg"])
    HexColor(tokens["accent"])
    text_color = tokens["cover_text"]

    c.setFillColor(bg)
    c.rect(0, 0, PAGE_W, PAGE_H, fill=True, stroke=False)

    # Hero image placeholder area (top third)
    cover_image = kw.get("cover_image")
    if cover_image and os.path.exists(cover_image):
        try:
            c.drawImage(cover_image, 0, PAGE_H * 0.55, PAGE_W, PAGE_H * 0.45, preserveAspectRatio=True, anchor="c")
        except Exception as exc:
            logger.warning("Failed to load cover image %s: %s", cover_image, exc)

    y = PAGE_H * 0.45
    y = _draw_wrapped_text(c, title, 80, y, PAGE_W - 160, "LiberationSans-Bold", 32, text_color, leading=40, align="center")

    # Abstract
    abstract = kw.get("abstract", "")
    if abstract:
        y -= 20
        y = _draw_wrapped_text(c, abstract, 80, y, PAGE_W - 160, "LiberationSans", 11, tokens.get("muted", "#888888"), leading=16, align="center")

    y -= 25
    if author:
        _draw_wrapped_text(c, author, 80, y, PAGE_W - 160, "LiberationSans", 12, text_color, align="center")


def _cover_darkroom(c, tokens, title, author, date_text, **kw):
    """Dark navy, centered stack, grayscale image."""
    bg = HexColor(tokens["cover_bg"])
    HexColor(tokens["accent"])
    text_color = tokens["cover_text"]

    c.setFillColor(bg)
    c.rect(0, 0, PAGE_W, PAGE_H, fill=True, stroke=False)

    cover_image = kw.get("cover_image")
    if cover_image and os.path.exists(cover_image):
        try:
            c.drawImage(cover_image, 0, PAGE_H * 0.5, PAGE_W, PAGE_H * 0.5, preserveAspectRatio=True, anchor="c")
        except Exception as exc:
            logger.warning("Failed to load cover image %s: %s", cover_image, exc)

    y = PAGE_H * 0.40
    y = _draw_wrapped_text(c, title, 80, y, PAGE_W - 160, "LiberationSans-Bold", 30, text_color, leading=38, align="center")

    abstract = kw.get("abstract", "")
    if abstract:
        y -= 15
        y = _draw_wrapped_text(c, abstract, 80, y, PAGE_W - 160, "LiberationSans", 10, tokens.get("muted", "#888888"), leading=14, align="center")

    y -= 20
    if author:
        _draw_wrapped_text(c, author, 80, y, PAGE_W - 160, "LiberationSans", 12, text_color, align="center")


def _cover_terminal(c, tokens, title, author, date_text, **kw):
    """Near-black, grid lines, monospace, neon green."""
    bg = HexColor(tokens["cover_bg"])
    accent = HexColor(tokens["accent"])
    text_color = tokens["cover_text"]

    c.setFillColor(bg)
    c.rect(0, 0, PAGE_W, PAGE_H, fill=True, stroke=False)

    # Grid lines
    c.setStrokeColor(Color(0, 1, 0.25, alpha=0.06))
    c.setLineWidth(0.3)
    for gy in range(0, int(PAGE_H), 18):
        c.line(0, gy, PAGE_W, gy)
    for gx in range(0, int(PAGE_W), 18):
        c.line(gx, 0, gx, PAGE_H)

    # Terminal prompt
    y = PAGE_H - 200
    _draw_wrapped_text(c, "$ cat README.md", 65, y, PAGE_W - 120, "LiberationMono", 14, text_color)
    y -= 40

    y = _draw_wrapped_text(c, title, 65, y, PAGE_W - 120, "LiberationMono", 28, text_color, leading=36)
    y -= 30
    if author:
        _draw_wrapped_text(c, f"# Author: {author}", 65, y, PAGE_W - 120, "LiberationMono", 12, text_color)
        y -= 20
    if date_text:
        _draw_wrapped_text(c, f"# Date: {date_text}", 65, y, PAGE_W - 120, "LiberationMono", 12, text_color)

    # Cursor blink
    y -= 30
    c.setFillColor(accent)
    c.rect(65, y, 10, 16, fill=True, stroke=False)


def _cover_atmospheric(c, tokens, title, author, date_text, **kw):
    """Near-black with radial glow."""
    bg = HexColor(tokens["cover_bg"])
    accent = HexColor(tokens["accent"])
    text_color = tokens["cover_text"]

    c.setFillColor(bg)
    c.rect(0, 0, PAGE_W, PAGE_H, fill=True, stroke=False)

    # Radial glow (concentric translucent circles)
    cx, cy = PAGE_W / 2, PAGE_H / 2
    for i in range(8, 0, -1):
        alpha = 0.02 * i
        radius = 40 * i
        c.setFillColor(Color(accent.red, accent.green, accent.blue, alpha=alpha))
        c.circle(cx, cy, radius, fill=True, stroke=False)

    y = PAGE_H - 340
    y = _draw_wrapped_text(c, title, 80, y, PAGE_W - 160, "LiberationSans-Bold", 34, text_color, leading=42, align="center")
    y -= 30
    if author:
        _draw_wrapped_text(c, author, 80, y, PAGE_W - 160, "LiberationSans", 13, text_color, align="center")
        y -= 22
    if date_text:
        _draw_wrapped_text(c, date_text, 80, y, PAGE_W - 160, "LiberationSans", 11, tokens.get("muted", "#888888"), align="center")


def _cover_poster(c, tokens, title, author, date_text, **kw):
    """White bg, thick sidebar, oversized title."""
    accent = HexColor(tokens["accent"])
    text_color = tokens["cover_text"]

    c.setFillColor(HexColor("#FFFFFF"))
    c.rect(0, 0, PAGE_W, PAGE_H, fill=True, stroke=False)

    # Thick sidebar
    sidebar_w = 80
    c.setFillColor(accent)
    c.rect(0, 0, sidebar_w, PAGE_H, fill=True, stroke=False)

    # Cover image
    cover_image = kw.get("cover_image")
    if cover_image and os.path.exists(cover_image):
        try:
            c.drawImage(cover_image, sidebar_w + 20, PAGE_H * 0.5, PAGE_W - sidebar_w - 40, PAGE_H * 0.45,
                        preserveAspectRatio=True, anchor="c")
        except Exception as exc:
            logger.warning("Failed to load cover image %s: %s", cover_image, exc)

    y = PAGE_H * 0.42
    y = _draw_wrapped_text(c, title, sidebar_w + 30, y, PAGE_W - sidebar_w - 60, "LiberationSans-Bold", 36, text_color, leading=44)
    y -= 25
    if author:
        _draw_wrapped_text(c, author, sidebar_w + 30, y, PAGE_W - sidebar_w - 60, "LiberationSans", 13, text_color)
        y -= 22
    if date_text:
        _draw_wrapped_text(c, date_text, sidebar_w + 30, y, PAGE_W - sidebar_w - 60, "LiberationSans", 11, tokens.get("muted", "#888888"))


# -- Pattern dispatcher --

COVER_PATTERNS = {
    "fullbleed": _cover_fullbleed,
    "split": _cover_split,
    "typographic": _cover_typographic,
    "minimal": _cover_minimal,
    "stripe": _cover_stripe,
    "diagonal": _cover_diagonal,
    "frame": _cover_frame,
    "editorial": _cover_editorial,
    "magazine": _cover_magazine,
    "darkroom": _cover_darkroom,
    "terminal": _cover_terminal,
    "atmospheric": _cover_atmospheric,
    "poster": _cover_poster,
}


def render_cover(
    tokens: dict,
    output_path: str,
    title: str = "Untitled",
    author: str = "",
    date_text: str = "",
    abstract: str = "",
    cover_image: str = "",
):
    """Render a cover page PDF using the design tokens.

    Args:
        tokens: Design tokens from palette.generate_palette().
        output_path: Path to write the cover PDF.
        title: Document title.
        author: Author name(s).
        date_text: Date string.
        abstract: Optional abstract text (for magazine/darkroom covers).
        cover_image: Optional path to a hero image.
    """
    pattern = tokens.get("cover_pattern", "fullbleed")
    renderer = COVER_PATTERNS.get(pattern, _cover_fullbleed)

    c = canvas.Canvas(output_path, pagesize=A4)
    renderer(c, tokens, title, author, date_text, abstract=abstract, cover_image=cover_image)
    c.showPage()
    c.save()


def main():
    # Import palette from same directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, script_dir)
    from palette import generate_palette

    parser = argparse.ArgumentParser(description="Render PDF cover page")
    parser.add_argument("--type", default="general", help="Document type")
    parser.add_argument("--title", required=True, help="Document title")
    parser.add_argument("--author", default="", help="Author name")
    parser.add_argument("--date", default="", help="Date string")
    parser.add_argument("--accent", default=None, help="Accent color")
    parser.add_argument("--cover-bg", default=None, help="Cover background color")
    parser.add_argument("--abstract", default="", help="Abstract text")
    parser.add_argument("--cover-image", default="", help="Hero image path")
    parser.add_argument("--out", default="cover.pdf", help="Output path")
    args = parser.parse_args()

    tokens = generate_palette(args.type, accent=args.accent, cover_bg=args.cover_bg)
    render_cover(tokens, args.out, title=args.title, author=args.author,
                 date_text=args.date, abstract=args.abstract, cover_image=args.cover_image)
    print(f"Cover written to {args.out}")


if __name__ == "__main__":
    main()
