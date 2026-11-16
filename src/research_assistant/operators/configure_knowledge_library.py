"""Configure verified source libraries and bind existing cached paper metadata.

The operator supplies links already verified outside this command. No remote
request, body refresh, summary, translation or reading assertion occurs here.
An unversioned identifier remains unknown; catalog.bind owns identity parsing.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from sqlalchemy import func, select

from research_assistant import storage as db
from research_assistant.assets import AssetError
from research_assistant.service import DomainError, ResearchService, canonical, digest, timestamp
from research_assistant.storage_paths import select_asset_root, validate_external_storage

PAPER_TYPES = frozenset({'preprint', 'journalArticle', 'conferencePaper'})
OTHER_PUBLICATIONS = frozenset({'book', 'bookSection', 'document'})
_NONPAPERS = frozenset({'attachment', 'note', 'annotation', 'webpage'})
_SAFE_ERRORS = frozenset({
    'catalog_source_unavailable', 'invalid_catalog_path', 'invalid_catalog_configuration',
    'unverified_catalog_link', 'catalog_not_configured', 'catalog_not_paper_metadata',
    'catalog_title_missing', 'forgotten', 'not_found', 'idempotency_conflict',
    'revision_conflict', 'invalid_request', 'external_storage_unavailable',
    'external_storage_mismatch', 'storage_configuration_unavailable',
})


class KnowledgeLibraryError(Exception):
    """A fixed, content/credential-free operator error."""


def _require(value, code):
    if not value:
        raise KnowledgeLibraryError(code)


def _safe_code(error):
    code = getattr(error, 'code', None)
    return code if code in _SAFE_ERRORS else 'catalog_bind_unavailable'


def _counts(service, connection):
    return {label: connection.scalar(select(func.count()).select_from(table).where(service._scope(table)))
            for label, table in (
                ('papers', db.works), ('paper_versions', db.versions),
                ('catalog_bindings', db.catalog_bindings), ('sources', db.sources),
                ('reading_sessions', db.reading_sessions),
            )}


def _registration(service, connection, provider, namespace, registration_id):
    query = select(db.connector_registrations).where(
        service._scope(db.connector_registrations),
        db.connector_registrations.c.provider == provider,
        db.connector_registrations.c.namespace == namespace,
    )
    if registration_id is not None:
        query = query.where(db.connector_registrations.c.id == registration_id)
    rows = list(connection.execute(query).mappings())
    _require(len(rows) == 1, 'knowledge_library_registration_required')
    return dict(rows[0])


def configure_cached_library(service, *, notion_url, zotero_url, verified_at,
                             notion_namespace, zotero_namespace,
                             notion_registration_id=None, zotero_registration_id=None):
    """Bind only current, authorized heads; report safe aggregate outcomes.

    Each domain call rechecks head/access/hash fences after this selection.
    Book/bookSection/document are counted separately and deliberately excluded
    from the initial three-type paper catalog. No source rows are imported.
    """
    try:
        checked_at = timestamp(verified_at)
    except (TypeError, ValueError, AttributeError):
        raise KnowledgeLibraryError('knowledge_library_verification_time_invalid') from None
    with service.storage.transaction() as c:
        service._epoch(c)
        registrations = [
            _registration(service, c, 'notion', notion_namespace, notion_registration_id),
            _registration(service, c, 'zotero', zotero_namespace, zotero_registration_id),
        ]
        before = _counts(service, c)
        old_receipts = set(c.execute(select(db.events.c.idempotency_key).where(
            service._scope(db.events), db.events.c.operation.in_(['catalog.configure', 'catalog.bind']))).scalars())
        rows = list(c.execute(select(
            db.source_objects.c.id.label('object_id'), db.source_objects.c.identity_hash,
            db.source_objects.c.registration_id, db.sources.c.id.label('source_snapshot_id'),
            db.sources.c.content, db.sources.c.sha256,
        ).join(db.sources, db.sources.c.id == db.source_objects.c.head_source_version_id).where(
            service._scope(db.source_objects), service._scope(db.sources),
            db.source_objects.c.registration_id.in_([r['id'] for r in registrations]),
            db.source_objects.c.access_state == 'accessible', db.sources.c.access_state == 'accessible',
        )).mappings())
        selected, selection, selection_errors = [], Counter(), Counter()
        providers = {r['id']: r['provider'] for r in registrations}
        for row in rows:
            if (service._blocked(c, identity=row['object_id'], fingerprint=row['identity_hash'])
                    or service._blocked(c, identity=row['source_snapshot_id'], fingerprint=row['sha256'])):
                selection['suppressed_heads_skipped'] += 1
                continue
            provider = providers[row['registration_id']]
            if provider == 'notion':
                kind = 'page'
            else:
                try:
                    payload = json.loads(row['content'])
                    data = payload.get('data')
                    _require(isinstance(data, dict), 'knowledge_library_metadata_invalid')
                    if data.get('deleted'):
                        selection['zotero_deleted_heads_skipped'] += 1
                        continue
                    kind = data.get('itemType')
                    _require(isinstance(kind, str), 'knowledge_library_metadata_invalid')
                except (ValueError, TypeError, AttributeError, KnowledgeLibraryError):
                    selection_errors['cached_metadata_invalid'] += 1
                    continue
                if kind in OTHER_PUBLICATIONS:
                    selection['other_publications_skipped_' + kind] += 1
                    continue
                if kind not in PAPER_TYPES:
                    selection['zotero_nonpapers_skipped_' + (kind if kind in _NONPAPERS else 'other')] += 1
                    continue
            selection[provider + '_' + kind + '_selected'] += 1
            selected.append(dict(source_snapshot_id=row['source_snapshot_id'], provider=provider, kind=kind))
    registration_ids = sorted(r['id'] for r in registrations)
    links = [dict(provider=r['provider'], namespace=r['namespace'], checked_at=checked_at,
                  url=notion_url if r['provider'] == 'notion' else zotero_url, valid=True) for r in registrations]
    configure_key = 'catalog-configure:' + digest(canonical([registration_ids, checked_at, links]))
    configured = service.execute('catalog.configure', dict(registration_ids=registration_ids,
        initial_read_status='unread', verified_links=links, idempotency_key=configure_key), trusted_capture=True)
    _require(configured.get('status') == 'ok' and configured.get('data', {}).get('configured') is True,
             'knowledge_library_configuration_failed')
    outcomes, errors = Counter(), selection_errors.copy()
    identity_states, version_states, by_provider = Counter(), Counter(), Counter()
    for item in sorted(selected, key=lambda row: (row['provider'] != 'zotero', row['source_snapshot_id'])):
        key = 'catalog-bind:' + digest(canonical([registration_ids, checked_at, item['source_snapshot_id']]))
        try:
            response = service.execute('catalog.bind', dict(source_snapshot_id=item['source_snapshot_id'],
                idempotency_key=key), trusted_capture=True)
            data = response.get('data', {})
            _require(response.get('status') == 'ok' and data.get('reading_mutated') is False
                     and data.get('initial_read_status') == 'unread'
                     and data.get('identity_state') in {'strong', 'provisional', 'conflict'}
                     and data.get('scientific_version_state') in {'explicit', 'unknown'},
                     'knowledge_library_bind_result_invalid')
            reused = digest(key) in old_receipts or data.get('deduplicated') is True
            outcomes['reused' if reused else 'bound'] += 1
            by_provider[item['provider'] + '_processed'] += 1
            identity_states[data['identity_state']] += 1
            version_states[data['scientific_version_state']] += 1
        except DomainError as error:
            errors[_safe_code(error)] += 1
    with service.storage.transaction() as c:
        service._epoch(c)
        after = _counts(service, c)
    _require(after['sources'] == before['sources'] and after['reading_sessions'] == before['reading_sessions'],
             'knowledge_library_source_or_reading_changed')
    return dict(status='partial' if errors else 'ok', configured=True, initial_read_status='unread',
        registration_ids=registration_ids, verified_at=checked_at, selection=dict(selection),
        outcomes=dict(outcomes), processed_by_provider=dict(by_provider), errors=dict(errors),
        identity_states=dict(identity_states), scientific_version_states=dict(version_states),
        before=before, after=after, added={key: after[key] - before[key] for key in before},
        remote_calls=0, remote_write=False, llm_calls=0, fulltext_fetches=0, reading_mutated=False,
        unsupported_publication_types=['book', 'bookSection', 'document'])


def _runtime(path):
    try:
        runtime = json.loads(path.read_text(encoding='utf-8'))
        _require(isinstance(runtime, dict) and isinstance(runtime.get('database_url'), str)
                 and runtime['database_url'].startswith('postgresql+psycopg:'),
                 'knowledge_library_runtime_invalid')
        root = select_asset_root(None, path, environ={})
        validate_external_storage(root, runtime['asset_storage'])
        storage = runtime['postgres_storage']
        _require(storage.get('kind') == 'bind', 'knowledge_library_runtime_invalid')
        validate_external_storage(Path(storage['data_dir']), storage)
        artifacts = Path(runtime['asset_storage']['backing_image_path']).parent / 'artifacts'
        return runtime, artifacts
    except (AssetError, KnowledgeLibraryError):
        raise
    except Exception:
        raise KnowledgeLibraryError('knowledge_library_runtime_invalid') from None


def _report_path(path, artifacts):
    _require(path.is_absolute() and '..' not in path.parts and path.is_relative_to(artifacts)
             and path != artifacts and not any(p.is_symlink() for p in (path, *path.parents)),
             'knowledge_library_report_path_invalid')
    return path


def _write_report(path, result):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name('.' + path.name + '.' + uuid4().hex + '.tmp')
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            output.write(json.dumps(result, sort_keys=True, indent=2) + '\n')
            output.flush(); os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-config', required=True, type=Path)
    parser.add_argument('--notion-url', required=True)
    parser.add_argument('--zotero-url', required=True)
    parser.add_argument('--verified-at', required=True)
    parser.add_argument('--notion-namespace', required=True, help='Namespace of the registered Notion collection')
    parser.add_argument('--zotero-namespace', required=True, help='Namespace of the registered Zotero library')
    parser.add_argument('--notion-registration-id')
    parser.add_argument('--zotero-registration-id')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    service = None
    try:
        runtime, artifacts = _runtime(args.runtime_config)
        filename = 'knowledge-library-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8] + '.json'
        report_path = _report_path(args.report or artifacts / filename, artifacts)
        service = ResearchService(runtime['database_url'], runtime.get('owner_id', 'local'), runtime.get('scope_id', 'private'))
        result = configure_cached_library(service, notion_url=args.notion_url, zotero_url=args.zotero_url,
            verified_at=args.verified_at, notion_namespace=args.notion_namespace, zotero_namespace=args.zotero_namespace,
            notion_registration_id=args.notion_registration_id, zotero_registration_id=args.zotero_registration_id)
        _write_report(report_path, result)
        print(json.dumps(result, sort_keys=True))
        return 0 if result['status'] == 'ok' else 2
    except Exception as error:
        code = _safe_code(error) if isinstance(error, (AssetError, DomainError)) else (
            str(error) if isinstance(error, KnowledgeLibraryError) else 'knowledge_library_unavailable')
        print(json.dumps(dict(status='failed', error_code=code)), file=sys.stderr)
        return 1
    finally:
        if service is not None:
            service.storage.engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
