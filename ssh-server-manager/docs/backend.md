# Shared backend and local API

The backend now supports multiple shells without replacing SQLite, OpenSSH,
the OS vault, or the existing frontend. CLI and HTTP use the same application
services. The frontend successor and its desktop presentation remain pending
the dedicated Paper design phase; `serverctl app` and `ui-next` are not shipped
by this backend upgrade.

## Boundaries

```text
CLI ------------------+
                      +--> ApplicationServices --> existing DB / SSH / Vault
FastAPI /api, /api/v1 -+

LocalRuntime --> loopback FastAPI --> existing browser UI
```

`application.py` owns host CRUD and config rendering, credential operations,
file operations, saved paths, diagnostics, contexts, Skills and imports.
Construction and inventory/workspace reads do not acquire the vault or start
SSH. Skill readiness reads local frontmatter; it never executes a skill body.
HTTP keeps cookie/CSRF/reveal authorization and transfer staging. CLI owns
arguments, local prompts, terminal checks and output. CLI never calls HTTP.

Core validation and schema version 5 remain authoritative. Config rendering
follows successful host/credential/import mutations, matching legacy behavior;
a filesystem failure while rendering does not roll back an already committed
database mutation. Saved-path and note edits remain local metadata operations.

## Runtime

`serverctl web` and its compatible `serverctl ui` alias use one `LocalRuntime`.
All previous flags, including `--credential`, `--url-file`, `--status`, and
`--stop`, remain available. One managed runtime runs at a time: an exclusive
OS lock in the private runtime directory is held for the runtime's lifetime, so
even simultaneous launches cannot both serve. The kernel releases the lock if
the process dies. The status record is written before the launch URL file, so
`--status`/`--stop` see a runtime as soon as its URL is usable. The launch URL
stays inside the process or an explicitly requested private file; it is never
printed. Access logging remains disabled. On macOS and Linux a fixed `--port`
can be reused immediately after `--stop`.

`LocalRuntime.start()` starts the backend on a worker thread and returns after
Uvicorn startup. A future native shell can use its private `launch_url` while
keeping its GUI event loop on the main thread. `stop()` or context-manager exit
joins the worker and cleans the runtime record and socket. Startup errors and
timeouts also clean resources. Browser mode serves in the foreground and opens
the browser after readiness. The loopback socket remains bound between port
selection and Uvicorn startup.

## API v1

All endpoints require the existing browser session established by consuming the
single-use launch URL. Read `/api/v1/runtime` first for the session CSRF token,
version, shell, authentication metadata and capabilities. Capabilities describe
implemented backend operations; native-window/dialog capabilities are false.

| Resource | Routes and operations |
|---|---|
| Runtime | `GET /api/v1/runtime` |
| Hosts | `GET, POST /api/v1/hosts`; `GET, PATCH, DELETE /api/v1/hosts/{identifier}` |
| Workspace | `GET /api/v1/hosts/{identifier}/workspace` returns host, credential metadata, saved paths and Skills |
| Notes | `PUT /api/v1/hosts/{identifier}/notes`, with `mode: append, set, clear` |
| Diagnostics | `POST /api/v1/hosts/{identifier}/test` or `/diagnose`; optional bounded `timeout` |
| Files | `GET /api/v1/hosts/{identifier}/files?path=...`; explicit `POST /download`, `POST /upload` |
| Saved paths | `GET, POST, PUT /api/v1/hosts/{identifier}/paths`; `PATCH, DELETE /paths/{path_id}`; `POST /paths/{path_id}/used` |
| Credentials | `GET, POST /api/v1/credentials`; `GET, PATCH, DELETE /credentials/{identifier}` |
| Contexts/tags | `GET, POST /api/v1/contexts`; `PATCH, DELETE /api/v1/contexts/{name}` |
| Skills | `GET, POST /api/v1/skills`; `GET /skills/discover`; `POST /skills/{identifier}/refresh`; `PUT /skills/{identifier}/servers`; `DELETE /skills/{identifier}` |
| Host Skills | `PUT /api/v1/hosts/{identifier}/skills` |
| Import | `POST /api/v1/import/preview`, `POST /api/v1/import/apply` |
| Reveal authentication | `POST /api/v1/auth/master/enroll`, `/master/verify`, `/passkey/register/options`, `/passkey/register/verify`, `/reveal/options`, `/reveal/verify` |
| Secret display | `POST /api/v1/credentials/{identifier}/reveal` after credential-specific reauthentication |

Host identifiers accept IDs or aliases. Host-list `query` searches alias,
hostname, username, tags and notes; `tag` is a case-insensitive exact tag filter.
Lists preserve the core's ordering and return arrays. Creates return `201`.
Host and credential updates are `PATCH` only — partial updates, with no `PUT`
full-replacement form. Omit fields to retain them, send `credential_id: null` to
detach a credential, and send an empty tags/jumps array to clear that list.
Other host fields reject explicit null. Contexts are addressed by name in the
path: percent-encode it as one segment (`Team A/B` → `Team%20A%2FB`); `PATCH`
accepts `new_name` and/or `server_ids`, and `DELETE` takes no body. Bulk
context `server_ids` are database IDs. Credential creation is discriminated by `kind`
and rejects fields belonging to another credential kind.

Every write/action requires `X-CSRF-Token`. Downloads remain POST with a JSON
`path`, returning binary content and a filename header; uploads use multipart
`file` and `path`. Both share the legacy 100 MiB limit and private temporary
staging cleanup. Versioned transfer results omit the internal staging path.
File browsing is read-only but does initiate an explicit SFTP operation.

All API responses are `no-store`. Output DTOs whitelist fields, and ordinary
credential/workspace responses contain no secret value. Reveal grants remain
credential-specific, expire after 30 seconds and are consumed once across both
API versions. IDs and labels resolve to the same grant identity. Session expiry,
Host/Origin checks, CSP and OpenSSH host-key verification remain enforced.
Validation errors never echo supplied input, including passwords.

Expected v1 errors use `{"ok": false, "error": "Type", "message": "..."}`.
Status codes are `400` for core validation/authentication errors, `401` for a
missing/expired session, `403` for Origin/CSRF failures, `404` for missing
resources, `409` for conflicts, `422` for typed request validation, and `502`
for SSH failures. Legacy endpoints retain their success payloads and status
codes. WebAuthn ceremony documents retain their algorithm-specific JSON shape.

## Offline contract and verification

Production `/docs`, `/redoc` and `/openapi.json` remain disabled. Generate the
versioned schema without reading real user state or acquiring the vault:

```bash
.venv/bin/python scripts/export-openapi.py --output contracts/api-v1.openapi.json
.venv/bin/python scripts/export-openapi.py --check contracts/api-v1.openapi.json
.venv/bin/python -m pytest tests/
```

The artifact exposes only v1 routes and their referenced DTOs. Its security
requirements specify cookie and CSRF together for mutations. CI checks drift
in the existing Python OS matrix, installing with
`-c contracts/constraints.txt` so the FastAPI/pydantic versions that shape the
JSON are pinned. To upgrade them, raise the pins, regenerate the artifact and
review its diff. After the Paper design, a frontend API client
can be generated from this artifact without opening production schema routes.

Regression coverage verifies CLI/legacy/v1 inventory parity, config side effects,
offline workspace reads, authentication/CSRF on every v1 operation, secret-free
validation/output, shared reveal grants, transfer limits/cleanup and real
loopback background-server startup/shutdown. It uses disposable data, runtime,
SSH config paths and a memory vault, with no real remote-host tests.
