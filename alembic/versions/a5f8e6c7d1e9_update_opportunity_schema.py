"""Update Opportunity schema

Revision ID: a5f8e6c7d1e9
Revises: 0bac91567d8c
Create Date: 2026-08-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a5f8e6c7d1e9'
down_revision: Union[str, Sequence[str], None] = '0bac91567d8c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Rename content to description
    op.alter_column('opportunities', 'content', new_column_name='description')
    # Add title and priority
    op.add_column('opportunities', sa.Column('title', sa.String(length=255), nullable=True))
    op.add_column('opportunities', sa.Column('priority', sa.String(length=50), nullable=True))
    
    # Update existing data with defaults
    op.execute("UPDATE opportunities SET title = 'Oportunidad Histórica', priority = 'medium' WHERE title IS NULL")
    
    # Make them non-nullable
    op.alter_column('opportunities', 'title', nullable=False)
    op.alter_column('opportunities', 'priority', nullable=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('opportunities', 'priority')
    op.drop_column('opportunities', 'title')
    op.alter_column('opportunities', 'description', new_column_name='content')
