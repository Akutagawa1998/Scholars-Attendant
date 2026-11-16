"""Zotero PDF bytes, redirects and permissions: synthetic MockTransport only."""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json
import pytest
import httpx

from research_assistant.assets import MAX_PDF_BYTES
from research_assistant.connectors import ConnectorError
from research_assistant.connectors.zotero_files import ZoteroFileClient

PDF = b"%PDF-1.7\n% SYNTHETIC bounded acquisition fixture\n%%EOF\n"
MD5 = hashlib.md5(PDF).hexdigest()
KEY = "ABCDEFGH"
API = "https://api.zotero.org"
FILE_PATH = "/users/123/items/" + KEY + "/file"
META_PATH = "/users/123/items/" + KEY
STORAGE = "https://zoterofilestorage.s3.us-east-1.amazonaws.com/object?X-Amz-Signature=PRIVATE-SIGNATURE"


def metadata():
    return {"key": KEY, "version": 7, "library": {"id": 123, "type": "user"}, "data": {
        "key": KEY, "version": 7, "itemType": "attachment", "contentType": "application/pdf",
        "linkMode": "imported_file", "md5": MD5, "filesize": len(PDF), "parentItem": "IJKLMNOP",
        "url": "http://127.0.0.1/NEVER-REQUEST-THIS", "filename": "Synthetic.pdf",
    }}


class Provider:
    def __init__(self, *, item=None, storage_url=STORAGE, file_body=PDF, file_status=200,
                 extra_file_headers=None, api_file_headers=None, grant=None, after=None):
        self.calls = []
        self.item = metadata() if item is None else item
        self.storage_url = storage_url
        self.file_body = file_body
        self.file_status = file_status
        self.extra_file_headers = extra_file_headers or {}
        self.api_file_headers = api_file_headers or {}
        self.grant = {"library": True, "notes": True, "files": True} if grant is None else grant
        self.after = after
        self.metadata_requests = 0

    def __call__(self, request):
        self.calls.append(request)
        assert request.method == "GET"
        if request.url.host == "api.zotero.org":
            assert request.headers["Zotero-API-Key"] == "PRIVATE-API-KEY"
            assert request.headers["Zotero-API-Version"] == "3"
            if request.url.path == "/keys/current":
                return httpx.Response(200, json={"userID": 123, "access": {"user": self.grant}})
            if request.url.path == META_PATH:
                self.metadata_requests += 1
                body = self.after if self.after is not None and self.metadata_requests > 1 else self.item
                return httpx.Response(200, json=body, headers={"Last-Modified-Version": str(body["version"])})
            if request.url.path == FILE_PATH:
                if self.file_status == 404:
                    return httpx.Response(404, json={"error": "PRIVATE missing provider body"})
                if self.storage_url is None:
                    return self.storage_response()
                return httpx.Response(302, headers={"Location": self.storage_url, "Zotero-File-MD5": MD5, **self.api_file_headers})
        assert request.url.host != "127.0.0.1"
        assert "Zotero-API-Key" not in request.headers
        assert "Authorization" not in request.headers
        assert "Cookie" not in request.headers
        return self.storage_response()

    def storage_response(self):
        return httpx.Response(self.file_status, stream=httpx.ByteStream(self.file_body), headers={
            "Content-Type": "application/pdf", "ETag": '"' + MD5 + '"', **self.extra_file_headers,
        })


def client(provider, **options):
    return ZoteroFileClient("PRIVATE-API-KEY", "123", transport=httpx.MockTransport(provider), **options)


def test_allowed_redirect_keeps_api_key_on_api_and_returns_original_hashes():
    provider = Provider()
    with client(provider) as fetcher:
        assert fetcher.verify_access() == {"library": True, "notes": True, "files": True}
        result = fetcher.fetch_pdf(KEY)
        assert fetcher.request_counts == {"metadata": 4, "file": 1, "storage": 1}
    assert result.status == "ok" and result.data == PDF
    assert result.sha256 == hashlib.sha256(PDF).hexdigest() and result.md5 == MD5
    assert result.byte_length == len(PDF)
    assert provider.metadata_requests == 2
    assert result.provenance["attachment_version"] == "7"
    assert result.provenance["metadata_md5"] == MD5
    assert result.provenance["content_type"] == "application/pdf"
    assert result.provenance["link_mode"] == "imported_file"
    serialized = json.dumps(result.provenance) + repr(result)
    assert "PRIVATE" not in serialized and "https://" not in serialized
    assert PDF.decode() not in repr(result)
    assert len(result.provenance_sha256) == 64


