"""Schema drift and target-dialect compilation; not a PostgreSQL runtime test."""
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable
from research_assistant.storage import metadata
from research_assistant.service import ResearchService

def test_packaged_migration_matches_current_canonical_schema(tmp_path):
    service = ResearchService('sqlite:///' + str(tmp_path / 'migrations.sqlite3'))
    service.initialize()
    service.initialize()
    with service.storage.engine.connect() as c:
        differences = compare_metadata(MigrationContext.configure(c), metadata)
    assert differences == []

def test_postgresql_schema_can_be_compiled():
    ddl = [str(CreateTable(table).compile(dialect=postgresql.dialect())) for table in metadata.sorted_tables]
    assert len(ddl) >= 10
    assert all('CREATE TABLE' in statement for statement in ddl)
