"""Connector domain rules mixed into the single transactional ResearchService."""
from sqlalchemy import select, update, and_, or_
from . import storage as db

class SourceOperations:
    def _registration(self, c, p):
        from .service import DomainError
        registration = c.execute(select(db.connector_registrations).where(self._scope(db.connector_registrations),
            db.connector_registrations.c.provider == p['provider'],
            db.connector_registrations.c.source_instance_id == p['source_instance_id'],
            db.connector_registrations.c.namespace == p['namespace'])).mappings().first()
        if registration is None:
            raise DomainError('forbidden', 'source_not_registered', 'Connector namespace is not registered by an operator')
        return registration

    def _sources_register(self, c, p, now):
        from .service import envelope
        existing = c.execute(select(db.connector_registrations).where(self._scope(db.connector_registrations),
            db.connector_registrations.c.provider == p['provider'],
            db.connector_registrations.c.source_instance_id == p['source_instance_id'],
            db.connector_registrations.c.namespace == p['namespace'])).mappings().first()
        if existing:
            return envelope(dict(registration_id=existing['id'])), []
        row = self._base(provider=p['provider'], source_instance_id=p['source_instance_id'],
                         namespace=p['namespace'], created_at=now)
        c.execute(db.connector_registrations.insert().values(**row))
        return envelope(dict(registration_id=row['id'])), []

    def _sources_ingest(self, c, p, now):
        from .service import canonical, digest, timestamp, DomainError, envelope
        from .retrieval import purge_sources
        registration = self._registration(c, p)
        key = digest(canonical([p['provider'],p['source_instance_id'],p['namespace'],p['external_id']]))
        if self._blocked(c, fingerprint=key):
            raise DomainError('forbidden','forgotten','This connector object is suppressed')
        obj = c.execute(select(db.source_objects).where(self._scope(db.source_objects),
            db.source_objects.c.identity_hash == key)).mappings().first()
        if obj is None:
            obj = self._base(registration_id=registration['id'], identity_hash=key, external_id=p['external_id'],
                             head_source_version_id=None, access_state='accessible', revision=1)
            c.execute(db.source_objects.insert().values(**obj))
        snapshots = list(c.execute(select(db.snapshot_metadata.c.source_version_id).where(
            self._scope(db.snapshot_metadata),db.snapshot_metadata.c.source_object_id == obj['id'])).scalars())
        if p['change'] in {'delete','access_revoked'}:
            state = 'deleted' if p['change']=='delete' else 'inaccessible'
            for sid in snapshots:
                source = self._row(c,db.sources,sid)
                c.execute(update(db.sources).where(db.sources.c.id==sid).values(access_state=state, revision=source['revision']+1))
            c.execute(update(db.source_objects).where(db.source_objects.c.id==obj['id']).values(access_state=state,revision=obj['revision']+1))
            c.execute(update(db.scopes).where(db.scopes.c.id==self.scope_key).values(access_epoch=db.scopes.c.access_epoch+1))
            purge_sources(c,list(self._dependent_asset_sources(c,snapshots)),self.owner_id,self.scope_id)
            return envelope(dict(source_object_id=obj['id'],source_snapshot_id=obj['head_source_version_id'],state=state)), []
        if obj['access_state']!='accessible' and p['change']!='restore':
            raise DomainError('forbidden','explicit_restore_required','A revoked/deleted source needs an explicit verified restore')
        content = canonical(p['raw_payload'])
        if self._blocked(c,fingerprint=digest(content)):
            raise DomainError('forbidden','forgotten','This source content is suppressed')
        # External revisions are opaque. Equal external version with different
        # bytes creates another immutable observation, never an in-place rewrite.
        for sid in snapshots:
            source=self._row(c,db.sources,sid)
            meta=c.execute(select(db.snapshot_metadata).where(db.snapshot_metadata.c.source_version_id==sid)).mappings().one()
            if source['sha256']==digest(content) and meta['external_version']==p['external_version']:
                if sid != obj['head_source_version_id']:
                    # A -> B -> A is a new observation of A. Reusing the old
                    # snapshot/head would lose the Jan3 activation and leak it
                    # into known_at views from before that activation.
                    continue
                if p['change']=='restore':
                    c.execute(update(db.sources).where(db.sources.c.id==sid).values(access_state='accessible',revision=source['revision']+1))
                    c.execute(update(db.source_objects).where(db.source_objects.c.id==obj['id']).values(access_state='accessible',head_source_version_id=sid,revision=obj['revision']+1))
                    c.execute(update(db.scopes).where(db.scopes.c.id==self.scope_key).values(access_epoch=db.scopes.c.access_epoch+1))
                return envelope(dict(source_object_id=obj['id'],source_snapshot_id=sid,evidence_ids=self._source_evidence(c,sid),deduplicated=True)), []
        source=self._new_source(c,key='connector:'+key,kind=p['provider'],content=content,url=p['source_url'],
            media='application/json',extraction='connector-json-v1',now=now)
        c.execute(db.snapshot_metadata.insert().values(**self._base(source_object_id=obj['id'],source_version_id=source['id'],
            external_version=p['external_version'],observed_at=timestamp(p['observed_at']),
            occurred_at=timestamp(p['occurred_at']) if p['occurred_at'] else None,
            completeness=p['completeness'],missing_parts=p['missing_parts'])))
        evidence_ids=[self._new_evidence(c,source['id'],passage['text'],passage['section'],
            dict(kind='json',json_path=passage['json_path'])) for passage in p['passages']]
        promote = True
        if obj['head_source_version_id'] and p['change']!='restore':
            previous_meta=c.execute(select(db.snapshot_metadata).where(
                db.snapshot_metadata.c.source_version_id==obj['head_source_version_id'])).mappings().one()
            promote=timestamp(p['observed_at']) >= previous_meta['observed_at']
        if promote:
            c.execute(update(db.source_objects).where(db.source_objects.c.id==obj['id']).values(
                head_source_version_id=source['id'],access_state='accessible',revision=obj['revision']+1))
        if p['change']=='restore':
            c.execute(update(db.scopes).where(db.scopes.c.id==self.scope_key).values(access_epoch=db.scopes.c.access_epoch+1))
        return envelope(dict(source_object_id=obj['id'],source_snapshot_id=source['id'],evidence_ids=evidence_ids,
            completeness=p['completeness'],deduplicated=False,current=promote)), []

    def _sources_checkpoint(self,c,p,now):
        from .service import envelope, DomainError
        registration=self._registration(c,p)
        persisted_complete = bool(p['source_snapshot_ids'])
        for sid in p['source_snapshot_ids']:
            source=self._row(c,db.sources,sid)
            metadata=c.execute(select(db.snapshot_metadata).join(db.source_objects,
                db.snapshot_metadata.c.source_object_id==db.source_objects.c.id).where(
                self._scope(db.snapshot_metadata),db.snapshot_metadata.c.source_version_id==sid,
                db.source_objects.c.registration_id==registration['id'])).mappings().first()
            if metadata is None or source['access_state']!='accessible':
                raise DomainError('conflict','incomplete_checkpoint','Snapshot is missing or outside the registered source')
            persisted_complete = persisted_complete and metadata['completeness']=='complete' and not metadata['missing_parts']
        # A caller's booleans and selected snapshots do not establish inventory
        # coverage. Only SyncCoordinator's fenced manifest finalizer may advance.
        complete=False
        run=self._base(registration_id=registration['id'],status='complete' if complete else 'partial',
            source_snapshot_ids=p['source_snapshot_ids'],recorded_at=now)
        c.execute(db.sync_runs.insert().values(**run))
        if complete:
            table=db.connector_cursors
            row=c.execute(select(table).where(table.c.registration_id==registration['id'])).mappings().first()
            values=dict(cursor=p['cursor'],run_id=run['id'],recorded_at=now)
            if row:
                c.execute(update(table).where(table.c.id==row['id']).values(**values))
            else:
                c.execute(table.insert().values(**self._base(registration_id=registration['id'],**values)))
        return envelope(dict(sync_run_id=run['id'],checkpoint_advanced=complete,status='complete' if complete else 'partial')), []

    def _sources_search(self,c,p,now):
        from .service import envelope, timestamp
        conditions=[self._scope(db.source_objects),self._scope(db.connector_registrations),db.source_objects.c.access_state=='accessible']
        if p['project_ids']:
            from .service import DomainError
            raise DomainError('failed','unsupported_filter','Project-linked source filtering is not implemented')
        for field in ['provider','source_instance_id','namespace']:
            if p.get(field): conditions.append(db.connector_registrations.c[field]==p[field])
        rows=c.execute(select(db.source_objects,db.connector_registrations.c.provider,
            db.connector_registrations.c.source_instance_id,db.connector_registrations.c.namespace).join(
            db.connector_registrations,db.source_objects.c.registration_id==db.connector_registrations.c.id).where(*conditions)).mappings()
        found=[]
        for obj in rows:
            if self._blocked(c,identity=obj['id']):continue
            if p.get('known_at'):
                meta=c.execute(select(db.snapshot_metadata).join(db.sources,db.snapshot_metadata.c.source_version_id==db.sources.c.id).where(
                    db.snapshot_metadata.c.source_object_id==obj['id'],db.sources.c.created_at<=timestamp(p['known_at'])).order_by(db.snapshot_metadata.c.observed_at.desc(),db.sources.c.created_at.desc())).mappings().first()
                if not meta:continue
                source=self._row(c,db.sources,meta['source_version_id'])
            else:
                if not obj['head_source_version_id']:continue
                source=self._row(c,db.sources,obj['head_source_version_id'])
                meta=c.execute(select(db.snapshot_metadata).where(db.snapshot_metadata.c.source_version_id==source['id'])).mappings().one()
            if source['access_state']!='accessible' or self._blocked(c,identity=source['id']):continue
            if p.get('valid_as_of'):
                # A source observation is not proof of real-world validity.
                continue
            if p['query'] and p['query'].casefold() not in source['content'].casefold():continue
            found.append(dict(source_object_id=obj['id'],source_snapshot_id=source['id'],provider=obj['provider'],
                source_instance_id=obj['source_instance_id'],namespace=obj['namespace'],external_id=obj['external_id'],
                external_version=meta['external_version'],source_url=source['source_url'],sha256=source['sha256'],
                observed_at=meta['observed_at'],occurred_at=meta['occurred_at'],completeness=meta['completeness'],
                missing_parts=meta['missing_parts'],epistemic_status='source_reported',
                evidence_ids=self._source_evidence(c,source['id'])))
        response=envelope(dict(sources=found[:p['limit']],total=len(found)),status='partial' if len(found)>p['limit'] else 'ok')
        if p.get('valid_as_of'):
            response['uncertainties']=[dict(kind='unknown_valid_time',message='Connector snapshots carry observation time, not verified real-world validity intervals')]
        return response,[]

    def _sources_get_evidence(self,c,p,now):
        from .service import envelope,DomainError
        source=self._row(c,db.sources,p['source_snapshot_id'])
        if (source['access_state']!='accessible' or self._blocked(c,identity=source['id'])
                or not self._linked_asset_available(c,source['id'])):
            raise DomainError('not_found','not_found','Source is not available')
        spans=[]
        citations=[]
        for span in c.execute(select(db.evidence).where(self._scope(db.evidence),db.evidence.c.source_version_id==source['id'])).mappings():
            if p['evidence_ids'] and span['id'] not in p['evidence_ids']:continue
            citation=self._citation(c,span['id'])
            spans.append(dict(evidence_id=span['id'],text=span['text'],locator=span['locator'],epistemic_status='source_reported',support_type='context_only'))
            citations.append(citation)
        response=envelope(dict(passages=spans))
        response['citations']=citations
        return response,[]
