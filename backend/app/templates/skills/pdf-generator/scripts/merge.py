#!/usr/bin/env python3
"""
merge.py -- Merge multiple PDF files into one using pypdf.

Usage:
    python3 merge.py cover.pdf body.pdf --out report.pdf

Or import directly:
    from merge import merge_pdfs
    merge_pdfs(["cover.pdf", "body.pdf"], "report.pdf")
"""

import argparse

from pypdf import PdfReader, PdfWriter


def merge_pdfs(input_paths: list[str], output_path: str):
    """Merge multiple PDF files into a single output PDF.

    Args:
        input_paths: List of paths to PDF files to merge (in order).
        output_path: Path to write the merged PDF.
    """
    writer = PdfWriter()

    for path in input_paths:
        reader = PdfReader(path)
        for page in reader.pages:
            writer.add_page(page)

    with open(output_path, "wb") as f:
        writer.write(f)


def main():
    parser = argparse.ArgumentParser(description="Merge PDF files")
    parser.add_argument("inputs", nargs="+", help="Input PDF files")
    parser.add_argument("--out", required=True, help="Output PDF path")
    args = parser.parse_args()

    merge_pdfs(args.inputs, args.out)
    print(f"Merged {len(args.inputs)} PDFs into {args.out}")


if __name__ == "__main__":
    main()
