"""Bind attachment PDF assets and text alignment to registered Zotero snapshots."""
from alembic import op
import sqlalchemy as sa

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade():
    for name, columns, unique in (
        ('attachment_asset_links', [
            sa.Column('metadata_snapshot_id', sa.String(), sa.ForeignKey('source_versions.id'), nullable=False),
            sa.Column('attachment_object_id', sa.String(), sa.ForeignKey('source_objects.id'), nullable=False),
            sa.Column('parent_object_id', sa.String(), sa.ForeignKey('source_objects.id')),
            sa.Column('parent_snapshot_id', sa.String(), sa.ForeignKey('source_versions.id')),
            sa.Column('pdf_source_id', sa.String(), sa.ForeignKey('source_versions.id'), nullable=False),
            sa.Column('provenance', sa.JSON(), nullable=False)],
            ('metadata_snapshot_id', 'pdf_source_id')),
        ('annotation_alignments', [
            sa.Column('annotation_snapshot_id', sa.String(), sa.ForeignKey('source_versions.id'), nullable=False),
            sa.Column('pdf_source_id', sa.String(), sa.ForeignKey('source_versions.id'), nullable=False),
            sa.Column('attachment_link_id', sa.String(), sa.ForeignKey('attachment_asset_links.id'), nullable=False),
            sa.Column('report', sa.JSON(), nullable=False)],
            ('annotation_snapshot_id', 'pdf_source_id')),
    ):
        op.create_table(name, sa.Column('id', sa.String(), primary_key=True),
            sa.Column('owner_id', sa.String(), nullable=False), sa.Column('scope_id', sa.String(), nullable=False),
            *columns, sa.UniqueConstraint(*unique))
        for field in ('owner_id', 'scope_id'):
            op.create_index('ix_' + name + '_' + field, name, [field])


def downgrade():
    op.drop_table('annotation_alignments')
    op.drop_table('attachment_asset_links')
