"""Versioned local derived reviews preserve the original annotation alignment."""
from alembic import op
import sqlalchemy as sa

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('annotation_alignment_resolutions',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('owner_id', sa.String(), nullable=False),
        sa.Column('scope_id', sa.String(), nullable=False),
        sa.Column('alignment_id', sa.String(), sa.ForeignKey('annotation_alignments.id'), nullable=False),
        sa.Column('algorithm_version', sa.String(), nullable=False),
        sa.Column('input_fingerprint', sa.String(), nullable=False),
        sa.Column('report', sa.JSON(), nullable=False),
        sa.Column('recorded_at', sa.String(), nullable=False),
        sa.UniqueConstraint('alignment_id', 'algorithm_version', 'input_fingerprint'))
    for field in ('owner_id', 'scope_id'):
        op.create_index('ix_annotation_alignment_resolutions_' + field, 'annotation_alignment_resolutions', [field])


def downgrade():
    op.drop_table('annotation_alignment_resolutions')
