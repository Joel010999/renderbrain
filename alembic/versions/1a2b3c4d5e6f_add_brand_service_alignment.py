"""add brand_service_alignment

Revision ID: 1a2b3c4d5e6f
Revises: ff66675b1f47
Create Date: 2026-09-01 13:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1a2b3c4d5e6f'
down_revision: Union[str, None] = 'ff66675b1f47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('content_briefs', sa.Column('brand_service_alignment', sa.String(length=50), nullable=True))
    op.execute("UPDATE content_briefs SET brand_service_alignment = 'crm' WHERE brand_service_alignment IS NULL")


def downgrade() -> None:
    op.drop_column('content_briefs', 'brand_service_alignment')
