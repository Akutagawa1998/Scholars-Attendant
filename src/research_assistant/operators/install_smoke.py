"""Verify a non-editable installation with isolated synthetic local data.

The caller supplies a new working directory on the registered private APFS
volume. No provider requests are made. PostgreSQL mode creates and removes a
new, randomly named fixture database using an explicit local administrator
password file; it never uses the configured real DB as the test target.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import hashlib
from importlib import import_module, metadata, resources
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from uuid import uuid4

import httpx
from sqlalchemy import create_engine, inspect, select, func, text
from sqlalchemy.engine import make_url

from research_assistant import __version__, storage as db
from research_assistant.annotation_alignment import align_annotations
from research_assistant.annotation_review import AnnotationReviewService
from research_assistant.asset_service import PdfIngestor, read_source_asset, purge_pending_assets
from research_assistant.assets import ArtifactStore
from research_assistant.local_annotation_resolution import resolve_local_annotation
from research_assistant.service import ResearchService
from research_assistant.storage_paths import select_asset_root, validate_external_storage


OPERATORS = ('sync_zotero_pdfs', 'verify_zotero_pdfs', 'cache_notion_page', 'review_zotero_annotations',
             'configure_knowledge_library')


def require(value):
    if not value:
        raise ValueError('installation_acceptance_mismatch')


def installation():
    distribution = metadata.distribution('scholars-attendant-core')
    require(distribution.version == __version__)
    direct = distribution.read_text('direct_url.json')
    require(not direct or not json.loads(direct).get('dir_info', {}).get('editable'))
    prefix = Path(sys.prefix).resolve()
    for name in ('research_assistant', *(f'research_assistant.operators.{name}' for name in OPERATORS),
                 'research_assistant.pdf_geometry', 'research_assistant.annotation_review',
                 'research_assistant.local_annotation_resolution'):
        module = import_module(name)
        require(Path(module.__file__).resolve().is_relative_to(prefix))
    migrations = sorted(path.name for path in resources.files('research_assistant').joinpath('migrations/versions').iterdir()
                        if path.name.endswith('.py') and path.name[:4].isdigit())
    require([name[:4] for name in migrations] == [f'{index:04d}' for index in range(1, 9)])
    expected = {'research-assistant', 'research-assistant-mcp', 'research-assistant-install-smoke'} | {
        'research-assistant-' + name.replace('_', '-') for name in OPERATORS}
    require(expected <= {entry.name for entry in distribution.entry_points if entry.group == 'console_scripts'})
    for command in expected - {'research-assistant-mcp'}:
        result = subprocess.run([str(prefix/'bin'/command), '--help'], capture_output=True, text=True, timeout=15)
        require(result.returncode == 0 and 'usage:' in result.stdout)
    version = subprocess.run([str(prefix/'bin/research-assistant'), '--version'], capture_output=True, text=True, timeout=10)
    require(version.returncode == 0 and version.stdout.strip() == __version__)
    return dict(version=__version__, non_editable=True, module_origin_inside_environment=True,
                migrations=migrations, operator_commands=len(OPERATORS))


def synthetic_pdf():
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({
        NameObject('/F1'): writer._add_object(font)})})
    content = DecodedStreamObject()
    content.set_data(b'BT /F1 12 Tf 72 720 Td (Synthetic installation quote.) Tj ET')
    page[NameObject('/Contents')] = writer._add_object(content)
    output = io.BytesIO(); writer.write(output)
    return output.getvalue()


def local_v2():
    value, quote = 'Unique preceding context ( 17) unique following context.', '(17)'
    sha = lambda data: hashlib.sha256(data.encode()).hexdigest()
    pdf_hash = sha('synthetic resolver PDF identity')
    page = dict(page_index=0, text=value, text_sha256=sha(value))
    raw = dict(annotationType='highlight', annotationText=quote, annotationPosition=dict(pageIndex=0, rects=[]))
    extraction = dict(source_sha256=pdf_hash, parser_name='pypdf', parser_version=metadata.version('pypdf'), pages=[page])
    original = align_annotations(extraction, [raw], expected_pdf_sha256=pdf_hash)[0]
    capture = dict(raw_annotation=raw, v1_report=original, canonical_page=page, pdf_sha256=pdf_hash,
                   canonical_parser=dict(name='pypdf', version=metadata.version('pypdf')), input_token=sha('synthetic token'))
    result = resolve_local_annotation(capture, {})
    AnnotationReviewService._validate_report(result, capture)
    require(original['match_status'] == 'unmatched' and result['status'] == 'resolved_by_local_text'
            and result['basis'] == 'canonical_literal' and 'geometry' not in result)
    return dict(status=result['status'], basis=result['basis'], literal_hash_verified=True, geometry_claimed=False)


@contextmanager
def fixture_database(runtime, work, postgres, admin_password_file=None):
    if not postgres:
        yield 'sqlite:///' + str(work/'synthetic.sqlite3')
        return
    original = make_url(runtime['database_url'])
    require(original.drivername == 'postgresql+psycopg' and original.host in {'127.0.0.1', 'localhost', '::1'}
            and type(runtime.get('port')) is int and original.port == runtime['port'])
    require(isinstance(admin_password_file, Path) and admin_password_file.is_file()
            and not admin_password_file.is_symlink() and admin_password_file.stat().st_mode & 0o077 == 0)
    secret = admin_password_file.read_text().strip()
    require(bool(secret))
    name = 'ra_install_' + uuid4().hex
    # Provisioning belongs to this explicit operator acceptance command. The
    # application role stays unprivileged; credentials never enter argv/logs.
    admin_url = original.set(database='postgres', username='scholars_admin', password=secret)
    admin = create_engine(admin_url, isolation_level='AUTOCOMMIT')
    created = False
    try:
        with admin.connect() as connection:
            quote = connection.dialect.identifier_preparer.quote
            connection.execute(text('CREATE DATABASE ' + quote(name) + ' OWNER ' + quote(original.username)))
        created = True
        provision = create_engine(admin_url.set(database=name), isolation_level='AUTOCOMMIT')
        try:
            with provision.connect() as connection:
                connection.execute(text('CREATE EXTENSION vector WITH SCHEMA public'))
        finally:
            provision.dispose()
        yield original.set(database=name).render_as_string(hide_password=False)
    finally:
        if created:
            with admin.connect() as connection:
                connection.execute(text('DROP DATABASE ' + connection.dialect.identifier_preparer.quote(name)))
        admin.dispose()


def transport_smoke(url, work, source_id):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0)); port = listener.getsockname()[1]
    service_url = f'http://127.0.0.1:{port}'
    environment = dict(os.environ)
    for key in list(environment):
        if key.startswith('RA_') or key in {'PYTHONPATH', 'PYTHONHOME'}:
            environment.pop(key)
    environment.update(RA_DATABASE_URL=url, RA_API_TOKEN='synthetic-install-token',
                       RA_OWNER_ID='installation-fixture', RA_SCOPE_ID='synthetic')
    process = subprocess.Popen([str(Path(sys.prefix)/'bin/research-assistant'), 'serve', '--port', str(port)],
        cwd=work, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            require(process.poll() is None)
            try:
                if httpx.get(service_url+'/health', timeout=0.2, trust_env=False).is_success:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        else:
            raise ValueError('installation_http_unavailable')
        with httpx.Client(base_url=service_url, trust_env=False) as client:
            require(client.post('/v1/operations/papers.search', json={}).status_code == 401)
            headers = {'Authorization': 'Bearer synthetic-install-token'}
            require(client.get('/openapi.json', headers=headers).json()['info']['version'] == __version__)
            response = client.post('/v1/operations/papers.search', headers=headers, json={}).json()
            require(response['status'] == 'ok' and source_id in json.dumps(response))
        command = subprocess.run([str(Path(sys.prefix)/'bin/research-assistant'), '--service-url', service_url,
            'call', 'papers.search'], input='{}', capture_output=True, text=True, timeout=15, env=environment, cwd=work)
        require(command.returncode == 0 and source_id in command.stdout)

        async def mcp_smoke():
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            params = StdioServerParameters(command=str(Path(sys.prefix)/'bin/research-assistant-mcp'),
                env=dict(RA_SERVICE_URL=service_url, RA_API_TOKEN='synthetic-install-token',
                         PYTHONDONTWRITEBYTECODE='1', TMPDIR=str(work)))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    require('papers_search' in {tool.name for tool in tools.tools})
                    result = await session.call_tool('papers_search', {'payload': {}})
                    require(not result.isError and source_id in json.dumps(result.structuredContent))
        asyncio.run(mcp_smoke())
        return dict(http_authenticated=True, http_unauthenticated_denied=True, cli_shared_state=True,
                    official_mcp_sdk_shared_state=True)
    finally:
        process.terminate()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(timeout=5)


def smoke(runtime, work, *, postgres=False, admin_password_file=None):
    installed = installation()
    with fixture_database(runtime, work, postgres, admin_password_file) as url:
        service = ResearchService(url, owner_id='installation-fixture', scope_id='synthetic')
        try:
            service.initialize(); service.initialize()
            with service.storage.engine.connect() as connection:
                require(connection.scalar(text('SELECT version_num FROM alembic_version')) == '0008')
                require({'source_assets', 'attachment_asset_links', 'annotation_alignments',
                         'annotation_alignment_resolutions', 'catalog_defaults', 'catalog_versions',
                         'catalog_bindings', 'catalog_aliases'} <= set(inspect(connection).get_table_names()))
                vector = connection.scalar(text("SELECT extversion FROM pg_extension WHERE extname='vector'")) if postgres else None
            with ArtifactStore(work/'synthetic-blobs') as store:
                raw = synthetic_pdf()
                importer = PdfIngestor(service, store)
                options = dict(identifier='synthetic:installation:v1', title='Synthetic installation paper',
                    version='v1', passages=[dict(page_index=0, text='Synthetic installation quote.', section='body')],
                    idempotency_key='synthetic-install-import')
                first = importer.ingest(raw, **options)
                require(first == importer.ingest(raw, **options))
                source_id = first['data']['source_snapshot_id']
                require(read_source_asset(service, store, source_id) == raw)
                transports = transport_smoke(url, work, source_id)
                with service.storage.transaction() as connection:
                    require(connection.scalar(select(func.count()).select_from(db.reading_sessions)) == 0)
                forget = service.execute('memory.forget', dict(target_ids=[source_id],
                    scope='memory_and_source_snapshots', idempotency_key='synthetic-install-forget'))
                require(forget['status'] == 'ok' and purge_pending_assets(service, store)['purged'] == 1)
                require(not list((work/'synthetic-blobs').rglob('*.pdf')))
            return dict(status='ok', installation=installed, database='postgresql' if postgres else 'sqlite',
                schema_revision='0008', pgvector_extension_version=vector, synthetic_pdf_import_read_forget_purge=True,
                synthetic_pdf_receipt_reused=True, reading_sessions=0, v2=local_v2(), transports=transports,
                provider_calls=0, llm_calls=0, real_library_modified=False)
        finally:
            service.storage.engine.dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-config', type=Path, default=Path.home()/'.scholars-attendant/runtime/postgres.json')
    parser.add_argument('--work-directory', required=True, type=Path)
    parser.add_argument('--postgres', action='store_true', help='Create and remove an isolated synthetic PostgreSQL database')
    parser.add_argument('--postgres-admin-password-file', type=Path,
                        help='Local fixture provisioning only; defaults to runtime-config sibling admin-password')
    args = parser.parse_args(argv)
    try:
        root = select_asset_root(None, args.runtime_config, environ={})
        runtime = json.loads(args.runtime_config.read_text())
        validate_external_storage(root, runtime['asset_storage'])
        if args.postgres:
            storage = runtime.get('postgres_storage', {})
            require(storage.get('kind') == 'bind')
            validate_external_storage(storage.get('data_dir'), storage)
        mount = Path(runtime['asset_storage']['mount_point']).resolve()
        work = args.work_directory
        require(work.is_absolute() and not work.exists() and work.resolve().is_relative_to(mount)
                and work.resolve() != mount and all(not parent.is_symlink() for parent in (work, *work.parents)))
        work.mkdir(parents=True, mode=0o700)
        result = smoke(runtime, work, postgres=args.postgres,
                       admin_password_file=args.postgres_admin_password_file or args.runtime_config.parent/'admin-password')
        print(json.dumps(result))
        return 0
    except Exception as error:
        print(json.dumps(dict(status='failed', error_code='installation_acceptance_unavailable',
                              error_type=type(error).__name__)), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
