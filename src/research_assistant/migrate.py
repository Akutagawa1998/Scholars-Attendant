"""Run packaged Alembic migrations on an explicitly configured database."""
from pathlib import Path
from alembic.config import Config
from alembic import command

def upgrade(engine):
    config = Config()
    config.set_main_option('script_location', str(Path(__file__).parent / 'migrations'))
    with engine.begin() as connection:
        config.attributes['connection'] = connection
        if engine.dialect.name == 'postgresql':
            # Serialize concurrent process startup before creating alembic_version.
            connection.exec_driver_sql('SELECT pg_advisory_xact_lock(728190471)')
        command.upgrade(config, 'head')
