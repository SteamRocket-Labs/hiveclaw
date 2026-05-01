#!/usr/bin/env python3
"""
render_body.py -- Render PDF body pages from content.json using reportlab Platypus.

Usage:
    python3 render_body.py --content content.json --type proposal --accent "#2D5F8A" --out body.pdf

Or import directly:
    from render_body import render_body
    from palette import generate_palette
    tokens = generate_palette("proposal", accent="#2D5F8A")
    render_body(tokens, "content.json", "body.pdf")
"""

import argparse
import json
import logging
import os
import sys
import tempfile

from reportlab.lib.pagesizes import A4
from reportlab.lib.colors import HexColor
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    Image, PageBreak, HRFlowable,
)
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

PAGE_W, PAGE_H = A4


# ---------------------------------------------------------------------------
# Page number footer
# ---------------------------------------------------------------------------

def _page_footer(canvas_obj, doc):
    """Add page number at bottom center."""
    canvas_obj.saveState()
    canvas_obj.setFont("LiberationSans", 8)
    canvas_obj.setFillColor(HexColor("#888888"))
    canvas_obj.drawCentredString(PAGE_W / 2, 30, str(doc.page))
    canvas_obj.restoreState()


# ---------------------------------------------------------------------------
# Style builder
# ---------------------------------------------------------------------------

def _build_styles(tokens: dict) -> dict:
    """Build ParagraphStyle instances from design tokens."""
    heading_font = tokens.get("heading_font", "LiberationSans-Bold")
    body_font = tokens.get("body_font", "LiberationSans")
    heading_color = tokens.get("heading", "#1A1A2E")
    text_color = tokens.get("text", "#333333")
    muted_color = tokens.get("muted", "#888888")
    tokens.get("accent", "#2D5F8A")

    return {
        "h1": ParagraphStyle(
            "H1", fontName=heading_font, fontSize=22, leading=28,
            textColor=HexColor(heading_color), spaceBefore=24, spaceAfter=10,
            borderWidth=0, borderPadding=0,
        ),
        "h2": ParagraphStyle(
            "H2", fontName=heading_font, fontSize=16, leading=22,
            textColor=HexColor(heading_color), spaceBefore=18, spaceAfter=8,
        ),
        "h3": ParagraphStyle(
            "H3", fontName=heading_font, fontSize=13, leading=18,
            textColor=HexColor(heading_color), spaceBefore=12, spaceAfter=6,
        ),
        "body": ParagraphStyle(
            "Body", fontName=body_font, fontSize=10.5, leading=15,
            textColor=HexColor(text_color), alignment=TA_JUSTIFY,
            spaceBefore=3, spaceAfter=3,
        ),
        "bullet": ParagraphStyle(
            "Bullet", fontName=body_font, fontSize=10.5, leading=15,
            textColor=HexColor(text_color), leftIndent=18, bulletIndent=6,
            spaceBefore=2, spaceAfter=2, bulletFontName=body_font,
        ),
        "numbered": ParagraphStyle(
            "Numbered", fontName=body_font, fontSize=10.5, leading=15,
            textColor=HexColor(text_color), leftIndent=24, bulletIndent=6,
            spaceBefore=2, spaceAfter=2,
        ),
        "callout": ParagraphStyle(
            "Callout", fontName=body_font, fontSize=10, leading=14,
            textColor=HexColor(text_color), leftIndent=12, spaceBefore=6, spaceAfter=6,
            borderWidth=0, borderPadding=(6, 8, 6, 8),
            backColor=HexColor(tokens.get("callout_bg", "#F0F4F8")),
        ),
        "code": ParagraphStyle(
            "Code", fontName="LiberationMono", fontSize=9, leading=13,
            textColor=HexColor(text_color), leftIndent=10, spaceBefore=6, spaceAfter=6,
            backColor=HexColor("#F5F5F5"),
        ),
        "caption": ParagraphStyle(
            "Caption", fontName=body_font, fontSize=8.5, leading=12,
            textColor=HexColor(muted_color), alignment=TA_CENTER,
            spaceBefore=4, spaceAfter=8,
        ),
        "bibliography": ParagraphStyle(
            "Bibliography", fontName=body_font, fontSize=9.5, leading=14,
            textColor=HexColor(text_color), leftIndent=24, firstLineIndent=-24,
            spaceBefore=2, spaceAfter=2,
        ),
    }


