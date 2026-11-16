"""Portable canonical schema; PostgreSQL is production, SQLite is fixture-only."""
from contextlib import contextmanager
from sqlalchemy import (MetaData, Table, Column, String, Integer, Text, JSON, Boolean,
                        UniqueConstraint, ForeignKey, create_engine, event)

metadata = MetaData()
def table(name, *columns, constraints=()):
    return Table(name, metadata,
        Column("id", String, primary_key=True),
        Column("owner_id", String, nullable=False, index=True),
        Column("scope_id", String, nullable=False, index=True), *columns, *constraints)

scopes = table("scopes", Column("deletion_epoch", Integer, nullable=False),
               Column("access_epoch", Integer, nullable=False),
               constraints=(UniqueConstraint("owner_id", "scope_id"),))
works = table("works", Column("identity_key", String, nullable=False), Column("title", Text, nullable=False),
              constraints=(UniqueConstraint("owner_id", "scope_id", "identity_key"),))
versions = table("work_versions", Column("work_id", ForeignKey("works.id"), nullable=False),
                 Column("identifier", String, nullable=False), Column("version", String, nullable=False),
                 Column("title", Text, nullable=False),
                 Column("source_version_id", String, nullable=False), Column("created_at", String, nullable=False),
                 constraints=(UniqueConstraint("owner_id", "scope_id", "identifier"),))
sources = table("source_versions", Column("identity_key", String, nullable=False),
                Column("kind", String, nullable=False), Column("content", Text, nullable=False),
                Column("sha256", String, nullable=False), Column("source_url", Text, nullable=False),
                Column("media_type", String, nullable=False), Column("extraction_version", String, nullable=False),
                Column("access_state", String, nullable=False), Column("revision", Integer, nullable=False),
                Column("created_at", String, nullable=False))
evidence = table("evidence_spans", Column("source_version_id", ForeignKey("source_versions.id"), nullable=False),
                 Column("text", Text, nullable=False), Column("section", String, nullable=False),
                 Column("locator", JSON, nullable=False), Column("quote_hash", String, nullable=False))
captures = table("captured_messages", Column("source_version_id", ForeignKey("source_versions.id"), nullable=False),
                 Column("session_id", String, nullable=False), Column("claims", JSON, nullable=False),
                 Column("created_at", String, nullable=False))
assertions = table("assertions", Column("logical_assertion_id", String, nullable=False, index=True),
                  Column("revision", Integer, nullable=False), Column("subject_id", String, nullable=False),
                  Column("predicate", String, nullable=False), Column("value", JSON, nullable=False),
                  Column("epistemic_status", String, nullable=False), Column("lifecycle_status", String, nullable=False),
                  Column("validation_status", String, nullable=False), Column("evidence_ids", JSON, nullable=False),
                  Column("source_message_id", String), Column("valid_from", String), Column("valid_to", String),
                  Column("system_from", String, nullable=False), Column("system_to", String),
                  constraints=(UniqueConstraint("logical_assertion_id", "revision"),))
reading_sessions = table("reading_sessions", Column("assertion_id", ForeignKey("assertions.id"), nullable=False),
                         Column("work_version_id", ForeignKey("work_versions.id"), nullable=False),
                         Column("created_at", String, nullable=False))
projects = table("projects", Column("title", Text, nullable=False), Column("revision", Integer, nullable=False))
events = table("events", Column("operation", String, nullable=False), Column("idempotency_key", String, nullable=False),
               Column("request_hash", String, nullable=False), Column("receipt", JSON, nullable=False),
               Column("recorded_at", String, nullable=False),
               constraints=(UniqueConstraint("owner_id", "scope_id", "idempotency_key"),))
tombstones = table("tombstones", Column("target_id", String, nullable=False),
                   Column("fingerprint", String, nullable=False), Column("created_at", String, nullable=False),
                   constraints=(UniqueConstraint("owner_id", "scope_id", "target_id"),))
deletion_jobs = table("deletion_jobs", Column("recorded_at", String, nullable=False),
                      Column("cleanup_state", String, nullable=False),
                      Column("cleared_memory_count", Integer, nullable=False),
                      Column("cleared_source_count", Integer, nullable=False),
                      Column("backup_status", String, nullable=False))
jobs = table("outbox_jobs", Column("status", String, nullable=False), Column("input_ids", JSON, nullable=False),
             Column("deletion_epoch", Integer, nullable=False), Column("access_epoch", Integer, nullable=False),
             Column("created_at", String, nullable=False), Column("attempts", Integer, nullable=False),
             Column("lease_until", String), Column("error_code", String))
