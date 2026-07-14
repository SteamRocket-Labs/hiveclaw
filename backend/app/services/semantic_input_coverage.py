"""Coverage-preserving model input preparation for semantic work.

Provider context windows are physical limits.  They may require multiple model
passes, but they must never authorize the platform to choose which semantic
bytes matter.  This module splits every supplied source into hash-addressed
chunks, asks the model to review every chunk, and reduces those reviews while
persisting a complete coverage ledger.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Iterable


ReviewChunk = Callable[[str, str], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class SemanticInputChunk:
    source_ref: str
    char_start: int
    char_end: int
    text: str
    sha256: str

    def to_manifest(self) -> dict[str, object]:
        return {
            "source_ref": self.source_ref,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "chars": len(self.text),
            "sha256": self.sha256,
        }


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _render_sources(sections: Iterable[tuple[str, str]]) -> str:
    rendered: list[str] = []
    for source_ref, text in sections:
        rendered.append(
            f'<semantic_source ref="{source_ref}" chars="{len(text)}" sha256="{_sha256(text)}">\n'
            f"{text}\n"
            "</semantic_source>"
        )
    return "\n\n".join(rendered)


def build_semantic_input_chunks(
    sections: Iterable[tuple[str, str]],
    *,
    max_chars: int,
) -> list[SemanticInputChunk]:
    """Split every source byte-for-byte; no source or tail is discarded."""

    chunk_chars = max(64, max_chars)
    chunks: list[SemanticInputChunk] = []
    for raw_ref, raw_text in sections:
        source_ref = str(raw_ref)
        text = str(raw_text or "")
        if not text:
            chunks.append(
                SemanticInputChunk(
                    source_ref=source_ref,
                    char_start=0,
                    char_end=0,
                    text="",
                    sha256=_sha256(""),
                )
            )
            continue
        # Overlap keeps concepts that cross an arbitrary character boundary
        # intact for at least one model pass. The manifest still records exact
        # ranges and hashes, so overlap adds context without hiding coverage.
        overlap_chars = min(512, max(64, chunk_chars // 2), chunk_chars - 1)
        step_chars = max(chunk_chars - overlap_chars, 1)
        for start in range(0, len(text), step_chars):
            value = text[start : start + chunk_chars]
            chunks.append(
                SemanticInputChunk(
                    source_ref=source_ref,
                    char_start=start,
                    char_end=start + len(value),
                    text=value,
                    sha256=_sha256(value),
                )
            )
            if start + len(value) >= len(text):
                break
    return chunks


def _write_manifest(path: Path, payload: dict[str, object]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return _sha256(canonical)


def _group_nodes(nodes: list[str], *, max_chars: int) -> list[list[str]]:
    """Build shrinking reduce groups without dropping oversized model notes."""

    if len(nodes) <= 1:
        return [nodes]
    target = max(max_chars - 1_500, 2_000)
    groups: list[list[str]] = []
    index = 0
    while index < len(nodes):
        group = [nodes[index]]
        size = len(nodes[index])
        index += 1
        # At least two nodes per non-final group guarantees progress.  The
        # prompt may exceed the target only when a model itself returned an
        # unexpectedly oversized note; no platform truncation is performed.
        while index < len(nodes) and len(group) < 8:
            next_size = len(nodes[index])
            if len(group) >= 2 and size + next_size > target:
                break
            group.append(nodes[index])
            size += next_size
            index += 1
        groups.append(group)
    if len(groups) == len(nodes):
        return [nodes[index : index + 2] for index in range(0, len(nodes), 2)]
    return groups


async def prepare_covered_semantic_input(
    *,
    phase: str,
    sections: list[tuple[str, str]],
    max_chars: int,
    coverage_path: Path,
    review_chunk: ReviewChunk,
) -> str:
    """Return full inline sources or model-authored notes covering every chunk.

    The persisted manifest is the mechanical proof of byte coverage.  The
    model remains the only owner of semantic selection and synthesis.
    """

    normalized = [(str(ref), str(text or "")) for ref, text in sections]
    full_text = _render_sources(normalized)
    chunks = build_semantic_input_chunks(normalized, max_chars=max_chars)
    manifest: dict[str, object] = {
        "schema": "hive.semantic_input_coverage.v1",
        "phase": phase,
        "complete": False,
        "source_chars": sum(len(text) for _, text in normalized),
        "sources": [{"source_ref": ref, "chars": len(text), "sha256": _sha256(text)} for ref, text in normalized],
        "chunks": [chunk.to_manifest() for chunk in chunks],
        "map_receipts": [],
        "reduce_receipts": [],
    }
    _write_manifest(coverage_path, manifest)

    if len(full_text) <= max_chars:
        manifest["complete"] = True
        manifest["mode"] = "inline_full"
        manifest_sha = _write_manifest(coverage_path, manifest)
        return (
            f'<coverage_manifest_ref path="{coverage_path.as_posix()}" sha256="{manifest_sha}" '
            f'complete="true" source_chars="{manifest["source_chars"]}" />\n\n{full_text}'
        )

    notes: list[str] = []
    map_receipts: list[dict[str, object]] = []
    total = len(chunks)
    for index, chunk in enumerate(chunks, start=1):
        map_phase = f"{phase}_coverage_map_{index}_of_{total}"
        prompt = (
            "Review this exact semantic-input chunk. Preserve every decision-relevant fact, conflict, "
            "uncertainty, source reference, and tail detail for a later reducer. Do not make a final "
            "accept/reject decision. Return coverage notes only.\n\n"
            f'<coverage_chunk source_ref="{chunk.source_ref}" char_range="{chunk.char_start}-{chunk.char_end}" '
            f'sha256="{chunk.sha256}">\n{chunk.text}\n</coverage_chunk>'
        )
        note = str(await review_chunk(map_phase, prompt) or "").strip()
        if not note:
            raise ValueError(f"semantic coverage map returned empty notes for {chunk.source_ref}")
        notes.append(note)
        map_receipts.append({**chunk.to_manifest(), "phase": map_phase, "notes_sha256": _sha256(note)})

    reduce_receipts: list[dict[str, object]] = []
    level = 1
    while len(notes) > 1:
        groups = _group_nodes(notes, max_chars=max_chars)
        reduced: list[str] = []
        for index, group in enumerate(groups, start=1):
            reduce_phase = f"{phase}_coverage_reduce_level_{level}_{index}_of_{len(groups)}"
            prompt = (
                "Synthesize these model-authored coverage notes without dropping source references, "
                "conflicts, exceptions, uncertainty, or decision-relevant tail evidence. Return coverage "
                "notes only; do not make the final domain decision.\n\n"
                + "\n\n".join(
                    f'<coverage_note index="{node_index}">\n{node}\n</coverage_note>'
                    for node_index, node in enumerate(group, start=1)
                )
            )
            note = str(await review_chunk(reduce_phase, prompt) or "").strip()
            if not note:
                raise ValueError(f"semantic coverage reducer returned empty notes at level {level}")
            reduced.append(note)
            reduce_receipts.append(
                {
                    "phase": reduce_phase,
                    "input_count": len(group),
                    "input_sha256": _sha256("\n\n".join(group)),
                    "notes_sha256": _sha256(note),
                }
            )
        notes = reduced
        level += 1

    manifest["complete"] = True
    manifest["mode"] = "model_map_reduce"
    manifest["map_receipts"] = map_receipts
    manifest["reduce_receipts"] = reduce_receipts
    manifest["final_notes_sha256"] = _sha256(notes[0])
    manifest_sha = _write_manifest(coverage_path, manifest)
    return (
        f'<coverage_manifest_ref path="{coverage_path.as_posix()}" sha256="{manifest_sha}" '
        f'complete="true" source_chars="{manifest["source_chars"]}" />\n'
        "<model_coverage_notes>\n"
        f"{notes[0]}\n"
        "</model_coverage_notes>"
    )