def test_direct_api_pdf_requires_no_storage_redirect():
    with client(Provider(storage_url=None)) as fetcher:
        assert fetcher.fetch_pdf(KEY).data == PDF


@pytest.mark.parametrize("url", [
    "http://files.zotero.net/object", "https://localhost/object", "https://127.0.0.1/object",
    "https://[::1]/object", "https://files.zotero.net.evil.invalid/object",
    "https://zoterofilestorage.s3.amazonaws.com.evil.invalid/object", "https://unrelated.s3.amazonaws.com/object",
    "https://s3.amazonaws.com/not-zotero/object", "https://user:PRIVATE@files.zotero.net/object",
    "https://files.zotero.net:8443/object", "https://api.zotero.org/keys/current",
    "//malicious.invalid/object",
    "https://s3.amazonaws.com/zoterofilestorage/../other-bucket/object",
    "https://s3.amazonaws.com/zoterofilestorage/%2e%2e/other-bucket/object",
])
def test_unsafe_redirect_is_refused_before_contacting_destination(url):
    provider = Provider(storage_url=url)
    with client(provider) as fetcher, pytest.raises(ConnectorError, match="unsafe_storage_redirect"):
        fetcher.fetch_pdf(KEY)
    assert all(request.url.host == "api.zotero.org" for request in provider.calls)


@pytest.mark.parametrize("url", [
    "https://files.zotero.net/object", "https://zoterofilestorage.s3.amazonaws.com/object",
    "https://s3.amazonaws.com/zoterofilestorage/object", "https://s3.us-east-1.amazonaws.com/zoterofilestorage/object",
])
def test_only_known_zotero_storage_hosts_or_bucket_paths_are_allowed(url):
    with client(Provider(storage_url=url)) as fetcher:
        assert fetcher.fetch_pdf(KEY).status == "ok"


@pytest.mark.parametrize("field,value", [("linkMode","linked_file"),("linkMode","linked_url"),
                                         ("itemType","journalArticle"),("contentType","text/html")])
def test_unaccepted_attachment_metadata_never_requests_a_file(field, value):
    item = metadata(); item["data"][field] = value
    provider = Provider(item=item)
    with client(provider) as fetcher, pytest.raises(ConnectorError, match="unsupported_attachment"):
        fetcher.fetch_pdf(KEY)
    assert not any(request.url.path == FILE_PATH for request in provider.calls)


def test_files_permission_is_checked_not_inferred_from_library_or_write_access():
    provider = Provider(grant={"library": True, "notes": True, "write": True, "files": False})
    with client(provider) as fetcher, pytest.raises(ConnectorError, match="file_access_not_granted"):
        fetcher.fetch_pdf(KEY)
    assert len(provider.calls) == 1


def test_cloud_missing_404_is_unavailable_not_a_deletion():
    with client(Provider(file_status=404)) as fetcher:
        result = fetcher.fetch_pdf(KEY)
    assert result.status == "unavailable" and result.data is None
    assert result.error_code == "not_found_or_inaccessible"
    assert "delet" not in repr(result).lower() and "PRIVATE" not in repr(result)


@pytest.mark.parametrize("headers,code", [({"Content-Length": "999999"}, "pdf_too_large"),
                                         ({"Content-Length": "1"}, "file_length_mismatch"),
                                         ({"Content-Length": "bad"}, "invalid_file_length"),
                                         ({"Content-Encoding": "gzip"}, "unsupported_content_encoding"),
                                         ({"Content-Type": "text/html"}, "invalid_pdf_media_type"),
                                         ({"ETag": '"' + "0"*32 + '"'}, "file_hash_mismatch")])
def test_invalid_length_encoding_or_response_hash_rejected(headers, code):
    with client(Provider(extra_file_headers=headers), max_bytes=1000) as fetcher, pytest.raises(ConnectorError, match=code):
        fetcher.fetch_pdf(KEY)


def test_original_byte_size_is_bounded_without_content_length():
    item = metadata(); item["data"].pop("filesize")
    with client(Provider(item=item, file_body=b"%PDF-" + b"x"*2000), max_bytes=100) as fetcher, pytest.raises(ConnectorError, match="pdf_too_large"):
        fetcher.fetch_pdf(KEY)


