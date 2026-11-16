"""Opt-in, real PostgreSQL acceptance; every test owns a disposable schema.

Set RA_TEST_POSTGRES_URL to an explicitly designated test database URL using
postgresql+psycopg. The suite never drops a database, existing schema, or any
unscoped user data. The vector extension must already be installed in public.
All inserted domain records are synthetic. Credentials are not printed.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import importlib.util
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, func, text, MetaData, Table, Column, String
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from research_assistant import storage as db
from research_assistant.service import DomainError, ResearchService


class SecretURL(str):
    def __repr__(self):
        return "'<redacted PostgreSQL test URL>'"


def _load_existing_suite():
    path = Path(__file__).with_name("test_memory_core.py")
    spec = importlib.util.spec_from_file_location("postgres_shared_core_cases", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


core = _load_existing_suite()
SCENARIOS = [function for name, function in vars(core).items()
             if name.startswith("test_") and callable(function)
             and name != "test_other_owner_or_scope_cannot_read_evidence"]


@pytest.fixture
def pg_url():
    configured = os.environ.get("RA_TEST_POSTGRES_URL")
    if not configured:
        pytest.skip("Set RA_TEST_POSTGRES_URL for isolated real PostgreSQL checks")
    admin = None
    schema = None
    schema_created = False
    setup_error = None
    try:
        parsed = make_url(configured)
        if parsed.drivername != "postgresql+psycopg" or not parsed.database:
            pytest.fail("RA_TEST_POSTGRES_URL must use postgresql+psycopg", pytrace=False)
        admin = create_engine(parsed, pool_pre_ping=True, connect_args={"connect_timeout": 10})
        schema = "ra_test_" + uuid4().hex
        with admin.begin() as connection:
            connection.execute(CreateSchema(schema))
            schema_created = True
            # Shadow a possible public.alembic_version. Otherwise Alembic's
            # default search_path lookup could see another schema's head and
            # skip creating this test's isolated canonical tables entirely.
            Table("alembic_version", MetaData(),
                  Column("version_num", String(32), primary_key=True), schema=schema).create(connection)
        options = parsed.query.get("options", "") + " -csearch_path=" + schema + ",public"
        isolated = parsed.update_query_dict({"options": options.strip()})
    except Exception as exc:
        setup_error = type(exc).__name__
        if admin is not None:
            if schema_created:
                try:
                    with admin.begin() as connection:
                        connection.execute(DropSchema(schema, cascade=True, if_exists=True))
                except Exception:
                    # Never surface a driver's credential-bearing traceback if
                    # setup cleanup fails after the original setup error.
                    pass
            admin.dispose()
    if setup_error is not None:
        # Fail outside the exception handler so pytest has no driver exception
        # context containing a connection URL or password to render.
        pytest.fail("PostgreSQL test setup failed (" + setup_error + ")", pytrace=False)
    try:
        yield SecretURL(isolated.render_as_string(hide_password=False))
    finally:
        cleanup_error = None
        try:
            with admin.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
        except Exception as exc:
            cleanup_error = type(exc).__name__
        finally:
            admin.dispose()
        if cleanup_error is not None:
            pytest.fail("PostgreSQL test cleanup failed (" + cleanup_error + ")", pytrace=False)


@pytest.fixture
def service(pg_url):
    result = ResearchService(pg_url, owner_id="fixture-owner", scope_id="private")
    result.initialize()
    try:
        yield result
    finally:
        result.storage.engine.dispose()


@pytest.fixture
def papers():
    return json.loads(core.FIXTURE.read_text())["papers"]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda function: function.__name__)
def test_existing_memory_semantics_on_postgresql(scenario, service, pg_url, papers):
    arguments = {"service": service, "database_url": pg_url, "papers": papers}
    scenario(**{key: arguments[key] for key in inspect.signature(scenario).parameters})


@pytest.mark.parametrize("owner,scope", [("other-owner", "private"), ("fixture-owner", "other-scope")])
def test_owner_scope_boundaries_on_postgresql(service, pg_url, papers, owner, scope):
    core.test_other_owner_or_scope_cannot_read_evidence(service, pg_url, papers, owner, scope)


def test_migration_is_repeatable_and_matches_postgresql_schema(service):
    service.initialize()
    with service.storage.engine.connect() as connection:
        def canonical_object(obj, name, kind, reflected, compare_to):
            return not ((kind == "table" and name == "evidence_embeddings")
                        or (kind == "index" and name == "ix_evidence_spans_fts"))
        differences = compare_metadata(MigrationContext.configure(connection, opts={
            "include_object": canonical_object,
        }), db.metadata)
        revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
    assert differences == []
    assert revision


def _services(pg_url):
    first = ResearchService(pg_url, owner_id="fixture-owner", scope_id="private")
    second = ResearchService(pg_url, owner_id="fixture-owner", scope_id="private")
    first.initialize()
    second.initialize()
    return first, second


def test_simultaneous_same_key_has_one_event_and_one_import(pg_url, papers):
    first, second = _services(pg_url)
    payload = core.paper_payload(papers[0], "simultaneous-shared-key")
    barrier = Barrier(2)
    def write(service):
        barrier.wait()
        return service.execute("papers.import", payload)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(write, [first, second]))
        assert receipts[0] == receipts[1]
        with first.storage.transaction() as connection:
            assert connection.scalar(select(func.count()).select_from(db.works)) == 1
            assert connection.scalar(select(func.count()).select_from(db.events)) == 1
    finally:
        first.storage.engine.dispose()
        second.storage.engine.dispose()


def test_simultaneous_reused_key_with_different_payload_conflicts(pg_url, papers):
    first, second = _services(pg_url)
    first_payload = core.paper_payload(papers[0], "simultaneous-conflict-key")
    second_payload = dict(first_payload, title="Different synthetic title")
    barrier = Barrier(2)
    def write(index):
        barrier.wait()
        try:
            return [first, second][index].execute("papers.import", [first_payload, second_payload][index])["status"]
        except DomainError as exc:
            return exc.code
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(write, range(2)))
        assert sorted(outcomes) == ["idempotency_conflict", "ok"]
        with first.storage.transaction() as connection:
            assert connection.scalar(select(func.count()).select_from(db.events)) == 1
    finally:
        first.storage.engine.dispose()
        second.storage.engine.dispose()


def test_simultaneous_revision_writers_have_one_winner(pg_url, papers):
    first, second = _services(pg_url)
    try:
        imported = core.import_paper(first, papers[0])
        original = core.record_reading(first, imported["paper_version_id"], "deep", ["method"])
        messages = [core.capture_reading(service, imported["paper_version_id"], depth, ["method"])
                    for service, depth in [(first, "skim"), (second, "unknown")]]
        barrier = Barrier(2)
        def write(index):
            barrier.wait()
            try:
                return [first, second][index].execute("memory.record_user_statement", {
                    "trusted_message_id": messages[index]["trusted_message_id"],
                    "memory_id": original["memory_id"], "expected_revision": 1,
                    "idempotency_key": "revision-writer-" + str(index),
                })["status"]
            except DomainError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(write, range(2)))
        assert sorted(outcomes) == ["ok", "revision_conflict"]
        current = core.execute(first, "memory.recall")["reported_facts"]
        assert len(current) == 1 and current[0]["revision"] == 2
    finally:
        first.storage.engine.dispose()
        second.storage.engine.dispose()


def test_postgres_failure_rolls_back_fact_event_and_outbox(service, papers):
    imported = core.import_paper(service, papers[0])
    old = core.record_reading(service, imported["paper_version_id"], "deep", ["method"])
    captured = core.capture_reading(service, imported["paper_version_id"], "skim", ["method"])
    def snapshot():
        with service.storage.transaction() as connection:
            return tuple(connection.scalar(select(func.count()).select_from(table))
                         for table in [db.assertions, db.events, db.jobs])
    before = snapshot()
    with pytest.raises(DomainError) as failure:
        service.execute("memory.record_user_statement", {
            "trusted_message_id": captured["trusted_message_id"], "memory_id": old["memory_id"],
            "expected_revision": 1, "change_kind": "state_change", "idempotency_key": "invalid-state-change",
        })
    assert failure.value.code == "invalid_transition"
    assert snapshot() == before
    current = core.execute(service, "memory.recall")["reported_facts"]
    assert current[0]["assertion_id"] == old["assertion_id"]
    assert current[0]["system_to"] is None


def test_independent_python_process_recovers_committed_memory(service, pg_url, papers):
    imported = core.import_paper(service, papers[0])
    memory = core.record_reading(service, imported["paper_version_id"], "deep", ["method"])
    script = """import json, os
