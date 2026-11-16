"""Full Zotero metadata protocol tests use synthetic responses only."""
from __future__ import annotations

from copy import deepcopy
import json

import httpx
import pytest

from research_assistant.connectors import ConnectorError
from research_assistant.connectors.zotero import ZoteroReadOnlyClient
from research_assistant.schemas import SourceSnapshot


def record(key="ABCDEFGH", version=20, kind="journalArticle", **fields):
    return {
        "key": key, "version": version,
        "library": {"id": 123, "type": "user", "name": "SYNTHETIC library"},
        "data": {"key": key, "version": version, "itemType": kind,
                 "dateModified": "2026-10-20T12:00:00Z", **fields},
    }


def client(handle, **kwargs):
    def authenticated(request):
        if request.url.path == "/keys/current":
            return httpx.Response(200, json={"userID": 123, "access": {"user": {"library": True, "notes": True, "files": False},
                "groups": {"all": {"library": True}}}})
        return handle(request)
    return ZoteroReadOnlyClient("synthetic-key", "123", transport=httpx.MockTransport(authenticated), **kwargs)


def test_inventory_is_unbounded_all_item_metadata_and_read_only():
    versions = {f"{index:08d}": index % 20 for index in range(250)}
    seen = []
    def handle(request):
        seen.append(request)
        assert request.method == "GET"
        assert request.url.host == "api.zotero.org"
        assert request.url.path == "/users/123/items"
        assert dict(request.url.params) == {"format": "versions", "includeTrashed": "1"}
        assert request.headers["Zotero-API-Key"] == "synthetic-key"
        assert "synthetic-key" not in str(request.url)
        return httpx.Response(200, json=versions, headers={"Last-Modified-Version": "22", "Total-Results": "250"})
    with client(handle) as adapter:
        inventory = adapter.inventory()
        with pytest.raises(ConnectorError, match="remote_write_forbidden"):
            adapter.request("POST", "/users/123/items", json={})
    assert len(seen) == 1
    assert inventory["object_versions"] == {key: str(value) for key, value in versions.items()}
    assert inventory["version"] == "22"
    assert inventory["consistent"] and inventory["complete"]
    assert inventory["metadata"]["scope"] == "all_item_metadata"
    assert inventory["metadata"]["original_files_acquired"] is False
    assert inventory["metadata"]["include_trashed"] is True
    assert "cursor_candidate" not in inventory


@pytest.mark.parametrize("body,headers,code", [
    ([], {"Last-Modified-Version": "22"}, "invalid_source_response"),
    ({"bad-key": 1}, {"Last-Modified-Version": "22"}, "invalid_source_response"),
    ({"ABCDEFGH": True}, {"Last-Modified-Version": "22"}, "invalid_source_response"),
    ({"ABCDEFGH": -1}, {"Last-Modified-Version": "22"}, "invalid_source_response"),
    ({"ABCDEFGH": 23}, {"Last-Modified-Version": "22"}, "source_changed_during_pagination"),
    ({}, {}, "missing_library_version"),
    ({}, {"Last-Modified-Version": "private-body"}, "invalid_source_response"),
    ({}, {"Last-Modified-Version": "22", "Total-Results": "1"}, "incomplete_source_response"),
    ({}, {"Last-Modified-Version": "22", "Link": '<https://malicious.invalid/>; rel="next"'}, "incomplete_source_response"),
    ({}, {"Last-Modified-Version": "22", "Link": '<https://malicious.invalid/>; rel="alternate next"'}, "incomplete_source_response"),
])
def test_inventory_refuses_malformed_or_truncated_authoritative_sets(body, headers, code):
    seen = []
    def handle(request):
        seen.append(request)
        return httpx.Response(200, json=body, headers=headers)
    with client(handle) as adapter:
        with pytest.raises(ConnectorError, match=code):
            adapter.inventory()
    assert len(seen) == 1


