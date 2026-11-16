"""Original PDF byte assets separate from extracted text source hashes."""
from alembic import op
import sqlalchemy as sa

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('source_assets',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('owner_id', sa.String(), nullable=False),
        sa.Column('scope_id', sa.String(), nullable=False),
        sa.Column('source_version_id', sa.String(), sa.ForeignKey('source_versions.id'), nullable=False),
        sa.Column('sha256', sa.String(), nullable=False),
        sa.Column('byte_size', sa.Integer(), nullable=False),
        sa.Column('blob_key', sa.String(), nullable=False),
        sa.Column('media_type', sa.String(), nullable=False),
        sa.Column('parser_name', sa.String(), nullable=False),
        sa.Column('parser_version', sa.String(), nullable=False),
        sa.Column('quality', sa.String(), nullable=False),
        sa.Column('warnings', sa.JSON(), nullable=False),
        sa.Column('page_metadata', sa.JSON(), nullable=False),
        sa.Column('state', sa.String(), nullable=False),
        sa.UniqueConstraint('source_version_id'))
    for field in ('owner_id', 'scope_id', 'sha256'):
        op.create_index('ix_source_assets_' + field, 'source_assets', [field])


def downgrade():
    op.drop_table('source_assets')