projections = table("projection_versions", Column("assertion_id", String, nullable=False),
                    Column("revision", Integer, nullable=False), Column("search_text", Text, nullable=False))

connector_registrations = table("connector_registrations", Column("provider", String, nullable=False),
    Column("source_instance_id", String, nullable=False), Column("namespace", String, nullable=False),
    Column("created_at", String, nullable=False),
    constraints=(UniqueConstraint("owner_id", "scope_id", "provider", "source_instance_id", "namespace"),))
source_objects = table("source_objects", Column("registration_id", ForeignKey("connector_registrations.id"), nullable=False),
    Column("identity_hash", String, nullable=False), Column("external_id", String, nullable=False),
    Column("head_source_version_id", String), Column("access_state", String, nullable=False),
    Column("revision", Integer, nullable=False),
    constraints=(UniqueConstraint("owner_id", "scope_id", "identity_hash"),))
snapshot_metadata = table("source_snapshot_metadata", Column("source_object_id", ForeignKey("source_objects.id"), nullable=False),
    Column("source_version_id", ForeignKey("source_versions.id"), nullable=False),
    Column("external_version", String), Column("observed_at", String, nullable=False), Column("occurred_at", String),
    Column("completeness", String, nullable=False), Column("missing_parts", JSON, nullable=False),
    constraints=(UniqueConstraint("source_version_id"),))
connector_cursors = table("connector_cursors", Column("registration_id", ForeignKey("connector_registrations.id"), nullable=False),
    Column("cursor", String, nullable=False), Column("run_id", String, nullable=False), Column("recorded_at", String, nullable=False),
    constraints=(UniqueConstraint("registration_id"),))
sync_runs = table("sync_runs", Column("registration_id", ForeignKey("connector_registrations.id"), nullable=False),
    Column("status", String, nullable=False), Column("source_snapshot_ids", JSON, nullable=False),
    Column("recorded_at", String, nullable=False))

# Sync bookkeeping stores identity hashes and safe error codes, never connector bodies.
sync_manifests = table("sync_manifests", Column("registration_id", ForeignKey("connector_registrations.id"), nullable=False),
    Column("source_version", String), Column("inventory_hash", String), Column("expected_count", Integer, nullable=False),
    Column("inventory_complete", Boolean, nullable=False), Column("source_consistent", Boolean, nullable=False),
    Column("deletions_complete", Boolean, nullable=False), Column("status", String, nullable=False),
    Column("recorded_at", String, nullable=False), Column("updated_at", String, nullable=False))
sync_items = table("sync_items", Column("manifest_id", ForeignKey("sync_manifests.id"), nullable=False),
    Column("identity_hash", String, nullable=False), Column("expected_version", String), Column("state", String, nullable=False),
    Column("snapshot_id", String), Column("attempts", Integer, nullable=False), Column("error_code", String),
    Column("next_retry_at", String), Column("updated_at", String, nullable=False),
    constraints=(UniqueConstraint("manifest_id", "identity_hash"),))
sync_failures = table("sync_failures", Column("registration_id", ForeignKey("connector_registrations.id"), nullable=False),
    Column("identity_hash", String, nullable=False), Column("expected_version", String), Column("attempts", Integer, nullable=False),
    Column("error_code", String, nullable=False), Column("next_retry_at", String), Column("updated_at", String, nullable=False),
    constraints=(UniqueConstraint("registration_id", "identity_hash"),))
sync_leases = table("sync_leases", Column("registration_id", ForeignKey("connector_registrations.id"), nullable=False),
    Column("token_hash", String, nullable=False), Column("run_id", String, nullable=False), Column("expires_at", String, nullable=False),
    constraints=(UniqueConstraint("registration_id"),))

# Private local blob keys are internal storage metadata, never model-facing URLs.
source_assets = table("source_assets", Column("source_version_id", ForeignKey("source_versions.id"), nullable=False),
    Column("sha256", String, nullable=False, index=True), Column("byte_size", Integer, nullable=False),
    Column("blob_key", String, nullable=False), Column("media_type", String, nullable=False),
    Column("parser_name", String, nullable=False), Column("parser_version", String, nullable=False),
    Column("quality", String, nullable=False), Column("warnings", JSON, nullable=False),
    Column("page_metadata", JSON, nullable=False), Column("state", String, nullable=False),
    constraints=(UniqueConstraint("source_version_id"),))

