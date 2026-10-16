"""Record the current directory department observation on org members.

Revision ID: m1d_directory_observation_1016
Revises: m1b_identity_links_1015

NULL is the M1-B shape: no current observation. A non-empty array is the
current observation. Empty arrays are not a fact. Downgrade removes only
this column and its check.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "m1d_directory_observation_1016"
down_revision = "m1b_identity_links_1015"
branch_labels = None
depends_on = None

_OBSERVATION_SQL = (
    "observed_directory_department_ids IS NULL OR ("
    "jsonb_typeof(observed_directory_department_ids) = 'array' "
    "AND jsonb_array_length(observed_directory_department_ids) >= 1)"
)


def upgrade() -> None:
    op.add_column(
        "org_members",
        sa.Column("observed_directory_department_ids", postgresql.JSONB(), nullable=True),
    )
    op.create_check_constraint(
        "ck_org_members_observed_directory_departments",
        "org_members",
        _OBSERVATION_SQL,
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_org_members_observed_directory_departments",
        "org_members",
        type_="check",
    )
    op.drop_column("org_members", "observed_directory_department_ids")
