"""Conservative, source-authorized catalog over existing registered snapshots.

Catalog versions are discovery records. Unknown is an explicit absence of a
scientific version, and a note's original-paper link does not turn its prose or
an attached PDF into that scientific version. No reading assertion is created.
"""
from __future__ import annotations

import json
import re
from urllib.parse import unquote, urlsplit

from sqlalchemy import select, update

from . import storage as db

_DOI = re.compile(r'10\.\d{4,9}/[^\s<>"?#]+\Z', re.I)
_ARXIV = re.compile(r'(?P<base>(?:\d{2}(?:0[1-9]|1[0-2])\.\d{4,5}|[a-z][a-z.-]*(?:\.[A-Z]{2})?/\d{7}))(?P<version>v[1-9]\d*)?\Z', re.I)
_PAPER_TYPES = frozenset({'journalArticle', 'conferencePaper', 'preprint'})


def _fail(code='catalog_source_unavailable', status='forbidden'):
    from .service import DomainError
    return DomainError(status, code, 'Catalog input is unavailable, unverified or changed')


def _json(value):
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError('duplicate_key')
            result[key] = item
        return result
    return json.loads(value, object_pairs_hook=unique)


def _at(raw, path):
    if (not isinstance(path, list) or not 1 <= len(path) <= 16
            or any(type(part) not in (str, int) or (type(part) is int and part < 0) for part in path)):
        raise _fail('invalid_catalog_path', 'failed')
    value = raw
    try:
        for part in path:
            if type(part) is str and isinstance(value, dict):
                value = value[part]
            elif type(part) is int and isinstance(value, list):
                value = value[part]
            else:
                return None
    except (KeyError, IndexError):
        return None
    return value


def parse_identity(value):
    """Parse a whole identifier or canonical DOI/arXiv URL, never body prose."""
    if not isinstance(value, str) or not 0 < len(value) <= 2000:
        return []
    value = value.strip()
    if value.lower().startswith(('http://', 'https://')):
        try:
            url = urlsplit(value)
            port = url.port
        except ValueError:
            return []
        if (url.username or url.password or url.query or url.fragment
                or port not in {None, 443 if url.scheme.lower() == 'https' else 80}):
            return []
        if url.hostname in {'doi.org', 'dx.doi.org', 'www.doi.org'}:
            value = unquote(url.path.lstrip('/'))
        elif url.hostname in {'arxiv.org', 'www.arxiv.org', 'export.arxiv.org'}:
            match = re.fullmatch(r'/(?:abs|pdf)/(.+?)(?:\.pdf)?', url.path)
            if not match:
                return []
            value = unquote(match[1])
        else:
            return []
    value = re.sub(r'^(?:doi|arxiv)\s*:\s*', '', value, flags=re.I)
    claims = []
    if _DOI.fullmatch(value):
        claims.append(dict(scheme='doi', value=value.lower(), scientific_version=None))
        # This DOI namespace explicitly encodes an arXiv identifier.
        value = value[len('10.48550/arxiv.'):] if value.lower().startswith('10.48550/arxiv.') else ''
    match = _ARXIV.fullmatch(value)
    if match:
        claims.append(dict(scheme='arxiv', value=match['base'].lower(), scientific_version=match['version']))
    return claims


def _native_paper_property(raw):
    from .service import digest
    value = raw.get('text')
    if not isinstance(value, str):
        return []
    opening, closing = value.find('<properties>'), value.find('</properties>')
    if opening < 0 or closing < opening or opening > 65536 or closing - opening > 65536:
        return []
    if value.find('<properties>', opening + 1) >= 0:
        return []
    if any(0 <= value.find(tag) < opening for tag in ('<content', '<body')):
        return []
    start, end = opening + len('<properties>'), closing
    try:
        properties = _json(value[start:end])
    except (ValueError, RecursionError):
        return []
    field = properties.get('Paper URL') if isinstance(properties, dict) else None
    return [(field, dict(kind='native_properties_json', json_path=['text'], text_start=start, text_end=end,
                         property='Paper URL', literal_sha256=digest(value[start:end])))] if isinstance(field, str) else []


def _claim_value(raw, proof):
    from .service import digest
    value = _at(raw, proof.get('json_path'))
    if not isinstance(value, str):
        return None
    if proof.get('kind') == 'native_properties_json':
        start, end = proof.get('text_start'), proof.get('text_end')
        if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(value)
                or digest(value[start:end]) != proof.get('literal_sha256') or proof.get('property') != 'Paper URL'):
            return None
        # Re-derive the restricted header, not an arbitrary JSON fragment in body.
        candidates = _native_paper_property(raw)
        return candidates[0][0] if len(candidates) == 1 and candidates[0][1] == proof else None
    return value if proof.get('kind') == 'json' and digest(value) == proof.get('literal_sha256') else None


