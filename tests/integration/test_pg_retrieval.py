"""Actual PG/pgvector adapter tests with explicitly synthetic vectors.

No model-quality claim: vectors are simple fixed geometric fixtures. The opt-in
PostgreSQL fixture owns and drops only its randomly generated database schema.
"""
from __future__ import annotations

import math
from uuid import uuid4

import pytest
from sqlalchemy import text

from research_assistant.retrieval import PgRetrieval
from research_assistant.service import DomainError, ResearchService
from test_postgres import pg_url, service, papers, core

MODEL = "synthetic-geometry-v1"


def imported(service, papers):
    return core.import_paper(service, papers[0])


def put(service, evidence_id, vector, model=MODEL, source_revision=1, deletion_epoch=0, access_epoch=0):
    return PgRetrieval(service).put_embedding(
        evidence_id, vector, model,
        expected_source_revision=source_revision,
        expected_deletion_epoch=deletion_epoch,
        expected_access_epoch=access_epoch,
    )


def rank(service, evidence_ids, query="", **options):
    with service.storage.transaction() as connection:
        return PgRetrieval(service).rank(connection, evidence_ids, query, **options)


def test_native_fulltext_and_cjk_fallback_return_canonical_evidence(service, papers):
    paper = imported(service, papers)
    ids = paper["evidence_ids"]
    hits = rank(service, ids, "method")
    assert hits and all(hit["evidence_id"] in ids for hit in hits)
    assert hits[0]["lexical_score"] > 0
    chinese = next(section["text"] for section in papers[0]["sections"] if any("\u4e00" <= char <= "\u9fff" for char in section["text"]))
    hits = rank(service, ids, chinese[:4])
    assert hits and all(hit["lexical_score"] > 0 for hit in hits)
    # Query content remains a parameter, even if it resembles a SQL statement.
    assert rank(service, ids, "'; DROP TABLE evidence_spans; --") == []
    assert rank(service, ids, "method")


def test_real_pgvector_cosine_ranking_and_model_binding(service, papers):
    paper = imported(service, papers)
    ids = paper["evidence_ids"][:3]
    for eid, vector in zip(ids, [[1, 0, 0], [0, 1, 0], [-1, 0, 0]]):
        put(service, eid, vector)
    hits = rank(service, ids, query_vector=[1, 0, 0], embedding_model=MODEL)
    assert [hit["evidence_id"] for hit in hits] == ids
    assert [hit["vector_distance"] for hit in hits] == pytest.approx([0, 1, 2])
    assert hits[0]["rrf_score"] > hits[1]["rrf_score"] > hits[2]["rrf_score"]
    assert all("text" not in hit for hit in hits)
    with pytest.raises(DomainError) as caught:
        rank(service, ids, query_vector=[1, 0], embedding_model=MODEL)
    assert caught.value.code == "embedding_dimension_mismatch"
    with pytest.raises(DomainError) as caught:
        put(service, paper["evidence_ids"][-1], [1, 0])
    assert caught.value.code == "embedding_dimension_mismatch"


def test_allowlist_is_applied_before_ranking(service, papers):
    paper = imported(service, papers)
    ids = paper["evidence_ids"][:2]
    put(service, ids[0], [1, 0, 0])
    put(service, ids[1], [0, 1, 0])
    assert [hit["evidence_id"] for hit in rank(service, [ids[1]], query_vector=[1, 0, 0], embedding_model=MODEL)] == [ids[1]]
    assert rank(service, [], "method") == []


def test_rank_rechecks_owner_and_scope_even_with_foreign_ids(service, pg_url, papers):
    paper = imported(service, papers)
    for owner, scope in [("other-owner", "private"), ("fixture-owner", "other-scope")]:
        outsider = ResearchService(pg_url, owner, scope)
        outsider.initialize()
        try:
            assert rank(outsider, paper["evidence_ids"], "method") == []
            with pytest.raises(DomainError):
                put(outsider, paper["evidence_ids"][0], [1, 0, 0])
        finally:
            outsider.storage.engine.dispose()


def test_revocation_fences_late_embedding_and_canonical_lexical_hits(service, papers):
    paper = imported(service, papers)
    eid = paper["evidence_ids"][0]
    put(service, eid, [1, 0, 0])
    assert rank(service, [eid], query_vector=[1, 0, 0], embedding_model=MODEL)
    core.execute(service, "sources.set_access", {
        "source_snapshot_id": paper["source_snapshot_id"], "expected_revision": 1,
        "state": "inaccessible", "idempotency_key": str(uuid4()),
    })
    assert rank(service, paper["evidence_ids"], "method") == []
    with pytest.raises(DomainError) as caught:
        put(service, eid, [1, 0, 0])
    assert caught.value.code == "stale_epoch"
    with pytest.raises(DomainError) as caught:
        put(service, eid, [1, 0, 0], source_revision=2, access_epoch=1)
    assert caught.value.code == "unavailable_evidence"
    with service.storage.transaction() as connection:
        assert connection.scalar(text("SELECT count(*) FROM evidence_embeddings")) == 0


