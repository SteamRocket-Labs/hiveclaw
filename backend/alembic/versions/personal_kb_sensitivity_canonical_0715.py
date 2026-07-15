"""Canonicalize Personal KB sensitivity labels and enforce their schema contract.

Revision ID: personal_kb_sensitivity_canonical_0715
Revises: memory_context_warning_0714
Create Date: 2026-07-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "personal_kb_sensitivity_canonical_0715"
down_revision = "memory_context_warning_0714"
branch_labels = None
depends_on = None


_DOCUMENT_RECOVERY_KEY = "_personal_kb_sensitivity_0715_legacy_sensitivity_original"
_PROPOSAL_RECOVERY_PREFIX = "migration:personal_kb_sensitivity_0715:legacy_sensitivity_original:"
_CANONICAL_VALUES = "'PL1_public','PL2_pii','PL3_sensitive','PL4_credential'"
_CANONICAL_CASE = """
CASE lower(trim(sensitivity))
    WHEN 'public' THEN 'PL1_public'
    WHEN 'internal' THEN 'PL1_public'
    WHEN 'pl1' THEN 'PL1_public'
    WHEN 'pl1_public' THEN 'PL1_public'
    WHEN 'pii' THEN 'PL2_pii'
    WHEN 'pl2' THEN 'PL2_pii'
    WHEN 'pl2_pii' THEN 'PL2_pii'
    WHEN 'private' THEN 'PL3_sensitive'
    WHEN 'confidential' THEN 'PL3_sensitive'
    WHEN 'secret' THEN 'PL3_sensitive'
    WHEN 'restricted' THEN 'PL3_sensitive'
    WHEN 'sensitive' THEN 'PL3_sensitive'
    WHEN 'pl3' THEN 'PL3_sensitive'
    WHEN 'pl3_sensitive' THEN 'PL3_sensitive'
    WHEN 'credential' THEN 'PL4_credential'
    WHEN 'credentials' THEN 'PL4_credential'
    WHEN 'pl4' THEN 'PL4_credential'
    WHEN 'pl4_credential' THEN 'PL4_credential'
    ELSE 'PL3_sensitive'
END
"""


def _set_rls(table: str, *, enabled: bool) -> None:
    action = "ENABLE" if enabled else "DISABLE"
    op.execute(f"ALTER TABLE {table} {action} ROW LEVEL SECURITY")
    if enabled:
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def upgrade() -> None:
    for table in ("knowledge_documents", "personal_knowledge_proposals"):
        _set_rls(table, enabled=False)

    op.execute(
        sa.text(
            f"""
            UPDATE knowledge_documents
            SET doc_metadata_json = jsonb_set(
                    COALESCE(doc_metadata_json, '{{}}'::jsonb),
                    '{{{_DOCUMENT_RECOVERY_KEY}}}',
                    to_jsonb(sensitivity),
                    true
                ),
                sensitivity = {_CANONICAL_CASE}
            WHERE sensitivity NOT IN ({_CANONICAL_VALUES})
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            UPDATE personal_knowledge_proposals
            SET policy_reason_codes_json = COALESCE(policy_reason_codes_json, '[]'::jsonb)
                    || jsonb_build_array('{_PROPOSAL_RECOVERY_PREFIX}' || sensitivity),
                sensitivity = {_CANONICAL_CASE}
            WHERE sensitivity NOT IN ({_CANONICAL_VALUES})
            """
        )
    )
    op.alter_column(
        "knowledge_documents",
        "sensitivity",
        existing_type=sa.String(length=30),
        server_default="PL1_public",
        existing_nullable=False,
    )
    op.create_check_constraint(
        "ck_knowledge_documents_sensitivity",
        "knowledge_documents",
        f"sensitivity IN ({_CANONICAL_VALUES})",
    )
    op.create_check_constraint(
        "ck_personal_kb_proposal_sensitivity",
        "personal_knowledge_proposals",
        f"sensitivity IN ({_CANONICAL_VALUES})",
    )

    for table in ("knowledge_documents", "personal_knowledge_proposals"):
        _set_rls(table, enabled=True)


def downgrade() -> None:
    for table in ("knowledge_documents", "personal_knowledge_proposals"):
        _set_rls(table, enabled=False)

    op.drop_constraint(
        "ck_personal_kb_proposal_sensitivity",
        "personal_knowledge_proposals",
        type_="check",
    )
    op.drop_constraint(
        "ck_knowledge_documents_sensitivity",
        "knowledge_documents",
        type_="check",
    )
    op.alter_column(
        "knowledge_documents",
        "sensitivity",
        existing_type=sa.String(length=30),
        server_default="internal",
        existing_nullable=False,
    )
    op.execute(
        sa.text(
            f"""
            UPDATE knowledge_documents
            SET sensitivity = doc_metadata_json ->> '{_DOCUMENT_RECOVERY_KEY}',
                doc_metadata_json = doc_metadata_json - '{_DOCUMENT_RECOVERY_KEY}'
            WHERE doc_metadata_json ? '{_DOCUMENT_RECOVERY_KEY}'
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            WITH recovered AS (
                SELECT proposal.id,
                       max(substring(item.value FROM char_length('{_PROPOSAL_RECOVERY_PREFIX}') + 1))
                           FILTER (WHERE item.value LIKE '{_PROPOSAL_RECOVERY_PREFIX}%') AS original,
                       COALESCE(
                           jsonb_agg(item.value)
                               FILTER (WHERE item.value NOT LIKE '{_PROPOSAL_RECOVERY_PREFIX}%'),
                           '[]'::jsonb
                       ) AS cleaned_reason_codes
                FROM personal_knowledge_proposals AS proposal
                CROSS JOIN LATERAL jsonb_array_elements_text(
                    COALESCE(proposal.policy_reason_codes_json, '[]'::jsonb)
                ) AS item(value)
                GROUP BY proposal.id
            )
            UPDATE personal_knowledge_proposals AS proposal
            SET sensitivity = recovered.original,
                policy_reason_codes_json = recovered.cleaned_reason_codes
            FROM recovered
            WHERE proposal.id = recovered.id
              AND recovered.original IS NOT NULL
            """
        )
    )

    for table in ("knowledge_documents", "personal_knowledge_proposals"):
        _set_rls(table, enabled=True)
