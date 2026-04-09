#!/usr/bin/env python3
"""
palette.py -- Generate design tokens for the PDF pipeline.

Usage:
    python3 palette.py --type proposal --accent "#2D5F8A"
    python3 palette.py --type report --cover-bg "#1A1A2E"

Or import directly:
    from palette import generate_palette
    tokens = generate_palette("proposal", accent="#2D5F8A")
"""

import argparse
import json


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """Convert '#RRGGBB' or 'RRGGBB' to (R, G, B) ints."""
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def rgb_to_hex(r: int, g: int, b: int) -> str:
    """Convert (R, G, B) ints to '#RRGGBB'."""
    return f"#{r:02X}{g:02X}{b:02X}"


def lighten(hex_color: str, factor: float = 0.4) -> str:
    """Lighten a color toward white by the given factor (0..1)."""
    r, g, b = hex_to_rgb(hex_color)
    r = int(r + (255 - r) * factor)
    g = int(g + (255 - g) * factor)
    b = int(b + (255 - b) * factor)
    return rgb_to_hex(r, g, b)


def darken(hex_color: str, factor: float = 0.3) -> str:
    """Darken a color toward black by the given factor (0..1)."""
    r, g, b = hex_to_rgb(hex_color)
    r = int(r * (1 - factor))
    g = int(g * (1 - factor))
    b = int(b * (1 - factor))
    return rgb_to_hex(r, g, b)


# -- Default palettes per document type --

TYPE_DEFAULTS = {
    "report": {
        "cover_bg": "#1A1A2E",
        "accent": "#2D5F8A",
        "heading": "#1A1A2E",
        "text": "#2C2C2C",
        "muted": "#888888",
        "cover_text": "#FFFFFF",
        "cover_pattern": "fullbleed",
        "body_font": "LiberationSans",
        "heading_font": "LiberationSans-Bold",
    },
    "proposal": {
        "cover_bg": "#22223B",
        "accent": "#2D5F8A",
        "heading": "#22223B",
        "text": "#333333",
        "muted": "#888888",
        "cover_text": "#FFFFFF",
        "cover_pattern": "split",
        "body_font": "LiberationSans",
        "heading_font": "LiberationSans-Bold",
    },
    "resume": {
        "cover_bg": "#FFFFFF",
        "accent": "#3D4C5E",
        "heading": "#1A1A2E",
        "text": "#2C2C2C",
        "muted": "#999999",
        "cover_text": "#1A1A2E",
        "cover_pattern": "typographic",
        "body_font": "LiberationSans",
        "heading_font": "LiberationSans-Bold",
    },
    "portfolio": {
        "cover_bg": "#0D0D0D",
        "accent": "#8A3A2A",
        "heading": "#1A1A1A",
        "text": "#2C2C2C",
        "muted": "#888888",
        "cover_text": "#F0F0F0",
        "cover_pattern": "atmospheric",
        "body_font": "LiberationSans",
        "heading_font": "LiberationSans-Bold",
    },
    "academic": {
        "cover_bg": "#F5F0EB",
        "accent": "#2A5A6B",
        "heading": "#1A1A2E",
        "text": "#333333",
        "muted": "#888888",
        "cover_text": "#1A1A2E",
        "cover_pattern": "typographic",
        "body_font": "LiberationSans",
        "heading_font": "LiberationSans-Bold",
    },
    "general": {
        "cover_bg": "#2E3440",
        "accent": "#3D4A5A",
        "heading": "#2E3440",
        "text": "#333333",
        "muted": "#888888",
        "cover_text": "#FFFFFF",
        "cover_pattern": "fullbleed",
        "body_font": "LiberationSans",
        "heading_font": "LiberationSans-Bold",
    },
    "minimal": {
        "cover_bg": "#FFFFFF",
        "accent": "#3D4A5A",
        "heading": "#1A1A1A",
        "text": "#333333",
        "muted": "#999999",
        "cover_text": "#1A1A1A",
        "cover_pattern": "minimal",
        "body_font": "LiberationSans",
        "heading_font": "LiberationSans-Bold",
    },
    "stripe": {
        "cover_bg": "#FFFFFF",
        "accent": "#2D5F8A",
        "heading": "#1A1A2E",
        "text": "#333333",
        "muted": "#888888",
        "cover_text": "#FFFFFF",
        "cover_pattern": "stripe",
        "body_font": "LiberationSans",
        "heading_font": "LiberationSans-Bold",
    },
    "diagonal": {
        "cover_bg": "#1A1A2E",
        "accent": "#3D4F8A",
        "heading": "#1A1A2E",
        "text": "#333333",
        "muted": "#888888",
        "cover_text": "#FFFFFF",
        "cover_pattern": "diagonal",
        "body_font": "LiberationSans",
        "heading_font": "LiberationSans-Bold",
    },
    "frame": {
        "cover_bg": "#FFFBF5",
        "accent": "#6B2A35",
        "heading": "#2C1810",
        "text": "#333333",
        "muted": "#888888",
        "cover_text": "#2C1810",
        "cover_pattern": "frame",
        "body_font": "LiberationSans",
        "heading_font": "LiberationSans-Bold",
    },
    "editorial": {
        "cover_bg": "#FFFFFF",
        "accent": "#1A1A2E",
        "heading": "#1A1A2E",
        "text": "#333333",
        "muted": "#888888",
        "cover_text": "#1A1A2E",
        "cover_pattern": "editorial",
        "body_font": "LiberationSans",
        "heading_font": "LiberationSans-Bold",
    },
    "magazine": {
        "cover_bg": "#FAF3EB",
        "accent": "#2D5F8A",
        "heading": "#1A1A2E",
        "text": "#333333",
        "muted": "#888888",
        "cover_text": "#1A1A2E",
        "cover_pattern": "magazine",
        "body_font": "LiberationSans",
        "heading_font": "LiberationSans-Bold",
    },
    "darkroom": {
        "cover_bg": "#0F1B2D",
        "accent": "#4A6B8A",
        "heading": "#1A1A2E",
        "text": "#333333",
        "muted": "#888888",
        "cover_text": "#E0E0E0",
        "cover_pattern": "darkroom",
        "body_font": "LiberationSans",
        "heading_font": "LiberationSans-Bold",
    },
    "terminal": {
        "cover_bg": "#0A0A0A",
        "accent": "#00FF41",
        "heading": "#0A0A0A",
        "text": "#333333",
        "muted": "#888888",
        "cover_text": "#00FF41",
        "cover_pattern": "terminal",
        "body_font": "LiberationMono",
        "heading_font": "LiberationMono",
    },
    "poster": {
        "cover_bg": "#FFFFFF",
        "accent": "#2D5F8A",
        "heading": "#1A1A2E",
        "text": "#333333",
        "muted": "#888888",
        "cover_text": "#1A1A2E",
        "cover_pattern": "poster",
        "body_font": "LiberationSans",
        "heading_font": "LiberationSans-Bold",
    },
}


