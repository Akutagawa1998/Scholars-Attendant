"""PostgreSQL-only rebuildable vector and full-text retrieval.

The server extension is provisioned by the database operator, never silently
installed into a disposable search_path schema by an application migration.
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        return
    installed = connection.scalar(sa.text(
        "SELECT EXISTS (SELECT 1 FROM pg_extension x JOIN pg_namespace n "
        "ON n.oid=x.extnamespace WHERE x.extname='vector' AND n.nspname='public')"
    ))
    if not installed:
        raise RuntimeError("Provision pgvector with CREATE EXTENSION vector WITH SCHEMA public before migrating")
    # No content copy lives in this table. Text and all serving permissions are
    # resolved against the canonical evidence/source rows on every query.
    connection.exec_driver_sql("""
        CREATE TABLE evidence_embeddings (
            id varchar PRIMARY KEY,
            owner_id varchar NOT NULL,
            scope_id varchar NOT NULL,
            evidence_id varchar NOT NULL REFERENCES evidence_spans(id) ON DELETE CASCADE,
            source_version_id varchar NOT NULL REFERENCES source_versions(id) ON DELETE CASCADE,
            source_revision integer NOT NULL,
            embedding_model varchar NOT NULL,
            dimensions integer NOT NULL CHECK (dimensions BETWEEN 1 AND 16000),
            embedding public.vector NOT NULL,
            CONSTRAINT uq_evidence_embeddings_model UNIQUE (evidence_id, embedding_model),
            CONSTRAINT ck_evidence_embeddings_dimensions CHECK (public.vector_dims(embedding)=dimensions)
        )
    """)
    connection.exec_driver_sql(
        "CREATE INDEX ix_evidence_embeddings_scope_model ON evidence_embeddings "
        "(owner_id, scope_id, embedding_model, dimensions)"
    )
    # 'simple' is an explicit baseline; CJK exact/substring fallback remains
    # available until a separately evaluated language tokenizer is configured.
    connection.exec_driver_sql(
        "CREATE INDEX ix_evidence_spans_fts ON evidence_spans "
        "USING gin (to_tsvector('simple'::regconfig, text))"
    )


def downgrade():
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        return
    connection.exec_driver_sql("DROP INDEX ix_evidence_spans_fts")
    connection.exec_driver_sql("DROP TABLE evidence_embeddings")
    # The operator-provisioned, potentially shared extension is not ours to drop.