from research_assistant.service import ResearchService
s=ResearchService(os.environ['RA_TEST_POSTGRES_URL'],owner_id='fixture-owner',scope_id='private')
s.initialize()
r=s.execute('memory.recall',{})
assert any(m['memory_id']==os.environ['RA_EXPECTED_MEMORY_ID'] for m in r['data']['reported_facts'])
print(json.dumps({'recovered':True}))
"""
    environment = dict(os.environ, RA_TEST_POSTGRES_URL=str(pg_url), RA_EXPECTED_MEMORY_ID=memory["memory_id"])
    process = subprocess.run([sys.executable, "-c", script], env=environment,
                             text=True, capture_output=True, timeout=30)
    assert process.returncode == 0, "Independent process could not recover PostgreSQL memory"
    assert json.loads(process.stdout) == {"recovered": True}


def test_pgvector_extension_persists_vectors_and_orders_cosine_distance(service):
    # This is an actual server extension/operator test, not an embedding-quality
    # claim. Fixed vectors make correctness independent of any external model.
    with service.storage.transaction() as connection:
        extension = connection.execute(text("SELECT extversion, extnamespace::regnamespace::text FROM pg_extension WHERE extname='vector'" )).first()
        assert extension is not None, "Install the pgvector server extension before acceptance"
        assert extension[1] == "public", "Keep the extension outside disposable test schemas"
        connection.exec_driver_sql("CREATE TABLE vector_runtime_probe (name text PRIMARY KEY, embedding public.vector(3))")
        connection.execute(text("INSERT INTO vector_runtime_probe VALUES ('parallel','[1,0,0]'), ('orthogonal','[0,1,0]'), ('opposite','[-1,0,0]')"))
    # Read in a second transaction so a successful INSERT alone is insufficient.
    with service.storage.transaction() as connection:
        rows = connection.execute(text("SELECT name, embedding <=> '[1,0,0]'::public.vector AS distance FROM vector_runtime_probe ORDER BY distance")).all()
    assert [row[0] for row in rows] == ["parallel", "orthogonal", "opposite"]
    assert [float(row[1]) for row in rows] == pytest.approx([0.0, 1.0, 2.0])


def test_postgresql_english_fulltext_and_chinese_exact_retrieval(service, papers):
    imported = core.import_paper(service, papers[0])
    # Direct PostgreSQL FTS capabilities are tested independently of the service
    # search mode. The baseline service must also retain Chinese substring recall.
    with service.storage.transaction() as connection:
        assert connection.scalar(text("SELECT to_tsvector('english','Synthetic collaborative decision methods') @@ plainto_tsquery('english','decision method')")) is True
    chinese = next(section["text"] for section in papers[0]["sections"] if any("\u4e00" <= char <= "\u9fff" for char in section["text"]))
    matched = core.execute(service, "papers.search", {"query": chinese[:4]})["papers"]
    assert {paper["paper_version_id"] for paper in matched} == {imported["paper_version_id"]}
