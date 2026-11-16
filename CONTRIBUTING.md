# Contributing to Scholars-Attendant

Use synthetic records and minimal reproductions. Keep real Notion links, Zotero libraries, tokens, reading notes, private storage paths, and screenshots with personal data out of reports and commits.

## Python tests

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-build-isolation '.[geometry]'
.venv/bin/python -m pytest -q
```

The SQLite tests create temporary databases. To include PostgreSQL integration coverage, set `RA_TEST_POSTGRES_URL` to a dedicated fixture database with pgvector installed and a role allowed to create schemas. The tests create and remove their own schemas. Make sure local tests can bind a temporary loopback port for HTTP and MCP. Geometry tests use the running Python by default and accept an explicit `RA_TEST_GEOMETRY_PYTHON` executable.

Use `.venv/bin/python -m build --no-isolation` for a Python sdist and wheel. Check installability and the archive paths before distributing them.

## OpenClaw tests

Use a Node.js release supported by OpenClaw. Run `npm test`. These tests use synthetic Notion and model responses. Review the log and request behavior when changing a tool that writes data or fetches a URL. The plugin package contains source, supported skills and public graphics. Use `npm pack --dry-run` to review its contents.

## Report a problem

Include the versions you ran, a minimal synthetic input, and the observed result. For package and security issues, describe the affected operation without attaching private data. Contributions are distributed under the repository's [MIT license](LICENSE).
