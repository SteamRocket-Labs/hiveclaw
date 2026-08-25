"""Shared V1/V2 user-checkpoint read semantics for session surfaces.

Session V2 stores the user's message as a ``human_input`` item whose
content-bearing lifecycles are ``accepted`` and ``revised``; the exact bytes
live in ``metadata['v2_payload']['content_parts']`` while the event row's
``content`` stays empty. Legacy V1 sessions store ``user_message`` rows with
inline content. Branch, session index, and session command surfaces must read
both generations through this module so the semantics cannot drift.

Rules (owner contract, 2026-08-26):

* Only ``accepted``/``revised`` are user checkpoints. ``queued``/``bound``/
  ``applied``/``cancelled``/``rejected`` are state facts, not extra user
  checkpoints.
* One checkpoint per ``human_input`` item: the latest ``revised`` wins over
  its superseded ``accepted`` row.
* Content renders through the same deterministic contract as live input:
  a single text part renders its exact bytes, anything else renders
  deterministic canonical JSON.
* ``assistant_final.completed`` is the V2 anchor of an assistant message for
  the existing regenerate mode; no other kinds are guessed here.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

HUMAN_INPUT_CHECKPOINT_LIFECYCLES = frozenset({"accepted", "revised"})
_ASSISTANT_FINAL_ROLE_LIFECYCLES = frozenset({"completed"})


def _event_metadata(event: Any) -> dict[str, Any]:
    metadata = getattr(event, "metadata_json", None)
    if isinstance(metadata, dict):
        return metadata
    metadata = getattr(event, "metadata", None)
    return metadata if isinstance(metadata, dict) else {}


def event_lifecycle(event: Any) -> str:
    lifecycle = getattr(event, "lifecycle", None)
    if isinstance(lifecycle, str) and lifecycle:
        return lifecycle
    event_type = str(getattr(event, "event_type", None) or "")
    _, _, lifecycle_suffix = event_type.rpartition(".")
    return lifecycle_suffix


def event_item_kind(event: Any) -> str:
    item_kind = getattr(event, "item_kind", None)
    if isinstance(item_kind, str) and item_kind:
        return item_kind
    event_type = str(getattr(event, "event_type", None) or "")
    kind, _, _ = event_type.rpartition(".")
    return kind


def _row_id(event: Any) -> str:
    """Stable row identity: T0-shaped rows carry ``event_id``; DB rows carry ``id``."""

    raw = getattr(event, "event_id", None)
    if raw is not None and str(raw):
        return str(raw)
    return str(getattr(event, "id", "") or "")


def _item_id(event: Any) -> str:
    """Stable item identity: ``item_id`` column, else the V2 payload's ``input_id``."""

    raw = getattr(event, "item_id", None)
    if raw is not None and str(raw):
        return str(raw)
    payload = _event_metadata(event).get("v2_payload")
    input_id = payload.get("input_id") if isinstance(payload, dict) else None
    if isinstance(input_id, str) and input_id:
        return input_id
    return _row_id(event)


def is_human_input_row(event: Any) -> bool:
    """True for any lifecycle row of a V2 HumanInput item (including state facts)."""

    return event_item_kind(event) == "human_input"


def is_human_input_checkpoint(event: Any) -> bool:
    """True for the content-bearing V2 HumanInput lifecycles (accepted/revised)."""

    return is_human_input_row(event) and event_lifecycle(event) in HUMAN_INPUT_CHECKPOINT_LIFECYCLES


def is_assistant_final_message(event: Any) -> bool:
    """True for a completed V2 assistant final (assistant anchor for regenerate)."""

    return event_item_kind(event) == "assistant_final" and event_lifecycle(event) in _ASSISTANT_FINAL_ROLE_LIFECYCLES


def event_role(event: Any) -> str | None:
    """Shared V2-aware role mapping for user/assistant-final anchors.

    Typed item identity wins: V2 HumanInput state facts (queued/bound/applied/
    cancelled/rejected) are never user anchors, and accepted/revised plus
    ``assistant_final.completed`` keep their typed roles even when stray
    legacy ``role`` metadata contradicts them. Legacy metadata role applies
    only to rows without a typed Session V2 identity. Legacy mappings stay
    with the callers: this module only owns the ``human_input``/
    ``assistant_final`` type truth so consumers cannot drift.
    """

    if is_human_input_row(event) and not is_human_input_checkpoint(event):
        return None
    if is_human_input_checkpoint(event):
        return "user"
    if is_assistant_final_message(event):
        return "assistant"
    metadata = _event_metadata(event)
    role = metadata.get("role")
    if isinstance(role, str) and role:
        return role
    if getattr(event, "event_type", None) == "user_message":
        return "user"
    return None


def render_human_input_content(content_parts: Any) -> str:
    """Deterministic lossless rendering, mirroring live input rendering.

    A single part's exact runtime bytes may live under ``text`` or ``content``
    (the public SessionHumanInputIn contract; ``text`` wins when both exist).
    Canonical JSON applies only to multi-part or non-string shapes.
    """

    if not isinstance(content_parts, list):
        return ""
    if len(content_parts) == 1 and isinstance(content_parts[0], dict):
        for key in ("text", "content"):
            value = content_parts[0].get(key)
            if isinstance(value, str):
                return value
    return json.dumps(content_parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def user_checkpoint_content(event: Any) -> str:
    """Exact current user content: V2 renders ``content_parts``; a payload that
    carried inline ``content`` instead (mirrored onto the row) falls back to the
    row's own durable content; legacy rows return their row content."""

    if is_human_input_checkpoint(event):
        payload = _event_metadata(event).get("v2_payload")
        content_parts = payload.get("content_parts") if isinstance(payload, dict) else None
        rendered = render_human_input_content(content_parts)
        if rendered:
            return rendered
    return str(getattr(event, "content", None) or "")


def user_checkpoint_events(events: Iterable[Any]) -> list[Any]:
    """Ordered user checkpoints: one per V2 HumanInput item (latest accepted/revised wins),
    legacy user-role rows pass through unchanged, empty-content rows drop."""

    latest_by_item: dict[str, Any] = {}
    for event in sorted(events, key=lambda item: int(getattr(item, "sequence", 0) or 0)):
        if event_role(event) != "user":
            continue
        if is_human_input_checkpoint(event):
            latest_by_item[_item_id(event)] = event
        else:
            latest_by_item[_row_id(event)] = event
    ordered = sorted(latest_by_item.values(), key=lambda item: int(getattr(item, "sequence", 0) or 0))
    return [event for event in ordered if user_checkpoint_content(event).strip()]
