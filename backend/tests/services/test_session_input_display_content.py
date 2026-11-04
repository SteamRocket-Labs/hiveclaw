"""B2: the session title comes from what the member typed, never from the
runtime's JSON encoding of the input.

`dispatch_admitted_input` used to hand `start_web_chat_run` an empty
`display_content`, so the first turn of a conversation titled the session from
`content` — which `_runtime_content` renders as a JSON array whenever the input
has attachments. Production showed `chat_sessions.title` starting with
`[{"display_content":` (2026-11-04 run, screenshot 32).
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]


def test_runtime_display_content_extracts_the_member_text_from_content_parts():
    from app.services.session_input_dispatch import _runtime_display_content

    # A message with attachments: the model gets the internal prompt, the
    # member gets their own words back.
    parts = [
        {
            "type": "text",
            "text": "[File: sales.csv]\nFile location: workspace/uploads/sales.csv …",
            "display_content": "[📎 sales.csv]\n读这个文件",
            "file_name": "sales.csv",
        },
        {"type": "attachment_ref", "attachment": {"name": "sales.csv", "path": "workspace/uploads/sales.csv"}},
    ]
    assert _runtime_display_content(parts) == "[📎 sales.csv]\n读这个文件"

    # A plain message has no separate display text: the text is the message.
    assert _runtime_display_content([{"type": "text", "text": "hello"}]) == "hello"

    # Nothing to show: empty, never the JSON encoding.
    assert _runtime_display_content([]) == ""


def test_dispatch_passes_the_display_content_to_the_run_gate():
    source = (BACKEND / "app/services/session_input_dispatch.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == "_start_input_runtime"
    )
    # Find the start_web_chat_run(...) call and check display_content is the
    # extracted member text, not a hardcoded empty string.
    call = next(
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "start_web_chat_run"
    )
    display_kw = next(kw for kw in call.keywords if kw.arg == "display_content")
    assert isinstance(display_kw.value, ast.Name), "display_content must be derived, not a literal"
    assert display_kw.value.id != ""
