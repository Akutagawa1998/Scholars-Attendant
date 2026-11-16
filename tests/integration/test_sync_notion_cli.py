"""Notion collection bundles have durable coverage, never atomic checkpoints."""
from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from research_assistant import storage as db
from research_assistant.connectors.bundle import NotionBundleAdapter
from research_assistant.connectors.common import ConnectorError
from research_assistant.schemas import SourceSnapshot
from research_assistant.service import ResearchService
import research_assistant.sync as sync_module

PAGE = "11111111-2222-4333-8444-555555555555"
SECOND = "22222222-3333-4444-8555-666666666666"
COLLECTION = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OTHER = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
NAMESPACE = "collection://" + COLLECTION


def page(identity=PAGE, *, parent=COLLECTION, completeness="complete", text="SYNTHETIC page body"):
    source_text = ('<page><ancestor-path><parent-data-source url="{{collection://' + parent + '}}"/>'
                   '</ancestor-path><content>' + text + '</content></page>')
    return {"external_id": identity, "raw_payload": {"id": identity, "title": "SYNTHETIC page", "text": source_text},
            "markdown_path": ["text"], "external_version": "2026-10-20T12:00:00Z", "completeness": completeness}


def bundle(pages=None, *, complete=True):
    return {"source_instance_id": "notion-mcp-synthetic", "namespace": NAMESPACE,
            "inventory_complete": complete, "pages": [page()] if pages is None else pages}


def test_bundle_inventory_is_explicit_traversal_without_fabricated_atomic_version():
    raw = bundle([page(PAGE.replace("-", ""))])
    raw.update(version="fabricated-version", consistent=True)
    raw["namespace"] = "collection://" + COLLECTION.replace("-", "")
    adapter = NotionBundleAdapter(raw, require_complete=True)
    inventory = adapter.inventory()
    assert adapter.namespace == NAMESPACE
    assert inventory["object_versions"] == {PAGE: "2026-10-20T12:00:00Z"}
    assert inventory["complete"] is True and inventory["consistent"] is False
    assert inventory["version"] is None
    assert inventory["metadata"]["atomic_source_version_available"] is False
    snapshot = adapter.fetch_objects([PAGE], None)[0]
    assert snapshot["external_id"] == PAGE
    assert snapshot["passages"][0]["text"] == snapshot["raw_payload"]["text"]
    SourceSnapshot.model_validate({**snapshot, "idempotency_key": "synthetic-bundle"})
    assert adapter.deleted(0, None) == []


@pytest.mark.parametrize("complete", [False, None, "true", 1])
def test_full_bundle_requires_explicit_true_inventory_assertion(complete):
    raw = bundle(complete=complete)
    with pytest.raises(ConnectorError, match="incomplete_bundle_inventory"):
        NotionBundleAdapter(raw, require_complete=True)
    assert NotionBundleAdapter(raw).inventory()["complete"] is False


def test_missing_page_version_stays_unknown_instead_of_using_observation_time():
    raw = bundle()
    raw["pages"][0].pop("external_version")
    adapter = NotionBundleAdapter(raw)
    assert adapter.inventory()["object_versions"] == {PAGE: None}
    assert adapter.fetch_objects([PAGE], None)[0]["external_version"] is None


@pytest.mark.parametrize("mutation,code", [
    (lambda raw: raw["pages"][0].update(namespace="collection://" + OTHER), "outside_registered_source"),
    (lambda raw: raw["pages"][0].update(source_instance_id="another-connection"), "outside_registered_source"),
    (lambda raw: raw["pages"][0]["raw_payload"].update(id=SECOND), "source_identity_mismatch"),
    (lambda raw: raw["pages"][0]["raw_payload"].update(text="<content>No parent ancestry</content>"), "outside_registered_source"),
    (lambda raw: raw["pages"][0]["raw_payload"].update(text=page(parent=OTHER)["raw_payload"]["text"]), "outside_registered_source"),
    (lambda raw: raw["pages"][0]["raw_payload"].update(parent={"data_source_id": OTHER}), "outside_registered_source"),
])
def test_full_bundle_rejects_wrong_identity_or_unproven_collection_membership(mutation, code):
    raw = bundle()
    mutation(raw)
    with pytest.raises(ConnectorError, match=code):
        NotionBundleAdapter(raw, require_complete=True)


def test_api_shaped_parent_membership_and_single_quoted_native_ancestry():
    first, second = page(), page(SECOND)
    first["raw_payload"] = {"page": {"id": PAGE, "parent": {"data_source_id": COLLECTION}}, "text": "SYNTHETIC API page"}
    second["raw_payload"]["text"] = "<ancestor-path><parent-data-source url='collection://" + COLLECTION + "'/></ancestor-path>SYNTHETIC page"
    adapter = NotionBundleAdapter(bundle([first, second]))
    assert set(adapter.inventory()["object_versions"]) == {PAGE, SECOND}