def test_objects_include_notes_annotations_attachments_and_structural_metadata():
    rows = [
        record(title="SYNTHETIC paper", abstractNote="SYNTHETIC abstract", tags=[{"tag": "Read"}]),
        record("IJKLMNOP", 18, "note", parentItem="ABCDEFGH", note="<p>SYNTHETIC note</p>"),
        record("QRSTUVWX", 19, "annotation", parentItem="YZABCDEF", annotationText="SYNTHETIC highlight",
               annotationComment="SYNTHETIC comment", annotationType="highlight"),
        record("YZABCDEF", 17, "attachment", parentItem="ABCDEFGH", contentType="application/pdf",
               linkMode="imported_file", filename="synthetic.pdf", md5="0" * 32),
        record("GHIJKLMN", 21, "book", title="SYNTHETIC book"),
    ]
    untouched = deepcopy(rows)
    keys = [row["key"] for row in rows]
    def handle(request):
        assert request.url.path == "/users/123/items"
        assert dict(request.url.params) == {"format": "json", "include": "data", "itemKey": ",".join(keys),
                                            "includeTrashed": "1", "limit": "50"}
        return httpx.Response(200, json=list(reversed(rows)), headers={"Last-Modified-Version": "22", "Total-Results": "5"})
    with client(handle) as adapter:
        snapshots = adapter.fetch_objects(keys, "22")
    assert [snapshot["external_id"] for snapshot in snapshots] == keys
    assert [snapshot["external_version"] for snapshot in snapshots] == ["20", "18", "19", "17", "21"]
    assert snapshots[3]["passages"] == []
    assert snapshots[2]["raw_payload"]["data"]["parentItem"] == "YZABCDEF"
    assert snapshots[1]["passages"][0]["section"] == "note"
    assert [passage["section"] for passage in snapshots[2]["passages"]] == ["annotationText", "annotationComment"]
    for snapshot in snapshots:
        assert snapshot["change"] == "upsert"
        assert set(snapshot).isdisjoint({"depth", "coverage", "reading_status", "read_at", "paper_version"})
        SourceSnapshot.model_validate({**snapshot, "idempotency_key": "synthetic-import"})
    assert rows == untouched


def test_objects_are_batched_at_50_without_fetching_or_following_file_links():
    keys = [f"{index:08d}" for index in range(51)]
    batches = []
    def handle(request):
        batch_keys = request.url.params["itemKey"].split(",")
        batches.append(batch_keys)
        assert len(batch_keys) <= 50 and request.url.host == "api.zotero.org"
        rows = [record(key, 20, "attachment", links={"enclosure": {"href": "https://files.invalid/private.pdf"}})
                for key in batch_keys]
        return httpx.Response(200, json=rows, headers={"Last-Modified-Version": "22"})
    with client(handle) as adapter:
        assert adapter.fetch_objects([], "22") == []
        snapshots = adapter.fetch_objects(keys, "22")
    assert [len(batch) for batch in batches] == [50, 1]
    assert [snapshot["external_id"] for snapshot in snapshots] == keys


def test_objects_discard_whole_call_on_library_drift_between_batches():
    keys = [f"{index:08d}" for index in range(51)]
    seen = []
    def handle(request):
        seen.append(request)
        rows = [record(key) for key in request.url.params["itemKey"].split(",")]
        return httpx.Response(200, json=rows, headers={"Last-Modified-Version": "22" if len(seen) == 1 else "23"})
    with client(handle) as adapter:
        with pytest.raises(ConnectorError, match="source_changed_during_pagination"):
            adapter.fetch_objects(keys, "22")
    assert len(seen) == 2


@pytest.mark.parametrize("rows,code", [
    ([], "objects_missing_from_response"),
    ([record("IJKLMNOP")], "source_identity_mismatch"),
    ([record(), record()], "source_identity_mismatch"),
    ([record(version=23)], "source_changed_during_pagination"),
    ([{**record(), "data": {"key": "IJKLMNOP", "version": 20, "itemType": "note"}}], "invalid_source_response"),
    ([{**record(), "library": {"type": "user", "id": 456}}], "source_identity_mismatch"),
    ([record(deleted="1")], "invalid_source_response"),
])
def test_missing_or_conflicting_objects_are_failures_never_fake_deletes(rows, code):
    def handle(request):
        return httpx.Response(200, json=rows, headers={"Last-Modified-Version": "22"})
    with client(handle) as adapter:
        with pytest.raises(ConnectorError, match=code):
            adapter.fetch_objects(["ABCDEFGH"], "22")


