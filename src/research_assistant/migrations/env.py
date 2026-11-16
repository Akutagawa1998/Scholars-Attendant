from alembic import context
from research_assistant.storage import metadata

config = context.config
connection = config.attributes['connection']
context.configure(connection=connection, target_metadata=metadata, compare_type=True)
with context.begin_transaction():
    context.run_migrations()