def test_duplicate_uuid_forms_and_page_limit_fail_before_persistence():
    raw = bundle([page(), page(PAGE.replace("-", ""))])
    with pytest.raises(ConnectorError, match="duplicate_source_identity"):
        NotionBundleAdapter(raw)
    with pytest.raises(ConnectorError, match="invalid_bounded_bundle"):
        NotionBundleAdapter(bundle([page(), page(SECOND)]), max_pages=1)
    assert NotionBundleAdapter(bundle([]), require_complete=True).inventory()["object_versions"] == {}


def test_bundle_snapshots_are_immutable_and_only_requested_subset_is_returned():
    raw = bundle([page(), page(SECOND)])
    untouched = deepcopy(raw)
    adapter = NotionBundleAdapter(raw)
    snapshot = adapter.fetch_objects([SECOND], None)[0]
    snapshot["raw_payload"]["text"] = "changed outside adapter"
    assert adapter.fetch_objects([SECOND], None)[0]["raw_payload"]["text"] != "changed outside adapter"
    assert len(adapter.snapshots) == 2 and raw == untouched
    with pytest.raises(ConnectorError, match="objects_missing_from_response"):
        adapter.fetch_objects([COLLECTION], None)
    with pytest.raises(ConnectorError, match="invalid_configuration"):
        adapter.fetch_objects([PAGE], "fabricated-version")


def test_explicit_bundle_deletions_only_and_no_absence_inference():
    raw = bundle()
    raw["deleted"] = [{"external_id": SECOND.replace("-", ""), "change": "delete",
                       "raw_payload": {"id": SECOND, "archived": True}, "completeness": "complete"}]
    adapter = NotionBundleAdapter(raw)
    assert adapter.inventory()["object_versions"] == {PAGE: "2026-10-20T12:00:00Z"}
    deleted = adapter.deleted(0, None)
    assert [snapshot["external_id"] for snapshot in deleted] == [SECOND]
    assert deleted[0]["change"] == "delete" and deleted[0]["passages"] == []
    assert deleted[0]["external_version"] is None
    SourceSnapshot.model_validate({**deleted[0], "idempotency_key": "synthetic-delete"})
    assert NotionBundleAdapter(bundle([])).deleted(0, None) == []


def test_signed_notional_file_urls_are_redacted_before_source_evidence():
    raw = bundle([page(text='SYNTHETIC <image url="https://files.invalid/image?X-Amz-Signature=PRIVATE-SIGNATURE"/>')])
    raw["pages"][0]["raw_payload"]["metadata"] = {"api_key": "PRIVATE-KEY"}
    snapshot = NotionBundleAdapter(raw).fetch_objects([PAGE], None)[0]
    assert "PRIVATE" not in json.dumps(snapshot)
    assert snapshot["completeness"] == "partial"
    assert "temporary_urls_or_credentials_redacted" in snapshot["missing_parts"]
    assert snapshot["passages"][0]["text"] == snapshot["raw_payload"]["text"]


def test_real_coordinator_persists_bundle_without_atomic_checkpoint(tmp_path):
    service = ResearchService("sqlite:///" + str(tmp_path / "bundle.sqlite3"), owner_id="fixture-owner", scope_id="private")
    service.initialize()
    adapter = NotionBundleAdapter(bundle(), require_complete=True)
    settings = {"provider": "notion", "source_instance_id": adapter.source_instance_id, "namespace": adapter.namespace}
    service.execute("sources.register", {**settings, "idempotency_key": "synthetic-registration"}, trusted_capture=True)
    try:
        result = sync_module.SyncCoordinator(service).run(adapter, **settings)
        assert result["data"]["status"] == "best_effort_complete"
        assert result["data"]["checkpoint_advanced"] is False
        assert result["data"]["item_counts"] == {"persisted": 1}
        with service.storage.transaction() as connection:
            assert connection.execute(select(db.connector_cursors.c.cursor)).all() == []
        assert service.execute("sources.search", {})["data"]["total"] == 1
        assert service.execute("papers.search", {"read_filter": "read_only"})["data"]["papers"] == []
    finally:
        service.storage.engine.dispose()


