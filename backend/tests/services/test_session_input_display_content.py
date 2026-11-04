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
    # …and the name is bound in this function from the member's parts, so the
    # value handed to the runtime is `_runtime_display_content(parts)`.
    binding = next(
        node
        for node in function.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == display_kw.value.id
    )
    assert isinstance(binding.value, ast.Call)
    func = binding.value.func
    assert isinstance(func, ast.Name) and func.id == "_runtime_display_content"
    # The actual value for a real attachment input is the member's sentence
    # (verified end-to-end by the pure function above), which is also what
    # `start_web_chat_run` titles the session from.
    from app.services.session_input_dispatch import _runtime_display_content

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


def test_prompt_stays_the_model_content_and_plan_mode_reads_the_member_sentence():
    """Ruled keep-as-is (B2 r2): dispatch's display_content is not title-only.

    `RuntimeTask.prompt` keeps the runtime JSON encoding byte-for-byte (the
    model still receives every part), while Plan Mode classification reads the
    member's own sentence via `classification_content` — an attachment whose
    body says 计划模式 with a plain request must not enter plan mode.
    """
    import json

    from app.services import plan_mode_core
    from app.services.session_input_dispatch import _runtime_content, _runtime_display_content

    class _Row:
        content_parts_json = [
            {
                "type": "text",
                "text": "[File: sales.csv] 附注：这份附件正文包含“计划模式”字样。\nQuestion: 总结这个附件",
                "display_content": "[📎 sales.csv]\n总结这个附件",
                "file_name": "sales.csv",
            },
            {"type": "attachment_ref", "attachment": {"name": "sales.csv", "path": "workspace/uploads/sales.csv"}},
        ]

    content = _runtime_content(_Row())
    display = _runtime_display_content(list(_Row.content_parts_json))
    # The model-facing prompt is the canonical runtime JSON, byte-identical to
    # the pre-B2 encoding (sort_keys, ensure_ascii=False, compact separators).
    assert content == json.dumps(
        list(_Row.content_parts_json), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    assert "计划模式" in content
    # The member's words are what display surfaces and Plan Mode classify on.
    assert display == "[📎 sales.csv]\n总结这个附件"
    assert "计划模式" not in display
    assert plan_mode_core.classify_plan_mode_entry(display).mode == "none"
    assert plan_mode_core.classify_plan_mode_entry(content).mode == "explicit"