def test_pdf_header_is_required_before_parser_or_persistence():
    with client(Provider(file_body=b"<html>PRIVATE login page")) as fetcher, pytest.raises(ConnectorError, match="invalid_pdf_header"):
        fetcher.fetch_pdf(KEY)


def test_metadata_checksum_must_match_original_bytes():
    item = metadata(); item["data"]["md5"] = "0"*32
    with client(Provider(item=item)) as fetcher, pytest.raises(ConnectorError, match="file_hash_mismatch"):
        fetcher.fetch_pdf(KEY)


def test_metadata_postcheck_rejects_revision_change_during_download():
    after = metadata(); after["version"] = after["data"]["version"] = 8
    with client(Provider(after=after)) as fetcher, pytest.raises(ConnectorError, match="attachment_changed_during_download"):
        fetcher.fetch_pdf(KEY)


def test_storage_redirect_loop_is_bounded_without_forwarding_api_auth():
    provider = Provider()
    def handle(request):
        if request.url.host == "api.zotero.org":
            return provider(request)
        assert "Zotero-API-Key" not in request.headers
        return httpx.Response(302, headers={"Location": STORAGE})
    with ZoteroFileClient("PRIVATE-API-KEY", "123", transport=httpx.MockTransport(handle)) as fetcher, pytest.raises(ConnectorError, match="storage_redirect_loop"):
        fetcher.fetch_pdf(KEY)


def test_api_rate_limit_and_backoff_are_safe_unavailable_results_without_body():
    calls=[]
    def handle(request):
        calls.append(request)
        return httpx.Response(429, json={"error": "PRIVATE-API-KEY"}, headers={"Retry-After": "30"})
    with ZoteroFileClient("PRIVATE-API-KEY", "123", transport=httpx.MockTransport(handle)) as fetcher:
        result=fetcher.fetch_pdf(KEY)
        assert result.status == "unavailable" and result.error_code == "rate_limited" and result.retry_after == 30
        second=fetcher.fetch_pdf(KEY)
        assert second.error_code == "source_backoff" and second.retry_after > 0
    assert len(calls)==1 and "PRIVATE" not in repr(result)


@pytest.mark.parametrize("options", [{"timeout":9},{"timeout":61},{"max_bytes":MAX_PDF_BYTES+1},{"max_bytes":True},{"max_redirects":6}])
def test_limits_are_validated_before_network(options):
    with pytest.raises(ConnectorError, match="invalid_configuration"):
        client(Provider(), **options)


def test_attachment_metadata_verifies_fresh_grant_and_identity_with_no_file_get():
    provider=Provider()
    with client(provider) as fetcher:
        result=fetcher.attachment_metadata(KEY)
        assert fetcher.request_counts == {"metadata": 2, "file": 0, "storage": 0}
    assert result["attachment_key"] == KEY and result["attachment_version"] == "7"
    assert result["metadata_md5"] == MD5 and result["content_type"] == "application/pdf"
    assert result["expected_byte_length"] == len(PDF)
    assert [r.url.path for r in provider.calls] == ["/keys/current", META_PATH]
    assert "PRIVATE" not in json.dumps(result) and "http" not in json.dumps(result)


def test_metadata_boolean_version_is_not_an_integer_revision():
    item=metadata(); item["version"]=1; item["data"]["version"]=True
    with client(Provider(item=item)) as fetcher, pytest.raises(ConnectorError, match="invalid_file_metadata"):
        fetcher.fetch_pdf(KEY)


def test_compressed_zotero_snapshot_is_not_treated_as_raw_pdf_bytes():
    provider=Provider(api_file_headers={"Zotero-File-Compressed":"Yes"})
    with client(provider) as fetcher, pytest.raises(ConnectorError, match="compressed_attachment_unsupported"):
        fetcher.fetch_pdf(KEY)
    assert all(request.url.host == "api.zotero.org" for request in provider.calls)


def test_missing_metadata_md5_does_not_fabricate_external_integrity_verification():
    item=metadata(); item["data"]["md5"]=None
    with client(Provider(item=item)) as fetcher:
        result=fetcher.fetch_pdf(KEY)
    assert result.status == "ok" and result.md5 == MD5
    assert result.provenance["metadata_md5"] is None
    assert result.provenance["metadata_md5_verified"] is False
    assert result.provenance["response_md5_verified"] is True