def test_trashed_item_is_explicit_delete_with_its_actual_item_revision():
    raw = record(version=20, title="SYNTHETIC trashed paper", deleted=1)
    def handle(request):
        return httpx.Response(200, json=[raw], headers={"Last-Modified-Version": "22"})
    with client(handle) as adapter:
        snapshot = adapter.fetch_objects(["ABCDEFGH"], "22")[0]
    assert snapshot["change"] == "delete"
    assert snapshot["external_version"] == "20"
    assert snapshot["raw_payload"]["data"]["deleted"] == 1
    assert snapshot["passages"] == []


def test_child_source_strings_are_redacted_before_passage_creation():
    raw = record(kind="annotation", annotationComment='<img src="https://files.invalid/a?X-Amz-Signature=PRIVATE"/>',
                 annotationText="SYNTHETIC text", credentials={"api_key": "PRIVATE-KEY"})
    def handle(request):
        return httpx.Response(200, json=[raw], headers={"Last-Modified-Version": "22"})
    with client(handle) as adapter:
        snapshot = adapter.fetch_objects(["ABCDEFGH"], "22")[0]
    assert "PRIVATE" not in json.dumps(snapshot)
    assert snapshot["completeness"] == "partial"
    assert snapshot["passages"][1]["text"] == snapshot["raw_payload"]["data"]["annotationComment"]
    SourceSnapshot.model_validate({**snapshot, "idempotency_key": "synthetic-redacted"})


def test_explicit_permanent_deleted_feed_uses_library_version_observation():
    def handle(request):
        assert request.method == "GET"
        assert request.url.path == "/groups/123/deleted"
        assert dict(request.url.params) == {"since": "19"}
        return httpx.Response(200, json={"items": ["ABCDEFGH"], "collections": ["IJKLMNOP"]},
                              headers={"Last-Modified-Version": "22"})
    with client(handle, library_type="group") as adapter:
        snapshots = adapter.deleted(19, "22")
    assert len(snapshots) == 1
    assert snapshots[0]["change"] == "delete" and snapshots[0]["passages"] == []
    assert snapshots[0]["namespace"] == "groups/123"
    assert snapshots[0]["raw_payload"] == {"deleted_item_key": "ABCDEFGH", "library_version": "22"}
    SourceSnapshot.model_validate({**snapshots[0], "idempotency_key": "synthetic-deleted"})


@pytest.mark.parametrize("body,version,code", [
    ({"items": []}, "23", "source_changed_during_pagination"),
    ({}, "22", "invalid_source_response"),
    ({"items": ["bad"]}, "22", "invalid_source_response"),
    ({"items": ["ABCDEFGH", "ABCDEFGH"]}, "22", "invalid_source_response"),
])
def test_deleted_feed_needs_explicit_valid_items_at_expected_library_version(body, version, code):
    def handle(request):
        return httpx.Response(200, json=body, headers={"Last-Modified-Version": version})
    with client(handle) as adapter:
        with pytest.raises(ConnectorError, match=code):
            adapter.deleted(0, "22")


@pytest.mark.parametrize("operation", [
    lambda adapter: adapter.fetch_objects(["ABCDEFGH", "ABCDEFGH"], "22"),
    lambda adapter: adapter.fetch_objects(["bad"], "22"),
    lambda adapter: adapter.fetch_objects("ABCDEFGH", "22"),
    lambda adapter: adapter.fetch_objects([], True),
    lambda adapter: adapter.deleted(True, "22"),
    lambda adapter: adapter.deleted(23, "22"),
])
def test_invalid_incremental_request_never_reaches_remote(operation):
    def handle(request):
        pytest.fail("invalid request reached remote")
    with client(handle) as adapter:
        with pytest.raises(ConnectorError, match="invalid_configuration"):
            operation(adapter)