@pytest.fixture
def runner(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts" / "sync-notion.py"
    spec = importlib.util.spec_from_file_location("synthetic_sync_notion_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    raw = bundle([page(text="PRIVATE-SOURCE-TEXT")])
    observed = {"registered": [], "ingested": [], "runs": []}
    class Service:
        def execute(self, operation, payload, **options):
            observed["registered" if operation == "sources.register" else "ingested"].append(payload)
            return {"status": "ok", "data": {"source_snapshot_id": "opaque-fixture-id", "revision": 1,
                                              "private": "PRIVATE-CONTENT"}}
    core = Service()
    result = {"status": "partial", "data": {"sync_run_id": "11111111-2222-4333-8444-555555555555",
        "status": "best_effort_complete", "checkpoint_advanced": False, "expected_count": 1,
        "item_counts": {"persisted": 1}, "error_code": None, "namespace": NAMESPACE,
        "raw_payload": "PRIVATE-SOURCE-TEXT", "private": "PRIVATE-CREDENTIAL"}}
    class Coordinator:
        def __init__(self, service): assert service is core
        def run(self, adapter, **settings):
            observed["runs"].append(settings)
            observed["inventory"] = adapter.inventory()
            observed["snapshots"] = adapter.fetch_objects(list(adapter.inventory()["object_versions"]), None)
            return result
    def configure(path):
        observed["configured"] = True
        return core
    def no_network(*args, **kwargs): pytest.fail("bundle CLI attempted remote request")
    monkeypatch.setattr(module, "object_file", lambda path: raw)
    monkeypatch.setattr(module, "configured_service", configure)
    monkeypatch.setattr(sync_module, "SyncCoordinator", Coordinator)
    monkeypatch.setattr("httpx.Client.request", no_network)
    return module, raw, observed, result


def test_full_cli_registers_exact_scope_and_prints_only_sanitized_best_effort_summary(runner, capsys):
    module, _, observed, _ = runner
    assert module.main(["--bundle", "synthetic.json", "--full-bundle"]) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and "PRIVATE" not in captured.out and COLLECTION not in captured.out
    result = json.loads(captured.out)
    assert observed["runs"] == [{"provider": "notion", "source_instance_id": "notion-mcp-synthetic", "namespace": NAMESPACE}]
    assert observed["inventory"]["version"] is None and observed["inventory"]["consistent"] is False
    assert observed["registered"][0]["namespace"] == NAMESPACE
    assert observed["ingested"] == []
    assert result["mode"] == "full_bundle" and result["atomic_source_version_available"] is False
    assert result["complete_source_sync"] is False and result["checkpoint_committed"] is False
    assert result["data"]["status"] == "best_effort_complete"
    assert set(result["data"]) == {"sync_run_id", "status", "checkpoint_advanced", "expected_count", "item_counts", "error_code"}


@pytest.mark.parametrize("mutation,flags,code", [
    (lambda raw: raw.update(inventory_complete=False), [], "incomplete_bundle_inventory"),
    (lambda raw: raw["pages"].append(page(SECOND)), ["--max-pages", "1"], "invalid_bounded_bundle"),
    (lambda raw: raw["pages"][0].update(namespace="collection://" + OTHER), [], "outside_registered_source"),
])
def test_full_cli_checks_complete_inventory_bounds_and_identity_before_database(runner, mutation, flags, code, capsys):
    module, raw, observed, _ = runner
    mutation(raw)
    assert module.main(["--bundle", "synthetic.json", "--full-bundle", *flags]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"status": "failed", "error_code": code}
    assert "configured" not in observed and observed["runs"] == []


def test_default_cli_retains_bounded_import_and_safe_receipts(runner, capsys):
    module, _, observed, _ = runner
    assert module.main(["--bundle", "synthetic.json"]) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and "PRIVATE" not in captured.out
    result = json.loads(captured.out)
    assert observed["runs"] == [] and len(observed["ingested"]) == 1
    assert result["objects_persisted"] == 1
    assert result["receipts"] == [{"source_snapshot_id": "opaque-fixture-id", "revision": 1}]
    assert not result["complete_source_sync"] and not result["checkpoint_committed"]


def test_partial_cli_does_not_claim_successful_complete_bundle(runner, capsys):
    module, _, _, result = runner
    result["data"].update(status="partial", item_counts={"partial": 1}, error_code="incomplete_response")
    assert module.main(["--bundle", "synthetic.json", "--full-bundle"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["data"]["status"] == "partial"
    assert not result["checkpoint_committed"]


def test_cli_rejects_accidental_atomic_checkpoint_claim(runner, capsys):
    module, _, _, result = runner
    result["data"]["checkpoint_advanced"] = True
    assert module.main(["--bundle", "synthetic.json", "--full-bundle"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error_code"] == "unproven_source_checkpoint"


def test_cli_help_explains_full_bundle_without_loading_private_files(runner, capsys):
    module, _, observed, _ = runner
    with pytest.raises(SystemExit) as caught:
        module.main(["--help"])
    assert caught.value.code == 0
    assert "--full-bundle" in capsys.readouterr().out
    assert observed["runs"] == [] and "configured" not in observed
