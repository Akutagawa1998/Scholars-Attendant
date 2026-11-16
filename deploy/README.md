# Local research service

Start with the [synthetic demo](../README.md#try-the-core). Persistent operation requires PostgreSQL with pgvector. Run the service and any clients on a trusted machine. The HTTP API uses one configured owner and scope.

## Start PostgreSQL

On a fresh installation, with the package installed in `.venv` and Docker running:

```sh
docker pull pgvector/pgvector:0.8.7-pg17-bookworm
.venv/bin/python scripts/postgres-runtime.py up
```

The bootstrap uses a dedicated named container and Docker volume, publishes only a loopback port, and creates a non-superuser application login. It writes a private configuration at `~/.scholars-attendant/runtime/postgres.json`. Existing instances need their original private configuration. Keep the generated config and password files outside the repository.

To use an existing PostgreSQL database, install the `vector` extension in it and grant a dedicated non-superuser login permission to migrate the application schema. Set `RA_DATABASE_URL` privately using SQLAlchemy's `postgresql+psycopg` URL form. Initialize with `research-assistant init`, or let `serve` apply the migrations on startup. Set `RA_OWNER_ID` and `RA_SCOPE_ID` if you need stable explicit labels.

## Run HTTP and stdio MCP

Set a high-entropy `RA_API_TOKEN` in the server environment. Keep the operator's capture credential separate from it. Start the server with the bootstrap config:

```sh
.venv/bin/research-assistant \
  --runtime-config "$HOME/.scholars-attendant/runtime/postgres.json" \
  serve --port 8765
```

For an environment-configured database, run `research-assistant serve --port 8765`. The CLI restricts `--host` to loopback addresses. `GET /health` returns basic liveness. Operation requests require the bearer token.

Configure your MCP host to launch the absolute path to `.venv/bin/research-assistant-mcp` with `RA_SERVICE_URL=http://127.0.0.1:8765` and the same `RA_API_TOKEN`. Store the token in private host settings or inject it at launch. The token grants the client's access to the configured library, including export and requested deletion. Give it only to trusted clients. `RA_CAPTURE_TOKEN` belongs exclusively to a trusted human-input adapter. Without verified capture, assistants can record proposals.

## Import and store sources

The operator scripts are intended for user-triggered imports. Run `--help` on each command before connecting a library.

| Task | Entry point |
| --- | --- |
| Sync Zotero item metadata | `scripts/sync-zotero.py` |
| Import a bounded Notion bundle | `scripts/sync-notion.py` |
| Verify Zotero PDF copies | `research-assistant-verify-zotero-pdfs` |
| Configure a catalog over cached metadata | `research-assistant-configure-knowledge-library` |
| Validate the installed package on a registered external volume | `research-assistant-install-smoke` |

Zotero metadata sync uses an explicitly configured library and credentials. Notion bundle sync requires an explicit input file. Bounded samples do not advance a full inventory checkpoint. A body with unknown completeness remains partial. Configure a catalog with your own registered `--notion-namespace` and `--zotero-namespace` and matching verified URLs. The catalog and installation acceptance operators currently require a registered external store on macOS. The [synthetic demo](../README.md#try-the-core) works on ordinary local filesystems.

The PDF workflow checks registered cached bytes and can use authorized local Zotero originals. Use the supported PDF operator for canonical copies. Keep the originals and the database outside Git. When external APFS storage is registered, runtime checks validate the mount and backing image identities before writing.

## Recovery and privacy

The local PostgreSQL helpers support a captured-state backup and an isolated restore drill for a registered external store. Restrict access to dumps and PDF artifacts. Choose backup retention that matches your privacy requirements. A restore requires reconciling newer forget and revoke decisions before serving its data. Automatic ledger replay, point-in-time recovery, and automated media rotation are outside the shipped tooling.

For contributor validation and tests, see [CONTRIBUTING.md](../CONTRIBUTING.md).
