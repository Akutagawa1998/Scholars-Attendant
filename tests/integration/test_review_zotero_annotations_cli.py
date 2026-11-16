"""Bounded local review orchestration uses real synthetic canonical storage."""
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import sys
from types import ModuleType
from uuid import uuid4

import pytest
from sqlalchemy import select, func

from research_assistant import storage as db
from research_assistant.annotation_review import AnnotationReviewService
from research_assistant.service import DomainError, canonical
from research_assistant.zotero_assets import ZoteroPdfIngestor
from test_assets import pdf
from test_zotero_assets import fixture, pg_url, metadata, ATTACHMENT, ANNOTATION, PARENT


@pytest.fixture
def runner(fixture, tmp_path, monkeypatch):
    service, store, *_ = fixture
    raw = pdf('PRIVATE-SYNTHETIC-QUOTE. PRIVATE-SYNTHETIC-QUOTE.', 'Other synthetic page.')
    md5 = hashlib.md5(raw, usedforsecurity=False).hexdigest()
    snapshot = metadata(service, ATTACHMENT, 'attachment', version=23, parentItem=PARENT,
        contentType='application/pdf', linkMode='imported_file', md5=md5)
    metadata(service, ANNOTATION, 'annotation', version=23, parentItem=ATTACHMENT, annotationType='highlight',
        annotationText='PRIVATE-SYNTHETIC-QUOTE.', annotationComment='PRIVATE-COMMENT',
        annotationPosition=json.dumps(dict(pageIndex=0, rects=[[0, 0, 10, 10]])))
    metadata(service, 'ANNOT002', 'annotation', version=23, parentItem=ATTACHMENT, annotationType='highlight',
        annotationText='PRIVATE-UNMATCHED-QUOTE', annotationComment='PRIVATE-COMMENT',
        annotationPosition=json.dumps(dict(pageIndex=1, rects=[[0, 0, 10, 10]])))
    ZoteroPdfIngestor(service, store).ingest(raw, attachment_snapshot_id=snapshot, provenance=dict(
        attachment_key=ATTACHMENT, attachment_version=23, attachment_md5=md5))
    calls = {'geometry': [], 'resolve': [], 'network': []}

    def resolve(capture, geometry):
        calls['resolve'].append(capture['alignment_id'])
        assert geometry['source_sha256'] == capture['pdf_sha256']
        params = dict(engine='pdfium', engine_version='1.0', coordinate_system='pdf_points_bottom_left')
        if capture['v1_report']['match_status'] == 'ambiguous':
            candidate = capture['v1_report']['matches'][1]
            return dict(status='resolved_by_geometry', method='geometry_overlap', basis='geometry_unique',
                parameters=params, selected_spans=[{key: candidate[key] for key in
                    ('text_start', 'text_end', 'literal_sha256')}], geometry=dict(candidate_index=1, overlap=0.9))
        return dict(status='unresolved', method='none', basis='no_unique_match', parameters=params, selected_spans=[])

    # The resolver has its own pure-module tests; isolate orchestration here.
    stub = ModuleType('research_assistant.local_annotation_resolution')
    stub.ALGORITHM_VERSION = 'zotero-local-review-v2'
    stub.resolve_local_annotation = resolve
    monkeypatch.setitem(sys.modules, stub.__name__, stub)
    path = Path(__file__).resolve().parents[2] / 'src/research_assistant/operators' / 'review_zotero_annotations.py'
    spec = importlib.util.spec_from_file_location('synthetic_review_cli_' + uuid4().hex, path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    def geometry(data, pages, *, python_executable):
        assert data == raw
        calls['geometry'].append((pages, python_executable))
        return dict(source_sha256=hashlib.sha256(data).hexdigest(),
            pages=[dict(page_index=index, private='PRIVATE-GEOMETRY-TEXT') for index in pages],
            engine=dict(name='pypdfium2', version='1.0', pdfium_version='1.0'),
            limits=dict(cpu_seconds=25, wall_seconds=30, rss_bytes=1024**3, memory_enforcement='parent_rss_monitor'))

    def network(*args, **kwargs):
        calls['network'].append(True)
        raise AssertionError('No provider calls are authorized by local review')

    monkeypatch.setattr(cli, 'extract_geometry', geometry)
    monkeypatch.setattr(socket, 'create_connection', network)
    # PG uses an already-open fixture connection pool; the CLI starts no provider.
    return cli, fixture, calls


def resolution_count(service):
    with service.storage.transaction() as c:
        return c.scalar(select(func.count()).select_from(db.annotation_alignment_resolutions))


def test_local_review_preserves_baseline_reads_saved_versions_and_reuses_repeat(runner):
    cli, fixture, calls = runner
    service, store, *_ = fixture
    before = cli.baseline(service)
    with service.storage.transaction() as c:
        original = [dict(row) for row in c.execute(select(db.annotation_alignments)).mappings()]
    result = cli.review(service, store, Path('/synthetic/installed/python'), max_cases=6)
    assert result['reviewed'] == 2 and result['resolution_counts'] == {'resolved_by_geometry': 1, 'unresolved': 1}
    assert result['source_baseline'] == before == cli.baseline(service)
    assert result['repeat_writes_reused'] == 2 and resolution_count(service) == 2
    assert len(calls['geometry']) == 1 and calls['geometry'][0][0] == [0, 1]
    assert len(calls['resolve']) == 2 and calls['network'] == []
    assert result['provider_calls'] == result['notion_calls'] == result['llm_calls'] == 0
    assert result['remote_write'] is False
    with service.storage.transaction() as c:
        assert [dict(row) for row in c.execute(select(db.annotation_alignments)).mappings()] == original
    operator = AnnotationReviewService(service, store)
    for item in result['results']:
        rows = operator.read(item['alignment_id'])
        assert len(rows) == 1 and rows[0]['review_id'] == item['review_id']
    repeated = cli.review(service, store, Path('/synthetic/installed/python'), max_cases=6)
    assert all(item['first_write_reused'] and item['repeat_reused'] for item in repeated['results'])
    assert resolution_count(service) == 2 and cli.baseline(service) == before
    serialized = canonical(result)
    assert 'PRIVATE' not in serialized and 'literal_text' not in serialized and '/synthetic/installed' not in serialized


def test_max_cases_bounds_capture_resolution_and_requested_geometry_pages(runner):
    cli, fixture, calls = runner
    result = cli.review(fixture[0], fixture[1], Path('/synthetic/installed/python'), max_cases=1)
    assert result['reviewed'] == 1 and len(calls['resolve']) == 1
    assert len(calls['geometry']) == 1 and len(calls['geometry'][0][0]) == 1
    assert resolution_count(fixture[0]) == 1


@pytest.mark.parametrize('maximum', [0, 21, True, 1.5])
def test_invalid_bound_fails_before_geometry_or_review_writes(runner, maximum):
    cli, fixture, calls = runner
    with pytest.raises(ValueError):
        cli.review(fixture[0], fixture[1], Path('/synthetic/installed/python'), max_cases=maximum)
    assert calls['geometry'] == [] and calls['resolve'] == [] and resolution_count(fixture[0]) == 0


def test_baseline_change_is_detected_instead_of_reporting_success(runner, monkeypatch):
    cli, fixture, _ = runner
    original = cli.baseline
    count = []
    def changed(service):
        baseline = original(service)
        count.append(True)
        if len(count) > 1:
            baseline['sources']['sha256'] = '0' * 64
        return baseline
    monkeypatch.setattr(cli, 'baseline', changed)
    with pytest.raises(ValueError):
        cli.review(fixture[0], fixture[1], Path('/synthetic/installed/python'), max_cases=1)


def test_private_report_fields_are_refused_before_any_write(runner, monkeypatch):
    cli, fixture, _ = runner
    original = cli.resolve_local_annotation
    def unsafe(capture, geometry):
        result = original(capture, geometry)
        result['quote'] = 'PRIVATE-UNMANAGED-COPY'
        return result
    monkeypatch.setattr(cli, 'resolve_local_annotation', unsafe)
    with pytest.raises(DomainError):
        cli.review(fixture[0], fixture[1], Path('/synthetic/installed/python'), max_cases=1)
    assert resolution_count(fixture[0]) == 0


def test_main_writes_only_safe_json_on_registered_report_path(runner, tmp_path, monkeypatch, capsys):
    cli, fixture, _ = runner
    service = fixture[0]
    backing = tmp_path / 'synthetic-backing'
    backing.mkdir()
    config = tmp_path / 'runtime.json'
    config.write_text(json.dumps(dict(database_url=service.storage.engine.url.render_as_string(hide_password=False),
        owner_id=service.owner_id, scope_id=service.scope_id,
        asset_storage=dict(backing_image_path=str(backing / 'ResearchData.sparsebundle')))))
    config.chmod(0o600)
    monkeypatch.setattr(cli, 'select_asset_root', lambda *a, **k: tmp_path / 'blobs')
    monkeypatch.setattr(cli, 'validate_external_storage', lambda *a, **k: tmp_path / 'blobs')
    monkeypatch.setattr(cli, 'ResearchService', lambda *a, **k: service)
    report_path = backing / 'safe-report.json'
    result = cli.main(['--runtime-config', str(config), '--geometry-python', '/synthetic/installed/python',
                      '--max-cases', '1', '--report', str(report_path)])
    captured = capsys.readouterr()
    assert result == 0 and not captured.err, captured.err
    stdout = json.loads(captured.out)
    assert stdout == json.loads(report_path.read_text()) and stdout['reviewed'] == 1
    assert 'PRIVATE' not in captured.out and 'PRIVATE' not in report_path.read_text()


def test_main_sanitizes_parser_failure_without_echoing_source_or_path(runner, tmp_path, monkeypatch, capsys):
    cli, fixture, _ = runner
    config = tmp_path / 'runtime.json'
    config.write_text('{}')
    def unavailable(*args, **kwargs):
        raise RuntimeError('PRIVATE-QUOTE /PRIVATE/PDF.pdf')
    monkeypatch.setattr(cli, 'select_asset_root', unavailable)
    result = cli.main(['--runtime-config', str(config), '--geometry-python', '/PRIVATE/python'])
    captured = capsys.readouterr()
    assert result == 1 and captured.out == ''
    assert json.loads(captured.err) == dict(status='failed', error_code='annotation_review_unavailable', error_type='RuntimeError')
    assert 'PRIVATE' not in captured.err
