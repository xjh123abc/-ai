"""record per-event rental direct expense confirmation

Revision ID: c31a82d7e40b
Revises: b710e94c152a
Create Date: 2026-10-27
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c31a82d7e40b"
down_revision: Union[str, None] = "b710e94c152a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("business_events", sa.Column("request_hash", sa.String(length=64), nullable=True))
    op.create_unique_constraint("uq_business_events_request_hash", "business_events",
                                ["account_set_id", "created_by", "request_hash"])
    op.add_column("business_events", sa.Column("rental_direct_expense_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("business_events", sa.Column("rental_direct_expense_confirmed_by", sa.String(length=200), nullable=True))
    op.add_column("business_events", sa.Column("rental_direct_expense_policy_version", sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column("business_events", "rental_direct_expense_confirmed_by")
    op.drop_column("business_events", "rental_direct_expense_policy_version")
    op.drop_column("business_events", "rental_direct_expense_confirmed")
    op.drop_constraint("uq_business_events_request_hash", "business_events", type_="unique")
    op.drop_column("business_events", "request_hash")
