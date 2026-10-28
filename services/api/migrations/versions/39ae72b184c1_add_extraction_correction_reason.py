"""add extraction correction reason

Revision ID: 39ae72b184c1
Revises: 51cb6da318ef
Create Date: 2026-10-27
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "39ae72b184c1"
down_revision: Union[str, None] = "51cb6da318ef"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("document_extractions", sa.Column("correction_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("document_extractions", "correction_reason")
