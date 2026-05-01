#!/usr/bin/env python3
"""
fill_inspect.py -- Inspect form fields in a PDF using pypdf.

Usage:
    python3 fill_inspect.py --input form.pdf
    python3 fill_inspect.py --input form.pdf --json

Or import directly:
    from fill_inspect import inspect_fields
    fields = inspect_fields("form.pdf")
"""

import argparse
import json

from pypdf import PdfReader


def inspect_fields(input_path: str) -> list[dict]:
    """Extract form field metadata from a PDF.

    Args:
        input_path: Path to the PDF file.

    Returns:
        List of dicts with field info: name, type, value, options, rect.
    """
    reader = PdfReader(input_path)
    fields = []

    if not reader.get_fields():
        return fields

    for name, field_obj in reader.get_fields().items():
        field_type = field_obj.get("/FT", "")
        # Map PDF field types to human-readable names
        type_map = {
            "/Tx": "text",
            "/Btn": "checkbox",
            "/Ch": "dropdown",
        }
        human_type = type_map.get(str(field_type), str(field_type))

        # For checkboxes, check if it's actually a radio group
        if human_type == "checkbox":
            flags = field_obj.get("/Ff", 0)
            if isinstance(flags, int) and (flags & (1 << 15)):  # Radio flag
                human_type = "radio"

        field_info = {
            "name": name,
            "type": human_type,
            "value": str(field_obj.get("/V", "")),
        }

        # For dropdowns/radios, include options
        if human_type in ("dropdown", "radio"):
            opts = field_obj.get("/Opt", [])
            if opts:
                field_info["options"] = [str(o) for o in opts]

        fields.append(field_info)

    return fields


def main():
    parser = argparse.ArgumentParser(description="Inspect PDF form fields")
    parser.add_argument("--input", required=True, help="Input PDF path")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    fields = inspect_fields(args.input)

    if not fields:
        print("No form fields found in this PDF.")
        return

    if args.json:
        print(json.dumps(fields, indent=2, ensure_ascii=False))
    else:
        print(f"Found {len(fields)} form field(s):\n")
        for f in fields:
            print(f"  Name: {f['name']}")
            print(f"  Type: {f['type']}")
            if f.get("value"):
                print(f"  Current value: {f['value']}")
            if f.get("options"):
                print(f"  Options: {f['options']}")
            print()


if __name__ == "__main__":
    main()
