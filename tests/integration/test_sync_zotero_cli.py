"""The Zotero CLI uses synthetic adapters/coordinators and never real accounts."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from research_assistant.connectors.common import ConnectorError
import research_assistant.sync as sync_module


@pytest.fixture
def runner(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts" / "sync-zotero.py"
    spec = importlib.util.spec_from_file_location("synthetic_sync_zotero_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    observed = {"config_reads": [], "registrations": [], "fetch": [], "inventory": 0, "run": []}

    class Service:
        def execute(self, operation, payload, **options):
            observed["registrations"].append((operation, payload, options))
            return {"status": "ok", "data": {}}

    class Adapter:
        source_instance_id = "api.zotero.org:v3"
        namespace = "users/PRIVATE-ACCOUNT-ID"
        def __init__(self, key, library_id, **options):
            observed["credentials"] = (key, library_id, options)
        def __enter__(self):
            observed["open"] = True
            return self
        def __exit__(self, *args):
            observed["open"] = False
        def fetch(self, **options):
            observed["fetch"].append(options)
            return SimpleNamespace(snapshots=[], provider="zotero", source_instance_id=self.source_instance_id,
                namespace=self.namespace, pages_fetched=1, library_version="22", missing_parts=[])
        def inventory(self):
            observed["inventory"] += 1
            return {"version": "22", "object_versions": {"PRIVATE-ITEM-ID": "20"},
                    "complete": True, "consistent": True, "metadata": {"namespace": self.namespace}}

    result = {"status": "ok", "data": {
        "sync_run_id": "11111111-2222-4333-8444-555555555555", "status": "complete",
        "checkpoint_advanced": True, "expected_count": 1, "item_counts": {"persisted": 1}, "error_code": None,
        "namespace": "PRIVATE-ACCOUNT-ID", "external_ids": ["PRIVATE-ITEM-ID"],
        "raw_payload": {"title": "PRIVATE-TITLE"}, "api_key": "PRIVATE-KEY",
    }}

    class Coordinator:
        def __init__(self, service):
            assert service is core
        def run(self, adapter, **settings):
            assert observed["open"] is True
            observed["run"].append(settings)
            return result

    def object_file(path):
        observed["config_reads"].append(path)
        return {"api_key": "PRIVATE-KEY", "user_id": "PRIVATE-ACCOUNT-ID"}
    def configured_service(path):
        observed["runtime_config"] = path
        return core
    def network_forbidden(*args, **kwargs):
        pytest.fail("CLI test attempted real network I/O")
    core = Service()
    monkeypatch.setattr(module, "object_file", object_file)
    monkeypatch.setattr(module, "configured_service", configured_service)
    monkeypatch.setattr(module, "ZoteroReadOnlyClient", Adapter)
    monkeypatch.setattr(sync_module, "SyncCoordinator", Coordinator)
    monkeypatch.setattr("httpx.Client.request", network_forbidden)
    return module, observed, result


def test_full_forwards_registered_identity_and_only_prints_safe_envelope(runner, capsys):
    module, observed, _ = runner
    assert module.main(["--full"]) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and "PRIVATE" not in captured.out
    summary = json.loads(captured.out)
    assert observed["fetch"] == []
    assert observed["run"] == [{"provider": "zotero", "source_instance_id": "api.zotero.org:v3",
                                  "namespace": "users/PRIVATE-ACCOUNT-ID"}]
    operation, registration, options = observed["registrations"][0]
    assert operation == "sources.register"
    assert options == {"trusted_capture": True}
    assert registration["source_instance_id"] == "api.zotero.org:v3"
    assert registration["namespace"] == "users/PRIVATE-ACCOUNT-ID"
    assert registration["idempotency_key"].startswith("zotero-register:")
    assert summary["mode"] == "full_metadata" and summary["read_only"] is True
    assert summary["checkpoint_committed"] is True and summary["complete_library_sync"] is True
    assert summary["original_files_acquired"] is False
    assert summary["data"] == {
        "sync_run_id": "11111111-2222-4333-8444-555555555555", "status": "complete",
        "checkpoint_advanced": True, "expected_count": 1, "item_counts": {"persisted": 1}, "error_code": None,
    }


def test_full_probe_only_reads_inventory_without_registering_or_opening_database(runner, capsys):
    module, observed, _ = runner
    assert module.main(["--full", "--probe"]) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and "PRIVATE" not in captured.out
    summary = json.loads(captured.out)
    assert observed["inventory"] == 1
    assert observed["fetch"] == [] and observed["run"] == [] and observed["registrations"] == []
    assert "runtime_config" not in observed
    assert summary["objects_listed"] == 1 and summary["probe_only"] is True
    assert summary["inventory_complete"] is True and summary["inventory_consistent"] is True
    assert summary["checkpoint_committed"] is False


def test_bounded_probe_remains_default_with_no_full_cursor_claim(runner, capsys):
    module, observed, _ = runner
    assert module.main(["--probe"]) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and "PRIVATE" not in captured.out
    summary = json.loads(captured.out)
    assert observed["fetch"] == [{"max_items": 5, "since": None, "include_deleted": False}]
    assert observed["run"] == [] and observed["inventory"] == 0
    assert observed["registrations"] == []
    assert not summary["checkpoint_committed"] and not summary["complete_library_sync"]


@pytest.mark.parametrize("flags", [["--since", "0"], ["--include-deleted"], ["--max-items", "3"]])
def test_full_rejects_bounded_or_manual_cursor_controls_before_credentials(runner, flags, capsys):
    module, observed, _ = runner
    with pytest.raises(SystemExit) as caught:
        module.main(["--full", *flags])
    assert caught.value.code == 2
    assert observed["config_reads"] == [] and observed["run"] == [] and observed["fetch"] == []
    assert "--full uses its durable cursor" in capsys.readouterr().err


def test_partial_full_run_has_nonzero_exit_and_no_cursor_claim(runner, capsys):
    module, _, result = runner
    result["status"] = "partial"
    result["data"].update(status="partial", checkpoint_advanced=False,
                          item_counts={"failed": 1}, error_code="source_unavailable")
    assert module.main(["--full"]) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "partial"
    assert summary["data"]["error_code"] == "source_unavailable"
    assert not summary["checkpoint_committed"] and not summary["complete_library_sync"]


def test_summary_filters_nested_non_summary_fields_and_invalid_values(runner, capsys):
    module, _, result = runner
    result["data"].update(sync_run_id="PRIVATE-ACCOUNT-ID", expected_count=True,
                          error_code="PRIVATE-ERROR-BODY", item_counts={"PRIVATE-ITEM-ID": 9, "persisted": 1, "failed": True})
    assert module.main(["--full"]) == 0
    captured = capsys.readouterr()
    assert "PRIVATE" not in captured.out
    data = json.loads(captured.out)["data"]
    assert "sync_run_id" not in data and "expected_count" not in data
    assert data["item_counts"] == {"persisted": 1}
    assert data["error_code"] == "sync_unavailable"


def test_full_known_operational_failure_is_sanitized(runner, monkeypatch, capsys):
    module, _, _ = runner
    class Coordinator:
        def __init__(self, service): pass
        def run(self, *args, **kwargs):
            raise ConnectorError("rate_limited", retry_after=3)
    monkeypatch.setattr(sync_module, "SyncCoordinator", Coordinator)
    assert module.main(["--full"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"status": "failed", "error_code": "rate_limited"}


def test_help_has_full_mode_without_loading_credentials(runner, capsys):
    module, observed, _ = runner
    with pytest.raises(SystemExit) as caught:
        module.main(["--help"])
    assert caught.value.code == 0
    assert "--full" in capsys.readouterr().out
    assert observed["config_reads"] == []