# ---------------------------------------------------------------------------
# Block renderers
# ---------------------------------------------------------------------------

def _render_h1(block, styles, tokens, col_width):
    """Render h1 heading with accent rule."""
    elements = []
    elements.append(Paragraph(block["text"], styles["h1"]))
    elements.append(HRFlowable(
        width="30%", thickness=2, lineCap="round",
        color=HexColor(tokens.get("rule_color", tokens.get("accent", "#2D5F8A"))),
        spaceBefore=2, spaceAfter=8,
    ))
    return elements


def _render_table(block, tokens, col_width):
    """Render a data table with accent header and alternating rows."""
    headers = block.get("headers", [])
    rows = block.get("rows", [])
    caption = block.get("caption")

    accent = tokens.get("accent", "#2D5F8A")
    alt_bg = tokens.get("table_alt_bg", "#F5F8FA")

    data = []
    if headers:
        data.append(headers)
    data.extend(rows)

    # Column widths
    num_cols = len(headers) if headers else (len(rows[0]) if rows else 1)
    custom_widths = block.get("col_widths")
    if custom_widths:
        total = sum(custom_widths)
        col_widths = [col_width * (w / total) for w in custom_widths]
    else:
        col_widths = [col_width / num_cols] * num_cols

    style_commands = [
        ("FONTNAME", (0, 0), (-1, -1), "LiberationSans"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("LEADING", (0, 0), (-1, -1), 13),
        ("TEXTCOLOR", (0, 0), (-1, -1), HexColor("#333333")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#DDDDDD")),
    ]

    if headers:
        style_commands.extend([
            ("BACKGROUND", (0, 0), (-1, 0), HexColor(accent)),
            ("TEXTCOLOR", (0, 0), (-1, 0), HexColor("#FFFFFF")),
            ("FONTNAME", (0, 0), (-1, 0), "LiberationSans-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 9.5),
        ])
        # Alternating row tints
        for i in range(1, len(data)):
            if i % 2 == 0:
                style_commands.append(("BACKGROUND", (0, i), (-1, i), HexColor(alt_bg)))

    tbl = Table(data, colWidths=col_widths, repeatRows=1 if headers else 0)
    tbl.setStyle(TableStyle(style_commands))

    elements = [Spacer(1, 6), tbl]
    if caption:
        cap_style = ParagraphStyle("TblCap", fontName="LiberationSans", fontSize=8.5,
                                   leading=12, textColor=HexColor("#888888"), alignment=TA_CENTER)
        elements.append(Paragraph(caption, cap_style))
    elements.append(Spacer(1, 6))
    return elements


def _render_chart(block, tokens, col_width):
    """Render a chart using matplotlib, return as Image element."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return [Paragraph("[Chart: matplotlib not available]",
                          ParagraphStyle("Err", fontName="LiberationSans", fontSize=9))]

    chart_type = block.get("chart_type", "bar")
    labels = block.get("labels", [])
    datasets = block.get("datasets", [])
    title = block.get("title", "")
    x_label = block.get("x_label", "")
    y_label = block.get("y_label", "")
    caption = block.get("caption", "")

    accent = tokens.get("accent", "#2D5F8A")
    accent_lt = tokens.get("accent_lt", "#7BA3C4")

    fig, ax = plt.subplots(figsize=(col_width / 72, 3.5))

    color_cycle = [accent, accent_lt, "#E07A5F", "#81B29A", "#F2CC8F", "#3D405B"]

    if chart_type == "bar":
        import numpy as np
        x = np.arange(len(labels))
        width = 0.8 / max(len(datasets), 1)
        for i, ds in enumerate(datasets):
            ax.bar(x + i * width, ds["values"], width, label=ds.get("label", ""),
                   color=color_cycle[i % len(color_cycle)])
        ax.set_xticks(x + width * (len(datasets) - 1) / 2)
        ax.set_xticklabels(labels)
    elif chart_type == "line":
        for i, ds in enumerate(datasets):
            ax.plot(labels, ds["values"], marker="o", label=ds.get("label", ""),
                    color=color_cycle[i % len(color_cycle)])
    elif chart_type == "pie":
        values = datasets[0]["values"] if datasets else []
        ax.pie(values, labels=labels, colors=color_cycle[:len(labels)], autopct="%1.1f%%")

    if title:
        ax.set_title(title, fontsize=10)
    if x_label:
        ax.set_xlabel(x_label, fontsize=9)
    if y_label:
        ax.set_ylabel(y_label, fontsize=9)
    if datasets and any(ds.get("label") for ds in datasets) and chart_type != "pie":
        ax.legend(fontsize=8)
    ax.tick_params(labelsize=8)

    plt.tight_layout()
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    fig.savefig(tmp.name, dpi=150, bbox_inches="tight")
    plt.close(fig)

    elements = [Spacer(1, 6), Image(tmp.name, width=col_width, height=col_width * 0.55)]
    if caption:
        cap_style = ParagraphStyle("ChartCap", fontName="LiberationSans", fontSize=8.5,
                                   leading=12, textColor=HexColor("#888888"), alignment=TA_CENTER)
        elements.append(Paragraph(caption, cap_style))
    elements.append(Spacer(1, 6))
    return elements


def _render_flowchart(block, tokens, col_width):
    """Render a flowchart using matplotlib, return as Image element."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
    except ImportError:
        return [Paragraph("[Flowchart: matplotlib not available]",
                          ParagraphStyle("Err", fontName="LiberationSans", fontSize=9))]

    nodes = block.get("nodes", [])
    edges = block.get("edges", [])
    caption = block.get("caption", "")

    accent = tokens.get("accent", "#2D5F8A")

    # Simple top-to-bottom layout
    node_map = {}
    fig, ax = plt.subplots(figsize=(col_width / 72, max(2.5, len(nodes) * 0.8)))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, len(nodes) * 2 + 1)
    ax.set_aspect("equal")
    ax.axis("off")

    for i, node in enumerate(nodes):
        cx, cy = 5, len(nodes) * 2 - i * 2
        node_map[node["id"]] = (cx, cy)
        shape = node.get("shape", "rect")

        if shape == "oval":
            ellipse = mpatches.Ellipse((cx, cy), 3, 1, facecolor=accent, edgecolor="black", linewidth=1)
            ax.add_patch(ellipse)
        elif shape == "diamond":
            diamond = mpatches.FancyBboxPatch((cx - 1.2, cy - 0.6), 2.4, 1.2,
                                               boxstyle="round,pad=0.1", facecolor="#F2CC8F",
                                               edgecolor="black", linewidth=1)
            ax.add_patch(diamond)
        else:
            rect = mpatches.FancyBboxPatch((cx - 1.5, cy - 0.5), 3, 1,
                                            boxstyle="round,pad=0.1", facecolor="#E8F0FE",
                                            edgecolor="black", linewidth=1)
            ax.add_patch(rect)

        ax.text(cx, cy, node.get("label", ""), ha="center", va="center", fontsize=8, fontweight="bold")

    for edge in edges:
        src = node_map.get(edge["from"])
        dst = node_map.get(edge["to"])
        if src and dst:
            ax.annotate("", xy=(dst[0], dst[1] + 0.5), xytext=(src[0], src[1] - 0.5),
                        arrowprops=dict(arrowstyle="->", color="#333333", lw=1.2))
            label = edge.get("label", "")
            if label:
                mx = (src[0] + dst[0]) / 2 + 0.3
                my = (src[1] + dst[1]) / 2
                ax.text(mx, my, label, fontsize=7, color="#555555")

    plt.tight_layout()
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    fig.savefig(tmp.name, dpi=150, bbox_inches="tight")
    plt.close(fig)

    elements = [Spacer(1, 6), Image(tmp.name, width=col_width, height=col_width * 0.6)]
    if caption:
        cap_style = ParagraphStyle("FlowCap", fontName="LiberationSans", fontSize=8.5,
                                   leading=12, textColor=HexColor("#888888"), alignment=TA_CENTER)
        elements.append(Paragraph(caption, cap_style))
    elements.append(Spacer(1, 6))
    return elements


def _render_math(block, tokens, col_width):
    """Render LaTeX math via matplotlib mathtext."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return [Paragraph(f"[Math: {block.get('text', '')}]",
                          ParagraphStyle("Err", fontName="LiberationMono", fontSize=9))]

    text = block.get("text", "")
    caption = block.get("caption", "")
    label = block.get("label", "")

    fig, ax = plt.subplots(figsize=(col_width / 72, 1.2))
    ax.axis("off")
    ax.text(0.5, 0.5, f"${text}$", transform=ax.transAxes, fontsize=14,
            ha="center", va="center")
    plt.tight_layout()
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    fig.savefig(tmp.name, dpi=150, bbox_inches="tight")
    plt.close(fig)

    elements = [Spacer(1, 4), Image(tmp.name, width=col_width * 0.7, height=60)]
    cap_text = ""
    if label:
        cap_text += f"({label}) "
    if caption:
        cap_text += caption
    if cap_text:
        cap_style = ParagraphStyle("MathCap", fontName="LiberationSans", fontSize=8.5,
                                   leading=12, textColor=HexColor("#888888"), alignment=TA_CENTER)
        elements.append(Paragraph(cap_text, cap_style))
    elements.append(Spacer(1, 4))
    return elements


# ---------------------------------------------------------------------------
# Main body renderer
# ---------------------------------------------------------------------------

def render_body(tokens: dict, content_path: str, output_path: str):
    """Render body pages from a content.json file.

    Args:
        tokens: Design tokens from palette.generate_palette().
        content_path: Path to content.json file with block array.
        output_path: Path to write the body PDF.
    """
    with open(content_path, "r", encoding="utf-8") as f:
        blocks = json.load(f)

    margin_top = tokens.get("margin_top", 72)
    margin_bottom = tokens.get("margin_bottom", 60)
    margin_left = tokens.get("margin_left", 65)
    margin_right = tokens.get("margin_right", 55)
    col_width = PAGE_W - margin_left - margin_right

    doc = SimpleDocTemplate(
        output_path, pagesize=A4,
        topMargin=margin_top, bottomMargin=margin_bottom,
        leftMargin=margin_left, rightMargin=margin_right,
    )

    styles = _build_styles(tokens)
    elements = []
    numbered_counter = 0
    figure_counter = 0

    for block in blocks:
        btype = block.get("type", "body")
        text = block.get("text", "")

        if btype == "h1":
            numbered_counter = 0
            elements.extend(_render_h1(block, styles, tokens, col_width))

        elif btype == "h2":
            numbered_counter = 0
            elements.append(Paragraph(text, styles["h2"]))

        elif btype == "h3":
            numbered_counter = 0
            elements.append(Paragraph(text, styles["h3"]))

        elif btype == "body":
            numbered_counter = 0
            elements.append(Paragraph(text, styles["body"]))

        elif btype == "bullet":
            numbered_counter = 0
            elements.append(Paragraph(f"\u2022  {text}", styles["bullet"]))

        elif btype == "numbered":
            numbered_counter += 1
            elements.append(Paragraph(f"{numbered_counter}.  {text}", styles["numbered"]))

        elif btype == "callout":
            # Callout box with accent left border
            callout_data = [[Paragraph(text, styles["callout"])]]
            callout_tbl = Table(callout_data, colWidths=[col_width - 6])
            callout_tbl.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), HexColor(tokens.get("callout_bg", "#F0F4F8"))),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("LINEBEFOREKIND", (0, 0), (0, -1), "LINEBEFOREKIND"),
            ]))
            # Left border via table line
            accent_hex = tokens.get("callout_border", tokens.get("accent", "#2D5F8A"))
            callout_tbl.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), HexColor(tokens.get("callout_bg", "#F0F4F8"))),
                ("LEFTPADDING", (0, 0), (-1, -1), 14),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("LINEBEFORE", (0, 0), (0, -1), 3, HexColor(accent_hex)),
            ]))
            elements.append(Spacer(1, 4))
            elements.append(callout_tbl)
            elements.append(Spacer(1, 4))

        elif btype == "table":
            numbered_counter = 0
            elements.extend(_render_table(block, tokens, col_width))

        elif btype == "image" or btype == "figure":
            numbered_counter = 0
            img_path = block.get("path") or block.get("src", "")
            caption = block.get("caption", "")

            if btype == "figure":
                figure_counter += 1
                caption = f"Figure {figure_counter}: {caption}" if caption else f"Figure {figure_counter}"

            if img_path and os.path.exists(img_path):
                try:
                    from PIL import Image as PILImage
                    pil_img = PILImage.open(img_path)
                    iw, ih = pil_img.size
                    aspect = ih / iw
                    display_w = min(col_width, iw)
                    display_h = display_w * aspect
                    elements.append(Spacer(1, 6))
                    elements.append(Image(img_path, width=display_w, height=display_h))
                except Exception as exc:
                    logger.warning("Failed to read image dimensions for %s, using column width: %s", img_path, exc)
                    elements.append(Spacer(1, 6))
                    elements.append(Image(img_path, width=col_width))

                if caption:
                    elements.append(Paragraph(caption, styles["caption"]))
                elements.append(Spacer(1, 6))
            else:
                elements.append(Paragraph(f"[Image not found: {img_path}]", styles["caption"]))

        elif btype == "code":
            numbered_counter = 0
            # Escape XML entities for Paragraph
            safe_text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            code_lines = safe_text.split("\n")
            formatted = "<br/>".join(code_lines)

            code_data = [[Paragraph(f"<font face='LiberationMono' size='9'>{formatted}</font>",
                                    styles["code"])]]
            code_tbl = Table(code_data, colWidths=[col_width - 6])
            code_tbl.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), HexColor("#F5F5F5")),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("LINEBEFORE", (0, 0), (0, -1), 3, HexColor(tokens.get("accent", "#2D5F8A"))),
            ]))
            elements.append(Spacer(1, 4))
            elements.append(code_tbl)
            elements.append(Spacer(1, 4))

        elif btype == "math":
            numbered_counter = 0
            elements.extend(_render_math(block, tokens, col_width))

        elif btype == "chart":
            numbered_counter = 0
            elements.extend(_render_chart(block, tokens, col_width))

        elif btype == "flowchart":
            numbered_counter = 0
            elements.extend(_render_flowchart(block, tokens, col_width))

        elif btype == "bibliography":
            numbered_counter = 0
            bib_title = block.get("title", "References")
            elements.append(Paragraph(bib_title, styles["h2"]))
            for item in block.get("items", []):
                ref_id = item.get("id", "")
                ref_text = item.get("text", "")
                elements.append(Paragraph(f"[{ref_id}] {ref_text}", styles["bibliography"]))

        elif btype == "divider":
            numbered_counter = 0
            elements.append(Spacer(1, 6))
            elements.append(HRFlowable(
                width="100%", thickness=1, lineCap="round",
                color=HexColor(tokens.get("accent", "#2D5F8A")),
                spaceBefore=4, spaceAfter=4,
            ))

        elif btype == "caption":
            numbered_counter = 0
            elements.append(Paragraph(text, styles["caption"]))

        elif btype == "pagebreak":
            numbered_counter = 0
            elements.append(PageBreak())

        elif btype == "spacer":
            numbered_counter = 0
            elements.append(Spacer(1, block.get("pt", 12)))

    doc.build(elements, onFirstPage=_page_footer, onLaterPages=_page_footer)


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, script_dir)
    from palette import generate_palette

    parser = argparse.ArgumentParser(description="Render PDF body from content.json")
    parser.add_argument("--content", required=True, help="Path to content.json")
    parser.add_argument("--type", default="general", help="Document type")
    parser.add_argument("--accent", default=None, help="Accent color")
    parser.add_argument("--out", default="body.pdf", help="Output path")
    args = parser.parse_args()

    tokens = generate_palette(args.type, accent=args.accent)
    render_body(tokens, args.content, args.out)
    print(f"Body written to {args.out}")


if __name__ == "__main__":
    main()
