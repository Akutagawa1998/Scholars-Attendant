"""Canonical PDF byte registration and forget/purge, synthetic data only."""
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select, func

from research_assistant import storage as db
from research_assistant.assets import ArtifactStore, AssetError
from research_assistant.asset_service import PdfIngestor, purge_pending_assets, read_source_asset
from research_assistant.service import DomainError, ResearchService
from test_assets import pdf
from test_postgres import pg_url


@pytest.fixture(params=["sqlite", "postgres"])
def service(request, tmp_path):
    url = "sqlite:///" + str(tmp_path / "pdf.sqlite3") if request.param == "sqlite" else request.getfixturevalue("pg_url")
    instance = ResearchService(url, owner_id="pdf-fixture-owner", scope_id="private")
    instance.initialize()
    instance.test_database_url = url
    yield instance
    instance.storage.engine.dispose()


@pytest.fixture
def store(tmp_path):
    with ArtifactStore(tmp_path / "private-blobs") as instance:
        yield instance


def ingest(service, store, data=None, **overrides):
    options = dict(identifier="synthetic:paper:v1", title="Synthetic paper", version="v1", source_url="",
                   passages=[dict(page_index=0, text="Synthetic quote.", section="abstract")], idempotency_key=str(uuid4()))
    options.update(overrides)
    return PdfIngestor(service, store).ingest(data or pdf("Synthetic quote.", "Second page."), **options)


def asset(service, source_id):
    with service.storage.transaction() as c:
        return dict(c.execute(select(db.source_assets).where(service._scope(db.source_assets),
            db.source_assets.c.source_version_id == source_id)).mappings().one())


def forget(service, source_id):
    return service.execute("memory.forget", dict(target_ids=[source_id], scope="memory_and_source_snapshots", idempotency_key=str(uuid4())))


def test_pdf_bytes_hash_page_anchors_and_registration_are_canonical(service, store):
    data = pdf("Synthetic quote.", "Second page.")
    receipt = ingest(service, store, data, idempotency_key="repeat")
    assert receipt == ingest(service, store, data, idempotency_key="repeat")
    source_id = receipt["data"]["source_snapshot_id"]
    record = asset(service, source_id)
    assert record["sha256"] == sha256(data).hexdigest()
    assert record["byte_size"] == len(data) and record["state"] == "active"
    assert record["media_type"] == "application/pdf" and record["parser_name"] == "pypdf"
    assert record["page_metadata"][1]["page_index"] == 1
    assert "text" not in record["page_metadata"][0]
    assert "blob_key" not in str(receipt) and str(store) not in str(receipt)
    assert read_source_asset(service, store, source_id) == data
    with service.storage.transaction() as c:
        source = service._row(c, db.sources, source_id)
        span = c.execute(select(db.evidence).where(db.evidence.c.source_version_id == source_id)).mappings().one()
        assert source["sha256"] != record["sha256"]
        assert span["locator"]["source_sha256"] == record["sha256"]
        assert span["locator"]["page_text_sha256"] == record["page_metadata"][0]["text_sha256"]
        assert span["locator"]["page_index"] == 0
        assert c.scalar(select(func.count()).select_from(db.source_assets).where(service._scope(db.source_assets))) == 1
        assert c.scalar(select(func.count()).select_from(db.reading_sessions).where(service._scope(db.reading_sessions))) == 0


def test_generic_pdf_import_cannot_forge_original_bytes(service):
    payload = dict(identifier="synthetic:forged:v1", title="Forged", version="v1", source_url="", content="Synthetic quote.",
        media_type="application/pdf", extraction_version="fabricated", passages=[dict(text="Synthetic quote.", section="body", locator=dict(kind="pdf", page_index=0))], idempotency_key="forged")
    with pytest.raises(DomainError) as caught:
        service.execute("papers.import", payload, trusted_capture=True)
    assert caught.value.code == "verified_pdf_required"
    with pytest.raises(DomainError) as caught:
        service.execute("papers.import", payload, trusted_capture=True, _asset_context={"sha256": "0" * 64})
    assert caught.value.code == "pdf_adapter_required"