def test_forgetting_purges_vectors_and_fences_late_indexer(service, papers):
    paper = imported(service, papers)
    eid = paper["evidence_ids"][0]
    put(service, eid, [1, 0, 0])
    core.execute(service, "memory.forget", {
        "target_ids": [paper["source_snapshot_id"]], "scope": "memory_and_source_snapshots",
        "idempotency_key": str(uuid4()),
    })
    assert rank(service, paper["evidence_ids"], "method") == []
    with pytest.raises(DomainError) as caught:
        put(service, eid, [1, 0, 0])
    assert caught.value.code == "stale_epoch"
    with service.storage.transaction() as connection:
        assert connection.scalar(text("SELECT count(*) FROM evidence_embeddings")) == 0


@pytest.mark.parametrize("vector", [[], [0, 0], [math.nan, 1], [math.inf, 1], [1e100, 1]])
def test_invalid_vectors_fail_without_creating_index_entries(service, papers, vector):
    paper = imported(service, papers)
    with pytest.raises(DomainError) as caught:
        put(service, paper["evidence_ids"][0], vector)
    assert caught.value.code == "invalid_embedding"
    with service.storage.transaction() as connection:
        assert connection.scalar(text("SELECT count(*) FROM evidence_embeddings")) == 0


def test_revised_source_requires_a_fresh_embedding(service, papers):
    paper = imported(service, papers)
    eid = paper["evidence_ids"][0]
    put(service, eid, [1, 0, 0])
    core.execute(service, "sources.set_access", {
        "source_snapshot_id": paper["source_snapshot_id"], "expected_revision": 1,
        "state": "accessible", "idempotency_key": str(uuid4()),
    }, trusted_capture=True)
    with pytest.raises(DomainError) as caught:
        put(service, eid, [1, 0, 0], source_revision=1, access_epoch=1)
    assert caught.value.code == "stale_source_revision"
    put(service, eid, [1, 0, 0], source_revision=2, access_epoch=1)
    assert rank(service, [eid], query_vector=[1, 0, 0], embedding_model=MODEL)


def test_service_hybrid_read_scope_filters_before_vector_ranking(service, papers):
    paper = imported(service, papers)
    unread_paper = core.import_paper(service, papers[1])
    method_id, experiment_id = paper["evidence_ids"][1:3]
    put(service, method_id, [0, 1, 0])
    put(service, experiment_id, [1, 0, 0])
    put(service, unread_paper["evidence_ids"][1], [1, 0, 0])
    core.record_reading(service, paper["paper_version_id"], "skim", ["abstract"])
    core.record_reading(service, paper["paper_version_id"], "deep", ["method"], unread=["experiments"])
    result = service.execute("papers.search", {
        "retrieval_mode": "hybrid", "query_vector": [1, 0, 0], "embedding_model": MODEL,
        "read_filter": "read_only", "read_depths": ["deep"], "coverage_filter": ["method"],
    })
    assert result["status"] == "ok"
    assert [p["paper_version_id"] for p in result["data"]["papers"]] == [paper["paper_version_id"]]
    assert result["data"]["papers"][0]["evidence_ids"] == [method_id]
    assert {citation["evidence_id"] for citation in result["citations"]} == {method_id}
    assert result["data"]["vector_index"] == "exact"
    assert not core.execute(service, "papers.search", {
        "retrieval_mode": "hybrid", "query": "experiments",
        "read_filter": "read_only", "read_depths": ["deep"], "coverage_filter": ["method"],
    })["papers"]
    # A skimmed abstract cannot qualify for an explicitly deep abstract request.
    assert not core.execute(service, "papers.search", {
        "retrieval_mode": "hybrid", "query_vector": [1, 0, 0], "embedding_model": MODEL,
        "read_filter": "read_only", "read_depths": ["deep"], "coverage_filter": ["abstract"],
    })["papers"]


def test_service_hybrid_chinese_text_and_model_argument_validation(service, papers):
    paper = imported(service, papers)
    core.record_reading(service, paper["paper_version_id"], "deep", ["method"])
    result = core.execute(service, "papers.search", {
        "retrieval_mode": "hybrid", "query": "章节阅读范围",
        "read_filter": "read_only", "coverage_filter": ["method"],
    })
    assert {p["paper_version_id"] for p in result["papers"]} == {paper["paper_version_id"]}
    for payload in [
        {"retrieval_mode": "hybrid", "query_vector": [1, 0, 0]},
        {"retrieval_mode": "substring", "query_vector": [1, 0, 0], "embedding_model": MODEL},
        {"retrieval_mode": "hybrid", "query_vector": [math.nan, 1], "embedding_model": MODEL},
    ]:
        with pytest.raises(DomainError) as caught:
            service.execute("papers.search", payload)
        assert caught.value.code == "invalid_request"
    put(service, paper["evidence_ids"][1], [1, 0, 0])
    with pytest.raises(DomainError) as caught:
        service.execute("papers.search", {
            "retrieval_mode": "hybrid", "query_vector": [1, 0], "embedding_model": MODEL,
        })
    assert caught.value.code == "embedding_dimension_mismatch"
