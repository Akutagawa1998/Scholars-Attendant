"""Source-authorized default catalog; scientific versions may remain unknown."""
from alembic import op
import sqlalchemy as sa

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def _table(name, *columns, constraints=()):
    op.create_table(name, sa.Column('id', sa.String(), primary_key=True),
        sa.Column('owner_id', sa.String(), nullable=False), sa.Column('scope_id', sa.String(), nullable=False),
        *columns, *constraints)
    for field in ('owner_id', 'scope_id'):
        op.create_index('ix_' + name + '_' + field, name, [field])


def upgrade():
    _table('catalog_defaults', sa.Column('name', sa.String(), nullable=False),
        sa.Column('registration_ids', sa.JSON(), nullable=False), sa.Column('initial_read_status', sa.String(), nullable=False),
        sa.Column('verified_links', sa.JSON(), nullable=False), sa.Column('created_at', sa.String(), nullable=False),
        sa.Column('updated_at', sa.String(), nullable=False), constraints=(sa.UniqueConstraint('owner_id', 'scope_id', 'name'),))
    _table('catalog_versions', sa.Column('work_version_id', sa.String(), sa.ForeignKey('work_versions.id'), nullable=False),
        sa.Column('identity_state', sa.String(), nullable=False), sa.Column('scientific_version_state', sa.String(), nullable=False),
        sa.Column('scientific_version', sa.String()), sa.Column('initial_read_status', sa.String(), nullable=False),
        sa.Column('created_at', sa.String(), nullable=False), constraints=(sa.UniqueConstraint('work_version_id'),))
    _table('catalog_bindings', sa.Column('work_version_id', sa.String(), sa.ForeignKey('work_versions.id'), nullable=False),
        sa.Column('source_snapshot_id', sa.String(), sa.ForeignKey('source_versions.id'), nullable=False),
        sa.Column('authority_source_snapshot_id', sa.String(), sa.ForeignKey('source_versions.id'), nullable=False),
        sa.Column('source_object_id', sa.String(), sa.ForeignKey('source_objects.id'), nullable=False),
        sa.Column('role', sa.String(), nullable=False), sa.Column('version_relation', sa.String(), nullable=False),
        sa.Column('identity_claims', sa.JSON(), nullable=False), sa.Column('conflicts', sa.JSON(), nullable=False),
        sa.Column('state', sa.String(), nullable=False), sa.Column('created_at', sa.String(), nullable=False),
        constraints=(sa.UniqueConstraint('work_version_id', 'source_snapshot_id'),))
    _table('catalog_aliases', sa.Column('work_id', sa.String(), sa.ForeignKey('works.id'), nullable=False),
        sa.Column('scheme', sa.String(), nullable=False), sa.Column('value', sa.String(), nullable=False),
        sa.Column('source_snapshot_id', sa.String(), sa.ForeignKey('source_versions.id'), nullable=False),
        sa.Column('source_object_id', sa.String(), sa.ForeignKey('source_objects.id'), nullable=False),
        sa.Column('proof', sa.JSON(), nullable=False), sa.Column('state', sa.String(), nullable=False),
        sa.Column('created_at', sa.String(), nullable=False), constraints=(sa.UniqueConstraint(
            'owner_id', 'scope_id', 'scheme', 'value', 'source_snapshot_id', 'work_id'),))


def downgrade():
    for name in ('catalog_aliases', 'catalog_bindings', 'catalog_versions', 'catalog_defaults'):
        op.drop_table(name)
