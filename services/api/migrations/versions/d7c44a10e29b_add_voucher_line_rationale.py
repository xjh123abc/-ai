"""persist AI rationale on each voucher line

Revision ID: d7c44a10e29b
Revises: c31a82d7e40b
Create Date: 2026-10-27
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d7c44a10e29b"
down_revision: Union[str, None] = "c31a82d7e40b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("voucher_draft_lines", sa.Column("rationale", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("voucher_draft_lines", "rationale")
