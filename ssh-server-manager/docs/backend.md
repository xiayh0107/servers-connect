# Shared backend and local API

CLI and HTTP use the same application services over SQLite, OpenSSH and the OS
credential vault. The React + TypeScript + Vite frontend replaces the former
browser UI and uses `/api/v1` throughout. Its packaged build is served locally;
Node.js is a development dependency. Desktop presentation remains optional
future work: `serverctl app` and `ui-next` are not shipped.

## Boundaries

```text
CLI ------------------+
                      +--> ApplicationServices --> existing DB / SSH / Vault
FastAPI /api, /api/v1 -+

LocalRuntime --> loopback FastAPI --> bundled React browser UI
```

`application.py` owns host CRUD and config rendering, credential operations,
file operations, saved paths, diagnostics, contexts, Skills and imports.
Construction and inventory/workspace reads do not acquire the vault or start
SSH. Skill readiness reads local frontmatter; it never executes a skill body.
HTTP keeps cookie/CSRF/reveal authorization and transfer staging. CLI owns
arguments, local prompts, terminal checks and output. CLI never calls HTTP.

Core validation and schema version 7 remain authoritative. Schema checks and
migrations, partial read/merge/write updates, and ProxyJump graph validation
hold one SQLite write transaction. Alias renames validate the resulting graph
before rewriting dependencies. Config rendering serializes its snapshot and
atomic replacement with database writers so an older snapshot cannot replace
a newer configuration. Rendering is invoked after the mutation commits; it
does not support being called inside an uncommitted external DB transaction.
Config rendering
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
version, shell, authentication metadata and capabilities. `ui` is `modern`;
`session.expires_at` is the actual UTC expiration and `server_time` supports a
clock-correct countdown. `vault.name` and `vault.status` describe the last
explicitly observed backend; initially the name can be null and status is
`unknown`. Reading runtime never opens or health-checks the vault. Capabilities
describe implemented operations; native-window/dialog capabilities are false.

| Resource | Routes and operations |
|---|---|
| Runtime | `GET /api/v1/runtime` |
| Hosts | `GET, POST /api/v1/hosts`; `GET, PATCH, DELETE /api/v1/hosts/{identifier}` |
| Workspace | `GET /api/v1/hosts/{identifier}/workspace` returns host, credential metadata, saved paths and Skills |
| Route | `GET /api/v1/hosts/{identifier}/route` returns actual managed hops, external boundaries and cached `blocked_by` |
| Notes | `PUT /api/v1/hosts/{identifier}/notes`, with `mode: append, set, clear` |
| Diagnostics | `POST /api/v1/hosts/{identifier}/test` or `/diagnose`; optional bounded `timeout` |
| Batch checks | `POST /api/v1/hosts/test` with `identifiers`, optional `timeout` and `concurrency` |
| Files | `GET /api/v1/hosts/{identifier}/files?path=...&name_filter=...`; explicit `POST /download`, `POST /upload`; `POST /files/metadata` for the optional precise-metadata probe |
| Saved paths | `GET, POST, PUT /api/v1/hosts/{identifier}/paths`; `PATCH, DELETE /paths/{path_id}`; `POST /paths/{path_id}/used` |
| Credentials | `GET, POST /api/v1/credentials`; `GET, PATCH, DELETE /credentials/{identifier}` |
| Credential tests | `POST /api/v1/credentials/{identifier}/test` returns NDJSON stages/result; `GET /credentials/{identifier}/tests` |
| Credential deletion | `GET /api/v1/credentials/{identifier}/deletion-preview`; `POST /credentials/{identifier}/detach-delete`; `GET /credential-deletions?pending=true`; `POST /credential-deletions/{operation_id}/retry` |
| Private keys | `POST /api/v1/credentials/key/inspect`; `POST /credentials/key/validate` |
| Local metadata | `POST /api/v1/local/key-browser/scopes`; `POST /local/key-browser/list`; `GET /local/ssh-agent` |
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
`geo` accepts a manual `{country_code, city?}` override or null to clear it.
Other host fields reject explicit null. Contexts are addressed by name in the
path: percent-encode it as one segment (`Team A/B` → `Team%20A%2FB`); `PATCH`
accepts `new_name` and/or `server_ids`, and `DELETE` takes no body. Bulk
context `server_ids` are database IDs. Credential creation is discriminated by `kind`
and rejects fields belonging to another credential kind.

Host responses include nullable, saved `facts`, `host_key`, `geo`,
`last_resolved_ip`, and `last_success_at`, plus local `address_scope`,
`proxy_jump_details` and `jump_for_count`. Only explicit tests resolve DNS and
inspect local known-host keys; successful diagnosis stores sanitized system
facts. `verified` requires a negotiated SHA256 key matching a saved ordinary
host key; missing evidence remains `unknown`. No key is accepted by HTTP.
Failure retains prior system facts and last-success history. Hostname/port
changes clear derived observations, and late old-endpoint results are ignored.
Country/city is currently manual only; no online lookup occurs.

