"""Lossless quoting for memory data beside trusted management references."""

import json


def quote_memory_text(value: str) -> str:
    # Keep data on one line and prevent a stored body from forging an id= label.
    return json.dumps(value, ensure_ascii=False).replace("=", "\\u003d")
