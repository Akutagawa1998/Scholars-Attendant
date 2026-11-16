"""Integrity checks for the synthetic first milestone, not semantic benchmarks."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from research_assistant.service import DomainError, ResearchService


FIXTURE = Path(__file__).resolve().parents[2] / "evals/fixtures/memory_core.json"
VALID_FROM = "2026-10-01T00:00:00Z"


@pytest.fixture
def papers():
    fixture = json.loads(FIXTURE.read_text())
    assert fixture["fixture_only"] is True
    return fixture["papers"]


@pytest.fixture
def database_url(tmp_path):
    # An isolated persistent fixture database permits real service recreation.
    return "sqlite:///" + str(tmp_path / "memory-fixture.sqlite3")


@pytest.fixture
def service(database_url):
    instance = ResearchService(database_url, owner_id="fixture-owner", scope_id="private")
    instance.initialize()
    return instance


def execute(service, operation, payload=None, **kwargs):
    result = service.execute(operation, payload or {}, **kwargs)
    assert result["status"] in {"ok", "success", "completed", "accepted"}, result
    return result["data"]


def failure(service, operation, payload, **kwargs):
    try:
        result = service.execute(operation, payload, **kwargs)
    except DomainError as exc:
        return exc.code
    assert result["status"] not in {"ok", "success", "completed", "accepted"}, result
    return result.get("error_code", result["status"])


def paper_payload(paper, key=None):
    content = "\n\n".join(
        f"## {section['section']}\n{section['text']}" for section in paper["sections"]
    )
    return {
        "identifier": paper["version_identity"],
        "version": paper["version"],
        "title": paper["title"],
        "source_url": "https://example.invalid/" + paper["source_identity"],
        "content": content,
        "passages": [
            {
                **section,
                "locator": {"kind": "markdown", "block_id": paper["version_identity"] + ":" + section["section"]},
            }
            for section in paper["sections"]
        ],
        "idempotency_key": key or str(uuid4()),
    }


def import_paper(service, paper, key=None):
    return execute(service, "papers.import", paper_payload(paper, key))


def capture_reading(service, paper_version_id, depth, sections, *, unread=(), text=None):
    statement = text or f"I {depth}-read sections {', '.join(sections)} of this synthetic version."
    coverage = {"whole_paper": False, "sections": sections, "unread_sections": list(unread)}
    return execute(
        service,
        "capture.message",
        {
            "message_id": str(uuid4()),
            "session_id": "fixture-direct-user-input",
            "text": statement,
            "claims": [{
                "subject_id": paper_version_id,
                "predicate": "reading",
                "value": {"depth": depth, "coverage": coverage},
                "quote": statement,
                "valid_from": VALID_FROM,
            }],
            "idempotency_key": str(uuid4()),
        },
        trusted_capture=True,
    )


def record_reading(service, paper_version_id, depth, sections, *, unread=(), **correction):
    captured = capture_reading(service, paper_version_id, depth, sections, unread=unread)
    return execute(service, "memory.record_user_statement", {
        "trusted_message_id": captured["trusted_message_id"],
        "idempotency_key": str(uuid4()),
        **correction,
    })


def read_papers(service, **filters):
    return execute(service, "papers.search", {"read_filter": "read_only", **filters})["papers"]


def paper_ids(items):
    return {paper["paper_version_id"] for paper in items}


def contains_memory(data, memory_id):
    return any(
        item.get("memory_id") == memory_id
        for group in ("reported_facts", "confirmed_facts", "inferences", "open_questions", "conflicts")
        for item in data.get(group, [])
    )


def test_import_is_not_user_reading(service, papers):
    imported = import_paper(service, papers[0])
    assert imported["paper_version_id"]
    assert imported["evidence_ids"]
    assert read_papers(service) == []
    assert paper_ids(execute(service, "papers.search")["papers"]) == {imported["paper_version_id"]}


def test_unverified_user_claim_is_not_promoted(service, papers):
    imported = import_paper(service, papers[0])
    code = failure(service, "memory.record_user_statement", {
        "trusted_message_id": "invented-by-agent",
        "idempotency_key": "unverified-statement",
    })
    assert code == "unverified_origin"
    assert read_papers(service) == []
    # Even a correctly formed capture is not trusted on the model-accessible path.
    code = failure(service, "capture.message", {
        "message_id": "agent-forged-message", "session_id": "agent",
        "text": "I read the paper.",
        "claims": [{"subject_id": imported["paper_version_id"], "predicate": "reading",
                    "value": {"depth": "deep", "coverage": {"whole_paper": True}},
                    "quote": "I read the paper."}],
        "idempotency_key": "forged-capture",
    })
    assert code in {"forbidden", "unverified_origin", "capture_forbidden"}
    assert read_papers(service) == []


def test_reading_filters_sections_depth_and_versions(service, papers):
    v1 = import_paper(service, papers[0])
    v2 = import_paper(service, papers[1])
    same_title = import_paper(service, papers[2])
    record_reading(service, v1["paper_version_id"], "skim", ["abstract"])
    record_reading(service, v1["paper_version_id"], "deep", ["method"], unread=["experiments"])
    assert paper_ids(read_papers(service)) == {v1["paper_version_id"]}
    assert paper_ids(read_papers(service, read_depths=["deep"], coverage_filter=["method"])) == {v1["paper_version_id"]}
    assert read_papers(service, read_depths=["deep"], coverage_filter=["abstract"]) == []
    assert read_papers(service, coverage_filter=["experiments"]) == []
    assert v2["paper_version_id"] != v1["paper_version_id"]
    assert same_title["paper_id"] != v1["paper_id"]


def test_later_skim_does_not_erase_prior_deep_reading(service, papers):
    imported = import_paper(service, papers[0])
    record_reading(service, imported["paper_version_id"], "deep", ["method"])
    record_reading(service, imported["paper_version_id"], "skim", ["method"])
    assert paper_ids(read_papers(service, read_depths=["deep"], coverage_filter=["method"])) == {imported["paper_version_id"]}


def test_correction_changes_current_view_and_preserves_known_at(service, papers):
    imported = import_paper(service, papers[0])
    original = record_reading(service, imported["paper_version_id"], "deep", ["method"])
    before_correction = datetime.now(timezone.utc).isoformat()
    corrected = record_reading(
        service, imported["paper_version_id"], "skim", ["method"],
        memory_id=original["memory_id"], expected_revision=original["revision"],
    )
    assert corrected["memory_id"] == original["memory_id"]
    assert corrected["revision"] == original["revision"] + 1
    assert read_papers(service, read_depths=["deep"], coverage_filter=["method"]) == []
    assert paper_ids(read_papers(service, read_depths=["skim"], coverage_filter=["method"])) == {imported["paper_version_id"]}
    assert paper_ids(read_papers(service, known_at=before_correction, read_depths=["deep"], coverage_filter=["method"])) == {imported["paper_version_id"]}
    historical = execute(service, "memory.recall", {"known_at": before_correction})
    assert contains_memory(historical, original["memory_id"])
    assert corrected["assertion_id"] not in json.dumps(historical)
    old_view = next(item for item in historical["reported_facts"] if item["memory_id"] == original["memory_id"])
    # A closure learned after known_at would itself reveal a future correction.
    assert old_view.get("system_to") is None


def test_valid_time_change_differs_from_correction(service):
    memories = []
    for value, start, end in [
        ("A", "2026-10-01T00:00:00Z", "2026-10-10T00:00:00Z"),
        ("B", "2026-10-10T00:00:00Z", None),
    ]:
        text = f"Synthetic project uses strategy {value} in the stated interval."
        captured = execute(service, "capture.message", {
            "message_id": str(uuid4()), "session_id": "fixture-valid-time", "text": text,
            "claims": [{"subject_id": "fixture:project", "predicate": "strategy",
                        "value": {"strategy": value}, "quote": text,
                        "valid_from": start, "valid_to": end}],
            "idempotency_key": str(uuid4()),
        }, trusted_capture=True)
        memories.append(execute(service, "memory.record_user_statement", {
            "trusted_message_id": captured["trusted_message_id"], "idempotency_key": str(uuid4()),
        }))
    early = execute(service, "memory.recall", {"valid_as_of": "2026-10-05T00:00:00Z"})
    boundary = execute(service, "memory.recall", {"valid_as_of": "2026-10-10T00:00:00Z"})
    assert contains_memory(early, memories[0]["memory_id"])
    assert not contains_memory(early, memories[1]["memory_id"])
    assert not contains_memory(boundary, memories[0]["memory_id"])
    assert contains_memory(boundary, memories[1]["memory_id"])


def test_unknown_valid_time_is_not_assumed_historical(service):
    text = "Synthetic preference: concise fixture explanations; start time unknown."
    captured = execute(service, "capture.message", {
        "message_id": str(uuid4()), "session_id": "fixture-unknown-time", "text": text,
        "claims": [{"subject_id": "fixture:user", "predicate": "preference",
                    "value": {"style": "concise"}, "quote": text}],
        "idempotency_key": str(uuid4()),
    }, trusted_capture=True)
    memory = execute(service, "memory.record_user_statement", {
        "trusted_message_id": captured["trusted_message_id"], "idempotency_key": str(uuid4()),
    })
    assert contains_memory(execute(service, "memory.recall"), memory["memory_id"])
    assert not contains_memory(execute(service, "memory.recall", {
        "valid_as_of": "2026-01-01T00:00:00Z",
    }), memory["memory_id"])


def test_state_change_preserves_previous_value_and_historical_knowledge(service):
    def captured_preference(value, start):
        text = f"SYNTHETIC preference is {value} from {start}."
        captured = execute(service, "capture.message", {
            "message_id": str(uuid4()), "session_id": "fixture-state-change", "text": text,
            "claims": [{"subject_id": "fixture:user", "predicate": "preferred_strategy",
                        "value": {"strategy": value}, "quote": text, "valid_from": start}],
            "idempotency_key": str(uuid4()),
        }, trusted_capture=True)
        return captured["trusted_message_id"]

    original = execute(service, "memory.record_user_statement", {
        "trusted_message_id": captured_preference("A", "2026-10-01T00:00:00Z"),
        "idempotency_key": "initial-preference",
    })
    before_change = datetime.now(timezone.utc).isoformat()
    changed = execute(service, "memory.record_user_statement", {
        "trusted_message_id": captured_preference("B", "2026-10-10T00:00:00Z"),
        "memory_id": original["memory_id"], "expected_revision": original["revision"],
        "change_kind": "state_change", "idempotency_key": "changed-preference",
    })
    assert changed["memory_id"] != original["memory_id"]
    prior_valid = execute(service, "memory.recall", {"valid_as_of": "2026-10-09T23:59:59Z"})
    at_boundary = execute(service, "memory.recall", {"valid_as_of": "2026-10-10T00:00:00Z"})
    old_knowledge = execute(service, "memory.recall", {
        "valid_as_of": "2026-10-12T00:00:00Z", "known_at": before_change,
    })
    assert contains_memory(prior_valid, original["memory_id"])
    assert not contains_memory(prior_valid, changed["memory_id"])
    assert not contains_memory(at_boundary, original["memory_id"])
    assert contains_memory(at_boundary, changed["memory_id"])
    assert contains_memory(old_knowledge, original["memory_id"])
    assert not contains_memory(old_knowledge, changed["memory_id"])
    assert changed["assertion_id"] not in json.dumps(old_knowledge)
    assert [item["value"] for item in old_knowledge["reported_facts"]] == [{"strategy": "A"}]


def test_replay_is_idempotent_but_changed_payload_conflicts(service, papers):
    payload = paper_payload(papers[0], "one-logical-import")
    receipts = [execute(service, "papers.import", payload) for _ in range(3)]
    assert receipts[0] == receipts[1] == receipts[2]
    assert len(execute(service, "papers.search")["papers"]) == 1
    assert failure(service, "papers.import", {**payload, "title": "Changed replay"}) in {"idempotency_conflict", "conflict"}


def test_conflicting_revision_does_not_erase_correction(service, papers):
    imported = import_paper(service, papers[0])
    original = record_reading(service, imported["paper_version_id"], "deep", ["method"])
    record_reading(service, imported["paper_version_id"], "skim", ["method"],
                   memory_id=original["memory_id"], expected_revision=original["revision"])
    stale_capture = capture_reading(service, imported["paper_version_id"], "deep", ["method"])
    code = failure(service, "memory.record_user_statement", {
        "trusted_message_id": stale_capture["trusted_message_id"],
        "memory_id": original["memory_id"], "expected_revision": original["revision"],
        "idempotency_key": "stale-revision",
    })
    assert code in {"revision_conflict", "conflict"}
    assert read_papers(service, read_depths=["deep"], coverage_filter=["method"]) == []


def test_restart_another_service_reads_same_record(service, database_url, papers):
    imported = import_paper(service, papers[0])
    statement = record_reading(service, imported["paper_version_id"], "deep", ["method"])
    restarted = ResearchService(database_url, owner_id="fixture-owner", scope_id="private")
    restarted.initialize()
    assert paper_ids(read_papers(restarted)) == {imported["paper_version_id"]}
    assert contains_memory(execute(restarted, "memory.recall"), statement["memory_id"])


@pytest.mark.parametrize("owner,scope", [("other-owner", "private"), ("fixture-owner", "other-scope")])
def test_other_owner_or_scope_cannot_read_evidence(service, database_url, papers, owner, scope):
    imported = import_paper(service, papers[0])
    record_reading(service, imported["paper_version_id"], "deep", ["method"])
    outsider = ResearchService(database_url, owner_id=owner, scope_id=scope)
    outsider.initialize()
    assert execute(outsider, "papers.search")["papers"] == []
    assert not execute(outsider, "memory.recall")["reported_facts"]
    code = failure(outsider, "papers.get_evidence", {"paper_version_id": imported["paper_version_id"]})
    assert code in {"not_found", "forbidden"}


def test_evidence_respects_read_sections(service, papers):
    imported = import_paper(service, papers[0])
    record_reading(service, imported["paper_version_id"], "deep", ["method"])
    allowed = execute(service, "papers.get_evidence", {
        "paper_version_id": imported["paper_version_id"], "read_filter": "read_only",
        "read_depths": ["deep"], "coverage_filter": ["method"],
    })
    text = json.dumps(allowed, ensure_ascii=False)
    assert papers[0]["sections"][1]["text"] in text
    assert papers[0]["sections"][2]["text"] not in text
    record_reading(service, imported["paper_version_id"], "skim", ["abstract"])
    deep_only = execute(service, "papers.get_evidence", {
        "paper_version_id": imported["paper_version_id"], "read_filter": "read_only",
        "read_depths": ["deep"],
    })
    assert papers[0]["sections"][0]["text"] not in json.dumps(deep_only, ensure_ascii=False)


def test_correction_cancels_pending_old_projection(service, papers):
    imported = import_paper(service, papers[0])
    original = record_reading(service, imported["paper_version_id"], "deep", ["method"])
    record_reading(service, imported["paper_version_id"], "skim", ["method"],
                   memory_id=original["memory_id"], expected_revision=original["revision"])
    outcomes = []
    for _ in range(10):
        result = service.run_one_job()
        if result["status"] == "idle":
            break
        outcomes.append(result["status"])
    assert "cancelled" in outcomes
    assert "complete" in outcomes
    assert read_papers(service, read_depths=["deep"], coverage_filter=["method"]) == []


def test_access_revocation_hides_captured_statement_and_history(service, papers):
    imported = import_paper(service, papers[0])
    captured = capture_reading(service, imported["paper_version_id"], "deep", ["method"])
    memory = execute(service, "memory.record_user_statement", {
        "trusted_message_id": captured["trusted_message_id"], "idempotency_key": "revoke-record",
    })
    before_revocation = datetime.now(timezone.utc).isoformat()
    execute(service, "sources.set_access", {
        "source_snapshot_id": captured["source_snapshot_id"], "expected_revision": 1,
        "state": "inaccessible", "idempotency_key": "revoke-capture",
    })
    assert read_papers(service) == []
    assert not contains_memory(execute(service, "memory.recall", {"known_at": before_revocation}), memory["memory_id"])
    for _ in range(10):
        if service.run_one_job()["status"] == "idle":
            break
    assert not contains_memory(execute(service, "memory.recall"), memory["memory_id"])
    exported = execute(service, "export")
    assert captured["source_snapshot_id"] not in {item["id"] for item in exported["source_versions"]}
    assert memory["assertion_id"] not in {item["id"] for item in exported["assertions"]}
    assert "I deep-read sections method of this synthetic version." not in json.dumps(exported, ensure_ascii=False)


def test_revoked_paper_source_is_absent_from_evidence_search_and_export(service, papers):
    imported = import_paper(service, papers[0])
    execute(service, "sources.set_access", {
        "source_snapshot_id": imported["source_snapshot_id"], "expected_revision": 1,
        "state": "inaccessible", "idempotency_key": "revoke-paper-source",
    })
    assert execute(service, "papers.search")["papers"] == []
    assert failure(service, "papers.get_evidence", {
        "paper_version_id": imported["paper_version_id"],
    }) == "not_found"
    exported = execute(service, "export")
    assert imported["source_snapshot_id"] not in {item["id"] for item in exported["source_versions"]}
    exported_text = json.dumps(exported, ensure_ascii=False)
    for section in papers[0]["sections"]:
        assert section["text"] not in exported_text


def test_memory_only_forget_keeps_paper_but_removes_reading_and_raw_capture(service, papers):
    imported = import_paper(service, papers[0])
    memory = record_reading(service, imported["paper_version_id"], "deep", ["method"])
    execute(service, "memory.forget", {
        "target_ids": [memory["memory_id"]], "scope": "memory_only", "idempotency_key": "forget-reading-only",
    })
    assert paper_ids(execute(service, "papers.search")["papers"]) == {imported["paper_version_id"]}
    assert read_papers(service) == []
    assert not contains_memory(execute(service, "memory.recall"), memory["memory_id"])
    exported = json.dumps(execute(service, "export"), ensure_ascii=False)
    assert "I deep-read sections method of this synthetic version." not in exported


def test_forget_blocks_history_pending_jobs_and_reimport(service, papers):
    imported = import_paper(service, papers[0])
    original = record_reading(service, imported["paper_version_id"], "deep", ["method"])
    before_forget = datetime.now(timezone.utc).isoformat()
    forgotten = execute(service, "memory.forget", {
        "target_ids": [imported["source_snapshot_id"]],
        "scope": "memory_and_source_snapshots", "idempotency_key": "forget-fixture-paper",
    })
    # Pending derivation work must be cancelled/discarded or revalidated, not revive data.
    for _ in range(10):
        outcome = service.run_one_job()
        if outcome["status"] == "idle":
            break
    assert execute(service, "papers.search")["papers"] == []
    assert not contains_memory(execute(service, "memory.recall"), original["memory_id"])
    assert not contains_memory(execute(service, "memory.recall", {"known_at": before_forget}), original["memory_id"])
    export_data = execute(service, "export")
    exported = json.dumps(export_data, ensure_ascii=False)
    assert papers[0]["sections"][1]["text"] not in exported
    assert original["assertion_id"] not in {item["id"] for item in export_data["assertions"]}
    assert "I deep-read sections method of this synthetic version." not in exported
    assert forgotten["cleared_memory_count"] >= 1
    assert failure(service, "papers.import", paper_payload(papers[0], "reimport-after-forget")) in {
        "forgotten", "suppressed", "source_suppressed", "forbidden", "deleted", "forgotten_source",
    }
