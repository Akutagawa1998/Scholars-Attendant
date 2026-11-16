"""PostgreSQL FTS + exact pgvector retrieval with canonical serving fences.

Embeddings are rebuildable records provided by an adapter. This module neither
invents embeddings nor asserts semantic quality for a configured provider.
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import bindparam, text

from . import storage as db
from .service import DomainError

if TYPE_CHECKING:
    from .service import ResearchService


def _vector(values):
    """Validate before passing a vector literal as a bound SQL parameter."""
    try:
        if isinstance(values, (str, bytes)) or not 1 <= len(values) <= 16000:
            raise ValueError
        result = [float(value) for value in values]
        if not all(math.isfinite(value) and abs(value) <= 3.402823466e38 for value in result):
            raise ValueError
        if not any(result):
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise DomainError("failed", "invalid_embedding", "Embedding must be a finite, nonzero vector with 1–16000 dimensions") from None
    return result


def _literal(values):
    # Numeric values were already checked. The string is still a SQL bind value.
    return "[" + ",".join(format(value, ".17g") for value in values) + "]"


def purge_sources(connection, source_ids, owner_id, scope_id):
    """Delete only this scope's derived vectors in the caller's transaction."""
    if connection.dialect.name != "postgresql" or not source_ids:
        return
    query = text(
        "DELETE FROM evidence_embeddings WHERE owner_id=:owner AND scope_id=:scope "
        "AND source_version_id IN :source_ids"
    ).bindparams(bindparam("source_ids", expanding=True))
    connection.execute(query, dict(owner=owner_id, scope=scope_id, source_ids=list(source_ids)))


class PgRetrieval:
    def __init__(self, service: ResearchService):
        self.service = service
        if service.storage.engine.dialect.name != "postgresql":
            raise DomainError("unavailable", "postgresql_required", "Hybrid retrieval requires the PostgreSQL backend")

    def _dimensions(self, connection, model):
        return connection.scalar(text(
            "SELECT dimensions FROM evidence_embeddings "
            "WHERE owner_id=:owner AND scope_id=:scope AND embedding_model=:model LIMIT 1"
        ), dict(owner=self.service.owner_id, scope=self.service.scope_id, model=model))

    def put_embedding(self, evidence_id, vector, model, *, expected_source_revision,
                      expected_deletion_epoch, expected_access_epoch):
        values = _vector(vector)
        if not isinstance(model, str) or not model.strip() or len(model) > 200:
            raise DomainError("failed", "invalid_embedding_model", "An explicit embedding model/version is required")
        s = self.service
        with s.storage.transaction() as connection:
            epoch = s._epoch(connection)
            if (epoch["deletion_epoch"] != expected_deletion_epoch or epoch["access_epoch"] != expected_access_epoch):
                raise DomainError("conflict", "stale_epoch", "Embedding inputs were deleted or their access changed")
            span = s._row(connection, db.evidence, evidence_id)
            source = s._row(connection, db.sources, span["source_version_id"])
            if source["revision"] != expected_source_revision:
                raise DomainError("conflict", "stale_source_revision", "Embedding source revision changed")
            if not s._evidence_available(connection, evidence_id):
                raise DomainError("forbidden", "unavailable_evidence", "Embedding source is not accessible")
            dimensions = self._dimensions(connection, model)
            if dimensions is not None and dimensions != len(values):
                raise DomainError("conflict", "embedding_dimension_mismatch", "Model dimensions differ from existing embeddings in this scope")
            connection.execute(text("""
                INSERT INTO evidence_embeddings
                    (id, owner_id, scope_id, evidence_id, source_version_id,
                     source_revision, embedding_model, dimensions, embedding)
                VALUES (:id, :owner, :scope, :evidence_id, :source_id,
                        :source_revision, :model, :dimensions, CAST(:embedding AS public.vector))
                ON CONFLICT (evidence_id, embedding_model) DO UPDATE SET
                    source_revision=EXCLUDED.source_revision, dimensions=EXCLUDED.dimensions,
                    embedding=EXCLUDED.embedding
                WHERE evidence_embeddings.owner_id=EXCLUDED.owner_id
                  AND evidence_embeddings.scope_id=EXCLUDED.scope_id
            """), dict(id=str(uuid4()), owner=s.owner_id, scope=s.scope_id,
                evidence_id=evidence_id, source_id=source["id"], source_revision=source["revision"],
                model=model, dimensions=len(values), embedding=_literal(values)))
        return dict(evidence_id=evidence_id, embedding_model=model, dimensions=len(values))

    def rank(self, connection, allowed_evidence_ids, query, *, query_vector=None,
             embedding_model=None, limit=50):
        """Return IDs/scores only; caller renders passages from canonical rows.

        ``allowed_evidence_ids`` must already reflect version/reading/coverage
        policy. Scope/access/deletion checks are repeated here for defense in
        depth. The per-scope lock also fences concurrent forget/access writes.
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise DomainError("failed", "invalid_limit", "Retrieval limit must be between 1 and 1000")
        if not isinstance(query, str):
            raise DomainError("failed", "invalid_query", "Query must be text")
        s = self.service
        s._epoch(connection)
        allowed = sorted(set(allowed_evidence_ids))
        if not allowed:
            return []
        parameters = dict(owner=s.owner_id, scope=s.scope_id, allowed=allowed, query=query)
        # This predicate is deliberately canonical, even when an index row is
        # stale or an upstream caller accidentally supplied an out-of-scope ID.
        canonical_from = """
            FROM evidence_spans e
            JOIN source_versions s ON s.id=e.source_version_id
            WHERE e.id IN :allowed
              AND e.owner_id=:owner AND e.scope_id=:scope
              AND s.owner_id=:owner AND s.scope_id=:scope
              AND s.access_state='accessible' AND e.text<>''
              AND NOT EXISTS (
                SELECT 1 FROM tombstones t
                WHERE t.owner_id=:owner AND t.scope_id=:scope
                  AND t.target_id IN (e.id, s.id)
              )
        """
        lexical = []
        if query.strip():
            lexical_query = text("""
                SELECT e.id AS evidence_id,
                    GREATEST(ts_rank_cd(to_tsvector('simple'::regconfig, e.text),
                        websearch_to_tsquery('simple'::regconfig, :query)),
                        CASE WHEN strpos(lower(e.text),lower(:query))>0 THEN 1.0 ELSE 0.0 END) AS lexical_score
            """ + canonical_from + """
                AND (to_tsvector('simple'::regconfig,e.text) @@ websearch_to_tsquery('simple'::regconfig,:query)
                     OR strpos(lower(e.text),lower(:query))>0)
                ORDER BY lexical_score DESC, e.id
            """).bindparams(bindparam("allowed", expanding=True))
            lexical = list(connection.execute(lexical_query, parameters).mappings())
        vectors = []
        if query_vector is not None:
            values = _vector(query_vector)
            if not isinstance(embedding_model, str) or not embedding_model.strip():
                raise DomainError("failed", "embedding_model_required", "Vector queries require the embedding model/version")
            dimensions = self._dimensions(connection, embedding_model)
            if dimensions is None:
                raise DomainError("unavailable", "embedding_model_unavailable", "No embeddings for this model exist in the authorized scope")
            if dimensions != len(values):
                raise DomainError("conflict", "embedding_dimension_mismatch", "Query dimensions differ from the embedding model")
            parameters.update(model=embedding_model, dimensions=len(values), vector=_literal(values))
            vector_query = text("""
                SELECT e.id AS evidence_id, (
                    SELECT v.embedding <=> CAST(:vector AS public.vector)
                    FROM evidence_embeddings v
                    WHERE v.evidence_id=e.id AND v.owner_id=:owner AND v.scope_id=:scope
                      AND v.source_version_id=s.id AND v.source_revision=s.revision
                      AND v.embedding_model=:model AND v.dimensions=:dimensions
                ) AS vector_distance
            """ + canonical_from + """
                AND EXISTS (
                    SELECT 1 FROM evidence_embeddings v
                    WHERE v.evidence_id=e.id AND v.owner_id=:owner AND v.scope_id=:scope
                      AND v.source_version_id=s.id AND v.source_revision=s.revision
                      AND v.embedding_model=:model AND v.dimensions=:dimensions
                ) ORDER BY vector_distance, e.id
            """).bindparams(bindparam("allowed", expanding=True))
            vectors = list(connection.execute(vector_query, parameters).mappings())
        merged = {}
        for kind, rows in [("lexical", lexical), ("vector", vectors)]:
            for position, row in enumerate(rows, start=1):
                hit = merged.setdefault(row["evidence_id"], dict(evidence_id=row["evidence_id"],
                    lexical_score=0.0, vector_distance=None, rrf_score=0.0))
                hit["rrf_score"] += 1.0 / (60 + position)
                if kind == "lexical":
                    hit["lexical_score"] = float(row["lexical_score"])
                else:
                    hit["vector_distance"] = float(row["vector_distance"])
        if not query.strip() and query_vector is None:
            fallback = text("SELECT e.id AS evidence_id " + canonical_from + " ORDER BY e.id").bindparams(bindparam("allowed", expanding=True))
            for eid in connection.execute(fallback, parameters).scalars():
                merged[eid] = dict(evidence_id=eid, lexical_score=0.0, vector_distance=None, rrf_score=0.0)
        return sorted(merged.values(), key=lambda hit: (-hit["rrf_score"], hit["evidence_id"]))[:limit]