def test_failed_ingest_cleans_new_blob_and_preserves_existing_shared_blob(service, store):
    first_data = pdf("Synthetic quote.")
    with pytest.raises(DomainError):
        ingest(service, store, first_data, identifier="missing-version")
    first_hash = sha256(first_data).hexdigest()
    first_key = f"sha256/{first_hash[:2]}/{first_hash}.pdf"
    with pytest.raises(AssetError, match="blob_not_found"):
        store.read_blob(first_key, first_hash)
    saved = ingest(service, store, first_data)
    with pytest.raises(DomainError):
        ingest(service, store, first_data, identifier="still-missing-version")
    assert read_source_asset(service, store, saved["data"]["source_snapshot_id"]) == first_data


def test_same_extracted_text_cannot_change_original_pdf_for_existing_version(service, store):
    original = pdf("Synthetic quote.")
    saved = ingest(service, store, original, idempotency_key="original")
    changed = original + b"\n% SYNTHETIC different raw PDF bytes\n"
    with pytest.raises(DomainError) as caught:
        ingest(service, store, changed)
    assert caught.value.code == "immutable_pdf_asset"
    assert read_source_asset(service, store, saved["data"]["source_snapshot_id"]) == original
    changed_hash = sha256(changed).hexdigest()
    with pytest.raises(AssetError, match="blob_not_found"):
        store.read_blob(f"sha256/{changed_hash[:2]}/{changed_hash}.pdf", changed_hash)
    # The old receipt cannot bypass byte identity validation either.
    with pytest.raises(DomainError) as caught:
        ingest(service, store, changed, idempotency_key="original")
    assert caught.value.code == "immutable_pdf_asset"
    with pytest.raises(AssetError, match="blob_not_found"):
        store.read_blob(f"sha256/{changed_hash[:2]}/{changed_hash}.pdf", changed_hash)


def test_asset_read_authorization_revoke_restore_and_scope(service, store):
    data = pdf("Synthetic quote.")
    source_id = ingest(service, store, data)["data"]["source_snapshot_id"]
    other = ResearchService(service.test_database_url, owner_id="other-pdf-owner", scope_id="private")
    other.initialize()
    try:
        with pytest.raises(DomainError) as caught:
            read_source_asset(other, store, source_id)
        assert caught.value.code == "not_found"
        service.execute("sources.set_access", dict(
            source_snapshot_id=source_id, expected_revision=1, state="inaccessible",
            idempotency_key="revoke",
        ))
        with pytest.raises(DomainError, match="Source asset is unavailable"):
            read_source_asset(service, store, source_id)
        assert purge_pending_assets(service, store) == {"purged": 0, "retained_shared": 0}
        service.execute("sources.set_access", dict(
            source_snapshot_id=source_id, expected_revision=2, state="accessible",
            idempotency_key="restore",
        ), trusted_capture=True)
        assert read_source_asset(service, store, source_id) == data
    finally:
        other.storage.engine.dispose()


def test_forget_purge_and_replay_cannot_resurrect_raw_bytes(service, store):
    data = pdf("Synthetic quote.")
    receipt = ingest(service, store, data, idempotency_key="old-key")
    source_id = receipt["data"]["source_snapshot_id"]
    original = asset(service, source_id)
    forget(service, source_id)
    assert asset(service, source_id)["state"] == "purge_pending"
    with pytest.raises(DomainError):
        read_source_asset(service, store, source_id)
    assert purge_pending_assets(service, store) == {"purged": 1, "retained_shared": 0}
    assert purge_pending_assets(service, store) == {"purged": 0, "retained_shared": 0}
    assert asset(service, source_id)["state"] == "purged"
    with pytest.raises(AssetError, match="blob_not_found"):
        store.read_blob(original["blob_key"], original["sha256"])
    for options in [dict(idempotency_key="old-key"), dict(identifier="synthetic:renamed:v2", version="v2")]:
        with pytest.raises(DomainError) as caught:
            ingest(service, store, data, **options)
        assert caught.value.code == "forgotten"
        with pytest.raises(AssetError, match="blob_not_found"):
            store.read_blob(original["blob_key"], original["sha256"])


