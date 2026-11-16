# Working on Scholars-Attendant

The Python core stores evidence, reading history, and permissions for a local research library. The OpenClaw TypeScript plugin and Codex skills provide paper collection and authoring workflows. Use synthetic fixtures for development and examples.

- Preserve the separation between imported sources, authoring, and confirmed reading. Only a trusted capture adapter may supply user-origin evidence.
- On a request to collect a paper, resolve its canonical URL and reuse the selected collection's existing entry when available. Write only the requested fields. A long-form write-up or translation requires a separate explicit request. Use a new page or a user-selected managed block.
- Connectors use the operator's explicitly selected account and library. Keep credentials, asset paths, logs, database snapshots, and personal content outside source control and public examples.
- Use the synthetic demo and regression suite described in [CONTRIBUTING.md](CONTRIBUTING.md). Preserve existing migration revisions and add a new revision for a schema change.
