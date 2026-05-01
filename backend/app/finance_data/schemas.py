"""Finance data schemas with field-level source attribution."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class MarketRegion(StrEnum):
    US = "us"
    HK = "hk"
    CN_A = "cn_a"
    GLOBAL = "global"


class EntityType(StrEnum):
    COMPANY = "company"
    PERSON = "person"
    FUND = "fund"
    LP = "lp"
    DEAL = "deal"
    SECURITY = "security"


class SourceRecord(BaseModel):
    source_id: str
    provider: str
    url: str | None = None
    filing_id: str | None = None
    retrieved_at: datetime
    license: str | None = None
    credential_scope: str = "public"
    raw_reference: dict[str, Any] = Field(default_factory=dict)


class SourceLedger(BaseModel):
    """Map normalized fields back to source records."""

    records: dict[str, SourceRecord] = Field(default_factory=dict)
    field_sources: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    def add_record(self, record: SourceRecord) -> None:
        self.records[record.source_id] = record

    def link_field(self, field_path: str, source_id: str) -> None:
        if source_id not in self.records:
            raise KeyError(f"Unknown source_id: {source_id}")
        existing = list(self.field_sources.get(field_path, ()))
        if source_id not in existing:
            existing.append(source_id)
        self.field_sources[field_path] = tuple(existing)

    def sources_for_field(self, field_path: str) -> tuple[SourceRecord, ...]:
        return tuple(self.records[source_id] for source_id in self.field_sources.get(field_path, ()) if source_id in self.records)

    def is_verified(self, field_path: str) -> bool:
        return bool(self.sources_for_field(field_path))

    def verification_label(self, field_path: str) -> str:
        return "VERIFIED" if self.is_verified(field_path) else "[UNVERIFIED]"

    def all_source_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.records))


class EntityMasterRecord(BaseModel):
    entity_id: str
    name: str
    entity_type: EntityType
    region: MarketRegion
    identifiers: dict[str, str] = Field(default_factory=dict)
    source_ids: tuple[str, ...] = ()


class FilingRecord(BaseModel):
    filing_id: str
    entity_id: str
    market: MarketRegion
    form_type: str
    filed_at: datetime | None = None
    url: str | None = None
    source_id: str


class FundingRound(BaseModel):
    round_id: str
    entity_id: str
    announced_at: datetime | None = None
    round_type: str | None = None
    amount: float | None = None
    currency: str | None = None
    investors: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()


class IPOEvent(BaseModel):
    ipo_id: str
    entity_id: str
    market: MarketRegion
    status: str
    expected_listing_date: datetime | None = None
    source_ids: tuple[str, ...] = ()