def generate_palette(
    doc_type: str = "general",
    accent: str | None = None,
    cover_bg: str | None = None,
) -> dict:
    """Generate a complete design token dictionary for the given document type.

    Args:
        doc_type: One of the supported document types.
        accent: Optional hex color override for the accent.
        cover_bg: Optional hex color override for the cover background.

    Returns:
        Dictionary of design tokens with colors, fonts, and layout settings.
    """
    base = TYPE_DEFAULTS.get(doc_type, TYPE_DEFAULTS["general"]).copy()

    if accent:
        base["accent"] = accent
    if cover_bg:
        base["cover_bg"] = cover_bg

    # Derive secondary colors
    base["accent_lt"] = lighten(base["accent"], 0.4)
    base["accent_dk"] = darken(base["accent"], 0.25)
    base["accent_bg"] = lighten(base["accent"], 0.85)
    base["table_header_bg"] = base["accent"]
    base["table_header_text"] = "#FFFFFF"
    base["table_alt_bg"] = lighten(base["accent"], 0.92)
    base["callout_bg"] = lighten(base["accent"], 0.88)
    base["callout_border"] = base["accent"]
    base["rule_color"] = base["accent"]
    base["doc_type"] = doc_type

    # CJK font path
    base["cjk_font_name"] = "NotoSansCJK"
    base["cjk_font_path"] = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"

    # Page layout
    base["page_width"] = 595.27  # A4
    base["page_height"] = 841.89
    base["margin_top"] = 72
    base["margin_bottom"] = 60
    base["margin_left"] = 65
    base["margin_right"] = 55

    return base


def main():
    parser = argparse.ArgumentParser(description="Generate PDF design tokens")
    parser.add_argument("--type", default="general", help="Document type")
    parser.add_argument("--accent", default=None, help="Accent color (#HEX)")
    parser.add_argument("--cover-bg", default=None, help="Cover background color (#HEX)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    tokens = generate_palette(args.type, accent=args.accent, cover_bg=args.cover_bg)

    if args.json:
        print(json.dumps(tokens, indent=2))
    else:
        for k, v in sorted(tokens.items()):
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
