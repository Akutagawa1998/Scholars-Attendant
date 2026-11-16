"""Default knowledge catalog contracts; synthetic sources and optional isolated PG.

The existing core fixtures supply trusted user capture and disposable PostgreSQL
schemas. No provider is contacted and no personal library is used.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from research_assistant import storage as db
from research_assistant.asset_service import read_source_asset
from research_assistant.assets import ArtifactStore
from research_assistant.service import DomainError, ResearchService
from research_assistant.zotero_assets import ZoteroPdfIngestor


def _fixture_module(name):
    path = Path(__file__).parent / "integration" / (name + ".py")
    spec = importlib.util.spec_from_file_location("catalog_shared_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_postgres = _fixture_module("test_postgres")
pg_url = _postgres.pg_url
core = _postgres.core
pdf = _fixture_module("test_assets").pdf

ZOTERO = dict(provider="zotero", source_instance_id="catalog-synthetic-library", namespace="users/123")
NOTION = dict(provider="notion", source_instance_id="catalog-synthetic-workspace",
              namespace="collection://0123456789abcdef0123456789abcdef")
NOW = "2026-10-26T00:00:00Z"
DOI = "10.5555/catalog-fixture"
TITLE = "Synthetic catalog paper"


@pytest.fixture(params=["sqlite", "postgres"])
def service(request, tmp_path):
    url = ("sqlite:///" + str(tmp_path / "catalog.sqlite3") if request.param == "sqlite"
           else request.getfixturevalue("pg_url"))
    instance = ResearchService(url, owner_id="catalog-fixture-owner", scope_id="private")
    instance.initialize()
    instance.test_database_url = url
    yield instance
    instance.storage.engine.dispose()


def call(service, operation, *, operator=False, **payload):
    response = service.execute(operation, payload, trusted_capture=operator)
    assert response["status"] in {"ok", "partial"}, response
    return response


def register(service, settings=ZOTERO):
    return call(service, "sources.register", operator=True, **settings,
                idempotency_key=str(uuid4()))["data"]["registration_id"]


def configure(service, *settings):
    settings = settings or (ZOTERO, NOTION)
    registrations = [register(service, item) for item in settings]
    links = [dict(provider=item["provider"], namespace=item["namespace"], checked_at=NOW, valid=True,
                  url=("https://www.zotero.org/users/123/library" if item["provider"] == "zotero"
                       else "https://www.notion.so/0123456789abcdef0123456789abcdef")) for item in settings]
    return call(service, "catalog.configure", operator=True, registration_ids=registrations,
                verified_links=links, initial_read_status="unread", idempotency_key=str(uuid4()))["data"]


def snapshot(service, *, settings=ZOTERO, key="PAPER001", revision="22", raw=None, passages=None,
             change="upsert"):
    register(service, settings)
    raw = raw if raw is not None else dict(key=key, version=int(revision), data=dict(
        key=key, version=int(revision), itemType="journalArticle", title=TITLE, DOI=DOI))
    if passages is None:
        passages = [dict(text=raw["data"]["title"], json_path=["data", "title"], section="title")]
    return call(service, "sources.ingest", operator=True, **settings, external_id=key,
                external_version=revision, source_url="https://example.invalid/catalog-source",
                raw_payload=raw, observed_at=datetime.now(timezone.utc).isoformat(), occurred_at=None,
                completeness="complete", missing_parts=[], change=change, passages=passages,
                idempotency_key=str(uuid4()))["data"]["source_snapshot_id"]


def bind(service, source_id, **options):
    return call(service, "catalog.bind", operator=True, source_snapshot_id=source_id,
                idempotency_key=str(uuid4()), **options)["data"]


def papers(service, **filters):
    return call(service, "papers.search", **filters)["data"]["papers"]


def count(service, table):
    with service.storage.transaction() as connection:
        return connection.scalar(select(func.count()).select_from(table).where(service._scope(table)))


def set_access(service, source_id, state):
    with service.storage.transaction() as connection:
        revision = service._row(connection, db.sources, source_id)["revision"]
    return call(service, "sources.set_access", operator=True, source_snapshot_id=source_id,
                expected_revision=revision, state=state, idempotency_key=str(uuid4()))


def assert_not_served(service, version_id, marker=TITLE, *, historical_source_allowed=False):
    assert all(paper["paper_version_id"] != version_id for paper in papers(service))
    response = service.execute("papers.get_evidence", dict(paper_version_id=version_id))
    assert response["data"].get("passages", []) == []
    exported = service.execute("export", {})["data"]
    assert all(version["id"] != version_id for version in exported["work_versions"])
    assert marker not in json.dumps(exported["works"], ensure_ascii=False)
    if not historical_source_allowed:
        assert marker not in json.dumps(exported, ensure_ascii=False)


def test_catalog_initial_unread_does_not_invent_user_reading(service):
    configure(service)
    source_id = snapshot(service)
    before = {table.name: count(service, table) for table in (db.sources, db.evidence, db.captures,
                                                             db.assertions, db.reading_sessions)}
    result = bind(service, source_id)
    assert result["initial_read_status"] == "unread"
    assert result["reading_mutated"] is False
    assert result["scientific_version_state"] == "unknown"
    assert result["scientific_version"] is None
    assert before == {table.name: count(service, table) for table in (db.sources, db.evidence, db.captures,
                                                                    db.assertions, db.reading_sessions)}
    selected = next(item for item in papers(service) if item["paper_version_id"] == result["paper_version_id"])
    assert selected["read_status"] == "unknown"
    assert selected["workflow_status"] == "unread"
    assert selected["reading"]["workflow_status"] == "unread"
    assert selected["reading"]["understanding_status"] == "not_assessed"
    assert papers(service, read_filter="read_only") == []


def test_catalog_bind_replay_with_new_request_key_has_no_duplicate_domain_effect(service):
    configure(service)
    source_id = snapshot(service)
    first = bind(service, source_id)
    tables = (db.works, db.versions, db.catalog_versions, db.catalog_bindings, db.catalog_aliases,
              db.sources, db.evidence, db.reading_sessions)
    baseline = {table.name: count(service, table) for table in tables}
    second = bind(service, source_id)
    assert second["paper_id"] == first["paper_id"]
    assert second["paper_version_id"] == first["paper_version_id"]
    assert baseline == {table.name: count(service, table) for table in tables}
    assert count(service, db.catalog_bindings) == 1


def test_catalog_search_and_evidence_keep_metadata_context_roles(service):
    configure(service)
    source_id = snapshot(service)
    version_id = bind(service, source_id)["paper_version_id"]
    selected = next(item for item in papers(service, query="catalog") if item["paper_version_id"] == version_id)
    assert selected["scientific_version_state"] == "unknown"
    assert selected["scientific_version"] is None
    response = call(service, "papers.get_evidence", paper_version_id=version_id)
    assert response["data"]["passages"]
    assert all(passage["support_type"] == "context_only" for passage in response["data"]["passages"])
    assert all(citation["source_kind"] == "zotero" and citation["support_type"] == "context_only"
               for citation in response["citations"])
    assert all(citation["source_snapshot_id"] == source_id and citation["locator"]["kind"] == "json"
               for citation in response["citations"])
    assert all("page_index" not in citation["locator"] for citation in response["citations"])


def test_existing_trusted_reading_survives_catalog_rebind_and_default_reset(service):
    configure(service)
    source_id = snapshot(service)
    version_id = bind(service, source_id)["paper_version_id"]
    core.record_reading(service, version_id, "deep", ["title"])
    assert count(service, db.reading_sessions) == 1
    configure(service)
    bind(service, source_id)
    selected = next(item for item in papers(service, read_filter="read_only")
                    if item["paper_version_id"] == version_id)
    assert selected["read_status"] == "read"
    assert selected["reading"]["sections"]["title"] == "deep"
    assert selected["reading"]["understanding_status"] == "not_assessed"
    assert count(service, db.reading_sessions) == 1


@pytest.mark.parametrize("change", ["stale", "revoke", "forget"])
def test_catalog_source_head_and_privacy_gates_block_every_serving_path(service, change):
    configure(service)
    source_id = snapshot(service)
    version_id = bind(service, source_id)["paper_version_id"]
    if change == "stale":
        newer = snapshot(service, revision="23", raw=dict(key="PAPER001", version=23, data=dict(
            key="PAPER001", version=23, itemType="journalArticle", title="Replacement current title", DOI=DOI)))
        assert newer != source_id
    elif change == "revoke":
        set_access(service, source_id, "inaccessible")
    else:
        call(service, "memory.forget", target_ids=[source_id], scope="memory_and_source_snapshots",
             idempotency_key=str(uuid4()))
    assert_not_served(service, version_id, historical_source_allowed=change == "stale")
    with pytest.raises(DomainError):
        bind(service, source_id)
    assert count(service, db.reading_sessions) == 0


@pytest.mark.parametrize("change", ["stale", "revoke", "forget"])
def test_delayed_trusted_reading_cannot_attach_to_unavailable_catalog_source(service, change):
    configure(service)
    source_id = snapshot(service)
    version_id = bind(service, source_id)["paper_version_id"]
    captured = core.capture_reading(service, version_id, "deep", ["title"])
    if change == "stale":
        snapshot(service, revision="23")
    elif change == "revoke":
        set_access(service, source_id, "inaccessible")
    else:
        call(service, "memory.forget", target_ids=[source_id], scope="memory_and_source_snapshots",
             idempotency_key=str(uuid4()))
    with pytest.raises(DomainError):
        call(service, "memory.record_user_statement", trusted_message_id=captured["trusted_message_id"],
             idempotency_key=str(uuid4()))
    assert count(service, db.reading_sessions) == 0


@pytest.mark.parametrize("operation", ["catalog.configure", "catalog.bind"])
def test_catalog_configuration_and_bind_require_operator_channel(service, operation):
    configure(service)
    source_id = snapshot(service)
    payload = (dict(registration_ids=[], verified_links=[], initial_read_status="unread")
               if operation == "catalog.configure" else dict(source_snapshot_id=source_id))
    with pytest.raises(DomainError) as failure:
        service.execute(operation, dict(payload, idempotency_key=str(uuid4())))
    assert failure.value.code == "capture_forbidden"


def test_registered_but_nondefault_namespace_is_not_silently_added(service):
    configure(service, ZOTERO)
    outside = dict(NOTION, namespace="collection://not-selected")
    source_id = snapshot(service, settings=outside, key="outside-page")
    with pytest.raises(DomainError):
        bind(service, source_id)
    assert papers(service) == []
    assert count(service, db.versions) == 0


def test_catalog_does_not_allow_cross_scope_bind_or_reconfigure(service):
    configure(service)
    source_id = snapshot(service)
    with service.storage.transaction() as connection:
        registration_id = service._registration(connection, ZOTERO)["id"]
    outsider = ResearchService(service.test_database_url, owner_id=service.owner_id, scope_id="other")
    outsider.initialize()
    try:
        with pytest.raises(DomainError):
            bind(outsider, source_id)
        with pytest.raises(DomainError):
            call(outsider, "catalog.configure", operator=True, registration_ids=[registration_id],
                 verified_links=[], initial_read_status="unread", idempotency_key=str(uuid4()))
        assert papers(outsider) == []
    finally:
        outsider.storage.engine.dispose()


def test_default_catalog_excludes_legacy_import_and_explicit_all_preserves_it(service):
    imported = core.import_paper(service, json.loads(core.FIXTURE.read_text())["papers"][0])
    configure(service)
    source_id = snapshot(service)
    catalog = bind(service, source_id)
    assert {paper["paper_version_id"] for paper in papers(service)} == {catalog["paper_version_id"]}
    assert {paper["paper_version_id"] for paper in papers(service, knowledge_base="all")} == {
        catalog["paper_version_id"], imported["paper_version_id"]}


def test_catalog_status_is_read_only_and_counts_only_current_available_bindings(service):
    configure(service)
    source_id = snapshot(service)
    bind(service, source_id)
    events = count(service, db.events)
    state = call(service, "catalog.status")["data"]
    assert state["configured"] is True
    assert state["catalog_versions"] == 1
    assert state["unknown_scientific_versions"] == 1
    assert state["explicit_scientific_versions"] == 0
    assert count(service, db.events) == events
    set_access(service, source_id, "inaccessible")
    assert call(service, "catalog.status")["data"]["catalog_versions"] == 0


def notion_snapshot(service, *, key="synthetic-notion-paper", paper_url="https://doi.org/" + DOI,
                    body="Synthetic personal prose; this is not the original paper.", revision="notion-revision-91"):
    text = '<properties>' + json.dumps({"Paper URL": paper_url}) + '</properties>\n<content>' + body + '</content>'
    raw = dict(title=TITLE, text=text)
    source_id = snapshot(service, settings=NOTION, key=key, revision=revision, raw=raw,
                         passages=[dict(text=TITLE, json_path=["title"], section="title"),
                                   dict(text=text, json_path=["text"], section="notes")])
    return source_id, text


def test_notion_note_title_and_prose_remain_secondary_context(service):
    configure(service)
    source_id, text = notion_snapshot(service)
    receipt = bind(service, source_id)
    selected, = papers(service, query="personal prose")
    assert selected["paper_version_id"] == receipt["paper_version_id"]
    assert selected["title"] == TITLE
    assert selected["sources"][0]["role"] == "note"
    assert selected["scientific_version_state"] == "unknown"
    assert selected["scientific_version"] is None
    evidence = call(service, "papers.get_evidence", paper_version_id=receipt["paper_version_id"])
    assert text in [span["text"] for span in evidence["data"]["passages"]]
    assert all(span["source_role"] == "note" and span["support_type"] == "context_only"
               for span in evidence["data"]["passages"])
    assert all(citation["source_kind"] == "notion" and citation["locator"]["kind"] == "json"
               for citation in evidence["citations"])
    assert count(service, db.reading_sessions) == 0


def test_catalog_pdf_binding_keeps_unknown_version_relation_and_attachment_gate(service, tmp_path):
    configure(service)
    parent_id = snapshot(service)
    raw_pdf = pdf("Synthetic canonical PDF original.")
    checksum = hashlib.md5(raw_pdf, usedforsecurity=False).hexdigest()
    attachment_id = snapshot(service, key="ATTACH01", raw=dict(key="ATTACH01", version=22, data=dict(
        key="ATTACH01", version=22, itemType="attachment", title="Synthetic attachment", parentItem="PAPER001",
        contentType="application/pdf", linkMode="imported_file", md5=checksum)))
    with ArtifactStore(tmp_path / "catalog-assets") as store:
        pdf_receipt = ZoteroPdfIngestor(service, store).ingest(raw_pdf, attachment_snapshot_id=attachment_id,
            provenance=dict(attachment_key="ATTACH01", attachment_version=22, attachment_md5=checksum))
        pdf_id = pdf_receipt["source_snapshot_id"]
        catalog = bind(service, parent_id)
        selected, = papers(service)
        bindings = {binding["role"]: binding for binding in selected["sources"]}
        assert set(bindings) == {"metadata", "original_pdf"}
        assert bindings["original_pdf"]["source_snapshot_id"] == pdf_id
        assert bindings["original_pdf"]["version_relation"] == "unknown"
        response = call(service, "papers.get_evidence", paper_version_id=catalog["paper_version_id"])
        pdf_passages = [span for span in response["data"]["passages"] if span["source_role"] == "original_pdf"]
        assert len(pdf_passages) == 1
        assert pdf_passages[0]["version_relation"] == "unknown"
        assert pdf_passages[0]["support_type"] == "context_only"
        assert read_source_asset(service, store, pdf_id) == raw_pdf
        set_access(service, attachment_id, "inaccessible")
        selected, = papers(service)
        assert {binding["role"] for binding in selected["sources"]} == {"metadata"}
        hidden = service.execute("papers.get_evidence", dict(paper_version_id=catalog["paper_version_id"],
                                 evidence_ids=[pdf_passages[0]["evidence_id"]]))
        assert hidden["status"] == "not_found"
        assert hidden["data"]["passages"] == []
        with pytest.raises(DomainError):
            read_source_asset(service, store, pdf_id)
        exported = call(service, "export")["data"]
        assert all(source["id"] != pdf_id for source in exported["source_versions"])
        assert count(service, db.reading_sessions) == 0


def test_doi_and_explicit_arxiv_identity_join_sources_without_changing_roles(service):
    configure(service)
    raw = dict(key="PAPER001", version=22, data=dict(key="PAPER001", version=22,
        itemType="preprint", title=TITLE, DOI=DOI.upper(), url="https://arxiv.org/abs/2601.01234v2"))
    zotero = bind(service, snapshot(service, raw=raw))
    notion_id, _ = notion_snapshot(service, paper_url="https://arxiv.org/pdf/2601.01234v2.pdf")
    notion = bind(service, notion_id)
    assert notion["paper_id"] == zotero["paper_id"]
    assert notion["paper_version_id"] == zotero["paper_version_id"]
    assert notion["scientific_version_state"] == "explicit"
    assert notion["scientific_version"] == "v2"
    selected, = papers(service)
    assert selected["version"] == "v2"
    assert {binding["role"] for binding in selected["sources"]} == {"metadata", "note"}
    evidence = call(service, "papers.get_evidence", paper_version_id=notion["paper_version_id"])
    assert {span["source_role"] for span in evidence["data"]["passages"]} == {"metadata", "note"}
    assert all(span["support_type"] == "context_only" for span in evidence["data"]["passages"])
    assert count(service, db.works) == 1 and count(service, db.versions) == 1
    assert count(service, db.reading_sessions) == 0


def test_provider_revision_and_body_citations_do_not_invent_scientific_version(service):
    configure(service)
    zotero = bind(service, snapshot(service, revision="91"))
    assert zotero["scientific_version"] is None
    assert zotero["scientific_version_state"] == "unknown"
    source_id, _ = notion_snapshot(service, paper_url="", body="A related citation https://arxiv.org/abs/2601.01234v9")
    notion = bind(service, source_id)
    assert notion["identity_state"] == "provisional"
    assert notion["scientific_version"] is None
    assert notion["paper_id"] != zotero["paper_id"]
    assert all(paper["version"] == "unknown" for paper in papers(service))


def test_new_scientific_version_does_not_inherit_prior_reading(service):
    configure(service)
    first_id, _ = notion_snapshot(service, key="notion-v1", paper_url="https://arxiv.org/abs/2601.01234v1")
    first = bind(service, first_id)
    core.record_reading(service, first["paper_version_id"], "skim", ["notes"])
    second_id, _ = notion_snapshot(service, key="notion-v2", paper_url="https://arxiv.org/abs/2601.01234v2")
    second = bind(service, second_id)
    assert first["paper_id"] == second["paper_id"]
    assert first["paper_version_id"] != second["paper_version_id"]
    assert {paper["paper_version_id"] for paper in papers(service, read_filter="read_only")} == {first["paper_version_id"]}
    current = {paper["paper_version_id"]: paper for paper in papers(service)}
    assert current[second["paper_version_id"]]["read_status"] == "unknown"
    assert current[second["paper_version_id"]]["workflow_status"] == "unread"
    assert count(service, db.reading_sessions) == 1


@pytest.mark.parametrize("version", ["1", "v1", "v111"])
def test_arxiv_import_requires_exact_version_token(service, version):
    with pytest.raises(DomainError) as failure:
        service.execute("papers.import", dict(identifier="arxiv:2601.01234v11", version=version,
            title=TITLE, source_url="https://arxiv.org/abs/2601.01234v11", content="Synthetic original text.",
            passages=[dict(text="Synthetic original text.", section="method", locator=dict(kind="text"))],
            idempotency_key=str(uuid4())))
    assert failure.value.code == "version_mismatch"
    assert count(service, db.works) == 0


@pytest.mark.parametrize("version", ["11", "v11"])
def test_arxiv_import_accepts_matching_full_version_number(service, version):
    result = call(service, "papers.import", identifier="arxiv:2601.01234v11", version=version,
        title=TITLE, source_url="https://arxiv.org/abs/2601.01234v11", content="Synthetic original text.",
        passages=[dict(text="Synthetic original text.", section="method", locator=dict(kind="text"))],
        idempotency_key=str(uuid4()))
    assert result["data"]["paper_version_id"]
    assert count(service, db.versions) == 1