Route expansion follows OpenSSH: only the first explicit jump inherits its
own route, because later jumps receive a command-line `-J` override. External
aliases remain marked boundaries; their external configuration is not inferred.
Batch checks resolve every selection before remote work, deduplicate aliases
and IDs, accept 1–64 hosts and 1–4 workers, and share four slots across batches
in the service instance. Results preserve selection order; per-host failures
and deletion while queued do not abort other results.

Every write/action requires `X-CSRF-Token`. Downloads remain POST with a JSON
`path`, returning binary content and a filename header; uploads use multipart
`file`, `path`, and optional `overwrite` (default false). Both share the legacy
100 MiB limit and private temporary staging cleanup. Download size is verified
in the SFTP namespace before transfer and checked again afterward. Nonregular
files and symlinks whose target size cannot be verified are refused by web
download; CLI `get` remains available without the web limit.

Default upload returns 409 with `error_code: exists` and sanitized `existing`
metadata if the target exists. A new upload goes to a random temporary file in
the same parent, then an SFTP hardlink atomically publishes it without replacing
a concurrently created target. Failure attempts to remove only the temporary
file. Servers without hardlink support return `unsupported` (501), with no
direct-put fallback. Explicit `overwrite=true` permits direct put. CLI `put`
keeps its existing overwrite behavior. Web file paths reject dot/parent
segments and trailing directory paths; local staging filenames are fixed.
Versioned transfer results omit the internal staging path.
File browsing is read-only but does initiate an explicit SFTP operation.

Directory responses keep SFTP display fields and numeric `uid`/`gid`. A bounded
optional Python3 probe enriches visible entries with owner/group names, precise
UTC `modified_at` and bounded `link_target`, only when SFTP type/size/IDs match.
Missing shell/Python leaves enrichment nullable and numeric ownership intact;
the display date is never used to guess UTC time. Metadata stdout/stderr are
limited while reading to 8 MiB/64 KiB. Filtering and directory-first/name
sorting precede the 5,000-entry response cap; `total` counts parsed entries,
`matching_total` counts matches, and `truncated` describes the filtered result.

Directory browsing prefers a one-exec listing: `python3` over the managed SSH
connection runs `os.scandir` and returns every display field plus the precise
metadata in a single round trip. The OpenSSH SFTP subsystem costs a handshake
and then one `lstat` round trip per entry, which dominates browsing on a
high-latency link; on a host with 387 entries the one-exec listing answered in
84 ms against 335 ms over SFTP. It is only used after the shell has been shown
to share SFTP's filesystem namespace — SFTP resolves `~`, the shell resolves
`~`, and the two must agree. A jail or any failed check keeps the SFTP listing
in charge, and transfers are always authorized from `stat_file`, which still
asks SFTP directly. `enriched: true` marks a response that already carries
precise metadata, so the UI skips the optional probe for it.

That route decision, and every fact it is based on, lives in
`ssh_server_manager/remote_listing.py`; the runner only performs the remote
calls. None of it is permanent: a namespace verdict expires after ten minutes
and is tied to the host identity it was proven for (hostname, port, user), so a
repointed host, a changed account or a jail that was lifted is noticed while the
app runs. A host that cannot answer at all is retried after a two-minute
back-off rather than being pinned to SFTP until the process restarts, and a host
whose shell is definitively jailed is not attempted at all — previously every
folder paid for an exec that was then discarded. Concurrent requests share one
in-flight check per host, so two folders opened together pay for one namespace
handshake instead of two.

The probe is a second remote round trip, so the web UI does not wait for it:
`GET /hosts/{identifier}/files` returns the SFTP listing alone (`enrichment_ms`
is `null`), and the page then sends the names it is showing to
`POST /hosts/{identifier}/files/metadata`. That request repeats each entry's
SFTP type, size, `uid` and `gid` as a claim, so the server still applies the
same type/size/ID agreement check before returning anything: a shell in a
different namespace contributes nothing, and an answer for a folder that has
since been replaced is dropped by the page. The probe is optional throughout —
a failure leaves the listing exactly as SFTP reported it and never replaces the
folder with an error. `latency_ms` remains the SFTP phase because it is also
the connection health sample; `total_ms` reports what a one-request caller (the
CLI path) actually waited.

All API responses are `no-store`. Output DTOs whitelist fields, and ordinary
credential/workspace responses contain no secret value. Reveal grants remain
credential-specific, expire after 30 seconds and are consumed once across both
API versions. IDs and labels resolve to the same grant identity. Session expiry,
Host/Origin checks, CSP and OpenSSH host-key verification remain enforced.
Validation errors never echo supplied input, including passwords.

## Credential verification and deletion

Schema 7 preserves previous host observations and adds credential revisions,
nullable cached key metadata, independent credential-test history, and a
durable credential-deletion journal. Inventory reads use saved metadata only;
they do not parse key files, query the agent, or open the vault. At most 50 test
results are retained per credential. `last_test` applies to its current
revision; replacing a secret or changing metadata invalidates that current
result without erasing prior test history.

