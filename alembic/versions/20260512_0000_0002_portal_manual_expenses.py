"""portal: allow image-less receipts (manual portal entries)

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-12

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("receipts", "telegram_file_id", existing_type=sa.String(length=512), nullable=True)
    op.alter_column("receipts", "s3_key", existing_type=sa.String(length=512), nullable=True)
    op.add_column(
        "receipts",
        sa.Column(
            "source",
            sa.String(length=16),
            nullable=False,
            server_default="telegram",
        ),
    )


def downgrade() -> None:
    op.drop_column("receipts", "source")
    op.alter_column("receipts", "s3_key", existing_type=sa.String(length=512), nullable=False)
    op.alter_column(
        "receipts", "telegram_file_id", existing_type=sa.String(length=512), nullable=False
    )
