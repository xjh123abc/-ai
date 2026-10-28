"""add business event assignments

Revision ID: 74b5e012a8c1
Revises: 1f5ffaa40ed1
Create Date: 2026-10-27
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "74b5e012a8c1"
down_revision: Union[str, None] = "1f5ffaa40ed1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "business_event_assignments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("account_set_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("assigned_by", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["account_set_id"], ["account_sets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["event_id"], ["business_events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["app_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "user_id", name="uq_business_event_assignments_event_id"),
    )
    op.create_index(
        "ix_business_event_assignments_user_scope",
        "business_event_assignments",
        ["tenant_id", "account_set_id", "user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_business_event_assignments_user_scope", table_name="business_event_assignments")
    op.drop_table("business_event_assignments")
