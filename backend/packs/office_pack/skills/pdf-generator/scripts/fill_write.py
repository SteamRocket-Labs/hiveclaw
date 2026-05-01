#!/usr/bin/env python3
"""
fill_write.py -- Fill form fields in a PDF using pypdf.

Usage:
    python3 fill_write.py --input form.pdf --out filled.pdf \
        --values '{"FirstName": "Jane", "Agree": "true", "Country": "US"}'

Or import directly:
    from fill_write import fill_fields
    fill_fields("form.pdf", "filled.pdf", {"FirstName": "Jane", "Agree": "true"})
"""

import argparse
import json
import sys

from pypdf import PdfReader, PdfWriter


def fill_fields(input_path: str, output_path: str, values: dict):
    """Fill form fields in a PDF and write to output.

    Args:
        input_path: Path to the input PDF with form fields.
        output_path: Path to write the filled PDF.
        values: Dict mapping field names to values.
            - text fields: any string
            - checkbox: "true" or "false"
            - dropdown: must match one of the field's options
            - radio: must match a radio value
    """
    reader = PdfReader(input_path)
    writer = PdfWriter()
    writer.append(reader)

    # Fill fields on each page
    for page_num in range(len(writer.pages)):
        writer.update_page_form_field_values(writer.pages[page_num], values)

    with open(output_path, "wb") as f:
        writer.write(f)


def main():
    parser = argparse.ArgumentParser(description="Fill PDF form fields")
    parser.add_argument("--input", required=True, help="Input PDF path")
    parser.add_argument("--out", required=True, help="Output PDF path")
    parser.add_argument("--values", required=True, help="JSON dict of field values")
    args = parser.parse_args()

    try:
        values = json.loads(args.values)
    except json.JSONDecodeError as e:
        print(f"Error parsing --values JSON: {e}", file=sys.stderr)
        sys.exit(1)

    fill_fields(args.input, args.out, values)

    filled_count = len(values)
    print(f"Filled {filled_count} field(s) -> {args.out}")


if __name__ == "__main__":
    main()
