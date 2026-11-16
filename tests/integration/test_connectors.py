"""Transport/snapshot adapter tests: no real accounts, keys or private content."""
from __future__ import annotations

from copy import deepcopy
import json
import pytest
import httpx
from research_assistant.connectors import ConnectorError
from research_assistant.connectors.zotero import ZoteroReadOnlyClient
from research_assistant.connectors.notion import NotionReadOnlyClient, connected_page_snapshot, NOTION_API_VERSION

PAGE = "11111111-2222-4333-8444-555555555555"
SOURCE = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def item(key="ABCDEFGH", version=20, kind="journalArticle"):
    return {"key": key, "version": version, "data": {
        "key": key, "version": version, "itemType": kind,
        "title": "Synthetic paper", "abstractNote": "SYNTHETIC abstract", "tags": [{"tag": "Read"}],
        "dateModified": "2026-10-20T12:00:00Z", "creators": [],
    }}


def test_zotero_fixed_origin_read_only_and_object_versions():
    requests = []
    def handle(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.host == "api.zotero.org"
        assert request.headers["Zotero-API-Version"] == "3"
        assert request.headers["Zotero-API-Key"] == "synthetic-secret"
        assert "synthetic-secret" not in str(request.url)
        return httpx.Response(200, json=[item()], headers={"Last-Modified-Version": "22", "Total-Results": "1"})
    with ZoteroReadOnlyClient("synthetic-secret", "123", transport=httpx.MockTransport(handle)) as adapter:
        batch = adapter.fetch()
        with pytest.raises(ConnectorError, match="remote_write_forbidden"):
            adapter.request("POST", "/users/123/items", json={})
    assert len(requests) == 1
    assert not batch.complete and batch.cursor_candidate is None
    snapshot = batch.snapshots[0]
    assert snapshot["external_version"] == "20"  # not library checkpoint 22 / paper version
    assert snapshot["raw_payload"]["data"]["tags"] == [{"tag": "Read"}]
    assert set(snapshot).isdisjoint({"depth", "coverage", "trusted_message_id", "speaker", "api_key"})
    for passage in snapshot["passages"]:
        value = snapshot["raw_payload"]
        for part in passage["json_path"]:
            value = value[part]
        assert passage["text"] == value


def test_zotero_bounded_sample_has_no_full_checkpoint():
    def handle(request):
        return httpx.Response(200, json=[item()], headers={"Last-Modified-Version": "22", "Total-Results": "50"})
    with ZoteroReadOnlyClient("synthetic-secret", "123", transport=httpx.MockTransport(handle)) as adapter:
        batch = adapter.fetch(max_items=1)
    assert "bounded_initial_sample" in batch.missing_parts
    assert not batch.complete and batch.cursor_candidate is None


def test_zotero_pagination_uses_fixed_origin_and_rejects_version_change():
    seen = []
    def handle(request):
        seen.append(request.url)
        offset = request.url.params["start"]
        return httpx.Response(200, json=[item("ABCDEFGH" if offset == "0" else "IJKLMNOP")], headers={
            "Last-Modified-Version": "22" if offset == "0" else "23", "Total-Results": "2",
            "Link": '<https://malicious.invalid/private>; rel="next"',
        })
    with ZoteroReadOnlyClient("synthetic-secret", "123", transport=httpx.MockTransport(handle)) as adapter:
        with pytest.raises(ConnectorError, match="source_changed_during_pagination"):
            adapter.fetch(max_items=5)
    assert len(seen) == 2 and all(url.host == "api.zotero.org" for url in seen)
    assert seen[1].params["start"] == "1"


@pytest.mark.parametrize("status,code", [(403,"access_revoked"),(404,"not_found_or_inaccessible"),(429,"rate_limited"),(503,"source_unavailable"),(302,"redirect_refused")])
def test_zotero_errors_are_sanitized_and_not_fake_deletes(status, code):
    def handle(request):
        return httpx.Response(status, json={"error": "PRIVATE source body synthetic-secret"}, headers={"Location": "https://malicious.invalid", "Retry-After": "3"})
    with ZoteroReadOnlyClient("synthetic-secret", "123", transport=httpx.MockTransport(handle)) as adapter:
        with pytest.raises(ConnectorError) as caught:
            adapter.fetch()
    assert str(caught.value) == code
    assert "PRIVATE" not in str(caught.value)
    if status == 429:
        assert caught.value.retry_after == 3


def test_zotero_explicit_deleted_feed_and_no_checkpoint():
    def handle(request):
        assert request.url.params["since"] == "19"
        body = {"items": ["IJKLMNOP"], "collections": []} if request.url.path.endswith("/deleted") else [item()]
        return httpx.Response(200, json=body, headers={"Last-Modified-Version": "22", "Total-Results": "1"})
    with ZoteroReadOnlyClient("synthetic-secret", "123", transport=httpx.MockTransport(handle)) as adapter:
        batch = adapter.fetch(since=19, include_deleted=True)
    assert [row["change"] for row in batch.snapshots] == ["upsert", "delete"]
    assert batch.snapshots[1]["passages"] == []
    assert batch.cursor_candidate is None


def test_zotero_filters_nonpaper_items_without_inventing_work():
    def handle(request):
        return httpx.Response(200, json=[item(kind="attachment")], headers={"Last-Modified-Version": "22", "Total-Results": "1"})
    with ZoteroReadOnlyClient("synthetic-secret", "123", transport=httpx.MockTransport(handle)) as adapter:
        batch = adapter.fetch()
    assert batch.snapshots == []


def notion_handler(markdown=None, after_changed=False):
    calls = []
    def handle(request):
        calls.append(request)
        assert request.url.host == "api.notion.com"
        assert request.headers["Notion-Version"] == NOTION_API_VERSION
        if request.url.path.endswith("/query"):
            assert request.method == "POST"
            return httpx.Response(200, json={"results": [{"id": PAGE}], "has_more": False})
        if request.url.path.endswith("/markdown"):
            return httpx.Response(200, json=markdown or {"markdown": "SYNTHETIC page", "truncated": False, "unknown_block_ids": []})
        edited = "2026-10-20T12:00:00Z"
        if after_changed and len(calls) >= 3:
            edited = "2026-10-20T12:01:00Z"
        return httpx.Response(200, json={"object": "page", "id": PAGE, "parent": {"data_source_id": SOURCE}, "last_edited_time": edited})
    return handle, calls


def test_notion_registered_source_query_and_original_markdown_path():
    handle, calls = notion_handler()
    with NotionReadOnlyClient("synthetic-secret", source_instance_id="synthetic-workspace", transport=httpx.MockTransport(handle)) as adapter:
        batch = adapter.fetch_data_source(SOURCE)
        with pytest.raises(ConnectorError, match="remote_write_forbidden"):
            adapter.request("POST", "/v1/pages", json={})
    snapshot = batch.snapshots[0]
    assert snapshot["namespace"] == "collection://" + SOURCE
    assert snapshot["raw_payload"]["page_markdown"]["markdown"] == snapshot["passages"][0]["text"]
    assert not batch.complete and batch.cursor_candidate is None
    assert all("/search" not in request.url.path for request in calls)


def test_notion_truncation_and_unknown_blocks_are_partial():
    handle, _ = notion_handler({"markdown": "SYNTHETIC partial", "truncated": True, "unknown_block_ids": [PAGE]})
    with NotionReadOnlyClient("synthetic-secret", source_instance_id="synthetic-workspace", transport=httpx.MockTransport(handle)) as adapter:
        snapshot = adapter.fetch_page(PAGE, namespace="collection://" + SOURCE)
    assert snapshot["completeness"] == "partial"
    assert snapshot["missing_parts"] == ["markdown_truncated", "unknown_blocks"]


def test_notion_changed_snapshot_is_not_silently_complete():
    handle, _ = notion_handler(after_changed=True)
    with NotionReadOnlyClient("synthetic-secret", source_instance_id="synthetic-workspace", transport=httpx.MockTransport(handle)) as adapter:
        with pytest.raises(ConnectorError, match="source_changed_during_read"):
            adapter.fetch_page(PAGE, namespace="collection://" + SOURCE)


def test_connected_notion_snapshot_preserves_export_path_and_unknown_status():
    raw = {"export": {"content": [{"text": "SYNTHETIC MCP page export"}]}}
    snapshot = connected_page_snapshot(source_instance_id="synthetic-connection", namespace="explicit-root", external_id=PAGE,
                                       raw_payload=raw, markdown_path=["export", "content", 0, "text"])
    assert snapshot["completeness"] == "unknown"
    assert snapshot["raw_payload"] == raw
    assert snapshot["passages"][0]["json_path"] == ["export", "content", 0, "text"]
    assert snapshot["occurred_at"] is None


def test_notion_embedded_signed_urls_and_credential_fields_are_redacted_before_evidence():
    from research_assistant.connectors.notion import normalize_mcp_page
    from research_assistant.source_ingestion import SourceSnapshot
    raw = {
        "title": "SYNTHETIC page",
        "text": '<image source="https://assets.example.invalid/file.png?X-Amz-Expires=99&amp;X-Amz-Signature=PRIVATE-SIGNATURE"/>',
        "metadata": {"authorization": "Bearer PRIVATE-TOKEN"},
    }
    untouched = deepcopy(raw)
    snapshot = normalize_mcp_page(raw, source_instance_id="synthetic-workspace", namespace="registered-root",
        external_id=PAGE, passage_paths=[{"json_path": ["title"], "section": "title"}, {"json_path": ["text"], "section": "body"}],
        completeness="complete")
    serialized = json.dumps(snapshot)
    assert "PRIVATE-SIGNATURE" not in serialized and "PRIVATE-TOKEN" not in serialized
    assert "X-Amz-Signature" not in serialized
    assert snapshot["passages"][1]["text"] == snapshot["raw_payload"]["text"]
    assert snapshot["completeness"] == "partial"
    assert "temporary_urls_or_credentials_redacted" in snapshot["missing_parts"]
    assert raw == untouched
    SourceSnapshot.model_validate({**snapshot, "idempotency_key": "synthetic-snapshot"})


def test_zotero_snapshot_signed_metadata_is_redacted_without_changing_identity():
    raw = item()
    raw["data"]["url"] = "https://assets.example.invalid/paper?access_token=PRIVATE-SECRET"
    def handle(request):
        return httpx.Response(200, json=[raw], headers={"Last-Modified-Version": "22", "Total-Results": "1"})
    with ZoteroReadOnlyClient("synthetic-key", "123", transport=httpx.MockTransport(handle)) as adapter:
        snapshot = adapter.fetch().snapshots[0]
    assert "PRIVATE-SECRET" not in json.dumps(snapshot)
    assert snapshot["external_id"] == "ABCDEFGH"
    assert snapshot["raw_payload"]["data"]["url"] == "https://assets.example.invalid/paper"


def test_zotero_runner_event_key_distinguishes_new_observation_and_exact_retry():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "scripts/sync-zotero.py"
    spec = importlib.util.spec_from_file_location("synthetic_zotero_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    snapshot = {"provider": "zotero", "source_instance_id": "synthetic", "namespace": "users/123",
                "external_id": "ABCDEFGH", "external_version": "3", "raw_payload": {"data": {"title": "Synthetic"}},
                "change": "upsert", "observed_at": "2026-10-20T00:00:00Z"}
    first = module.request_key(snapshot)
    assert module.request_key(deepcopy(snapshot)) == first
    assert module.request_key(dict(snapshot, observed_at="2026-10-21T00:00:00Z")) != first