def _claims(raw, provider, paths):
    from .service import digest
    if paths is None:
        paths = ([['data', field] for field in ('DOI', 'url', 'archiveID')] if provider == 'zotero'
                 else [[field] for field in ('paper_url', 'doi', 'arxiv_id')])
    if not isinstance(paths, list) or len(paths) > 16:
        raise _fail('invalid_catalog_path', 'failed')
    fields = []
    for path in paths:
        value = _at(raw, path)
        if isinstance(value, str):
            fields.append((value, dict(kind='json', json_path=path, literal_sha256=digest(value))))
    if provider == 'notion':
        fields.extend(_native_paper_property(raw))
    if provider == 'zotero':
        extra = _at(raw, ['data', 'extra'])
        if isinstance(extra, str) and len(extra) <= 65536:
            # A typed Extra line is a source claim. Unlabelled references do not count.
            for line in extra.splitlines():
                if re.fullmatch(r'\s*(?:DOI|arXiv)\s*:\s*\S+\s*', line, re.I):
                    fields.append((line.strip(), dict(kind='extra_line', json_path=['data', 'extra'],
                        literal_sha256=digest(extra), line_sha256=digest(line))))
    result = []
    for value, proof in fields:
        for claim in parse_identity(value):
            entry = dict(claim, proof=proof)
            if entry not in result:
                result.append(entry)
    return result


