"""Regression tests for privacy and identity defects found during core review."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from research_assistant import storage as db
from research_assistant.service import DomainError, ResearchService


@pytest.fixture
def service(tmp_path):
    instance = ResearchService("sqlite:///" + str(tmp_path / "regressions.sqlite3"))
    instance.initialize()
    return instance


def call(service, operation, **payload):
    result = service.execute(operation, payload)
    assert result["status"] == "ok", result
    return result["data"]


def paper(service, *, marker="regression-paper"):
    content = "## method\nSynthetic method evidence."
    return call(service, "papers.import", identifier=f"fixture:{marker}:v1", version="v1",
                title=f"Synthetic {marker}", source_url=f"https://example.invalid/{marker}",
                content=content, passages=[dict(text="Synthetic method evidence.", section="method",
                locator=dict(kind="markdown", block_id="method"))], idempotency_key=str(uuid4()))


def capture(service, subject_id, text, *, minimal_coverage=False):
    coverage = dict(sections=["method"])
    if not minimal_coverage:
        coverage.update(whole_paper=False, unread_sections=[])
    result = service.execute("capture.message", dict(message_id=str(uuid4()), session_id="regression",
        text=text, claims=[dict(subject_id=subject_id, predicate="reading", value=dict(depth="deep",
        coverage=coverage), quote=text)], idempotency_key=str(uuid4())), trusted_capture=True)
    assert result["status"] == "ok", result
    return result["data"]


def canonical_rows(service):
    # Logical SQL rows verify canonical cleanup, not removal of filesystem pages
    # or backups, which require the deployment retention policy.
    with service.storage.transaction() as connection:
        rows = {table.name: [dict(row) for row in connection.execute(select(table)).mappings()]
                for table in db.metadata.sorted_tables}
    return json.dumps(rows, sort_keys=True)


@pytest.mark.parametrize("minimal_coverage", [False, True])
def test_capture_replay_is_blocked_before_raw_text_is_persisted(service, minimal_coverage):
    imported = paper(service)
    captured = capture(service, imported["paper_version_id"], "Original private reading statement.",
                       minimal_coverage=minimal_coverage)
    recorded = call(service, "memory.record_user_statement", trusted_message_id=captured["trusted_message_id"],
                    idempotency_key=str(uuid4()))
    call(service, "memory.forget", target_ids=[recorded["memory_id"]], idempotency_key=str(uuid4()))
    replay_text = "Replayed private reading statement with the same confirmed reading claim."
    with pytest.raises(DomainError) as error:
        capture(service, imported["paper_version_id"], replay_text, minimal_coverage=minimal_coverage)
    assert error.value.code == "forgotten"
    assert replay_text not in canonical_rows(service)
    assert replay_text not in json.dumps(service.execute("export", {}))


def test_exact_original_capture_text_is_suppressed_after_forget(service):
    imported = paper(service)
    text = "private-original-marker, I deep-read method."
    captured = capture(service, imported["paper_version_id"], text)
    recorded = call(service, "memory.record_user_statement", trusted_message_id=captured["trusted_message_id"],
                    idempotency_key=str(uuid4()))
    call(service, "memory.forget", target_ids=[recorded["memory_id"]], idempotency_key=str(uuid4()))
    with pytest.raises(DomainError) as error:
        capture(service, imported["paper_version_id"], text)
    assert error.value.code == "forgotten"
    assert text not in canonical_rows(service)


def test_forget_scrubs_free_text_assertion_identity_fields(service):
    marker = "synthetic-private-assertion-marker-824931"
    proposed = call(service, "memory.propose", subject_id=marker + "-subject", predicate=marker + "-predicate",
                    value=dict(detail=marker + "-value"), idempotency_key=str(uuid4()))
    deleted = call(service, "memory.forget", target_ids=[proposed["proposal_id"]], idempotency_key=str(uuid4()))
    assert deleted["canonical_cleanup_complete"] is True
    assert marker not in canonical_rows(service)


def test_forget_source_scrubs_private_url_external_identity_and_title(service):
    marker = "synthetic-private-source-marker-418206"
    imported = paper(service, marker=marker)
    deleted = call(service, "memory.forget", target_ids=[imported["source_snapshot_id"]],
                   scope="memory_and_source_snapshots", idempotency_key=str(uuid4()))
    assert deleted["canonical_cleanup_complete"] is True
    assert marker not in canonical_rows(service)
    assert marker not in json.dumps(service.execute("export", {}))


def test_project_external_key_resolves_to_one_revision_chain(service):
    created = call(service, "projects.upsert", project_id="fixture-external-project", title="Initial title",
                   idempotency_key="project-create")
    updated = call(service, "projects.upsert", project_id="fixture-external-project", title="Updated title",
                   expected_revision=1, idempotency_key="project-update")
    assert updated["project_id"] == created["project_id"]
    assert updated["revision"] == 2
    snapshot = call(service, "projects.snapshot", project_id=created["project_id"])
    assert snapshot["title"] == "Updated title"
    assert snapshot["revision"] == 2
    with pytest.raises(DomainError) as error:
        call(service, "projects.upsert", project_id="fixture-external-project", title="Stale title",
             expected_revision=1, idempotency_key="project-stale")
    assert error.value.code == "revision_conflict"
    with service.storage.transaction() as connection:
        assert len(list(connection.execute(select(db.projects)).mappings())) == 1
