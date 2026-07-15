"""Bind Agent and Role Template model references to their tenant.

Revision ID: agent_model_tenant_authority_0715
Revises: agent_tool_config_encryption_0715
Create Date: 2026-07-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "agent_model_tenant_authority_0715"
down_revision = "agent_tool_config_encryption_0715"
branch_labels = None
depends_on = None


PRIMARY_FK = "fk_agents_primary_model_tenant"
FALLBACK_FK = "fk_agents_fallback_model_tenant"
MODEL_UNIQUE = "uq_llm_models_tenant_id_id"
TEMPLATE_FK = "fk_agent_templates_model_tenant"


def _constraint_exists(table_name: str, constraint_name: str) -> bool:
    return bool(
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM pg_constraint "
                "WHERE conrelid = CAST(:table_name AS regclass) AND conname = :constraint_name"
            ),
            {"table_name": table_name, "constraint_name": constraint_name},
        )
        .scalar()
    )


def _quarantine_invalid_reference(field: str) -> None:
    if field not in {"primary_model_id", "fallback_model_id"}:
        raise ValueError(f"unsupported Agent model field: {field}")

    op.execute(
        f"""
        INSERT INTO audit_logs (id, tenant_id, agent_id, action, details)
        SELECT
            gen_random_uuid(),
            agent.tenant_id,
            agent.id,
            'migration.agent_model_reference_quarantined',
            json_build_object(
                'schema', 'hive.audit.agent_model_reference_quarantine.v1',
                'field', '{field}',
                'model_id', agent.{field}::text,
                'agent_tenant_id', agent.tenant_id::text,
                'model_tenant_id', model.tenant_id::text,
                'reason', CASE
                    WHEN model.id IS NULL THEN 'missing_model'
                    ELSE 'cross_tenant_model'
                END,
                'recovery', 'select an enabled same-tenant model through governed Agent settings'
            )
        FROM agents AS agent
        LEFT JOIN llm_models AS model ON model.id = agent.{field}
        WHERE agent.{field} IS NOT NULL
          AND (model.id IS NULL OR agent.tenant_id IS DISTINCT FROM model.tenant_id)
        """
    )
    op.execute(
        f"""
        UPDATE agents AS agent
        SET {field} = NULL
        WHERE agent.{field} IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM llm_models AS model
              WHERE model.id = agent.{field}
                AND model.tenant_id = agent.tenant_id
          )
        """
    )


def _quarantine_invalid_template_reference() -> None:
    op.execute(
        """
        INSERT INTO audit_logs (id, tenant_id, action, details)
        SELECT
            gen_random_uuid(),
            template.tenant_id,
            'migration.agent_template_model_reference_quarantined',
            json_build_object(
                'schema', 'hive.audit.agent_template_model_reference_quarantine.v1',
                'template_id', template.id::text,
                'model_id', template.model_id::text,
                'template_tenant_id', template.tenant_id::text,
                'model_tenant_id', model.tenant_id::text,
                'reason', CASE
                    WHEN model.id IS NULL THEN 'missing_model'
                    ELSE 'cross_tenant_model'
                END,
                'recovery', 'select an enabled same-tenant model through governed Role Template settings'
            )
        FROM agent_templates AS template
        LEFT JOIN llm_models AS model ON model.id = template.model_id
        WHERE template.model_id IS NOT NULL
          AND (model.id IS NULL OR template.tenant_id IS DISTINCT FROM model.tenant_id)
        """
    )
    op.execute(
        """
        UPDATE agent_templates AS template
        SET model_id = NULL
        WHERE template.model_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM llm_models AS model
              WHERE model.id = template.model_id
                AND model.tenant_id IS NOT DISTINCT FROM template.tenant_id
          )
        """
    )


def _ensure_tenant_constraints() -> None:
    if not _constraint_exists("llm_models", MODEL_UNIQUE):
        op.create_unique_constraint(MODEL_UNIQUE, "llm_models", ["tenant_id", "id"])

    for constraint_name, model_field in (
        (PRIMARY_FK, "primary_model_id"),
        (FALLBACK_FK, "fallback_model_id"),
    ):
        if not _constraint_exists("agents", constraint_name):
            op.execute(
                f"""
                ALTER TABLE agents
                ADD CONSTRAINT {constraint_name}
                FOREIGN KEY (tenant_id, {model_field})
                REFERENCES llm_models (tenant_id, id)
                NOT VALID
                """
            )
        op.execute(f"ALTER TABLE agents VALIDATE CONSTRAINT {constraint_name}")

    if not _constraint_exists("agent_templates", TEMPLATE_FK):
        op.execute(
            f"""
            ALTER TABLE agent_templates
            ADD CONSTRAINT {TEMPLATE_FK}
            FOREIGN KEY (tenant_id, model_id)
            REFERENCES llm_models (tenant_id, id)
            NOT VALID
            """
        )
    op.execute(f"ALTER TABLE agent_templates VALIDATE CONSTRAINT {TEMPLATE_FK}")


def upgrade() -> None:
    _quarantine_invalid_reference("primary_model_id")
    _quarantine_invalid_reference("fallback_model_id")
    _quarantine_invalid_template_reference()
    _ensure_tenant_constraints()


def downgrade() -> None:
    # Secure downgrade: keep tenant authority constraints and immutable
    # quarantine evidence. Older application code is compatible with both.
    pass
