"""add durable posting attempts

Revision ID: 51cb6da318ef
Revises: 9b604f6c9d21
Create Date: 2026-10-27
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "51cb6da318ef"
down_revision: Union[str, None] = "9b604f6c9d21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "posting_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("account_set_id", sa.String(length=36), nullable=False),
        sa.Column("voucher_draft_id", sa.String(length=36), nullable=False),
        sa.Column("voucher_version", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("request_payload", sa.JSON().with_variant(
            postgresql.JSONB(astext_type=sa.Text()), "postgresql"
        ), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("external_reference", sa.String(length=200), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('SUBMITTING', 'UNKNOWN', 'FAILED', 'VERIFIED')", name="ck_posting_attempts_posting_status"),
        sa.ForeignKeyConstraint(["account_set_id"], ["account_sets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["voucher_draft_id"], ["voucher_drafts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_posting_attempts_idempotency_key"),
        sa.UniqueConstraint("voucher_draft_id", "voucher_version", name="uq_posting_attempts_voucher_draft_id"),
    )
    op.create_index(
        "ix_posting_attempts_scope_status",
        "posting_attempts",
        ["tenant_id", "account_set_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_posting_attempts_scope_status", table_name="posting_attempts")
    op.drop_table("posting_attempts")
