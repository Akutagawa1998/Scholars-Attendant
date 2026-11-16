"""Run the synthetic memory loop without accessing real accounts or documents.

Usage after installing the package: python evals/run_memory_demo.py
The default SQLite database is temporary and fixture-only. This does not claim
to verify PostgreSQL, pgvector, or live Claude Code/Codex message capture.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from research_assistant.service import ResearchService


def run(database_url: str) -> dict:
    fixture = json.loads((Path(__file__).parent / "fixtures/memory_core.json").read_text())
    assert fixture["fixture_only"] is True
    paper = fixture["papers"][0]
    service = ResearchService(database_url, owner_id="fixture-demo", scope_id="synthetic")
    service.initialize()

    def call(operation, payload=None, *, trusted_capture=False):
        receipt = service.execute(operation, payload or {}, trusted_capture=trusted_capture)
        assert receipt["status"] in {"ok", "success", "completed", "accepted"}, receipt
        return receipt["data"]

    imported = call("papers.import", {
        "identifier": paper["version_identity"], "title": paper["title"],
        "version": paper["version"], "source_url": "https://example.invalid/fixture-paper-a-v1",
        "content": "\n\n".join(s["text"] for s in paper["sections"]),
        "passages": [dict(s, locator={"kind": "markdown", "block_id": paper["version_identity"] + ":" + s["section"]})
                     for s in paper["sections"]],
        "idempotency_key": "demo-import",
    })
    assert call("papers.search", {"read_filter": "read_only"})["papers"] == []
    version_id = imported["paper_version_id"]

    def record(depth, sections, *, unread=(), correction=None):
        text = f"SYNTHETIC USER INPUT: {depth} reading of {', '.join(sections)}."
        captured = call("capture.message", {
            "message_id": str(uuid4()), "session_id": "demo-direct-input", "text": text,
            "claims": [{"subject_id": version_id, "predicate": "reading", "quote": text,
                        "value": {"depth": depth, "coverage": {"whole_paper": False,
                                 "sections": sections, "unread_sections": list(unread)}},
                        "valid_from": "2026-10-01T00:00:00Z"}],
            "idempotency_key": str(uuid4()),
        }, trusted_capture=True)
        payload = {"trusted_message_id": captured["trusted_message_id"], "idempotency_key": str(uuid4())}
        if correction:
            payload.update(memory_id=correction["memory_id"], expected_revision=correction["revision"])
        return call("memory.record_user_statement", payload)

    record("skim", ["abstract"])
    method = record("deep", ["method"], unread=["experiments"])
    restarted = ResearchService(database_url, owner_id="fixture-demo", scope_id="synthetic")
    restarted.initialize()
    recovered = restarted.execute("papers.search", {"read_filter": "read_only"})["data"]["papers"]
    assert {p["paper_version_id"] for p in recovered} == {version_id}
    corrected = record("skim", ["method"], unread=["experiments"], correction=method)
    assert call("papers.search", {"read_filter": "read_only", "read_depths": ["deep"]})["papers"] == []
    evidence = call("papers.get_evidence", {
        "paper_version_id": version_id, "read_filter": "read_only", "read_depths": ["skim"],
        "coverage_filter": ["method"],
    })
    assert paper["sections"][1]["text"] in json.dumps(evidence, ensure_ascii=False)
    assert paper["sections"][2]["text"] not in json.dumps(evidence, ensure_ascii=False)
    exported = call("export")
    assert version_id in json.dumps(exported)
    forgotten = call("memory.forget", {
        "target_ids": [imported["source_snapshot_id"]], "scope": "memory_and_source_snapshots",
        "idempotency_key": "demo-forget",
    })
    for _ in range(10):
        if service.run_one_job()["status"] == "idle":
            break
    assert call("papers.search")["papers"] == []
    assert paper["sections"][1]["text"] not in json.dumps(call("export"), ensure_ascii=False)
    return {
        "fixture_only": True,
        "checks": ["import_does_not_mean_read", "section_reading", "service_recreation",
                   "shared_database", "explicit_correction", "read_scope_evidence", "export",
                   "forget", "pending_jobs_cannot_restore"],
        "paper_version_id": version_id,
        "corrected_revision": corrected["revision"],
        "forget_receipt": forgotten,
        "limitations": ["SQLite fixture run", "No real account or live-client integration",
                        "Evidence location checked; semantic entailment not evaluated"],
    }


if __name__ == "__main__":
    with TemporaryDirectory(prefix="scholars-memory-demo-") as directory:
        result = run("sqlite:///" + str(Path(directory) / "synthetic.sqlite3"))
    print(json.dumps(result, ensure_ascii=False, indent=2))
