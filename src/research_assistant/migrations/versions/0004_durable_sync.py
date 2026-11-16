"""Durable inventories, retry bookkeeping and fenced connector leases."""
from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None

def make(name, *columns, unique=None):
    constraints = [sa.UniqueConstraint(*unique)] if unique else []
    op.create_table(name, sa.Column('id',sa.String(),primary_key=True),
        sa.Column('owner_id',sa.String(),nullable=False), sa.Column('scope_id',sa.String(),nullable=False),
        *columns, *constraints)
    for field in ('owner_id','scope_id'):
        op.create_index('ix_'+name+'_'+field,name,[field])

def col(name,typ=sa.String,nullable=False):
    return sa.Column(name,typ(),nullable=nullable)

def registration():
    return sa.Column('registration_id',sa.String(),sa.ForeignKey('connector_registrations.id'),nullable=False)

def upgrade():
    make('sync_manifests',registration(),col('source_version',nullable=True),col('inventory_hash',nullable=True),
        col('expected_count',sa.Integer),col('inventory_complete',sa.Boolean),col('source_consistent',sa.Boolean),
        col('deletions_complete',sa.Boolean),col('status'),col('recorded_at'),col('updated_at'))
    make('sync_items',sa.Column('manifest_id',sa.String(),sa.ForeignKey('sync_manifests.id'),nullable=False),
        col('identity_hash'),col('expected_version',nullable=True),col('state'),col('snapshot_id',nullable=True),
        col('attempts',sa.Integer),col('error_code',nullable=True),col('next_retry_at',nullable=True),col('updated_at'),
        unique=('manifest_id','identity_hash'))
    make('sync_failures',registration(),col('identity_hash'),col('expected_version',nullable=True),col('attempts',sa.Integer),
        col('error_code'),col('next_retry_at',nullable=True),col('updated_at'),unique=('registration_id','identity_hash'))
    make('sync_leases',registration(),col('token_hash'),col('run_id'),col('expires_at'),unique=('registration_id',))

def downgrade():
    for name in ('sync_leases','sync_failures','sync_items','sync_manifests'):
        op.drop_table(name)