def test_shared_hash_purge_retains_other_owner_bytes(service, store):
    data = pdf("Synthetic quote.")
    first = ingest(service, store, data)["data"]["source_snapshot_id"]
    other = ResearchService(service.test_database_url, owner_id="other-pdf-owner", scope_id="private")
    other.initialize()
    try:
        second = ingest(other, store, data)["data"]["source_snapshot_id"]
        forget(service, first)
        assert purge_pending_assets(service, store) == {"purged": 0, "retained_shared": 1}
        assert read_source_asset(other, store, second) == data
        forget(other, second)
        assert purge_pending_assets(service, store) == {"purged": 1, "retained_shared": 0}
        assert purge_pending_assets(other, store) == {"purged": 1, "retained_shared": 0}
    finally:
        other.storage.engine.dispose()


def test_purge_between_put_and_registration_is_repaired_under_hash_lock(service, store, monkeypatch):
    data = pdf("Synthetic quote.")
    source_id = ingest(service, store, data)["data"]["source_snapshot_id"]
    forget(service, source_id)
    other = ResearchService(service.test_database_url, owner_id="other-pdf-owner", scope_id="private")
    other.initialize()
    original_execute = other.execute
    def purge_before_register(*args, **kwargs):
        assert purge_pending_assets(service, store)["purged"] == 1
        return original_execute(*args, **kwargs)
    monkeypatch.setattr(other, "execute", purge_before_register)
    try:
        second = ingest(other, store, data)["data"]["source_snapshot_id"]
        assert read_source_asset(other, store, second) == data
    finally:
        other.storage.engine.dispose()


def pdf_cli():
    path = Path(__file__).resolve().parents[2] / "scripts" / "import-pdf.py"
    spec = importlib.util.spec_from_file_location("pdf_operator_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_local_operator_cli_import_and_purge_hide_private_paths(service, tmp_path, monkeypatch, capsys):
    module = pdf_cli()
    monkeypatch.setattr(module, "configured_service", lambda _: service)
    local = tmp_path / "PRIVATE-title.pdf"
    local.write_bytes(pdf("Synthetic quote."))
    metadata = tmp_path / "PRIVATE-metadata.json"
    metadata.write_text(json.dumps(dict(identifier="synthetic:cli:v1", title="PRIVATE title", version="v1",
        passages=[dict(page_index=0, text="Synthetic quote.", section="abstract")], idempotency_key="cli")))
    root = tmp_path / "PRIVATE-blob-root"
    args = ["--pdf", str(local), "--metadata", str(metadata), "--asset-root", str(root)]
    assert module.main(args) == 0
    stdout, stderr = capsys.readouterr()
    assert not stderr and "PRIVATE" not in stdout and str(tmp_path) not in stdout
    receipt = json.loads(stdout)
    assert receipt["original_file_acquired"] and not receipt["remote_write"]
    assert receipt["evidence_count"] == 1
    forget(service, receipt["data"]["source_snapshot_id"])
    assert module.main(["--purge-pending", "--asset-root", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["cleanup"]["purged"] == 1
    assert not list(root.rglob("*.pdf"))


def test_local_operator_cli_redacts_invalid_metadata_and_rejects_repo_asset_root(tmp_path, capsys):
    module = pdf_cli()
    local = tmp_path / "PRIVATE.pdf"
    local.write_bytes(pdf("Synthetic quote."))
    metadata = tmp_path / "PRIVATE.json"
    metadata.write_text(json.dumps(dict(identifier="synthetic:cli:v1", title="PRIVATE", version="v1", source_url="file:///PRIVATE/path.pdf",
        passages=[dict(page_index=0, text="Synthetic quote.", section="abstract")], idempotency_key="cli")))
    assert module.main(["--pdf", str(local), "--metadata", str(metadata), "--asset-root", str(tmp_path / "blobs")]) == 1
    output = capsys.readouterr()
    assert not output.out and "PRIVATE" not in output.err and str(tmp_path) not in output.err
    assert json.loads(output.err)["error_code"] == "invalid_pdf_source_url"
    with pytest.raises(AssetError, match="private_asset_root_required"):
        module.private_root(Path(__file__).resolve().parents[2] / "runtime-blobs")
