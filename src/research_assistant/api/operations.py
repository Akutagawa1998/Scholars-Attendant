"""Public operation names. Trusted message capture is deliberately separate."""

OPERATIONS = frozenset(
    {
        "papers.import",
        "papers.mark_read",
        "papers.search",
        "papers.get_evidence",
        "memory.recall",
        "memory.propose",
        "memory.record_user_statement",
        "memory.retract",
        "memory.forget",
        "projects.upsert",
        "projects.snapshot",
        "sources.set_access",
        "sources.search",
        "sources.get_evidence",
        "catalog.status",
        "export",
    }
)

MCP_OPERATIONS = {
    "papers_import": "papers.import",
    "papers_mark_read": "papers.mark_read",
    "papers_search": "papers.search",
    "papers_get_evidence": "papers.get_evidence",
    "memory_recall": "memory.recall",
    "memory_propose": "memory.propose",
    "memory_record_user_statement": "memory.record_user_statement",
    "memory_retract": "memory.retract",
    "memory_forget": "memory.forget",
    "projects_upsert": "projects.upsert",
    "projects_snapshot": "projects.snapshot",
    "sources_set_access": "sources.set_access",
    "sources_search": "sources.search",
    "sources_get_evidence": "sources.get_evidence",
    "catalog_status": "catalog.status",
    "memory_export": "export",
}


def error_result(code: str, message: str, status: str = "failed") -> dict:
    """Transport failures use the same envelope as domain failures."""
    from uuid import uuid4

    return {
        "schema_version": "1.0",
        "request_id": str(uuid4()),
        "status": status,
        "error_code": code,
        "message": message,
        "data": {},
        "citations": [],
        "source_status": [],
        "uncertainties": [{"kind": code, "message": message}],
        "next_cursor": None,
        "operation": None,
    }