attachment_asset_links = table("attachment_asset_links",
    Column("metadata_snapshot_id", ForeignKey("source_versions.id"), nullable=False),
    Column("attachment_object_id", ForeignKey("source_objects.id"), nullable=False),
    Column("parent_object_id", ForeignKey("source_objects.id")),
    Column("parent_snapshot_id", ForeignKey("source_versions.id")),
    Column("pdf_source_id", ForeignKey("source_versions.id"), nullable=False),
    Column("provenance", JSON, nullable=False),
    constraints=(UniqueConstraint("metadata_snapshot_id", "pdf_source_id"),))
annotation_alignments = table("annotation_alignments",
    Column("annotation_snapshot_id", ForeignKey("source_versions.id"), nullable=False),
    Column("pdf_source_id", ForeignKey("source_versions.id"), nullable=False),
    Column("attachment_link_id", ForeignKey("attachment_asset_links.id"), nullable=False),
    Column("report", JSON, nullable=False),
    constraints=(UniqueConstraint("annotation_snapshot_id", "pdf_source_id"),))

annotation_alignment_resolutions = table("annotation_alignment_resolutions",
    Column("alignment_id", ForeignKey("annotation_alignments.id"), nullable=False),
    Column("algorithm_version", String, nullable=False),
    Column("input_fingerprint", String, nullable=False),
    Column("report", JSON, nullable=False), Column("recorded_at", String, nullable=False),
    constraints=(UniqueConstraint("alignment_id", "algorithm_version", "input_fingerprint"),))

catalog_defaults = table("catalog_defaults", Column("name", String, nullable=False),
    Column("registration_ids", JSON, nullable=False), Column("initial_read_status", String, nullable=False),
    Column("verified_links", JSON, nullable=False), Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False), constraints=(UniqueConstraint("owner_id", "scope_id", "name"),))
catalog_versions = table("catalog_versions", Column("work_version_id", ForeignKey("work_versions.id"), nullable=False),
    Column("identity_state", String, nullable=False), Column("scientific_version_state", String, nullable=False),
    Column("scientific_version", String), Column("initial_read_status", String, nullable=False),
    Column("created_at", String, nullable=False), constraints=(UniqueConstraint("work_version_id"),))
catalog_bindings = table("catalog_bindings", Column("work_version_id", ForeignKey("work_versions.id"), nullable=False),
    Column("source_snapshot_id", ForeignKey("source_versions.id"), nullable=False),
    Column("authority_source_snapshot_id", ForeignKey("source_versions.id"), nullable=False),
    Column("source_object_id", ForeignKey("source_objects.id"), nullable=False), Column("role", String, nullable=False),
    Column("version_relation", String, nullable=False), Column("identity_claims", JSON, nullable=False),
    Column("conflicts", JSON, nullable=False), Column("state", String, nullable=False),
    Column("created_at", String, nullable=False), constraints=(UniqueConstraint("work_version_id", "source_snapshot_id"),))
catalog_aliases = table("catalog_aliases", Column("work_id", ForeignKey("works.id"), nullable=False),
    Column("scheme", String, nullable=False), Column("value", String, nullable=False),
    Column("source_snapshot_id", ForeignKey("source_versions.id"), nullable=False),
    Column("source_object_id", ForeignKey("source_objects.id"), nullable=False),
    Column("proof", JSON, nullable=False), Column("state", String, nullable=False), Column("created_at", String, nullable=False),
    constraints=(UniqueConstraint("owner_id", "scope_id", "scheme", "value", "source_snapshot_id", "work_id"),))

class Storage:
    def __init__(self, database_url):
        if not database_url.startswith(("sqlite:", "postgresql+psycopg:")):
            raise ValueError("use postgresql+psycopg:// or explicit sqlite fixture URL")
        self.engine = create_engine(database_url, pool_pre_ping=True)
        if self.engine.dialect.name == "sqlite":
            @event.listens_for(self.engine, "connect")
            def configure(dbapi, _):
                dbapi.isolation_level = None
                dbapi.execute("PRAGMA foreign_keys=ON")
                dbapi.execute("PRAGMA busy_timeout=10000")
            @event.listens_for(self.engine, "begin")
            def begin(connection):
                connection.exec_driver_sql("BEGIN IMMEDIATE")

    @contextmanager
    def transaction(self):
        with self.engine.begin() as connection:
            yield connection
