"""Canonical focus.md task parsing and rendering helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass

_CHECKLIST_RE = re.compile(
    r"^\s*-\s*\[(?P<mark>[ xX])\]\s*(?P<task_id>[^:\n]+?)\s*::\s*(?P<description>.+?)\s*$"
)


@dataclass(frozen=True, slots=True)
class FocusTask:
    task_id: str
    description: str
    completed: bool
    raw_line: str


def normalize_focus_task_id(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip()).strip("_").lower()
    return cleaned or "task"


def normalize_focus_task_entry(raw_item: str, index: int) -> tuple[str, str]:
    item = raw_item.strip().lstrip("-*•").strip()
    item = re.sub(r"^\[[ xX/]\]\s*", "", item)
    item = re.sub(r"^\d+[\.\)]\s*", "", item)
    if "::" in item:
        raw_task_id, raw_description = item.split("::", 1)
        description = raw_description.strip()
        task_id = normalize_focus_task_id(raw_task_id)
        if description:
            return task_id, description
    return f"task_{index}", item.strip()


def render_focus_tasks(task_items: list[str], fallback: list[str]) -> tuple[str, list[tuple[str, str]]]:
    items = task_items or fallback
    normalized: list[tuple[str, str]] = []
    for index, raw_item in enumerate(items, 1):
        task_id, description = normalize_focus_task_entry(raw_item, index)
        if description:
            normalized.append((task_id, description))
    if not normalized:
        normalized = [("task_1", "Understand the mission, verify capabilities, and deliver a first visible outcome.")]
    rendered = "\n".join(f"- [ ] {task_id} :: {description}" for task_id, description in normalized)
    return rendered, normalized


def parse_focus_tasks(text: str) -> list[FocusTask]:
    tasks: list[FocusTask] = []
    for line in text.splitlines():
        match = _CHECKLIST_RE.match(line)
        if not match:
            continue
        task_id = normalize_focus_task_id(match.group("task_id"))
        tasks.append(
            FocusTask(
                task_id=task_id,
                description=match.group("description").strip(),
                completed=match.group("mark").lower() == "x",
                raw_line=line.strip(),
            )
        )
    return tasks


def extract_completed_focus_refs(text: str) -> set[str]:
    return {task.task_id for task in parse_focus_tasks(text) if task.completed}
