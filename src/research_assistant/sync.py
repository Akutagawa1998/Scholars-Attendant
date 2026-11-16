"""Operator-only synchronization with durable coverage and fenced commits.

Adapters perform read-only remote I/O outside transactions. Every local item
commit and checkpoint is fenced by the registered connector's expiring lease.
"""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import secrets
from sqlalchemy import select, update, delete
from . import storage as db
from .service import DomainError, canonical, digest, timestamp, uid, envelope
from .schemas import SourceSnapshot
from .connectors.common import ConnectorError

TERMINAL = {'persisted','unchanged','suppressed','deleted'}
SAFE_ERRORS = {'source_unavailable','unauthorized','forbidden','not_found','rate_limited',
    'source_changed','invalid_response','incomplete_response','invalid_snapshot',
    'deletion_unreconciled','explicit_restore_required','forgotten','access_revoked','authentication_failed',
    'source_changed_during_pagination','incomplete_source_response','source_version_regression'}
AUTH_ERRORS={'access_revoked','authentication_failed','unauthorized','forbidden','insufficient_permissions','identity_mismatch'}
SAFE_ERRORS |= AUTH_ERRORS

class SyncCoordinator:
    def __init__(self, service, *, lease_seconds=120, clock=None):
        if lease_seconds <= 0:
            raise ValueError('positive lease duration required')
        self.service = service
        self.lease_seconds = lease_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self):
        return timestamp(self.clock())

    @contextmanager
    def _locked(self, *, renew=False):
        with self.service.storage.transaction() as c:
            epoch=self.service._epoch(c)
            self.service._check_sync_guard(c,self.guard,now=self._now())
            if renew:
                c.execute(update(db.sync_leases).where(db.sync_leases.c.registration_id==self.registration_id).values(
                    expires_at=timestamp(self.clock()+timedelta(seconds=self.lease_seconds))))
            yield c

    def _key(self,external_id):
        return digest(canonical([self.settings['provider'],self.settings['source_instance_id'],
                                 self.settings['namespace'],external_id]))

    def _acquire(self):
        now=self._now()
        with self.service.storage.transaction() as c:
            epoch=self.service._epoch(c)
            registration=self.service._registration(c,self.settings)
            self.registration_id=registration['id']
            lease=c.execute(select(db.sync_leases).where(db.sync_leases.c.registration_id==self.registration_id)).mappings().first()
            if lease and lease['token_hash'] and lease['expires_at']>now:
                raise DomainError('conflict','sync_lease_busy','Another connector run holds the lease')
            self.run_id=uid()
            self.guard=dict(registration_id=self.registration_id,run_id=self.run_id,
                            token_hash=digest(secrets.token_urlsafe(32)),_clock=self.clock,
                            access_epoch=epoch['access_epoch'],deletion_epoch=epoch['deletion_epoch'])
            values=dict(token_hash=self.guard['token_hash'],run_id=self.run_id,
                        expires_at=timestamp(self.clock()+timedelta(seconds=self.lease_seconds)))
            if lease:
                c.execute(update(db.sync_leases).where(db.sync_leases.c.id==lease['id']).values(**values))
                c.execute(update(db.sync_manifests).where(db.sync_manifests.c.id==lease['run_id'],
                    db.sync_manifests.c.status=='running').values(status='interrupted',updated_at=now))
            else:
                c.execute(db.sync_leases.insert().values(**self.service._base(registration_id=self.registration_id,**values)))
            c.execute(db.sync_manifests.insert().values(**self.service._base(id=self.run_id,
                registration_id=self.registration_id,source_version=None,inventory_hash=None,expected_count=0,
                inventory_complete=False,source_consistent=False,deletions_complete=False,status='running',
                recorded_at=now,updated_at=now)))
            cursor=c.execute(select(db.connector_cursors.c.cursor).where(
                db.connector_cursors.c.registration_id==self.registration_id)).scalar_one_or_none()
            return cursor

    def _release(self):
        with self.service.storage.transaction() as c:
            self.service._epoch(c)
            c.execute(update(db.sync_manifests).where(db.sync_manifests.c.id==self.run_id,
                db.sync_manifests.c.status=='running').values(status='interrupted',updated_at=self._now()))
            c.execute(update(db.sync_leases).where(db.sync_leases.c.registration_id==self.registration_id,
                db.sync_leases.c.token_hash==self.guard['token_hash'],db.sync_leases.c.run_id==self.run_id).values(
                token_hash='',expires_at=self._now()))

    def _revoke_registration(self):
        """Explicit authentication/permission failure closes previously served copies."""
        with self._locked() as c:
            now=self._now()
            rows=list(c.execute(select(db.source_objects).where(self.service._scope(db.source_objects),
                db.source_objects.c.registration_id==self.registration_id,
                db.source_objects.c.access_state=='accessible')).mappings())
            count=0
            for obj in rows:
                if not obj['external_id'] or self.service._blocked(c,fingerprint=obj['identity_hash']):continue
                snapshot=SourceSnapshot.model_validate(dict(self.settings,external_id=obj['external_id'],external_version=None,
                    raw_payload={},source_url='',observed_at=now,occurred_at=None,
                    completeness='unknown',missing_parts=[],change='access_revoked',passages=[],
                    idempotency_key='sync-revoke:'+self.run_id+':'+obj['identity_hash'])).model_dump(mode='json')
                # One scope-locked transaction closes the whole registration;
                # no reader can observe only some cached objects being revoked.
                self.service._sources_ingest(c,snapshot,now)
                count+=1
            event_id=uid()
            receipt=envelope(dict(registration_id=self.registration_id,revoked_count=count,sync_run_id=self.run_id))
            receipt['operation']=dict(event_id=event_id,persisted=True,recorded_at=now)
            c.execute(db.events.insert().values(**self.service._base(id=event_id,
                idempotency_key=digest('sync-permission-revoke:'+self.run_id),
                operation='sources.permission_revoked',
                request_hash=digest(canonical(['sources.permission_revoked',self.registration_id,self.run_id])),
                receipt=receipt,recorded_at=now)))
            epoch=self.service._epoch(c)
            self.guard['access_epoch']=epoch['access_epoch']
            self.guard['deletion_epoch']=epoch['deletion_epoch']

    def _remote(self,call):
        with self._locked(renew=True):
            pass
        try:
            return call()
        except Exception as exc:
            if getattr(exc,'code',None) in AUTH_ERRORS:self.auth_lost=True
            raise

    @staticmethod
    def _inventory(value):
        if not isinstance(value,dict) or not isinstance(value.get('object_versions'),dict):
            raise ConnectorError('invalid_response')
        objects=value['object_versions']
        if any(not isinstance(k,str) or not k or (v is not None and not isinstance(v,str)) for k,v in objects.items()):
            raise ConnectorError('invalid_response')
        version=value.get('version')
        if version is not None and not isinstance(version,str):
            raise ConnectorError('invalid_response')
        return dict(version=version,object_versions=objects,complete=value.get('complete') is True,
                    consistent=value.get('consistent') is True)

    def _add_item(self,c,key,version):
        old=c.execute(select(db.sync_items).where(db.sync_items.c.manifest_id==self.run_id,
            db.sync_items.c.identity_hash==key)).mappings().first()
        if not old:
            c.execute(db.sync_items.insert().values(**self.service._base(manifest_id=self.run_id,
                identity_hash=key,expected_version=version,state='pending',snapshot_id=None,attempts=0,
                error_code=None,next_retry_at=None,updated_at=self._now())))

    def _mark(self,key,version,state,*,snapshot_id=None,error=None,retry_after=None):
        code=error if error in SAFE_ERRORS else 'source_unavailable' if error else None
        due=None
        if retry_after is not None:
            try:
                seconds=min(max(float(retry_after),0),86400*30)
                due=timestamp(self.clock()+timedelta(seconds=seconds))
            except (TypeError,ValueError,OverflowError):
                due=timestamp(self.clock()+timedelta(seconds=60))
        with self._locked() as c:
            if self.service._blocked(c,fingerprint=key):
                state,version,snapshot_id,code,due='suppressed',None,None,None,None
            self._add_item(c,key,version)
            c.execute(update(db.sync_items).where(db.sync_items.c.manifest_id==self.run_id,
                db.sync_items.c.identity_hash==key).values(state=state,snapshot_id=snapshot_id,
                    expected_version=None if state=='suppressed' else version,
                    attempts=db.sync_items.c.attempts+1,error_code=code,next_retry_at=due,updated_at=self._now()))
            failure=c.execute(select(db.sync_failures).where(db.sync_failures.c.registration_id==self.registration_id,
                db.sync_failures.c.identity_hash==key)).mappings().first()
            if state in TERMINAL:
                if failure:c.execute(delete(db.sync_failures).where(db.sync_failures.c.id==failure['id']))
            elif code:
                values=dict(expected_version=version,attempts=(failure['attempts'] if failure else 0)+1,
                            error_code=code,next_retry_at=due,updated_at=self._now())
                if failure:c.execute(update(db.sync_failures).where(db.sync_failures.c.id==failure['id']).values(**values))
                else:c.execute(db.sync_failures.insert().values(**self.service._base(
                    registration_id=self.registration_id,identity_hash=key,**values)))

    def _fail(self,key,version,exc):
        code=getattr(exc,'code',None)
        self._mark(key,version,'failed',error=code or 'source_unavailable',retry_after=getattr(exc,'retry_after',None) or 1)

    def _validated(self,value,expected,*,deletion=False):
        try:
            value=dict(value)
            value['idempotency_key']='sync:'+self.run_id+':'+digest(canonical(value))
            snapshot=SourceSnapshot.model_validate(value).model_dump(mode='json')
        except Exception as exc:
            raise ConnectorError('invalid_snapshot') from exc
        if any(snapshot[k]!=v for k,v in self.settings.items()) or snapshot['external_id'] not in expected:
            raise ConnectorError('invalid_snapshot')
        if deletion:
            if snapshot['change'] not in {'delete','access_revoked'}:
                raise ConnectorError('invalid_snapshot')
            if self.settings['provider']=='zotero' and snapshot['external_version']!=self.initial['version']:
                raise ConnectorError('invalid_snapshot')
        elif snapshot['external_version']!=expected[snapshot['external_id']]:
            raise ConnectorError('invalid_snapshot')
        elif snapshot['change']=='restore' and not (self.initial['complete'] and self.initial['consistent']
                and snapshot['completeness']=='complete' and not snapshot['missing_parts']):
            raise ConnectorError('invalid_snapshot')
        elif snapshot['completeness']!='complete' or snapshot['missing_parts']:
            # Store partial observations, but they are never terminal coverage.
            pass
        return snapshot

    def _apply(self,snapshot):
        key=self._key(snapshot['external_id'])
        try:
            if snapshot['change']=='upsert' and self.initial['complete'] and self.initial['consistent'] and snapshot['completeness']=='complete' and not snapshot['missing_parts']:
                with self._locked() as c:
                    obj=c.execute(select(db.source_objects).where(self.service._scope(db.source_objects),
                        db.source_objects.c.identity_hash==key)).mappings().first()
                    if obj and obj['access_state']!='accessible':snapshot=dict(snapshot,change='restore')
            self.guard['_object_fence']=dict(identity_hash=key,revision=self.baseline.get(key,{}).get('revision'))
            try:
                result=self.service.execute('sources.ingest',snapshot,trusted_capture=True,_sync_guard=self.guard)
            finally:self.guard.pop('_object_fence',None)
            complete_snapshot=snapshot['completeness']=='complete' and not snapshot['missing_parts']
            state=(('deleted' if snapshot['change'] in {'delete','access_revoked'} else 'persisted')
                   if complete_snapshot else 'partial')
            self._mark(key,snapshot['external_version'],state,
                snapshot_id=result['data'].get('source_snapshot_id'),error='incomplete_response' if state=='partial' else None)
        except DomainError as exc:
            if exc.code=='sync_lease_lost':raise
            if exc.code=='forgotten':self._mark(key,None,'suppressed')
            else:self._fail(key,snapshot['external_version'],exc)

    def run(self,adapter,*,provider,source_instance_id,namespace):
        self.auth_lost=False
        self.settings=dict(provider=provider,source_instance_id=source_instance_id,namespace=namespace)
        cursor=self._acquire()
        global_error=None
        initial=None
        deletion_ok=False
        stable=False
        try:
            initial=self._inventory(self._remote(adapter.inventory))
            self.initial=initial
            if (provider=='zotero' and cursor is not None and initial['version'] is not None
                    and cursor.isdigit() and initial['version'].isdigit() and int(initial['version'])<int(cursor)):
                raise ConnectorError('source_version_regression')
            objects=initial['object_versions']
            with self._locked() as c:
                c.execute(update(db.sync_manifests).where(db.sync_manifests.c.id==self.run_id).values(
                    source_version=initial['version'],inventory_hash=digest(canonical(objects)),expected_count=len(objects),
                    inventory_complete=initial['complete'],source_consistent=initial['consistent'],updated_at=self._now()))
                for external_id,version in objects.items():self._add_item(c,self._key(external_id),version)
                current={row['identity_hash']:dict(row) for row in c.execute(select(db.source_objects,
                    db.snapshot_metadata.c.external_version,db.snapshot_metadata.c.completeness,db.snapshot_metadata.c.missing_parts).outerjoin(
                    db.snapshot_metadata,db.source_objects.c.head_source_version_id==db.snapshot_metadata.c.source_version_id).where(
                    self.service._scope(db.source_objects),db.source_objects.c.registration_id==self.registration_id)).mappings()}
                failures={row['identity_hash']:dict(row) for row in c.execute(select(db.sync_failures).where(
                    db.sync_failures.c.registration_id==self.registration_id)).mappings()}
                # A trash observation does not create a content snapshot/head.
                # Its successful item disposition is the durable proof that we
                # have already fetched this exact object revision as deleted.
                # Inaccessible objects deliberately cannot use this shortcut:
                # restored permissions require a fresh verified remote fetch.
                deleted_versions=set(c.execute(select(db.sync_items.c.identity_hash,
                    db.sync_items.c.expected_version).join(db.sync_manifests,
                    db.sync_items.c.manifest_id==db.sync_manifests.c.id).where(
                    self.service._scope(db.sync_items),self.service._scope(db.sync_manifests),
                    db.sync_manifests.c.registration_id==self.registration_id,
                    db.sync_items.c.state=='deleted',db.sync_items.c.expected_version.is_not(None))).all())
            self.baseline=current
            needed=[]
            for external_id,version in objects.items():
                key=self._key(external_id)
                with self._locked() as c:blocked=self.service._blocked(c,fingerprint=key)
                if blocked:
                    self._mark(key,None,'suppressed')
                    continue
                old=current.get(key)
                failure=failures.get(key)
                if failure and failure['next_retry_at'] and failure['next_retry_at']>self._now():
                    self._mark(key,version,'deferred')
                elif (old and old['access_state']=='deleted' and version and not failure
                      and initial['complete'] and initial['consistent']
                      and (key,version) in deleted_versions):
                    self._mark(key,version,'unchanged',snapshot_id=old['head_source_version_id'])
                elif (old and old['access_state']=='accessible' and old['external_version']==version and version is not None
                      and old['completeness']=='complete' and not old['missing_parts'] and not failure):
                    self._mark(key,version,'unchanged',snapshot_id=old['head_source_version_id'])
                else:needed.append(external_id)
            for offset in range(0,len(needed),50):
                keys=needed[offset:offset+50]
                try:
                    values=self._remote(lambda:adapter.fetch_objects(keys,initial['version']))
                    if not isinstance(values,list):raise ConnectorError('invalid_response')
                    expected={k:objects[k] for k in keys}
                    validated=[self._validated(v,expected) for v in values]
                    ids=[s['external_id'] for s in validated]
                    if len(set(ids))!=len(ids) or set(ids)!=set(keys):raise ConnectorError('incomplete_response')
                    for snapshot in validated:self._apply(snapshot)
                except DomainError as exc:
                    if exc.code=='sync_lease_lost':raise
                    for k in keys:self._fail(self._key(k),objects[k],exc)
                    if self.auth_lost:
                        raise ConnectorError(exc.code if exc.code in AUTH_ERRORS else 'access_revoked') from None
                except Exception as exc:
                    for k in keys:self._fail(self._key(k),objects[k],exc)
                    if self.auth_lost:
                        code=getattr(exc,'code',None)
                        raise ConnectorError(code if code in AUTH_ERRORS else 'access_revoked') from None
            try:
                deleted=self._remote(lambda:adapter.deleted(int(cursor or 0),initial['version']))
                if not isinstance(deleted,list):raise ConnectorError('invalid_response')
                deletion_ids=[v.get('external_id') for v in deleted if isinstance(v,dict)]
                if len(deletion_ids)!=len(deleted) or len(set(deletion_ids))!=len(deleted):raise ConnectorError('invalid_response')
                validated=[self._validated(v,{k:None for k in deletion_ids},deletion=True) for v in deleted]
                if any(k in objects for k in deletion_ids):raise ConnectorError('source_changed')
                for snapshot in validated:self._apply(snapshot)
                deletion_keys={self._key(s['external_id']) for s in validated}
                # Absence alone proves neither deletion nor loss of access.
                if initial['complete'] and initial['consistent']:
                    missing=(set(current)|set(failures))-{self._key(k) for k in objects}-deletion_keys
                    for key in missing:
                        with self._locked() as c:blocked=self.service._blocked(c,fingerprint=key)
                        if blocked:self._mark(key,None,'suppressed')
                        elif key in current and current[key]['access_state'] in {'deleted','inaccessible'}:
                            self._mark(key,None,'deleted')
                        else:self._mark(key,None,'failed',error='deletion_unreconciled')
                deletion_ok=True
            except DomainError as exc:
                if exc.code=='sync_lease_lost':raise
                if self.auth_lost:
                    raise ConnectorError(exc.code if exc.code in AUTH_ERRORS else 'access_revoked') from None
                global_error='source_unavailable'
            except Exception as exc:
                code=getattr(exc,'code',None)
                if self.auth_lost:
                    raise ConnectorError(code if code in AUTH_ERRORS else 'access_revoked') from None
                global_error=code if code in SAFE_ERRORS else 'source_unavailable'
            final=self._inventory(self._remote(adapter.inventory))
            stable=(final==initial)
            if not stable:global_error='source_changed'
        except DomainError as exc:
            if exc.code=='sync_lease_lost':raise
            global_error=exc.code if exc.code in SAFE_ERRORS else 'source_unavailable'
        except Exception as exc:
            code=getattr(exc,'code',None)
            global_error=code if code in SAFE_ERRORS else 'source_unavailable'
        finally:
            # A stale worker may release only its own token, never the successor's.
            try:
                if self.auth_lost or global_error in AUTH_ERRORS:self._revoke_registration()
                result=self._finish(initial,stable,deletion_ok,global_error)
            finally:self._release()
        return result

    def _finish(self,initial,stable,deletion_ok,error):
        with self._locked() as c:
            items=list(c.execute(select(db.sync_items).where(db.sync_items.c.manifest_id==self.run_id)).mappings())
            covered=all(item['state'] in TERMINAL for item in items)
            complete=bool(initial and initial['complete'] and initial['consistent'] and initial['version'] is not None
                          and stable and deletion_ok and covered and not error)
            status='complete' if complete else ('best_effort_complete' if initial and stable and covered and not error else 'partial')
            c.execute(update(db.sync_manifests).where(db.sync_manifests.c.id==self.run_id).values(
                status=status,expected_count=len(items),deletions_complete=deletion_ok,source_consistent=bool(initial and initial['consistent'] and stable),updated_at=self._now()))
            snapshots=[item['snapshot_id'] for item in items if item['snapshot_id']]
            c.execute(db.sync_runs.insert().values(**self.service._base(id=self.run_id,registration_id=self.registration_id,
                status=status,source_snapshot_ids=snapshots,recorded_at=self._now())))
            if complete:
                old=c.execute(select(db.connector_cursors).where(db.connector_cursors.c.registration_id==self.registration_id)).mappings().first()
                values=dict(cursor=initial['version'],run_id=self.run_id,recorded_at=self._now())
                if old:c.execute(update(db.connector_cursors).where(db.connector_cursors.c.id==old['id']).values(**values))
                else:c.execute(db.connector_cursors.insert().values(**self.service._base(registration_id=self.registration_id,**values)))
            counts={state:sum(item['state']==state for item in items) for state in sorted({i['state'] for i in items})}
            return envelope(dict(sync_run_id=self.run_id,status=status,checkpoint_advanced=complete,
                expected_count=len(items),item_counts=counts,error_code=error),status='ok' if complete else 'partial')
