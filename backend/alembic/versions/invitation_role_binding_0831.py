"""Bind every invitation code to one explicit tenant role.

Revision ID: invitation_role_binding_0831
Revises: runtime_terminal_boundary_0831
Create Date: 2026-08-31
"""

from alembic import op


revision = "invitation_role_binding_0831"
down_revision = "runtime_terminal_boundary_0831"
branch_labels = None
depends_on = None


_CONSTRAINT = "ck_invitation_codes_granted_role"
_TRIGGER_FUNCTION = "bind_legacy_invitation_granted_role_0831"
_TRIGGER = "trg_invitation_codes_bind_legacy_granted_role_0831"


def upgrade() -> None:
    # Preserve the old join contract at cutover: a code grants org-admin only
    # when its target tenant has no administrator yet. This also distinguishes
    # old platform-admin company bootstrap writes from ordinary member codes.
    op.execute("ALTER TABLE public.invitation_codes ADD COLUMN IF NOT EXISTS granted_role VARCHAR(20)")
    op.execute("ALTER TABLE public.invitation_codes ALTER COLUMN granted_role DROP DEFAULT")
    op.execute(
        """
        UPDATE public.invitation_codes AS invitation
        SET granted_role = CASE
          WHEN EXISTS (
            SELECT 1
            FROM public.users AS tenant_admin
            WHERE tenant_admin.tenant_id = invitation.tenant_id
              AND tenant_admin.role::text IN ('org_admin', 'platform_admin')
          ) THEN 'member'
          ELSE 'org_admin'
        END
        WHERE invitation.granted_role IS NULL
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.{_TRIGGER_FUNCTION}()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        SET app.current_tenant_id = 'BYPASS'
        AS $$
        DECLARE target_has_admin boolean;
        BEGIN
          IF NEW.granted_role IS NULL THEN
            SELECT EXISTS (
              SELECT 1
              FROM public.users AS tenant_admin
              WHERE tenant_admin.tenant_id = NEW.tenant_id
                AND tenant_admin.role::text IN ('org_admin', 'platform_admin')
            ) INTO target_has_admin;
            NEW.granted_role := CASE
              WHEN target_has_admin THEN 'member'
              ELSE 'org_admin'
            END;
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON public.invitation_codes")
    op.execute(
        f"""
        CREATE TRIGGER {_TRIGGER}
        BEFORE INSERT ON public.invitation_codes
        FOR EACH ROW
        EXECUTE FUNCTION public.{_TRIGGER_FUNCTION}()
        """
    )
    op.execute("ALTER TABLE public.invitation_codes ALTER COLUMN granted_role SET NOT NULL")
    op.execute(f"ALTER TABLE public.invitation_codes DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    op.create_check_constraint(
        _CONSTRAINT,
        "invitation_codes",
        "granted_role IN ('member', 'org_admin')",
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON public.invitation_codes")
    op.execute(f"DROP FUNCTION IF EXISTS public.{_TRIGGER_FUNCTION}()")
    op.execute(f"ALTER TABLE public.invitation_codes DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    op.execute("ALTER TABLE public.invitation_codes DROP COLUMN IF EXISTS granted_role")
