"""Canonical research operations, shared by CLI, HTTP and MCP.

No model, connector or index can create a trusted user identity. The explicit
operator capture adapter confirms both original text and its structured claim.
"""
from datetime import datetime, timezone
import hashlib
import json
import uuid
from typing import Any
from pydantic import ValidationError
from sqlalchemy import select, update, delete, and_, or_
from . import storage as db
from .schemas import OPERATIONS, OPERATOR_OPERATIONS, Mutation
from .ingestion_service import SourceOperations
from .asset_service import AssetOperations
from .knowledge_catalog import CatalogOperations

class DomainError(Exception):
    def __init__(self, status: str, code: str, message: str):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)

def uid():
    return str(uuid.uuid4())

def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

def timestamp(value=None):
    value = value or datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.utcoffset() is None:
        raise ValueError("timezone required")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")

def envelope(data=None, status="ok", **extras):
    result = dict(schema_version="1.0", request_id=uid(), status=status,
                data=data or {}, citations=[], source_status=[], uncertainties=[],
                next_cursor=None, operation=None, **extras)
    if status not in {"ok", "partial"}:
        result["error_code"] = status
    return result

class ResearchService(CatalogOperations, AssetOperations, SourceOperations):
    def __init__(self, database_url, owner_id="local", scope_id="private"):
        self.storage = db.Storage(database_url)
        self.owner_id, self.scope_id = owner_id, scope_id
        self.scope_key = str(uuid.uuid5(uuid.NAMESPACE_URL, canonical([owner_id, scope_id])))

    def initialize(self):
        from .migrate import upgrade
        upgrade(self.storage.engine)
        # Idempotent even when API and operator processes start together.
        dialect = self.storage.engine.dialect.name
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        insert = pg_insert if dialect == "postgresql" else sqlite_insert
        with self.storage.transaction() as c:
            c.execute(insert(db.scopes).values(id=self.scope_key, owner_id=self.owner_id,
                scope_id=self.scope_id, deletion_epoch=0, access_epoch=0).on_conflict_do_nothing())

    def _scope(self, table):
        return and_(table.c.owner_id == self.owner_id, table.c.scope_id == self.scope_id)

    def _base(self, **values):
        return dict({"id": uid(), "owner_id": self.owner_id, "scope_id": self.scope_id}, **values)

    def _row(self, c, table, identity):
        row = c.execute(select(table).where(self._scope(table), table.c.id == identity)).mappings().first()
        if row is None:
            raise DomainError("not_found", "not_found", "Object is not available in this scope")
        return dict(row)

    def _blocked(self, c, identity=None, fingerprint=None):
        terms = []
        if identity:
            terms.append(db.tombstones.c.target_id == identity)
        if fingerprint:
            terms.append(db.tombstones.c.fingerprint == fingerprint)
        return bool(terms and c.execute(select(db.tombstones.c.id).where(
            self._scope(db.tombstones), or_(*terms))).first())

    def _epoch(self, c):
        return dict(c.execute(select(db.scopes).where(db.scopes.c.id == self.scope_key)
                              .with_for_update()).mappings().one())

    def execute(self, operation: str, payload: dict, *, trusted_capture=False, _sync_guard=None, _asset_context=None):
        if operation not in OPERATIONS:
            raise DomainError("not_found", "unknown_operation", "Unknown domain operation")
        if operation in OPERATOR_OPERATIONS and not trusted_capture:
            raise DomainError("forbidden", "capture_forbidden", "Use the direct operator capture adapter")
        try:
            request = OPERATIONS[operation].model_validate(payload)
        except ValidationError as exc:
            # Do not echo input values, which may include private source text.
            fields = [".".join(str(p) for p in e["loc"]) for e in exc.errors()]
            raise DomainError("failed", "invalid_request", "Invalid fields: " + ", ".join(fields)) from exc
        params = request.model_dump(mode="json")
        if operation == "sources.set_access" and params["state"] == "accessible" and not trusted_capture:
            raise DomainError("forbidden", "operator_restore_required", "Restoring source access requires a verified operator adapter")
        request_hash = digest(canonical([operation, params]))
        with self.storage.transaction() as c:
            epoch = self._epoch(c)  # Serialize per scope, including final serving validation.
            if _sync_guard is not None:
                self._check_sync_guard(c, _sync_guard)
            if operation == "catalog.bind":
                # A saved receipt cannot authorize an obsolete or revoked head.
                self._catalog_current(c, params["source_snapshot_id"])
            if operation=='papers.import' and params['media_type']=='application/pdf':
                if not trusted_capture or _asset_context is None:
                    raise DomainError('forbidden','verified_pdf_required','PDF imports require the operator asset adapter')
                self._validate_asset_context(c,params,_asset_context)
            elif _asset_context is not None:
                raise DomainError('failed','invalid_asset_context','Asset context is only valid for PDF import')
            if isinstance(request, Mutation):
                previous = c.execute(select(db.events).where(self._scope(db.events),
                    db.events.c.idempotency_key == digest(request.idempotency_key))).mappings().first()
                if previous:
                    if previous["request_hash"] != request_hash:
                        raise DomainError("conflict", "idempotency_conflict", "Idempotency key was used for another request")
                    # Receipts contain IDs only. Never replay forgotten data from an old response.
                    return previous["receipt"]
            now = timestamp()
            handler = getattr(self, "_" + operation.replace(".", "_"))
            response, input_ids = handler(c, params, now)
            if _asset_context is not None:
                self._register_source_asset(c,response,_asset_context)
            if _sync_guard is not None:
                # Carry forward only epoch changes made by this fenced commit.
                current_epoch=self._epoch(c)
                _sync_guard['access_epoch']=current_epoch['access_epoch']
                _sync_guard['deletion_epoch']=current_epoch['deletion_epoch']
            if isinstance(request, Mutation):
                event_id = uid()
                response["operation"] = dict(event_id=event_id, persisted=True, recorded_at=now)
                c.execute(db.events.insert().values(**self._base(idempotency_key=digest(request.idempotency_key),
                    operation=operation, request_hash=request_hash, receipt=response, recorded_at=now,
                    id=event_id)))
                if input_ids:
                    c.execute(db.jobs.insert().values(**self._base(status="pending", input_ids=input_ids,
                        deletion_epoch=epoch["deletion_epoch"], access_epoch=epoch["access_epoch"],
                        created_at=now, attempts=0)))
            return response

    def _check_sync_guard(self, c, guard, *, now=None):
        if now is None:
            now = timestamp(guard['_clock']()) if guard.get('_clock') else timestamp()
        lease = c.execute(select(db.sync_leases).where(self._scope(db.sync_leases),
            db.sync_leases.c.registration_id == guard['registration_id'])).mappings().first()
        if (not lease or lease['token_hash'] != guard['token_hash'] or lease['run_id'] != guard['run_id']
                or lease['expires_at'] <= now):
            raise DomainError('conflict', 'sync_lease_lost', 'Connector lease expired or was replaced')
        epoch=self._epoch(c)
        if any(key in guard and guard[key]!=epoch[key] for key in ('access_epoch','deletion_epoch')):
            raise DomainError('conflict','sync_lease_lost','Source permissions or deletion state changed during synchronization')
        fence=guard.get('_object_fence')
        if fence is not None:
            obj=c.execute(select(db.source_objects).where(self._scope(db.source_objects),
                db.source_objects.c.identity_hash==fence['identity_hash'])).mappings().first()
            if (obj['revision'] if obj else None)!=fence['revision']:
                raise DomainError('conflict','sync_lease_lost','Source revision changed during synchronization')

    def _new_source(self, c, *, key, kind, content, url, media, extraction, now):
        row = self._base(identity_key=key, kind=kind, content=content, sha256=digest(content),
            source_url=url, media_type=media, extraction_version=extraction,
            access_state="accessible", revision=1, created_at=now)
        c.execute(db.sources.insert().values(**row))
        return row

    def _new_evidence(self, c, source_id, text, section, locator):
        row = self._base(source_version_id=source_id, text=text, section=section,
                         locator=locator, quote_hash=digest(text))
        c.execute(db.evidence.insert().values(**row))
        return row["id"]

    def _papers_import(self, c, p, now):
        key = p["identifier"].strip()
        if key.startswith("arxiv:"):
            import re
            if not re.fullmatch(r"arxiv:(?:\d{4}\.\d{4,5}|[a-z-]+/\d{7})v[1-9]\d*", key):
                raise DomainError("failed", "version_required", "arXiv identity must include its explicit version")
            if key.rsplit("v", 1)[1] != p["version"].removeprefix("v"):
                raise DomainError("conflict", "version_mismatch", "Identifier and version disagree")
            work_key = key.rsplit("v", 1)[0]
        else:
            if not key.endswith(":" + p["version"]):
                raise DomainError("failed", "version_required", "Identifier must end in : plus the explicit version")
            work_key = key[:-(len(p["version"]) + 1)]
        if self._blocked(c, fingerprint=digest(key)) or self._blocked(c, fingerprint=digest(p["content"])):
            raise DomainError("forbidden", "forgotten", "This identity is suppressed by a deletion record")
        old = c.execute(select(db.versions).where(self._scope(db.versions),
                    db.versions.c.identifier == key)).mappings().first()
        if old:
            if not self._paper_sources(c, old):
                raise DomainError("forbidden", "unavailable_paper", "Paper version is not currently available")
            source = self._row(c, db.sources, old["source_version_id"])
            if source["sha256"] != digest(p["content"]):
                raise DomainError("conflict", "immutable_version", "Content changed; import a new explicit version")
            return envelope(dict(paper_id=old["work_id"], paper_version_id=old["id"],
                source_snapshot_id=source["id"], evidence_ids=self._source_evidence(c, source["id"]))), []
        for passage in p["passages"]:
            if passage["text"] not in p["content"]:
                raise DomainError("failed", "unanchored_evidence", "Passage does not occur in the source snapshot")
            locator = passage["locator"]
            if locator.get("kind") == "pdf":
                if p["media_type"] != "application/pdf" or not isinstance(locator.get("page_index"), int) or locator["page_index"] < 0:
                    raise DomainError("failed", "invalid_locator", "PDF anchors require a PDF asset and zero-based page index")
            elif locator.get("kind") not in {"markdown", "html", "text", "epub"}:
                raise DomainError("failed", "invalid_locator", "An explicit source locator kind is required")
        work = c.execute(select(db.works).where(self._scope(db.works), db.works.c.identity_key == work_key)).mappings().first()
        if not work:
            work = self._base(identity_key=work_key, title=p["title"])
            c.execute(db.works.insert().values(**work))
        source = self._new_source(c, key=key, kind="paper", content=p["content"], url=p["source_url"],
            media=p["media_type"], extraction=p["extraction_version"], now=now)
        version = self._base(work_id=work["id"], identifier=key, version=p["version"],
                             title=p["title"], source_version_id=source["id"], created_at=now)
        c.execute(db.versions.insert().values(**version))
        ids = [self._new_evidence(c, source["id"], passage["text"], passage["section"], passage["locator"])
               for passage in p["passages"]]
        return envelope(dict(paper_id=work["id"], paper_version_id=version["id"],
            source_snapshot_id=source["id"], evidence_ids=ids)), []

    def _source_evidence(self, c, source_id):
        return list(c.execute(select(db.evidence.c.id).where(self._scope(db.evidence),
            db.evidence.c.source_version_id == source_id)).scalars())

    def _capture_message(self, c, p, now):
        if self._blocked(c, fingerprint=digest(p["text"])):
            raise DomainError("forbidden", "forgotten", "Captured content is suppressed by a deletion record")
        for claim in p["claims"]:
            fingerprint = digest(canonical([claim["subject_id"], claim["predicate"], claim["value"]]))
            if self._blocked(c, identity=claim["subject_id"], fingerprint=fingerprint):
                raise DomainError("forbidden", "forgotten", "Captured claim is suppressed by a deletion record")
        message_key = str(uuid.uuid5(uuid.NAMESPACE_URL, canonical([self.owner_id, self.scope_id, p["message_id"]])))
        if c.execute(select(db.captures.c.id).where(db.captures.c.id == message_key)).first():
            raise DomainError("conflict", "message_exists", "This message already has an immutable capture")
        source = self._new_source(c, key="capture:" + message_key, kind="user_capture", content=p["text"],
            url="capture:" + message_key, media="text/plain", extraction="operator-confirmed-v1", now=now)
        for i, claim in enumerate(p["claims"]):
            self._new_evidence(c, source["id"], claim["quote"], str(i), dict(kind="text", claim_index=i))
        c.execute(db.captures.insert().values(**self._base(id=message_key, source_version_id=source["id"],
            session_id=p["session_id"], claims=p["claims"], created_at=now)))
        return envelope(dict(trusted_message_id=message_key, source_snapshot_id=source["id"])), []

    def _trusted_claim(self, c, p):
        try:
            captured = self._row(c, db.captures, p["trusted_message_id"])
            source = self._row(c, db.sources, captured["source_version_id"])
        except DomainError:
            return None
        if source["access_state"] != "accessible" or self._blocked(c, identity=source["id"]):
            return None
        if p["claim_index"] >= len(captured["claims"]):
            raise DomainError("failed", "unknown_claim", "No confirmed claim at that index")
        claim = captured["claims"][p["claim_index"]]
        ev = c.execute(select(db.evidence.c.id).where(self._scope(db.evidence),
            db.evidence.c.source_version_id == source["id"], db.evidence.c.section == str(p["claim_index"]))).scalar_one()
        return captured, claim, ev

    def _unverified(self, c, p, now):
        # Do not store model-supplied text as a user statement.
        proposal = self._base(logical_assertion_id=uid(), revision=1, subject_id="unresolved",
            predicate="unverified_user_statement", value={}, epistemic_status="inferred",
            lifecycle_status="proposed", validation_status="unverified_origin", evidence_ids=[],
            source_message_id=None, valid_from=None, valid_to=None, system_from=now, system_to=None)
        c.execute(db.assertions.insert().values(**proposal))
        result = envelope(dict(proposal_id=proposal["logical_assertion_id"]), status="partial")
        result["error_code"] = "unverified_origin"
        result["uncertainties"] = [dict(kind="unverified_origin", message="No verifiable operator capture; proposal saved")]
        return result, []

    def _memory_record_user_statement(self, c, p, now):
        trusted = self._trusted_claim(c, p)
        if trusted is None:
            return self._unverified(c, p, now)
        captured, claim, evidence_id = trusted
        return self._write_claim(c, p, claim, evidence_id, captured["id"], now)

    def _papers_mark_read(self, c, p, now):
        trusted = self._trusted_claim(c, p)
        if trusted is None:
            return self._unverified(c, p, now)
        captured, claim, evidence_id = trusted
        expected = dict(depth=p["depth"], coverage=p["coverage"])
        if (claim["predicate"] != "reading" or claim["subject_id"] != p["paper_version_id"]
                or canonical(claim["value"]) != canonical(expected)
                or claim["valid_from"] != p["valid_from"] or claim["valid_to"] != p["valid_to"]):
            raise DomainError("conflict", "claim_mismatch", "Reading must match the operator-confirmed structured claim")
        return self._write_claim(c, p, claim, evidence_id, captured["id"], now)

    def _write_claim(self, c, p, claim, evidence_id, message_id, now):
        reading = claim["predicate"] == "reading"
        if reading:
            from .schemas import Coverage
            if set(claim["value"]) != {"depth", "coverage"} or claim["value"]["depth"] not in {"deep", "skim", "unknown"}:
                raise DomainError("failed", "invalid_reading", "Reading needs a separate depth and coverage")
            try:
                claim = dict(claim, value=dict(depth=claim["value"]["depth"], coverage=Coverage.model_validate(claim["value"]["coverage"]).model_dump()))
            except ValidationError as exc:
                raise DomainError("failed", "invalid_coverage", "Invalid reading coverage") from exc
            version = self._row(c, db.versions, claim["subject_id"])
            if not self._paper_sources(c, version):
                raise DomainError("forbidden", "unavailable_paper", "Paper version is not currently available")
        fingerprint = digest(canonical([claim["subject_id"], claim["predicate"], claim["value"]]))
        if self._blocked(c, fingerprint=fingerprint):
            raise DomainError("forbidden", "forgotten", "This claim is suppressed by a deletion record")
        memory_id, revision = p.get("memory_id") or uid(), 1
        if p.get("memory_id"):
            old = self._current_assertion(c, memory_id)
            if old["revision"] != p.get("expected_revision"):
                raise DomainError("conflict", "revision_conflict", "Memory changed; refresh before correcting")
            if old["subject_id"] != claim["subject_id"] or old["predicate"] != claim["predicate"]:
                raise DomainError("conflict", "identity_conflict", "Correction must keep subject and predicate")
            c.execute(update(db.assertions).where(db.assertions.c.id == old["id"]).values(system_to=now))
            c.execute(delete(db.projections).where(db.projections.c.assertion_id == old["id"]))
            revision = old["revision"] + 1
            if p.get("change_kind") == "state_change":
                transition = timestamp(claim["valid_from"]) if claim["valid_from"] else None
                if transition is None or (old["valid_from"] and transition <= old["valid_from"]) or (old["valid_to"] and transition >= old["valid_to"]):
                    raise DomainError("failed", "invalid_transition", "A state change needs a known start within the previous valid interval")
                prior = dict(old, id=uid(), revision=revision, valid_to=transition, system_from=now, system_to=None)
                c.execute(db.assertions.insert().values(**prior))
                memory_id, revision = uid(), 1
        elif p.get("expected_revision") is not None:
            raise DomainError("failed", "unexpected_revision", "A new memory has no expected revision")
        row = self._base(logical_assertion_id=memory_id, revision=revision, subject_id=claim["subject_id"],
            predicate=claim["predicate"], value=claim["value"], epistemic_status="user_reported",
            lifecycle_status="active", validation_status="operator_confirmed", evidence_ids=[evidence_id],
            source_message_id=message_id,
            valid_from=timestamp(claim["valid_from"]) if claim["valid_from"] else None,
            valid_to=timestamp(claim["valid_to"]) if claim["valid_to"] else None,
            system_from=now, system_to=None)
        c.execute(db.assertions.insert().values(**row))
        data = dict(memory_id=memory_id, assertion_id=row["id"], revision=revision)
        if reading:
            session = self._base(assertion_id=row["id"], work_version_id=claim["subject_id"], created_at=now)
            c.execute(db.reading_sessions.insert().values(**session))
            data["reading_session_id"] = session["id"]
        return envelope(data), [dict(assertion_id=row["id"], revision=revision)]

    def _current_assertion(self, c, memory_id):
        row = c.execute(select(db.assertions).where(self._scope(db.assertions),
            db.assertions.c.logical_assertion_id == memory_id, db.assertions.c.system_to.is_(None))).mappings().first()
        if row is None or self._blocked(c, identity=memory_id):
            raise DomainError("not_found", "not_found", "Memory is not available")
        return dict(row)

    def _evidence_available(self, c, evidence_id):
        row = c.execute(select(db.evidence, db.sources.c.access_state).join(db.sources,
            db.evidence.c.source_version_id == db.sources.c.id).where(self._scope(db.evidence),
            self._scope(db.sources), db.evidence.c.id == evidence_id)).mappings().first()
        return bool(row and row["access_state"] == "accessible" and not self._blocked(c, identity=row["source_version_id"])
                    and self._linked_asset_available(c, row["source_version_id"]))

    def _visible_assertions(self, c, p, now):
        known = timestamp(p["known_at"]) if p.get("known_at") else now
        valid = timestamp(p["valid_as_of"]) if p.get("valid_as_of") else now
        rows = c.execute(select(db.assertions).where(self._scope(db.assertions),
            db.assertions.c.system_from <= known,
            or_(db.assertions.c.system_to.is_(None), db.assertions.c.system_to > known),
            or_(db.assertions.c.valid_to.is_(None), db.assertions.c.valid_to > valid),
            or_(db.assertions.c.valid_from.is_(None), db.assertions.c.valid_from <= valid),
            db.assertions.c.lifecycle_status.in_(["active", "proposed"]))).mappings()
        result = []
        for row in rows:
            row = dict(row)
            if p.get("valid_as_of") and row["valid_from"] is None:
                continue  # Unknown is not infinitely old.
            if self._blocked(c, identity=row["logical_assertion_id"]) or self._blocked(c, identity=row["subject_id"]):
                continue
            # v0.1 supports one AND evidence set. Empty evidence only permits a pending proposal.
            if row["evidence_ids"] and not all(self._evidence_available(c, eid) for eid in row["evidence_ids"]):
                continue
            result.append(row)
        return result

    def _public_assertion(self, row, *, historical=False):
        result = dict(memory_id=row["logical_assertion_id"], assertion_id=row["id"], revision=row["revision"],
            subject_id=row["subject_id"], predicate=row["predicate"], value=row["value"],
            epistemic_status=row["epistemic_status"], lifecycle_status=row["lifecycle_status"],
            validation_status=row["validation_status"], evidence_ids=row["evidence_ids"],
            valid_from=row["valid_from"], valid_to=row["valid_to"], system_from=row["system_from"],
            system_to=None if historical else row["system_to"])
        # A later system_to would reveal a correction the historical client could not know.
        return result

    def _memory_recall(self, c, p, now):
        if p["project_ids"]:
            raise DomainError("failed", "unsupported_filter", "Project-linked memory filtering is not implemented yet")
        data = dict(confirmed_facts=[], reported_facts=[], inferences=[], open_questions=[], conflicts=[])
        response = envelope(data)
        for row in self._visible_assertions(c, p, now):
            if p["query"] and p["query"].casefold() not in canonical(row["value"]).casefold() + row["predicate"].casefold():
                continue
            group = "reported_facts" if row["epistemic_status"] == "user_reported" else "inferences"
            if row["lifecycle_status"] == "proposed":
                group = "open_questions"
            if sum(len(v) for v in data.values()) >= p["limit"]:
                response["status"] = "partial"
                break
            data[group].append(self._public_assertion(row, historical=bool(p.get("known_at"))))
            for eid in row["evidence_ids"]:
                response["citations"].append(self._citation(c, eid))
            if row["valid_from"] is None:
                response["uncertainties"].append(dict(kind="unknown_valid_time", subject_id=row["logical_assertion_id"],
                    message="Applicability start is unknown; not evidence of historical applicability"))
        return response, []

    def _memory_propose(self, c, p, now):
        for eid in p["evidence_ids"]:
            if not self._evidence_available(c, eid):
                raise DomainError("not_found", "missing_evidence", "Evidence is unavailable")
        if self._blocked(c, identity=p["subject_id"], fingerprint=digest(canonical([p["subject_id"], p["predicate"], p["value"]]))):
            raise DomainError("forbidden", "forgotten", "This claim is suppressed")
        row = self._base(logical_assertion_id=uid(), revision=1, subject_id=p["subject_id"], predicate=p["predicate"],
            value=p["value"], epistemic_status="inferred", lifecycle_status="proposed", validation_status="pending",
            evidence_ids=p["evidence_ids"], source_message_id=None, valid_from=timestamp(p["valid_from"]) if p["valid_from"] else None,
            valid_to=timestamp(p["valid_to"]) if p["valid_to"] else None, system_from=now, system_to=None)
        c.execute(db.assertions.insert().values(**row))
        return envelope(dict(proposal_id=row["logical_assertion_id"], decision="pending", resulting_memory_id=None,
                             validation_reasons=["Requires a source-aware validator; inference cannot set user reading"], conflicts=[])), []

    def _memory_retract(self, c, p, now):
        old = self._current_assertion(c, p["memory_id"])
        if old["revision"] != p["expected_revision"]:
            raise DomainError("conflict", "revision_conflict", "Memory changed; refresh before retracting")
        c.execute(update(db.assertions).where(db.assertions.c.id == old["id"]).values(system_to=now))
        row = dict(old, id=uid(), revision=old["revision"] + 1, lifecycle_status="retracted", system_from=now, system_to=None)
        c.execute(db.assertions.insert().values(**row))
        c.execute(delete(db.projections).where(db.projections.c.assertion_id == old["id"]))
        return envelope(dict(memory_id=p["memory_id"], revision=row["revision"])), []

    def _reading(self, c, version_id, p, now):
        result = dict(sections={}, unread_sections=[], whole_paper=False, understanding_status="not_assessed")
        depths = {"unknown": 0, "skim": 1, "deep": 2}
        for row in self._visible_assertions(c, p, now):
            if row["subject_id"] != version_id or row["predicate"] != "reading" or row["epistemic_status"] != "user_reported" or row["lifecycle_status"] != "active":
                continue
            value = row["value"]
            depth, coverage = value["depth"], value["coverage"]
            for section in coverage["sections"] + (["*"] if coverage["whole_paper"] else []):
                if depths[depth] > depths[result["sections"].get(section, "unknown")]:
                    result["sections"][section] = depth
            result["unread_sections"].extend(coverage["unread_sections"])
        result["whole_paper"] = "*" in result["sections"]
        result["unread_sections"] = sorted(set(result["unread_sections"]) - set(result["sections"]))
        result["read_status"] = "read" if result["sections"] else "unknown"
        catalog = self._catalog_version(c, version_id)
        if catalog:
            result["initial_read_status"] = catalog["initial_read_status"]
            result["workflow_status"] = "read" if result["read_status"] == "read" else catalog["initial_read_status"]
        return result

    def _paper_sources(self, c, version):
        if self._blocked(c, identity=version["id"]) or self._blocked(c, identity=version["work_id"]):
            return []
        if self._catalog_version(c, version["id"]):
            return self._catalog_sources(c, version["id"])
        source = self._row(c, db.sources, version["source_version_id"])
        if (source["access_state"] != "accessible" or self._blocked(c, identity=source["id"])
                or not self._linked_asset_available(c, source["id"])):
            return []
        return [dict(source_snapshot_id=source["id"], role="original_text", version_relation="explicit")]

    def _paper_title(self, c, version, bindings):
        if not self._catalog_version(c, version["id"]):
            return version["title"]
        # Never return a title copied from a now unavailable primary snapshot.
        for binding in bindings:
            if binding["role"] not in {"metadata", "note"}:
                continue
            source = self._row(c, db.sources, binding["source_snapshot_id"])
            try:
                raw = json.loads(source["content"])
                title = raw.get("data", {}).get("title") or raw.get("title")
                if isinstance(title, str) and title.strip():
                    return title
            except (ValueError, AttributeError, TypeError):
                continue
        return ""

    def _candidate_papers(self, c, p, now):
        if p["project_ids"]:
            raise DomainError("failed", "unsupported_filter", "Project-linked search is not implemented yet")
        result = []
        default_configured = c.execute(select(db.catalog_defaults.c.id).where(
            self._scope(db.catalog_defaults), db.catalog_defaults.c.name == "default")).first() is not None
        for row in c.execute(select(db.versions).where(self._scope(db.versions))).mappings():
            catalog = self._catalog_version(c, row["id"])
            if default_configured and p.get("knowledge_base", "default") == "default" and not catalog:
                continue
            bindings = self._paper_sources(c, row)
            if p.get("known_at"):
                bindings = [binding for binding in bindings if not binding.get("binding_created_at")
                    or binding["binding_created_at"] <= timestamp(p["known_at"])]
            if not bindings:
                continue
            if p.get("known_at") and row["created_at"] > timestamp(p["known_at"]):
                continue
            reading = self._reading(c, row["id"], p, now)
            # Structured reading filter precedes content retrieval.
            if p["read_filter"] == "read_only" and reading["read_status"] != "read":
                continue
            if p["read_depths"] and not set(p["read_depths"]) & set(reading["sections"].values()):
                continue
            if p["coverage_filter"] and not reading["whole_paper"] and not set(p["coverage_filter"]).issubset(reading["sections"]):
                continue
            if p["coverage_filter"] and p["read_depths"] and any(
                self._section_depth(reading, section) not in p["read_depths"]
                for section in p["coverage_filter"]
            ):
                continue
            title = self._paper_title(c, row, bindings)
            if p["query"] and p["query"].casefold() not in (" ".join(
                self._row(c, db.sources, b["source_snapshot_id"])["content"] for b in bindings
            ) + title + row["identifier"]).casefold():
                continue
            result.append(dict(paper_id=row["work_id"], paper_version_id=row["id"], identifier=row["identifier"],
                version=row["version"], title=title, source_snapshot_id=bindings[0]["source_snapshot_id"],
                sources=bindings, read_status=reading["read_status"], reading=reading,
                workflow_status=reading.get("workflow_status", reading["read_status"]),
                scientific_version_state=catalog["scientific_version_state"] if catalog else "explicit",
                scientific_version=catalog["scientific_version"] if catalog else row["version"],
                identity_state=catalog["identity_state"] if catalog else "strong",
                origin="default_knowledge_library" if catalog else "personal_library"))
        return result

    def _section_depth(self, reading, section):
        ranks = {"unknown": 0, "skim": 1, "deep": 2}
        return max((reading["sections"].get(section, "unknown"),
                    reading["sections"].get("*", "unknown")), key=ranks.get)

    def _papers_search(self, c, p, now):
        if p["retrieval_mode"] == "hybrid":
            if c.dialect.name != "postgresql":
                raise DomainError("unavailable", "postgresql_required", "Hybrid retrieval requires the configured PostgreSQL backend")
            from .retrieval import PgRetrieval
            papers = self._candidate_papers(c, dict(p, query=""), now)
            by_source = {}
            for paper in papers:
                for binding in paper["sources"]:
                    by_source.setdefault(binding["source_snapshot_id"], []).append((paper, binding))
            allowed = []
            evidence_papers = {}
            for span in c.execute(select(db.evidence).where(self._scope(db.evidence))).mappings():
                matches = by_source.get(span["source_version_id"], [])
                if not matches:
                    continue
                # One source may describe more than one explicit catalog version;
                # do not silently attribute its passages to an arbitrary version.
                if len(matches) != 1:
                    continue
                paper, binding = matches[0]
                reading = paper["reading"]
                if p["read_filter"] == "read_only" and not reading["whole_paper"] and span["section"] not in reading["sections"]:
                    continue
                if p["coverage_filter"] and span["section"] not in p["coverage_filter"]:
                    continue
                if p["read_depths"] and self._section_depth(reading, span["section"]) not in p["read_depths"]:
                    continue
                if not self._evidence_available(c, span["id"]):
                    continue
                allowed.append(span["id"])
                evidence_papers[span["id"]] = paper
            hits = PgRetrieval(self).rank(c, allowed, p["query"], query_vector=p["query_vector"],
                embedding_model=p["embedding_model"], limit=min(1000, max(p["limit"] * 10, 200)))
            ranked = {}
            for hit in hits:
                paper = evidence_papers[hit["evidence_id"]]
                if paper["paper_version_id"] not in ranked:
                    ranked[paper["paper_version_id"]] = dict(paper, retrieval_score=hit["rrf_score"], evidence_ids=[])
                ranked[paper["paper_version_id"]]["evidence_ids"].append(hit["evidence_id"])
            papers = list(ranked.values())
            response = envelope(dict(papers=papers[:p["limit"]], total=len(papers),
                retrieval_mode="postgresql_fts_substring_pgvector_rrf", vector_index="exact" if p["query_vector"] is not None else "not_requested"),
                status="partial" if len(papers) > p["limit"] else "ok")
            response["citations"] = [dict(self._citation(c, eid),
                source_role=next(b["role"] for b in paper["sources"] if b["source_snapshot_id"] == self._row(c, db.evidence, eid)["source_version_id"]),
                version_relation=next(b["version_relation"] for b in paper["sources"] if b["source_snapshot_id"] == self._row(c, db.evidence, eid)["source_version_id"]))
                for paper in papers[:p["limit"]] for eid in paper["evidence_ids"]]
            return response, []
        papers = self._candidate_papers(c, p, now)
        return envelope(dict(papers=papers[:p["limit"]], total=len(papers),
            retrieval_mode="exact_identifier_and_unicode_substring", vector_index="not_configured"),
            status="partial" if len(papers) > p["limit"] else "ok"), []

    def _citation(self, c, eid):
        if not self._evidence_available(c, eid):
            raise DomainError("not_found", "missing_evidence", "Evidence is unavailable")
        span = self._row(c, db.evidence, eid)
        source = self._row(c, db.sources, span["source_version_id"])
        return dict(evidence_id=eid, source_snapshot_id=source["id"], source_kind=source["kind"],
            source_sha256=source["sha256"], extraction_version=source["extraction_version"],
            source_url=source["source_url"], locator=dict(span["locator"], section=span["section"], quote_hash=span["quote_hash"]),
            support_type="context_only", access_state=source["access_state"])

    def _papers_get_evidence(self, c, p, now):
        candidates = {paper["paper_version_id"]: paper for paper in self._candidate_papers(c, p, now)}
        if p["paper_version_id"] not in candidates:
            return envelope(dict(passages=[]), status="not_found"), []
        paper = candidates[p["paper_version_id"]]
        bindings = {b["source_snapshot_id"]: b for b in paper["sources"]}
        passages, citations = [], []
        for span in c.execute(select(db.evidence).where(self._scope(db.evidence),
            db.evidence.c.source_version_id.in_(bindings))).mappings():
            if p["evidence_ids"] and span["id"] not in p["evidence_ids"]:
                continue
            if p["read_filter"] == "read_only" and not paper["reading"]["whole_paper"] and span["section"] not in paper["reading"]["sections"]:
                continue
            if p["read_depths"] and self._section_depth(paper["reading"], span["section"]) not in p["read_depths"]:
                continue
            if p["coverage_filter"] and span["section"] not in p["coverage_filter"]:
                continue
            if p["query"] and p["query"].casefold() not in span["text"].casefold():
                continue
            binding = bindings[span["source_version_id"]]
            citation = dict(self._citation(c, span["id"]), paper_id=paper["paper_id"], paper_version_id=paper["paper_version_id"],
                source_role=binding["role"], version_relation=binding["version_relation"])
            passages.append(dict(text=span["text"], evidence_id=span["id"], section=span["section"],
                source_sha256=citation["source_sha256"], locator=citation["locator"], support_type="context_only",
                source_role=binding["role"], version_relation=binding["version_relation"]))
            citations.append(citation)
            if len(passages) >= p["max_passages"]:
                break
        response = envelope(dict(passages=passages), status="ok" if passages else "not_found")
        response["citations"] = citations
        return response, []

    def _projects_upsert(self, c, p, now):
        internal_id = str(uuid.uuid5(uuid.NAMESPACE_URL, canonical([self.owner_id, self.scope_id, "project", p["project_id"]])))
        existing = c.execute(select(db.projects).where(self._scope(db.projects), db.projects.c.id.in_([p["project_id"], internal_id]))).mappings().first()
        if existing:
            if existing["revision"] != p["expected_revision"]:
                raise DomainError("conflict", "revision_conflict", "Project changed")
            c.execute(update(db.projects).where(db.projects.c.id == existing["id"]).values(title=p["title"], revision=existing["revision"] + 1))
            revision = existing["revision"] + 1
        else:
            if p["expected_revision"] is not None:
                raise DomainError("conflict", "revision_conflict", "Project does not exist")
            # External project keys are scoped, never cross-owner primary keys.
            c.execute(db.projects.insert().values(**self._base(id=internal_id, title=p["title"], revision=1)))
            revision = 1
        return envelope(dict(project_id=existing["id"] if existing else internal_id, revision=revision)), []

    def _projects_snapshot(self, c, p, now):
        project = self._row(c, db.projects, p["project_id"])
        return envelope(dict(project_id=project["id"], title=project["title"], revision=project["revision"],
            status="unknown", sync_status="not_connected", evidence_ids=[], observed_at=None)), []

    def _sources_set_access(self, c, p, now):
        source = self._row(c, db.sources, p["source_snapshot_id"])
        if source["revision"] != p["expected_revision"]:
            raise DomainError("conflict", "revision_conflict", "Source changed")
        if self._blocked(c, identity=source["id"]):
            raise DomainError("forbidden", "forgotten", "Forgotten source cannot be restored")
        affected = [source["id"]]
        meta = c.execute(select(db.snapshot_metadata).where(self._scope(db.snapshot_metadata),
            db.snapshot_metadata.c.source_version_id == source["id"])).mappings().first()
        if meta:
            obj = self._row(c, db.source_objects, meta["source_object_id"])
            affected = list(c.execute(select(db.snapshot_metadata.c.source_version_id).where(
                self._scope(db.snapshot_metadata), db.snapshot_metadata.c.source_object_id == obj["id"])).scalars())
            c.execute(update(db.source_objects).where(db.source_objects.c.id == obj["id"]).values(
                access_state=p["state"], revision=obj["revision"] + 1))
        for sid in affected:
            current = self._row(c, db.sources, sid)
            c.execute(update(db.sources).where(db.sources.c.id == sid).values(access_state=p["state"], revision=current["revision"] + 1))
        c.execute(update(db.scopes).where(db.scopes.c.id == self.scope_key).values(access_epoch=db.scopes.c.access_epoch + 1))
        c.execute(delete(db.projections).where(self._scope(db.projections)))
        if p["state"] != "accessible":
            from .retrieval import purge_sources
            purge_sources(c, list(self._dependent_asset_sources(c, affected)), self.owner_id, self.scope_id)
        return envelope(dict(source_snapshot_id=source["id"], revision=source["revision"] + 1, state=p["state"])), []

    def _tombstone(self, c, target, fingerprint, now):
        if not self._blocked(c, identity=target):
            c.execute(db.tombstones.insert().values(**self._base(target_id=target, fingerprint=fingerprint, created_at=now)))

    def _memory_forget(self, c, p, now):
        assertion_ids, source_ids, memory_ids = set(), set(), set()
        for target in p["target_ids"]:
            if self._blocked(c, identity=target):
                continue
            rows = list(c.execute(select(db.assertions).where(self._scope(db.assertions),
                or_(db.assertions.c.logical_assertion_id == target, db.assertions.c.id == target))).mappings())
            if rows:
                memory_ids.update(row["logical_assertion_id"] for row in rows)
            elif p["scope"] == "memory_and_source_snapshots":
                version = c.execute(select(db.versions).where(self._scope(db.versions), db.versions.c.id == target)).mappings().first()
                source = c.execute(select(db.sources).where(self._scope(db.sources), db.sources.c.id == target)).mappings().first()
                if version:
                    self._tombstone(c, version["id"], digest(version["identifier"]), now)
                    source_ids.add(version["source_version_id"])
                    memory_ids.update(c.execute(select(db.assertions.c.logical_assertion_id).where(
                        self._scope(db.assertions), db.assertions.c.subject_id == target)).scalars())
                elif source:
                    source_ids.add(source["id"])
                else:
                    raise DomainError("not_found", "not_found", "Deletion target is not available")
            else:
                raise DomainError("not_found", "not_found", "Deletion target is not an available memory")
        source_ids = self._catalog_dependent_sources(c, p["target_ids"], source_ids)
        # Forgetting a registered object suppresses every imported revision and
        # its stable external identity, including future connector replays.
        object_ids = set()
        for meta in c.execute(select(db.snapshot_metadata).where(self._scope(db.snapshot_metadata))).mappings():
            if meta["source_version_id"] in source_ids:
                object_ids.add(meta["source_object_id"])
        for oid in object_ids:
            obj = self._row(c, db.source_objects, oid)
            self._tombstone(c, oid, obj["identity_hash"], now)
            source_ids.update(c.execute(select(db.snapshot_metadata.c.source_version_id).where(
                self._scope(db.snapshot_metadata), db.snapshot_metadata.c.source_object_id == oid)).scalars())
            c.execute(update(db.source_objects).where(db.source_objects.c.id == oid).values(
                external_id="", head_source_version_id=None, access_state="deleted", revision=obj["revision"] + 1))
            c.execute(update(db.snapshot_metadata).where(db.snapshot_metadata.c.source_object_id == oid).values(
                external_version=None, missing_parts=[]))
            c.execute(delete(db.sync_failures).where(self._scope(db.sync_failures),
                db.sync_failures.c.identity_hash==obj['identity_hash']))
            c.execute(update(db.sync_items).where(self._scope(db.sync_items),
                db.sync_items.c.identity_hash==obj['identity_hash']).values(
                expected_version=None,state='suppressed',snapshot_id=None,error_code=None,next_retry_at=None))
        source_ids = self._dependent_asset_sources(c, source_ids)
        self._catalog_forget(c, source_ids, now)
        for alignment in c.execute(select(db.annotation_alignments).where(self._scope(db.annotation_alignments))).mappings():
            if alignment['annotation_snapshot_id'] in source_ids or alignment['pdf_source_id'] in source_ids:
                c.execute(update(db.annotation_alignments).where(db.annotation_alignments.c.id == alignment['id']).values(report={}))
                c.execute(update(db.annotation_alignment_resolutions).where(
                    self._scope(db.annotation_alignment_resolutions),
                    db.annotation_alignment_resolutions.c.alignment_id == alignment['id']).values(report={}))
        for link in c.execute(select(db.attachment_asset_links).where(self._scope(db.attachment_asset_links))).mappings():
            if link['pdf_source_id'] in source_ids:
                c.execute(update(db.attachment_asset_links).where(db.attachment_asset_links.c.id == link['id']).values(provenance={}))
        for version in c.execute(select(db.versions).where(self._scope(db.versions))).mappings():
            if version["source_version_id"] in source_ids:
                self._tombstone(c, version["id"], digest(version["identifier"]), now)
                memory_ids.update(c.execute(select(db.assertions.c.logical_assertion_id).where(
                    self._scope(db.assertions), db.assertions.c.subject_id == version["id"])).scalars())
        # Captured raw text can repeat a forgotten statement. Purge its source,
        # and conservatively purge other memories sharing that same capture.
        changed = True
        while changed:
            changed = False
            for row in c.execute(select(db.assertions).where(self._scope(db.assertions))).mappings():
                if row["logical_assertion_id"] in memory_ids:
                    assertion_ids.add(row["id"])
                    if row["source_message_id"]:
                        captured = self._row(c, db.captures, row["source_message_id"])
                        if captured["source_version_id"] not in source_ids:
                            source_ids.add(captured["source_version_id"])
                            changed = True
                for eid in row["evidence_ids"]:
                    span = self._row(c, db.evidence, eid)
                    if span["source_version_id"] in source_ids and row["logical_assertion_id"] not in memory_ids:
                        memory_ids.add(row["logical_assertion_id"])
                        changed = True
        for row in c.execute(select(db.assertions).where(self._scope(db.assertions))).mappings():
            if row["logical_assertion_id"] in memory_ids:
                assertion_ids.add(row["id"])
                fingerprint = digest(canonical([row["subject_id"], row["predicate"], row["value"]]))
                self._tombstone(c, row["id"], fingerprint, now)
                self._tombstone(c, row["logical_assertion_id"], fingerprint, now)
                c.execute(update(db.assertions).where(db.assertions.c.id == row["id"]).values(
                    subject_id="", predicate="forgotten", value={}, evidence_ids=[], source_message_id=None))
        for sid in source_ids:
            self._mark_source_asset_for_purge(c,sid,now)
            source = self._row(c, db.sources, sid)
            self._tombstone(c, sid, digest(source["identity_key"]), now)
            self._tombstone(c, "content:" + source["id"], source["sha256"], now)
            c.execute(update(db.sources).where(db.sources.c.id == sid).values(content="", source_url="", identity_key="",
                extraction_version="", access_state="deleted", revision=source["revision"] + 1))
            c.execute(update(db.evidence).where(db.evidence.c.source_version_id == sid).values(text="", section="", locator={}))
            c.execute(update(db.captures).where(db.captures.c.source_version_id == sid).values(claims=[], session_id=""))
            for version in c.execute(select(db.versions).where(self._scope(db.versions), db.versions.c.source_version_id == sid)).mappings():
                self._tombstone(c, version["id"], digest(version["identifier"]), now)
                c.execute(update(db.versions).where(db.versions.c.id == version["id"]).values(
                    identifier="forgotten:" + version["id"], version="forgotten", title=""))
                # Preserve no title when all versions of a work are forgotten.
                live = [v for v in c.execute(select(db.versions).where(db.versions.c.work_id == version["work_id"])).mappings()
                        if not self._blocked(c, identity=v["id"])]
                if not live:
                    c.execute(update(db.works).where(db.works.c.id == version["work_id"]).values(title="", identity_key="forgotten:" + version["work_id"]))
        c.execute(update(db.scopes).where(db.scopes.c.id == self.scope_key).values(deletion_epoch=db.scopes.c.deletion_epoch + 1))
        c.execute(delete(db.projections).where(self._scope(db.projections)))
        from .retrieval import purge_sources
        purge_sources(c, list(source_ids), self.owner_id, self.scope_id)
        c.execute(update(db.jobs).where(self._scope(db.jobs), db.jobs.c.status.in_(["pending", "running"])).values(status="cancelled", error_code="deletion_epoch_changed"))
        deletion_job = self._base(recorded_at=now, cleanup_state="canonical_complete", cleared_memory_count=len(memory_ids),
                                 cleared_source_count=len(source_ids), backup_status="deployment_policy_required")
        c.execute(db.deletion_jobs.insert().values(**deletion_job))
        pending_assets=c.execute(select(db.source_assets.c.id).where(self._scope(db.source_assets),
            db.source_assets.c.state=='purge_pending')).first() is not None
        return envelope(dict(deletion_job_id=deletion_job["id"], serving_blocked=True, canonical_cleanup_complete=True,
            asset_cleanup='pending' if pending_assets else 'not_needed',
            provider_cleanup="not_configured", backup_status="deployment_policy_required",
            cleared_memory_count=len(memory_ids), cleared_source_count=len(source_ids),
            note="Shared capture dependencies are conservatively purged; database file/backup retention requires deployment policy")), []

    def _export(self, c, p, now):
        # This is an authorized logical export, not a disaster recovery backup.
        result = {}
        visible_work_titles = {}
        for version in c.execute(select(db.versions).where(self._scope(db.versions))).mappings():
            bindings = self._paper_sources(c, version)
            if bindings:
                visible_work_titles[version["work_id"]] = self._paper_title(c, version, bindings)
        for table in (db.works, db.versions, db.sources, db.evidence, db.captures, db.assertions, db.projects):
            rows = []
            for row in c.execute(select(table).where(self._scope(table))).mappings():
                row = dict(row)
                if table == db.works and row["id"] not in visible_work_titles:
                    continue
                if table == db.works:
                    row["title"] = visible_work_titles[row["id"]]
                if table == db.versions:
                    bindings = self._paper_sources(c, row)
                    if not bindings:
                        continue
                    row["title"] = self._paper_title(c, row, bindings)
                if self._blocked(c, identity=row["id"]) or ("logical_assertion_id" in row and self._blocked(c, identity=row["logical_assertion_id"])):
                    continue
                if "source_version_id" in row:
                    source = self._row(c, db.sources, row["source_version_id"])
                    if source["access_state"] != "accessible" or not self._linked_asset_available(c, source['id']):
                        continue
                if table == db.sources and (row["access_state"] != "accessible" or not self._linked_asset_available(c, row['id'])):
                    continue
                if table == db.assertions and row["evidence_ids"] and not all(self._evidence_available(c, eid) for eid in row["evidence_ids"]):
                    continue
                rows.append(row)
            result[table.name] = rows
        result["tombstones"] = [dict(row) for row in c.execute(select(db.tombstones).where(self._scope(db.tombstones))).mappings()]
        return envelope(result), []

    def run_one_job(self):
        """A local rebuildable projection, committed with version/epoch validation."""
        with self.storage.transaction() as c:
            epoch = self._epoch(c)
            job = c.execute(select(db.jobs).where(self._scope(db.jobs), db.jobs.c.status == "pending")
                            .order_by(db.jobs.c.created_at).with_for_update(skip_locked=True)).mappings().first()
            if job is None:
                return dict(status="idle")
            valid = epoch["deletion_epoch"] == job["deletion_epoch"] and epoch["access_epoch"] == job["access_epoch"]
            for dependency in job["input_ids"]:
                row = self._row(c, db.assertions, dependency["assertion_id"])
                valid = valid and row["revision"] == dependency["revision"] and row["system_to"] is None and row["lifecycle_status"] == "active"
                valid = valid and not self._blocked(c, identity=row["logical_assertion_id"])
                valid = valid and all(self._evidence_available(c, eid) for eid in row["evidence_ids"])
                if valid:
                    c.execute(db.projections.insert().values(**self._base(assertion_id=row["id"], revision=row["revision"], search_text=canonical(row["value"]))))
            c.execute(update(db.jobs).where(db.jobs.c.id == job["id"]).values(status="complete" if valid else "cancelled", attempts=job["attempts"] + 1,
                error_code=None if valid else "stale_input"))
            return dict(status="complete" if valid else "cancelled", job_id=job["id"])
