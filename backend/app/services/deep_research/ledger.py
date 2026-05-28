from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from app.services.deep_research.grading import grade_source
from app.services.deep_research.schemas import (
    ClaimRecord,
    ClaimStatus,
    SourceRecord,
    SourceType,
    new_id,
    to_jsonable,
)


class EvidenceLedger:
    """Durable source and claim ledger for one deep-research run."""

    def __init__(self, artifact_dir: str | Path):
        self.artifact_dir = Path(artifact_dir)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.sources_path = self.artifact_dir / "sources.jsonl"
        self.claims_path = self.artifact_dir / "claims.jsonl"
        self.sources: dict[str, SourceRecord] = {}
        self.claims: list[ClaimRecord] = []

    def add_source(
        self,
        *,
        url: str,
        title: str,
        publisher: str,
        source_type: SourceType = SourceType.UNKNOWN,
        content: str,
        published_at: str | None = None,
        lane_id: str = "",
        query: str = "",
        fetch_tool: str = "",
        source_id: str = "",
    ) -> SourceRecord:
        # P4: preserve a stable id assigned at fetch time; only mint one if absent or colliding.
        resolved_id = source_id.strip() if source_id and source_id.strip() not in self.sources else new_id("src")
        record = SourceRecord(
            source_id=resolved_id,
            url=url.strip(),
            title=title.strip() or url.strip(),
            publisher=publisher.strip(),
            source_type=source_type,
            content=content.strip(),
            published_at=published_at,
            lane_id=lane_id,
            query=query,
            fetch_tool=fetch_tool,
        )
        record.evidence_tier, record.evidence_grade = grade_source(record)
        self.sources[record.source_id] = record
        self._append_jsonl(self.sources_path, record)
        return record

    def add_claim(
        self,
        *,
        text: str,
        status: ClaimStatus,
        source_ids: list[str] | None = None,
        evidence: str = "",
        notes: str = "",
        contradiction_group: str | None = None,
    ) -> ClaimRecord:
        valid_source_ids = [source_id for source_id in (source_ids or []) if source_id in self.sources]
        effective_status = ClaimStatus(status)
        if effective_status in {
            ClaimStatus.VERIFIED,
            ClaimStatus.INFERRED,
            ClaimStatus.CONTRADICTED,
            ClaimStatus.STALE,
        }:
            if not valid_source_ids:
                effective_status = ClaimStatus.UNSUPPORTED
        record = ClaimRecord(
            claim_id=new_id("claim"),
            text=text.strip(),
            status=effective_status,
            source_ids=valid_source_ids,
            evidence=evidence.strip(),
            notes=notes.strip(),
            contradiction_group=contradiction_group,
        )
        self.claims.append(record)
        self._append_jsonl(self.claims_path, record)
        return record

    def summary(self) -> dict[str, int]:
        counts = Counter(claim.status.value for claim in self.claims)
        return {
            "source_count": len(self.sources),
            "claim_count": len(self.claims),
            "verified_claims": counts[ClaimStatus.VERIFIED.value],
            "inferred_claims": counts[ClaimStatus.INFERRED.value],
            "contradicted_claims": counts[ClaimStatus.CONTRADICTED.value],
            "stale_claims": counts[ClaimStatus.STALE.value],
            "unsupported_claims": counts[ClaimStatus.UNSUPPORTED.value],
        }

    @staticmethod
    def _append_jsonl(path: Path, record) -> None:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(to_jsonable(record), ensure_ascii=False) + "\n")
