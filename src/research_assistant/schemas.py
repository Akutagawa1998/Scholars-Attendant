"""Strict boundary schemas. Identity and system time never come from a model."""
from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")

class Mutation(Request):
    idempotency_key: str = Field(min_length=1, max_length=200)

class Coverage(Request):
    whole_paper: bool = False
    sections: list[str] = Field(default_factory=list)
    unread_sections: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def coherent(self):
        self.sections = sorted(set(self.sections))
        self.unread_sections = sorted(set(self.unread_sections))
        if any(not section.strip() for section in self.sections + self.unread_sections):
            raise ValueError("section identifiers must not be empty")
        if set(self.sections) & set(self.unread_sections):
            raise ValueError("a section cannot be read and unread")
        if self.whole_paper and self.unread_sections:
            raise ValueError("whole_paper conflicts with unread_sections")
        return self

class Timed(Request):
    valid_from: datetime | None = None
    valid_to: datetime | None = None

    @field_validator("valid_from", "valid_to")
    @classmethod
    def aware(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("timezone required")
        return value

    @model_validator(mode="after")
    def ordered(self):
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must follow valid_from")
        return self

class Claim(Timed):
    subject_id: str
    predicate: str
    value: dict[str, Any]
    quote: str = Field(min_length=1)

    @model_validator(mode="after")
    def normalize_reading(self):
        if self.predicate == "reading":
            if set(self.value) != {"depth", "coverage"} or self.value["depth"] not in {"unknown", "skim", "deep"}:
                raise ValueError("reading needs depth and coverage")
            self.value = {"depth": self.value["depth"], "coverage": Coverage.model_validate(self.value["coverage"]).model_dump()}
        return self

class Capture(Mutation):
    message_id: str
    session_id: str
    text: str = Field(min_length=1)
    claims: list[Claim] = Field(min_length=1)

    @model_validator(mode="after")
    def quoted(self):
        if any(c.quote not in self.text for c in self.claims):
            raise ValueError("claim quote must occur in the captured message")
        return self

class Passage(Request):
    text: str = Field(min_length=1)
    section: str
    locator: dict[str, Any]

class ImportPaper(Mutation):
    identifier: str = Field(min_length=1)
    title: str = Field(min_length=1)
    version: str = Field(min_length=1)
    source_url: str
    content: str = Field(min_length=1)
    media_type: Literal["text/markdown", "text/plain", "text/html", "application/pdf"] = "text/markdown"
    extraction_version: str = "fixture-v1"
    passages: list[Passage] = Field(min_length=1)

class Statement(Mutation):
    trusted_message_id: str
    claim_index: int = Field(default=0, ge=0)
    memory_id: str | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    change_kind: Literal["correction", "state_change"] = "correction"

class MarkRead(Statement, Timed):
    paper_version_id: str
    depth: Literal["unknown", "skim", "deep"]
    coverage: Coverage

class Proposal(Mutation, Timed):
    subject_id: str
    predicate: str
    value: dict[str, Any]
    evidence_ids: list[str] = Field(default_factory=list)
    candidate_type: Literal["extracted_fact", "inference", "preference_hypothesis", "project_link", "correction_proposal"] = "inference"

class Recall(Request):
    query: str = ""
    valid_as_of: datetime | None = None
    known_at: datetime | None = None
    project_ids: list[str] = Field(default_factory=list)
    limit: int = Field(default=50, ge=1, le=200)

    @field_validator("valid_as_of", "known_at")
    @classmethod
    def aware(cls, value):
        return Timed.aware(value)

class Search(Recall):
    knowledge_base: Literal["default", "all"] = "default"
    read_filter: Literal["all", "read_only"] = "all"
    read_depths: list[Literal["skim", "deep"]] = Field(default_factory=list)
    coverage_filter: list[str] = Field(default_factory=list)
    retrieval_mode: Literal["substring", "hybrid"] = "substring"
    query_vector: list[float] | None = Field(default=None, min_length=1, max_length=16000)
    embedding_model: str | None = None

    @model_validator(mode="after")
    def retrieval_settings(self):
        if self.query_vector is not None:
            import math
            if self.retrieval_mode != "hybrid" or not self.embedding_model:
                raise ValueError("vector search needs hybrid mode and an embedding model")
            if not all(math.isfinite(value) for value in self.query_vector):
                raise ValueError("vector values must be finite")
        return self

class EvidenceRequest(Search):
    paper_version_id: str
    evidence_ids: list[str] = Field(default_factory=list)
    max_passages: int = Field(default=10, ge=1, le=50)

    @model_validator(mode="after")
    def exact_evidence(self):
        if self.retrieval_mode != "substring" or self.query_vector is not None:
            raise ValueError("use hybrid papers.search first, then request its exact evidence IDs")
        return self

class Retract(Mutation):
    memory_id: str
    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=1)

class Forget(Mutation):
    target_ids: list[str] = Field(min_length=1)
    scope: Literal["memory_only", "memory_and_source_snapshots"] = "memory_only"