`POST /credentials/{id}/test` takes `{host_id, timeout?}` and tests the selected
credential against any saved host, temporarily overriding the target binding.
It does not change that binding or host health records, and executes no remote
command. NDJSON emits sanitized `stage` events followed by a `result` event.
Password tests permit only password authentication; private-key tests permit
only the selected public key; agent tests use agent identities without private
key/default-identity fallback. Normal host connections continue to follow the
managed OpenSSH configuration. A server that accepts `none` authentication
returns `credential-not-used`, not a verified credential. Disconnecting the
stream cancels the probe. Errors and progress contain no raw SSH/vault output.

Key inspection accepts `{key_path}` and returns `key_metadata` plus an opaque
`key_revision`. Metadata contains format, algorithm, SHA256 public fingerprint,
bits, encryption status and a platform-specific permissions assessment. An
encrypted key inspected without its passphrase has nullable algorithm,
fingerprint and bits, with `status: passphrase_required`. Unsupported formats
or algorithms return explicit errors; unavailable permission evidence is
`unknown`, never an assumed safe mode. Validation accepts `passphrase`, or
`credential_id` with `use_stored_passphrase: true`, and loads OpenSSH/PEM key
bytes in process memory using cryptography. It does not put the passphrase in
argv, environment variables, logs, temporary files or an AskPass script.

Pass the returned `expected_key_revision` when saving a changed key. The token
is scoped to the browser session, canonical path and file snapshot, expires
after two minutes, and cannot be reused for a different file or session. Save
revalidates the file snapshot before database commit; a changed file aborts the
mutation and restores any prior vault value. `expected_updated_at` provides
credential edit concurrency checks. Omitted, null or empty-string secret fields
preserve existing values; whitespace is preserved literally. Only
`clear_passphrase: true` clears a saved passphrase, and replacement plus clear
in the same request is rejected. Private key contents are never returned.

The Web key picker is a local path browser, not a native desktop dialog. Create
a session scope with `{root_path?}` (default `~/.ssh`), then list one directory
with `{scope_id, path?, offset?}`. Scopes expire after five minutes. Listings
return path/name/type only, skip symlinks and junctions, and reject paths
outside the authorized canonical root. Identity checks prevent directory
replacement from widening the scope. Session expiration removes all scopes and
key revisions. Agent inspection calls only local `ssh-add -l -E sha256`, with a
bounded timeout and identity count; no private key is loaded or returned.

Per-credential OS locks serialize vault changes, reveal, tests, host binding
changes and deletion across threads and processes. Ordinary `DELETE` still
rejects any referenced credential. Explicit detach/delete first reads a
deletion preview, then posts `{operation_id, expected_updated_at,
expected_host_ids}`. In one database transaction it checks the snapshot,
detaches the complete saved reference set, removes the credential and records
cleanup work. A stale preview returns conflict before committing. Deletion
revokes that credential's reveal grants in every live browser session.

After commit, password/passphrase vault slots and managed-config cleanup are
retried from the journal. Success returns 200 with `status: complete`; a cleanup
failure returns 202 with `committed: true`, `cleanup_pending: true` and remaining
slots. It does not undo the detached host state or report that secrets were
removed. Pending-operation list/retry survive runtime restarts; reuse of the
same operation ID is idempotent for the same deletion. Default DELETE retains
the existing credential response fields plus operation/cleanup metadata.
Neither path deletes the user's private-key file.

Expected v1 errors use `{"ok": false, "error": "Type", "message": "..."}`.
Status codes are `400` for core validation/authentication errors, `401` for a
missing/expired session, `403` for Origin/CSRF failures, `404` for missing
resources, `409` for conflicts, `422` for typed request validation, and `502`
for SSH failures. V1 file failures also provide `error_code`: permission denied
maps to 403, missing file/directory to 404, existing target to 409, oversize to
413, and unsupported safe publication to 501; other transport/file errors use
502. Legacy transfers keep 400 for oversize, 409 for upload conflicts and 502
for file-operation failures. WebAuthn ceremony documents retain their
algorithm-specific JSON shape.

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
review its diff. The frontend's generated TypeScript DTOs come from this
artifact; `npm run check-types --prefix frontend` checks drift without opening
production schema routes. `npm run build --prefix frontend` writes the bundled
assets, and `check-build` verifies reproducibility, manifest integrity and
compressed-size budgets.

Regression coverage verifies CLI/legacy/v1 inventory parity, config side effects,
offline workspace reads, authentication/CSRF on every v1 operation, secret-free
validation/output, shared reveal grants, transfer limits/cleanup and real
loopback background-server startup/shutdown. It uses disposable data, runtime,
SSH config paths and a memory vault. Additional disposable loopback SSH/SFTP
fixtures cover direct, multi-hop and external-hop connections, auth failures,
large directories and transfer conflicts; they never use saved real hosts or
the developer's credential vault.
