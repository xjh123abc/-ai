"""configure chart account auxiliary dimensions

Revision ID: a62f0b39d815
Revises: 39ae72b184c1
Create Date: 2026-10-27
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a62f0b39d815"
down_revision: Union[str, None] = "39ae72b184c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("chart_accounts", sa.Column(
        "required_auxiliary_fields",
        sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
        server_default=sa.text("'[]'"),
        nullable=False,
    ))


def downgrade() -> None:
    op.drop_column("chart_accounts", "required_auxiliary_fields")