class SourceAccess(Mutation):
    source_snapshot_id: str
    expected_revision: int = Field(ge=1)
    state: Literal["accessible", "inaccessible", "deleted"]

class ProjectUpsert(Mutation):
    project_id: str
    title: str
    expected_revision: int | None = Field(default=None, ge=1)

class ProjectSnapshot(Request):
    project_id: str

OPERATIONS = {
    "capture.message": Capture, "papers.import": ImportPaper,
    "papers.mark_read": MarkRead, "papers.search": Search,
    "papers.get_evidence": EvidenceRequest, "memory.recall": Recall,
    "memory.propose": Proposal, "memory.record_user_statement": Statement,
    "memory.retract": Retract, "memory.forget": Forget,
    "sources.set_access": SourceAccess, "projects.upsert": ProjectUpsert,
    "projects.snapshot": ProjectSnapshot, "export": Request,
}


class Registration(Mutation):
    provider: Literal['notion', 'zotero']
    source_instance_id: str = Field(min_length=1, max_length=200)
    namespace: str = Field(min_length=1, max_length=500)

class JsonPassage(Request):
    text: str = Field(min_length=1)
    json_path: list[str | int]
    section: str = 'metadata'

class SourceSnapshot(Registration):
    external_id: str = Field(min_length=1, max_length=500)
    external_version: str | None = None
    source_url: str = ''
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    observed_at: datetime
    occurred_at: datetime | None = None
    completeness: Literal['complete', 'partial', 'unknown'] = 'unknown'
    missing_parts: list[str] = Field(default_factory=list)
    change: Literal['upsert', 'delete', 'access_revoked', 'restore'] = 'upsert'
    passages: list[JsonPassage] = Field(default_factory=list)

    @field_validator('observed_at', 'occurred_at')
    @classmethod
    def aware(cls, value):
        return Timed.aware(value)

    @model_validator(mode='after')
    def validate_paths(self):
        from urllib.parse import urlsplit, parse_qsl
        from html import unescape
        from .source_privacy import CREDENTIAL_KEYS, SIGNED_QUERY_KEYS, URL_PATTERN
        # Signed URLs and credentials stay out of the canonical snapshot.
        def inspect(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    normalized = key.lower().replace('-', '_')
                    if normalized in CREDENTIAL_KEYS:
                        raise ValueError('credentials cannot be stored in a snapshot')
                    inspect(child)
            elif isinstance(value, list):
                for child in value:
                    inspect(child)
            elif isinstance(value, str):
                for match in URL_PATTERN.finditer(value):
                    parsed = urlsplit(unescape(match.group(0)))
                    if parsed.username or parsed.password or any(k.lower().replace('-','_') in SIGNED_QUERY_KEYS for k,_ in parse_qsl(parsed.query)):
                        raise ValueError('temporary signed URLs must be redacted by the connector')
        inspect(self.raw_payload)
        inspect(self.source_url)
        for passage in self.passages:
            current: Any = self.raw_payload
            try:
                for component in passage.json_path:
                    current = current[component]
            except (KeyError, TypeError, IndexError):
                raise ValueError('evidence JSON path does not resolve') from None
            if not isinstance(current, str) or current != passage.text:
                raise ValueError('evidence must equal the decoded source value at its JSON path')
        return self

class Checkpoint(Registration):
    cursor: str
    run_id: str
    source_snapshot_ids: list[str]
    snapshot_complete: bool
    source_consistent: bool

class SourceSearch(Recall):
    provider: Literal['notion', 'zotero'] | None = None
    source_instance_id: str | None = None
    namespace: str | None = None

class SourceEvidence(Request):
    source_snapshot_id: str
    evidence_ids: list[str] = Field(default_factory=list)


OPERATOR_OPERATIONS = frozenset({"capture.message", "sources.register", "sources.ingest", "sources.checkpoint"})
OPERATIONS.update({
    "sources.register": Registration, "sources.ingest": SourceSnapshot,
    "sources.checkpoint": Checkpoint, "sources.search": SourceSearch,
    "sources.get_evidence": SourceEvidence,
})

class VerifiedLibraryLink(Request):
    provider: Literal["notion", "zotero"]
    namespace: str = Field(min_length=1)
    url: str = Field(min_length=1)
    checked_at: datetime
    valid: Literal[True]

    @field_validator("checked_at")
    @classmethod
    def aware(cls, value):
        return Timed.aware(value)

class CatalogConfigure(Mutation):
    registration_ids: list[str] = Field(min_length=1)
    verified_links: list[VerifiedLibraryLink] = Field(min_length=1)
    initial_read_status: Literal["unread"] = "unread"

class CatalogBind(Mutation):
    source_snapshot_id: str
    title_path: list[str | int] | None = None
    identity_paths: list[list[str | int]] | None = None

OPERATIONS.update({"catalog.configure": CatalogConfigure, "catalog.bind": CatalogBind,
                   "catalog.status": Request})
OPERATOR_OPERATIONS = OPERATOR_OPERATIONS | {"catalog.configure", "catalog.bind"}