class CatalogOperations:
    def _catalog_default(self, c):
        return c.execute(select(db.catalog_defaults).where(self._scope(db.catalog_defaults),
            db.catalog_defaults.c.name == 'default')).mappings().first()

    def _catalog_configure(self, c, p, now):
        from .service import envelope, timestamp
        registrations = p.get('registration_ids')
        if (not isinstance(registrations, list) or not 1 <= len(registrations) <= 20
                or any(not isinstance(identity, str) for identity in registrations)
                or p.get('initial_read_status', 'unread') != 'unread'):
            raise _fail('invalid_catalog_configuration', 'failed')
        rows = [self._row(c, db.connector_registrations, identity) for identity in sorted(set(registrations))]
        links = p.get('verified_links')
        if not isinstance(links, list) or len(links) != len(rows):
            raise _fail('invalid_catalog_configuration', 'failed')
        safe = []
        for row in rows:
            matches = [link for link in links if isinstance(link, dict) and link.get('provider') == row['provider']
                       and link.get('namespace') == row['namespace']]
            if len(matches) != 1:
                raise _fail('invalid_catalog_configuration', 'failed')
            link = matches[0]
            if set(link) != {'provider', 'namespace', 'url', 'checked_at', 'valid'} or link['valid'] is not True:
                raise _fail('unverified_catalog_link', 'failed')
            try:
                checked = timestamp(link['checked_at'])
                url = urlsplit(link['url'])
                port = url.port
            except (ValueError, TypeError, AttributeError):
                raise _fail('invalid_catalog_configuration', 'failed') from None
            hosts = {'notion.so', 'www.notion.so', 'notion.com', 'www.notion.com', 'app.notion.com'} if row['provider'] == 'notion' else {'zotero.org', 'www.zotero.org', 'api.zotero.org'}
            if (url.scheme != 'https' or url.hostname not in hosts or url.username or url.password
                    or port not in {None, 443} or url.query or url.fragment or len(link['url']) > 2000):
                raise _fail('invalid_catalog_configuration', 'failed')
            path = unquote(url.path).replace('-', '').lower()
            namespace = row['namespace']
            if row['provider'] == 'notion':
                identity = namespace.removeprefix('collection://').replace('-', '').lower()
                # A data-source UUID and its containing database/page UUID are
                # distinct. Their verified relationship is operator attestation,
                # not something derivable by substituting one URL component.
                if (not namespace.startswith('collection://') or not re.fullmatch(r'[a-f0-9]{32}', identity)
                        or not re.search(r'[a-f0-9]{32}/?\Z', path)):
                    raise _fail('catalog_link_namespace_mismatch', 'failed')
            elif not re.fullmatch(r'(?:users|groups)/[1-9]\d*', namespace) or not (
                    url.path == '/' + namespace or url.path.startswith('/' + namespace + '/')):
                raise _fail('catalog_link_namespace_mismatch', 'failed')
            safe.append(dict(provider=row['provider'], namespace=row['namespace'], url=link['url'], checked_at=checked, valid=True))
        current = self._catalog_default(c)
        values = dict(registration_ids=[row['id'] for row in rows], initial_read_status='unread', verified_links=safe, updated_at=now)
        if current:
            c.execute(update(db.catalog_defaults).where(db.catalog_defaults.c.id == current['id']).values(**values))
        else:
            c.execute(db.catalog_defaults.insert().values(**self._base(name='default', created_at=now, **values)))
        return envelope(dict(configured=True, **values)), []

    def _catalog_current(self, c, source_id):
        from .service import digest
        default = self._catalog_default(c)
        if not default:
            raise _fail('catalog_not_configured')
        source = self._row(c, db.sources, source_id)
        meta = c.execute(select(db.snapshot_metadata).where(self._scope(db.snapshot_metadata),
            db.snapshot_metadata.c.source_version_id == source_id)).mappings().first()
        if not meta:
            raise _fail()
        obj = self._row(c, db.source_objects, meta['source_object_id'])
        registration = self._row(c, db.connector_registrations, obj['registration_id'])
        if (registration['id'] not in default['registration_ids'] or registration['provider'] not in {'notion', 'zotero'}
                or source['kind'] != registration['provider'] or obj['head_source_version_id'] != source_id
                or source['access_state'] != 'accessible' or obj['access_state'] != 'accessible'
                or self._blocked(c, identity=source_id, fingerprint=source['sha256'])
                or self._blocked(c, identity=obj['id'], fingerprint=obj['identity_hash'])
                or digest(source['content']) != source['sha256']):
            raise _fail()
        try:
            raw = _json(source['content'])
        except (ValueError, RecursionError):
            raise _fail() from None
        if not isinstance(raw, dict):
            raise _fail()
        if registration['provider'] == 'zotero':
            data = raw.get('data')
            if (not isinstance(data, dict) or raw.get('key') != obj['external_id']
                    or data.get('key', obj['external_id']) != obj['external_id'] or data.get('deleted')):
                raise _fail()
        return source, obj, registration, raw

    def _catalog_alias_live(self, c, alias):
        from .service import DomainError, digest
        if alias['state'] != 'active' or self._blocked(c, identity=alias['work_id']):
            return False
        try:
            _, obj, _, raw = self._catalog_current(c, alias['source_snapshot_id'])
        except DomainError:
            return False
        if obj['id'] != alias['source_object_id']:
            return False
        proof = alias['proof']
        if proof.get('kind') == 'extra_line':
            value = _at(raw, proof.get('json_path'))
            if not isinstance(value, str) or digest(value) != proof.get('literal_sha256'):
                return False
            values = [line.strip() for line in value.splitlines() if digest(line) == proof.get('line_sha256')]
        else:
            value = _claim_value(raw, proof)
            values = [value] if value is not None else []
        return any(claim['scheme'] == alias['scheme'] and claim['value'] == alias['value']
                   for value in values for claim in parse_identity(value))

    def _catalog_version(self, c, work_version_id):
        row = c.execute(select(db.catalog_versions).where(self._scope(db.catalog_versions),
            db.catalog_versions.c.work_version_id == work_version_id)).mappings().first()
        return dict(row) if row else None

    def _catalog_sources(self, c, work_version_id):
        from .service import DomainError
        catalog = self._catalog_version(c, work_version_id)
        if not catalog or self._blocked(c, identity=work_version_id):
            return []
        version = self._row(c, db.versions, work_version_id)
        if self._blocked(c, identity=version['work_id']):
            return []
        result = []
        rows = c.execute(select(db.catalog_bindings).where(self._scope(db.catalog_bindings),
            db.catalog_bindings.c.work_version_id == work_version_id, db.catalog_bindings.c.state == 'active')).mappings()
        for binding in rows:
            try:
                _, obj, _, _ = self._catalog_current(c, binding['authority_source_snapshot_id'])
                source = self._row(c, db.sources, binding['source_snapshot_id'])
                if (obj['id'] != binding['source_object_id'] or source['access_state'] != 'accessible'
                        or self._blocked(c, identity=source['id'], fingerprint=source['sha256'])):
                    continue
                if binding['role'] == 'original_pdf':
                    link = c.execute(select(db.attachment_asset_links.c.id).where(self._scope(db.attachment_asset_links),
                        db.attachment_asset_links.c.pdf_source_id == source['id'],
                        db.attachment_asset_links.c.parent_snapshot_id == binding['authority_source_snapshot_id'])).first()
                    if (not link or source['kind'] != 'zotero_attachment_pdf' or source['media_type'] != 'application/pdf'
                            or not self._linked_asset_available(c, source['id'])):
                        continue
                elif source['id'] != binding['authority_source_snapshot_id']:
                    continue
            except DomainError:
                continue
            result.append(dict(source_snapshot_id=source['id'], authority_source_snapshot_id=binding['authority_source_snapshot_id'],
                source_object_id=obj['id'], role=binding['role'], version_relation=binding['version_relation'],
                source_kind=source['kind'], media_type=source['media_type'], identity_state=catalog['identity_state'],
                scientific_version_state=catalog['scientific_version_state'], scientific_version=catalog['scientific_version'],
                binding_created_at=binding['created_at']))
        return sorted(result, key=lambda row: (row['role'], row['source_snapshot_id']))

    def _catalog_bind(self, c, p, now):
        from .service import canonical, digest, envelope
        source, obj, registration, raw = self._catalog_current(c, p['source_snapshot_id'])
        provider = registration['provider']
        if provider == 'zotero' and raw['data'].get('itemType') not in _PAPER_TYPES:
            raise _fail('catalog_not_paper_metadata', 'failed')
        title_path = p.get('title_path') or (['data', 'title'] if provider == 'zotero' else ['title'])
        title = _at(raw, title_path)
        if not isinstance(title, str) or not title.strip() or len(title) > 10000:
            raise _fail('catalog_title_missing', 'failed')
        claims = _claims(raw, provider, p.get('identity_paths'))
        identities = sorted({(claim['scheme'], claim['value']) for claim in claims})
        explicit = sorted({claim['value'] + claim['scientific_version'] for claim in claims
                           if claim['scheme'] == 'arxiv' and claim['scientific_version']})
        internal_conflict = (any(sum(scheme == kind for scheme, _ in identities) > 1 for kind in ('doi', 'arxiv'))
                             or len(explicit) > 1)
        known = set()
        for scheme, value in identities:
            aliases = c.execute(select(db.catalog_aliases).where(self._scope(db.catalog_aliases),
                db.catalog_aliases.c.scheme == scheme, db.catalog_aliases.c.value == value)).mappings()
            known.update(alias['work_id'] for alias in aliases if self._catalog_alias_live(c, alias))
        conflict = internal_conflict or len(known) > 1
        work = self._row(c, db.works, next(iter(known))) if len(known) == 1 and not conflict else None
        if work is None:
            key = 'catalog:' + digest(canonical([obj['id'], identities]))
            if self._blocked(c, fingerprint=digest(key)):
                raise _fail('forgotten')
            work = c.execute(select(db.works).where(self._scope(db.works), db.works.c.identity_key == key)).mappings().first()
            if not work:
                work = self._base(identity_key=key, title=title)
                c.execute(db.works.insert().values(**work))
        identity_state = 'conflict' if conflict else 'strong' if identities else 'provisional'
        scientific = explicit[0] if len(explicit) == 1 and not conflict else None
        label = scientific.rsplit('v', 1)[-1] if scientific else None
        scientific_version = 'v' + label if label else None
        identifier = 'catalog:' + work['id'] + ':' + (('arxiv:' + scientific) if scientific else 'unknown')
        if self._blocked(c, identity=work['id']) or self._blocked(c, fingerprint=digest(identifier)):
            raise _fail('forgotten')
        version = c.execute(select(db.versions).where(self._scope(db.versions), db.versions.c.identifier == identifier)).mappings().first()
        if version and self._blocked(c, identity=version['id']):
            raise _fail('forgotten')
        deduplicated = bool(version and c.execute(select(db.catalog_bindings.c.id).where(self._scope(db.catalog_bindings),
            db.catalog_bindings.c.work_version_id == version['id'], db.catalog_bindings.c.source_snapshot_id == source['id'])).first())
        if not version:
            version = self._base(work_id=work['id'], identifier=identifier, version=scientific_version or 'unknown',
                                 title=title, source_version_id=source['id'], created_at=now)
            c.execute(db.versions.insert().values(**version))
            c.execute(db.catalog_versions.insert().values(**self._base(work_version_id=version['id'],
                identity_state=identity_state, scientific_version_state='explicit' if scientific else 'unknown',
                scientific_version=scientific_version, initial_read_status='unread', created_at=now)))
        conflicts = ([dict(kind='conflicting_strong_identity', work_ids=sorted(known))] if conflict else [])
        role = 'note' if provider == 'notion' else 'metadata'
        def bind(sid, relation, source_role):
            existing = c.execute(select(db.catalog_bindings).where(self._scope(db.catalog_bindings),
                db.catalog_bindings.c.work_version_id == version['id'], db.catalog_bindings.c.source_snapshot_id == sid)).mappings().first()
            if existing:
                if existing['state'] != 'active':
                    raise _fail('forgotten')
                return
            c.execute(db.catalog_bindings.insert().values(**self._base(work_version_id=version['id'], source_snapshot_id=sid,
                authority_source_snapshot_id=source['id'], source_object_id=obj['id'], role=source_role,
                version_relation=relation, identity_claims=claims if source_role != 'original_pdf' else [],
                conflicts=conflicts, state='active', created_at=now)))
        bind(source['id'], 'explicit' if scientific else 'unknown', role)
        for link in c.execute(select(db.attachment_asset_links).where(self._scope(db.attachment_asset_links),
                db.attachment_asset_links.c.parent_snapshot_id == source['id'])).mappings():
            if self._linked_asset_available(c, link['pdf_source_id']):
                bind(link['pdf_source_id'], 'unknown', 'original_pdf')
        if not conflict:
            for claim in claims:
                exists = c.execute(select(db.catalog_aliases.c.id).where(self._scope(db.catalog_aliases),
                    db.catalog_aliases.c.work_id == work['id'], db.catalog_aliases.c.source_snapshot_id == source['id'],
                    db.catalog_aliases.c.scheme == claim['scheme'], db.catalog_aliases.c.value == claim['value'])).first()
                if not exists:
                    c.execute(db.catalog_aliases.insert().values(**self._base(work_id=work['id'], scheme=claim['scheme'],
                        value=claim['value'], source_snapshot_id=source['id'], source_object_id=obj['id'], proof=claim['proof'],
                        state='active', created_at=now)))
        catalog = self._catalog_version(c, version['id'])
        return envelope(dict(paper_id=work['id'], paper_version_id=version['id'], source_snapshot_id=source['id'],
            identity_state=catalog['identity_state'], scientific_version_state=catalog['scientific_version_state'],
            scientific_version=catalog['scientific_version'], initial_read_status='unread', reading_mutated=False,
            deduplicated=deduplicated, conflicts=conflicts, bindings=self._catalog_sources(c, version['id']))), []

    def _catalog_status(self, c, p, now):
        from .service import envelope
        default = self._catalog_default(c)
        versions = list(c.execute(select(db.catalog_versions).where(self._scope(db.catalog_versions))).mappings())
        visible = [version for version in versions if self._catalog_sources(c, version['work_version_id'])]
        return envelope(dict(configured=bool(default), registration_ids=list(default['registration_ids']) if default else [],
            verified_links=list(default['verified_links']) if default else [], initial_read_status='unread',
            catalog_versions=len(visible), explicit_scientific_versions=sum(version['scientific_version_state'] == 'explicit' for version in visible),
            unknown_scientific_versions=sum(version['scientific_version_state'] == 'unknown' for version in visible))), []

    def _catalog_dependent_sources(self, c, targets, source_ids):
        selected = set(source_ids)
        versions = list(c.execute(select(db.versions).where(self._scope(db.versions))).mappings())
        ids = {version['id'] for version in versions if version['id'] in targets or version['work_id'] in targets}
        if ids:
            selected.update(c.execute(select(db.catalog_bindings.c.source_snapshot_id).where(self._scope(db.catalog_bindings),
                db.catalog_bindings.c.work_version_id.in_(ids))).scalars())
            selected.update(c.execute(select(db.catalog_bindings.c.authority_source_snapshot_id).where(self._scope(db.catalog_bindings),
                db.catalog_bindings.c.work_version_id.in_(ids))).scalars())
        return selected

    def _catalog_forget(self, c, source_ids, now):
        if not source_ids:
            return
        c.execute(update(db.catalog_aliases).where(self._scope(db.catalog_aliases),
            db.catalog_aliases.c.source_snapshot_id.in_(source_ids)).values(value='', proof={}, state='forgotten'))
        c.execute(update(db.catalog_bindings).where(self._scope(db.catalog_bindings),
            (db.catalog_bindings.c.source_snapshot_id.in_(source_ids) | db.catalog_bindings.c.authority_source_snapshot_id.in_(source_ids)))
            .values(identity_claims=[], conflicts=[], state='forgotten'))
