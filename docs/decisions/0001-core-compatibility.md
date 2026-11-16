# Shared core and paper authoring

The research core owns source snapshots, reading events, citations, memory corrections, and deletion state. Notion and Zotero retain their original records. A cached source or a generated summary creates evidence a client can inspect. Confirmed reading requires a trusted user-origin event.

## Clients and storage

The project ships an independently packaged Python module and retains the OpenClaw `paper-collector` tool namespace and Codex skill names. The CLI and MCP proxy can use the same authenticated, loopback HTTP service. Trusted capture uses a separate operator credential. PostgreSQL is the persistent backend. SQLite supports isolated fixtures and the synthetic demo.

Scientific versions follow canonical identifiers such as a versioned arXiv ID. Connector revisions identify provider observations. Catalog searches can return an explicit scientific version or an unknown version. Literal anchor hashes verify the recorded span against the retained source bytes or provider snapshot. They support traceability. Scientific entailment requires separate assessment.

## Source and write boundaries

Notion and Zotero operators use explicit source registrations. Access and deletion fences apply to derived records, even when a stale sync job finishes later. A source import adds no personal reading. The current OpenClaw authoring interface appends to a selected managed destination or creates a new child page. Core memory and its external projections have separate write receipts.

Paper Collector 2.0 preserves collection tool names and the essential plugin configuration keys. The former whole-page clear flag is now a validation error. An API-generated summary uses its `page_id` as a parent. [Local setup](../../deploy/README.md) describes the current runtime and backup scope.
