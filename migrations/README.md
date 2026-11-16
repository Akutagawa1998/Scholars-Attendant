Canonical Alembic migrations are packaged at `src/research_assistant/migrations/`.
`ResearchService.initialize()` upgrades the explicitly configured database before serving.
The initial migration is frozen, independent of live model metadata. Future schema changes
must add a new revision; do not replace the initial migration after deployment.

PostgreSQL startup takes a transaction-scoped advisory lock; SQLite fixture startup takes
an immediate transaction. No command defaults to an implicit writable memory database.
