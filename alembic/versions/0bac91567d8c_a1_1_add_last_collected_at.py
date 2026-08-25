"""a1_1_add_last_collected_at

Revision ID: 0bac91567d8c
Revises: 6a8d2434e1a1
Create Date: 2026-08-25 03:47:27.923305

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0bac91567d8c'
down_revision: Union[str, Sequence[str], None] = '6a8d2434e1a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('missions', sa.Column('last_collected_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('missions', 'last_collected_at')
