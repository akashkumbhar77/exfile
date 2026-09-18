"""enrollments: owner instruction text gets a designated column (B.6 addendum)

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-18 20:00:00

Also scrubs the `reason` key from existing `onboarding.needs_human` events: the agent's failure
text can quote the instruction, and from now on it is persisted only in enrollments.failure.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'enrollments',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('org_id', sa.String(length=64), nullable=False),
        sa.Column('sheet_id', sa.BigInteger(), nullable=False),
        sa.Column('instruction', sa.Text(), nullable=False),
        sa.Column('state', sa.String(length=32), server_default='queued', nullable=False),
        sa.Column('job_id', sa.String(length=64), nullable=True),
        sa.Column('session_id', sa.String(length=64), nullable=True),
        sa.Column('config_id', sa.BigInteger(), nullable=True),
        sa.Column('failure', sa.Text(), nullable=True),
        sa.Column('failures', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['config_id'], ['configs.id']),
        sa.ForeignKeyConstraint(['org_id'], ['orgs.id']),
        sa.ForeignKeyConstraint(['sheet_id'], ['sheets.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_enrollments_org_id'), 'enrollments', ['org_id'], unique=False)
    op.create_index(op.f('ix_enrollments_sheet_id'), 'enrollments', ['sheet_id'], unique=False)
    op.create_index(op.f('ix_enrollments_session_id'), 'enrollments', ['session_id'], unique=False)
    op.execute("UPDATE events SET payload = payload - 'reason' WHERE kind = 'onboarding.needs_human'")


def downgrade() -> None:
    op.drop_index(op.f('ix_enrollments_session_id'), table_name='enrollments')
    op.drop_index(op.f('ix_enrollments_sheet_id'), table_name='enrollments')
    op.drop_index(op.f('ix_enrollments_org_id'), table_name='enrollments')
    op.drop_table('enrollments')
