"""Close Company Ontology runtime identity and candidate persistence gaps.

Revision ID: company_ontology_runtime_0724
Revises: company_knowledge_runtime_0724
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "company_ontology_runtime_0724"
down_revision: str | None = "company_knowledge_runtime_0724"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COMPANY_TENANT_TABLES = (
    "company_knowledge_source_contracts",
    "company_knowledge_sources",
    "company_knowledge_evidence",
    "company_knowledge_import_jobs",
    "company_knowledge_proposals",
    "company_knowledge_reviews",
    "company_knowledge_publications",
    "company_knowledge_events",
    "company_knowledge_outbox",
    "company_ontology_packages",
    "company_ontology_package_versions",
    "company_ontology_package_installations",
    "company_ontology_activations",
    "company_ontology_curation_runs",
    "company_ontology_releases",
    "company_ontology_object_types",
    "company_ontology_property_types",
    "company_ontology_link_types",
    "company_ontology_event_types",
    "company_ontology_rule_definitions",
    "company_ontology_action_types",
    "company_ontology_objects",
    "company_ontology_object_identities",
    "company_ontology_assertions",
    "company_ontology_links",
    "company_ontology_events",
    "company_ontology_evidence_bindings",
    "company_ontology_release_items",
)


def _enable_strict_rls(table: str) -> None:
    predicate = (
        "current_setting('app.current_tenant_id', true) = 'BYPASS' "
        "OR tenant_id::text = current_setting('app.current_tenant_id', true)"
    )
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    op.execute(f'DROP POLICY IF EXISTS "tenant_isolation_{table}" ON "{table}"')
    op.execute(f'CREATE POLICY "tenant_isolation_{table}" ON "{table}" USING ({predicate}) WITH CHECK ({predicate})')


def upgrade() -> None:
    op.drop_constraint(
        "uq_company_ontology_activation_version",
        "company_ontology_activations",
        type_="unique",
    )
    op.execute(
        """
        WITH ranked AS (
          SELECT
            id,
            row_number() OVER (
              PARTITION BY tenant_id, namespace
              ORDER BY created_at, id
            ) AS canonical_version
          FROM company_ontology_activations
        )
        UPDATE company_ontology_activations AS activation
        SET activation_version = ranked.canonical_version
        FROM ranked
        WHERE activation.id = ranked.id
          AND activation.activation_version <> ranked.canonical_version
        """
    )
    op.create_unique_constraint(
        "uq_company_ontology_activation_version",
        "company_ontology_activations",
        ["tenant_id", "namespace", "activation_version"],
    )
    op.add_column(
        "company_ontology_activations",
        sa.Column("idempotency_key", sa.String(length=300), nullable=True),
    )
    op.execute(
        "UPDATE company_ontology_activations "
        "SET idempotency_key = 'legacy-activation:' || id::text "
        "WHERE idempotency_key IS NULL"
    )
    op.alter_column("company_ontology_activations", "idempotency_key", nullable=False)
    op.create_unique_constraint(
        "uq_company_ontology_activation_idempotency",
        "company_ontology_activations",
        ["tenant_id", "idempotency_key"],
    )

    op.add_column(
        "company_ontology_curation_runs",
        sa.Column(
            "candidate_patch_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )

    op.add_column(
        "company_ontology_assertions",
        sa.Column("stable_assertion_key", sa.String(length=500), nullable=True),
    )
    op.execute(
        "UPDATE company_ontology_assertions "
        "SET stable_assertion_key = 'legacy-assertion:' || id::text "
        "WHERE stable_assertion_key IS NULL"
    )
    op.alter_column("company_ontology_assertions", "stable_assertion_key", nullable=False)

    op.add_column(
        "company_ontology_links",
        sa.Column("stable_link_key", sa.String(length=500), nullable=True),
    )
    op.execute(
        "UPDATE company_ontology_links SET stable_link_key = 'legacy-link:' || id::text WHERE stable_link_key IS NULL"
    )
    op.alter_column("company_ontology_links", "stable_link_key", nullable=False)

    op.drop_constraint(
        "uq_company_ontology_object_key",
        "company_ontology_objects",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_company_ontology_object_release_key",
        "company_ontology_objects",
        ["tenant_id", "release_id", "stable_object_key"],
    )

    op.drop_constraint(
        "uq_company_ontology_source_identity",
        "company_ontology_object_identities",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_company_ontology_source_identity_object",
        "company_ontology_object_identities",
        ["tenant_id", "object_id", "source_contract_id", "source_identity_key"],
    )

    op.create_unique_constraint(
        "uq_company_ontology_assertion_release_key",
        "company_ontology_assertions",
        ["tenant_id", "release_id", "stable_assertion_key"],
    )
    op.create_unique_constraint(
        "uq_company_ontology_link_release_key",
        "company_ontology_links",
        ["tenant_id", "release_id", "stable_link_key"],
    )

    op.drop_constraint(
        "uq_company_ontology_event_key",
        "company_ontology_events",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_company_ontology_event_release_key",
        "company_ontology_events",
        ["tenant_id", "release_id", "stable_event_key"],
    )

    # HN-04A/B originally installed a second, incompatible bypass GUC on these
    # tables. Rebuild every Company policy against the audited
    # ``enter_rls_bypass`` contract used by the application and fresh
    # bootstrap path so rolling upgrades and new deployments enforce the same
    # authority.
    for table in _COMPANY_TENANT_TABLES:
        _enable_strict_rls(table)


def downgrade() -> None:
    # Restoring the old schema would discard exact LLM candidate material and
    # release-versioned identities. Refuse instead of silently destroying it.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM company_ontology_curation_runs
            WHERE candidate_patch_json <> '{}'::jsonb
          ) OR EXISTS (SELECT 1 FROM company_ontology_activations)
             OR EXISTS (SELECT 1 FROM company_ontology_assertions)
             OR EXISTS (SELECT 1 FROM company_ontology_links)
          THEN
            RAISE EXCEPTION
              'company_ontology_runtime_0724 downgrade blocked: ontology runtime data would be lost';
          END IF;
          IF EXISTS (
            SELECT 1
            FROM company_ontology_objects
            GROUP BY tenant_id, stable_object_key
            HAVING count(*) > 1
          ) OR EXISTS (
            SELECT 1
            FROM company_ontology_object_identities
            GROUP BY tenant_id, source_contract_id, source_identity_key
            HAVING count(*) > 1
          ) OR EXISTS (
            SELECT 1
            FROM company_ontology_events
            GROUP BY tenant_id, stable_event_key
            HAVING count(*) > 1
          )
          THEN
            RAISE EXCEPTION
              'company_ontology_runtime_0724 downgrade blocked: release-versioned identity would collapse';
          END IF;
        END
        $$;
        """
    )

    op.drop_constraint(
        "uq_company_ontology_event_release_key",
        "company_ontology_events",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_company_ontology_event_key",
        "company_ontology_events",
        ["tenant_id", "stable_event_key"],
    )
    op.drop_constraint(
        "uq_company_ontology_link_release_key",
        "company_ontology_links",
        type_="unique",
    )
    op.drop_constraint(
        "uq_company_ontology_assertion_release_key",
        "company_ontology_assertions",
        type_="unique",
    )
    op.drop_constraint(
        "uq_company_ontology_source_identity_object",
        "company_ontology_object_identities",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_company_ontology_source_identity",
        "company_ontology_object_identities",
        ["tenant_id", "source_contract_id", "source_identity_key"],
    )
    op.drop_constraint(
        "uq_company_ontology_object_release_key",
        "company_ontology_objects",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_company_ontology_object_key",
        "company_ontology_objects",
        ["tenant_id", "stable_object_key"],
    )
    op.drop_constraint(
        "uq_company_ontology_activation_idempotency",
        "company_ontology_activations",
        type_="unique",
    )
    op.drop_column("company_ontology_activations", "idempotency_key")
    op.drop_constraint(
        "uq_company_ontology_activation_version",
        "company_ontology_activations",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_company_ontology_activation_version",
        "company_ontology_activations",
        ["tenant_id", "installation_id", "namespace", "activation_version"],
    )
    op.drop_column("company_ontology_links", "stable_link_key")
    op.drop_column("company_ontology_assertions", "stable_assertion_key")
    op.drop_column("company_ontology_curation_runs", "candidate_patch_json")