def test_successful_backoff_defers_next_call_without_sleep_or_more_requests(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("research_assistant.connectors.zotero.time.monotonic", lambda: clock[0])
    requests = []
    def handle(request):
        requests.append(request)
        headers = {"Last-Modified-Version": "22"}
        if len(requests) == 1:
            headers["Backoff"] = "3"
        return httpx.Response(200, json={}, headers=headers)
    with client(handle) as adapter:
        assert adapter.inventory()["metadata"]["backoff_seconds"] == 3
        with pytest.raises(ConnectorError, match="rate_limited") as caught:
            adapter.inventory()
        assert caught.value.retry_after == 3
        assert len(requests) == 1
        clock[0] += 3
        assert adapter.inventory()["metadata"]["backoff_seconds"] == 0
    assert len(requests) == 2


@pytest.mark.parametrize("description,code", [
    ({"userID": 456, "access": {"user": {"library": True, "notes": True}}}, "identity_mismatch"),
    ({"userID": 123, "access": {"user": {"library": False, "notes": True}}}, "insufficient_permissions"),
    ({"userID": 123, "access": {"user": {"library": True, "notes": False}}}, "insufficient_permissions"),
    ({"userID": 123, "access": {"user": {"library": True}}}, "insufficient_permissions"),
    ({"userID": 123, "access": {}}, "insufficient_permissions"),
    ({"userID": 123}, "invalid_source_response"),
])
def test_inventory_checks_personal_key_identity_and_notes_grants_before_read(description, code):
    requests = []
    def handle(request):
        requests.append(request)
        assert request.url.path == "/keys/current" and request.method == "GET"
        assert request.headers["Zotero-API-Key"] == "synthetic-key"
        assert "synthetic-key" not in str(request.url)
        return httpx.Response(200, json={**description, "key": "PRIVATE-KEY", "username": "PRIVATE-USERNAME"})
    with ZoteroReadOnlyClient("synthetic-key", "123", transport=httpx.MockTransport(handle)) as adapter:
        with pytest.raises(ConnectorError) as caught:
            adapter.inventory()
    assert str(caught.value) == code and "PRIVATE" not in str(caught.value)
    assert len(requests) == 1


@pytest.mark.parametrize("groups,allowed", [
    ({"123": {"library": True}}, True),
    ({"all": {"library": True}}, True),
    ({"123": {"library": True, "notes": True, "files": False}}, True),
    ({"123": {"library": True, "notes": False}}, False),
    ({"all": {"library": True, "notes": False}}, False),
    ({"123": {"library": False}}, False),
    ({"456": {"library": True}}, False),
])
def test_group_key_access_accepts_specific_or_all_grants_and_rejects_disabled_notes(groups, allowed):
    def handle(request):
        assert request.url.path == "/keys/current"
        return httpx.Response(200, json={"userID": 456, "access": {"groups": groups}})
    with ZoteroReadOnlyClient("synthetic-key", "123", library_type="group", transport=httpx.MockTransport(handle)) as adapter:
        if allowed:
            assert adapter.verify_access() == {"library_access": True, "notes_access": True}
        else:
            with pytest.raises(ConnectorError, match="insufficient_permissions"):
                adapter.verify_access()


@pytest.mark.parametrize("status,code", [(401, "authentication_failed"), (403, "access_revoked")])
def test_revoked_key_is_not_an_empty_complete_library(status, code):
    def handle(request):
        assert request.url.path == "/keys/current"
        return httpx.Response(status, json={"error": "PRIVATE-REVOKED-BODY"})
    with ZoteroReadOnlyClient("synthetic-key", "123", transport=httpx.MockTransport(handle)) as adapter:
        with pytest.raises(ConnectorError, match=code):
            adapter.inventory()


def test_every_inventory_rechecks_permissions_so_notes_revocation_cannot_commit():
    calls = []
    key_checks = 0
    def handle(request):
        nonlocal key_checks
        calls.append(request.url.path)
        if request.url.path == "/keys/current":
            key_checks += 1
            return httpx.Response(200, json={"userID": 123, "key": "PRIVATE-KEY", "access": {
                "user": {"library": True, "notes": key_checks == 1, "files": False}}})
        return httpx.Response(200, json={"ABCDEFGH": 20}, headers={"Last-Modified-Version": "22"})
    with ZoteroReadOnlyClient("synthetic-key", "123", transport=httpx.MockTransport(handle)) as adapter:
        initial = adapter.inventory()
        assert "PRIVATE" not in json.dumps(initial)
        assert initial["complete"] is True
        with pytest.raises(ConnectorError, match="insufficient_permissions"):
            adapter.inventory()
    assert calls == ["/keys/current", "/users/123/items", "/keys/current"]
