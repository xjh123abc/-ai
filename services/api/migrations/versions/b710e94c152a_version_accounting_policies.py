"""version account set accounting policies

Revision ID: b710e94c152a
Revises: a62f0b39d815
Create Date: 2026-10-27
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b710e94c152a"
down_revision: Union[str, None] = "a62f0b39d815"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "accounting_policies",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account_set_id", sa.String(length=36), nullable=False),
        sa.Column("business_type", sa.String(length=40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("configuration", sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"), nullable=False),
        sa.Column("confirmed_by", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["account_set_id"], ["account_sets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_set_id", "business_type", "version"),
    )
    op.create_index("ix_accounting_policies_scope", "accounting_policies",
                    ["account_set_id", "business_type", "version"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_accounting_policies_scope", table_name="accounting_policies")
    op.drop_table("accounting_policies")
