"""Review a bounded set of existing ambiguous/unmatched annotations locally.

Uses the registered external-store PDF bytes and an explicitly supplied, already installed
PDFium Python runtime. No installation, provider call, prose generation, source
ingestion or reading-state mutation. Original v1 alignment reports stay intact.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import sys

from sqlalchemy import select

from research_assistant import storage as db
from research_assistant.annotation_review import AnnotationReviewService
from research_assistant.asset_service import read_source_asset
from research_assistant.assets import ArtifactStore
from research_assistant.local_annotation_resolution import ALGORITHM_VERSION, resolve_local_annotation
from research_assistant.pdf_geometry import extract_geometry
from research_assistant.service import ResearchService, canonical
from research_assistant.storage_paths import select_asset_root, validate_external_storage

def require(value):
    if not value:
        raise ValueError('annotation_review_acceptance_mismatch')


def baseline(service):
    """Hash original reports and source state without emitting private text."""
    state = {}
    with service.storage.transaction() as c:
        service._epoch(c)
        for name in ('annotation_alignments', 'source_objects', 'connector_cursors',
                     'sources', 'evidence', 'reading_sessions', 'versions'):
            table = getattr(db, name)
            rows = [dict(row) for row in c.execute(select(table).where(service._scope(table))).mappings()]
            state[name] = dict(count=len(rows), sha256=hashlib.sha256(
                canonical(sorted(rows, key=lambda row: row['id'])).encode()).hexdigest())
    return state


def review(service, store, geometry_python, *, max_cases=6):
    require(type(max_cases) is int and 1 <= max_cases <= 20)
    before = baseline(service)
    operator = AnnotationReviewService(service, store)
    with service.storage.transaction() as c:
        rows = list(c.execute(select(db.annotation_alignments).where(service._scope(db.annotation_alignments))
            .order_by(db.annotation_alignments.c.id)).mappings())
    ids = [row['id'] for row in rows if row['report'].get('match_status') in {'ambiguous', 'unmatched'}][:max_cases]
    captures = [operator.capture(identity) for identity in ids]
    require(captures)
    pages_by_source = defaultdict(set)
    for capture in captures:
        require(capture['canonical_page'] is not None)
        pages_by_source[capture['pdf_source_id']].add(capture['canonical_page']['page_index'])
    geometries = {}
    for source_id, indices in pages_by_source.items():
        raw = read_source_asset(service, store, source_id)
        geometries[source_id] = extract_geometry(raw, sorted(indices), python_executable=geometry_python)
    counts = Counter()
    results = []
    for capture in captures:
        report = resolve_local_annotation(capture, geometries[capture['pdf_source_id']])
        first = operator.record(capture['alignment_id'], report, algorithm_version=ALGORITHM_VERSION,
                                expected_capture=capture)
        repeated = operator.record(capture['alignment_id'], report, algorithm_version=ALGORITHM_VERSION,
                                   expected_capture=capture)
        require(repeated['deduplicated'] and repeated['review_id'] == first['review_id'])
        saved = operator.read(capture['alignment_id'])
        require(any(row['review_id'] == first['review_id'] and row['report']['status'] == report['status'] for row in saved))
        counts[report['status']] += 1
        results.append(dict(alignment_id=capture['alignment_id'], review_id=first['review_id'],
            original_status=capture['v1_report']['match_status'], status=report['status'],
            method=report['method'], basis=report['basis'], page_index=capture['canonical_page']['page_index'],
            first_write_reused=first['deduplicated'], repeat_reused=repeated['deduplicated'],
            selected_spans=report['selected_spans'], geometry=report.get('geometry'),
            parameters=report['parameters'], input_fingerprint=first['input_fingerprint']))
    require(baseline(service) == before)
    return dict(status='ok', algorithm_version=ALGORITHM_VERSION, reviewed=len(results),
        resolution_counts=dict(counts), results=results, original_reports_and_source_state_unchanged=True,
        source_baseline=before, pdfs_checked=len(geometries),
        geometry_pages_checked=sum(len(value['pages']) for value in geometries.values()),
        geometry_engines=[value['engine'] for value in geometries.values()],
        geometry_resource_limits=[value['limits'] for value in geometries.values()],
        repeat_writes_reused=len(results), provider_calls=0, llm_calls=0,
        notion_calls=0, remote_write=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-config', type=Path, default=Path.home()/'.scholars-attendant/runtime/postgres.json')
    parser.add_argument('--geometry-python', required=True, type=Path)
    parser.add_argument('--max-cases', type=int, default=6)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    service = None
    try:
        root = select_asset_root(None, args.runtime_config, environ={})
        runtime = json.loads(args.runtime_config.read_text())
        validate_external_storage(root, runtime['asset_storage'])
        if args.report is not None:
            backing_root = Path(runtime['asset_storage']['backing_image_path']).parent
            require(args.report.is_absolute() and args.report.resolve().is_relative_to(backing_root.resolve()))
        service = ResearchService(runtime['database_url'], runtime.get('owner_id', 'local'), runtime.get('scope_id', 'private'))
        service.initialize()
        with ArtifactStore(root) as store:
            result = review(service, store, args.geometry_python, max_cases=args.max_cases)
        if args.report is not None:
            # Safe count/hash reports still belong to the operator's registered disk.
            with open(args.report, 'w', encoding='utf-8', opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
                output.write(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result))
        return 0
    except Exception as error:
        print(json.dumps(dict(status='failed', error_code='annotation_review_unavailable',
                              error_type=type(error).__name__)), file=sys.stderr)
        return 1
    finally:
        if service is not None:
            service.storage.engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
