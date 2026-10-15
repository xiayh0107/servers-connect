# UI redesign: backend follow-ups

Working list of backend support that the Paper redesign (file
"SSH Server Manager · UI 重新设计") relies on. Each item records the backend
support now available and the UI fallback when an observation is unavailable.
Not published to the docs site yet.

**Current implementation:** the host metadata, route, bounded batch-check,
and Files API support below is implemented on this branch. Schema 6 and the
versioned OpenAPI contract carry the new fields. The shipped UI is still the
legacy shell; the Paper frontend can now integrate these APIs. Optional local
GeoIP loading is not implemented; manual location overrides are available.

Guiding constraints: opening the inventory or a host's details must not start
SSH, touch the vault, or call a third-party service; secrets never reach the
browser outside the existing reveal flow.

## 1. Persist the detected operating system

**Needed by:** Hosts list OS logo + "Ubuntu 24.04" line; host details System
section.

`diagnose` already collects `os_id`, `os_version`, `os_pretty`, family, kernel,
arch, CPU, memory, disk and uptime (`_summarize_remote_identity`). The last
successful identity is now stored as `facts` with `observed_at` and exposed on
the v1 `Host`/`Workspace` DTOs as read-only fields. Failed diagnosis retains
the previous facts; changing hostname or port clears endpoint observations.

- Map `os_id`/`ID_LIKE` to a closed logo key (`debian`, `ubuntu`, `rocky`,
  `alpine`, `rhel`, `fedora`, `arch`, `macos`, `freebsd`, `windows`,
  `unknown`) so the UI never renders arbitrary remote strings as asset names.
- Remote strings are untrusted: length-limit and strip control characters.

**UI fallback:** generic server icon and "OS unknown · run a check" until a
successful diagnosis records facts.

## 2. Address scope and location

**Needed by:** flag / network icon before each endpoint, "Tokyo, JP · direct".

- **Scope** (no network access needed): classify the configured hostname or
  literal IP as `public`, `private` (RFC 1918 / ULA), `lan` (`.lan`, `.local`,
  `.home.arpa`), `loopback`, or `unresolved`. Add `address_scope` to `Host`.
- **Country/city:** only from an optional local GeoIP database
  (e.g. GeoLite2 file configured by the user) or a manual per-host override.
  Never call an online lookup API — it would disclose every host IP.
  `geo: {country_code, city, source: "geoip"|"manual"} | null` is exposed;
  `POST /api/v1/hosts` and `PATCH /api/v1/hosts/{id}` accept a manual
  `{country_code, city?}` override. City may be null; `geo: null` clears it.
  The current implementation supports manual overrides only.
- Hostnames resolve to an IP only during an explicit check; record the IP
  seen by the last check (`last_resolved_ip`) instead of resolving on read.

**Implemented:** scope classification is local and literal; DNS is recorded
only by an explicit check. With no manual location, render a scope icon
without a flag. No online or automatic GeoIP lookup occurs.

## 3. Last successful check

**Needed by:** "Blocked · lab-gateway is down" and "Last OK 9 days ago".

`record_test` now retains `last_success_at` independently of the latest
`last_test_*` attempt. The UI derives *blocked* when any actual hop in
`proxy_jumps` has a failed latest attempt newer than the host's own check; no
stored state is needed for that. The route endpoint also provides `blocked_by`.

## 4. Resolved route

**Needed by:** route chain in host details (`You → bastion → prod-web-01`) and
the "Jump for N" badge.

`Host` includes `proxy_jump_details` and `jump_for_count` for inventory badges.
`GET /api/v1/hosts/{id}/route` returns ordered hops and `blocked_by`:

- Jump entries that are not managed aliases (allowed by the graph validator
  when the node is absent) should come back flagged as `external: true`.
- Each hop includes its latest cached check status. Only the first explicit
  hop inherits its own configured ProxyJump; later explicit hops are overridden
  by OpenSSH's preceding `-J` chain. External aliases are shown as external
  boundaries because their configuration is outside the managed inventory.

## 5. Host key fingerprint

**Needed by:** "Host key verified · SHA256:…" in host details.

`host_key` returns the known-hosts fingerprint and match state recorded by
the last explicit check (`verified`, `unknown`, `changed`, or `revoked`).
The check uses effective OpenSSH known_hosts settings, including hashed entries;
`verified` requires the actually negotiated SHA256 key to match a saved key.
DNS and local key inspection share a bounded metadata deadline. Never
add any "accept new key" action to the web UI; host-key changes stay a CLI
decision.

## 6. Bulk checks

**Needed by:** "Run checks" on a multi-row selection.

`POST /api/v1/hosts/test` accepts `identifiers` (1–64), `timeout` (1–120 seconds),
and `concurrency` (1–4, default 4). Aliases/IDs for one host are deduplicated;
all identifiers are resolved before any network work. Concurrent batches share
four service slots, and responses preserve selection order with a per-host
result. A host deleted while queued becomes a per-host failure.

## 7. Files page

**Needed by:** Files workspace (session tabs, breadcrumb, saved-path rail,
file table, per-row download, "Over 100 MB · use CLI" badge, Upload here).
Existing endpoints cover the flow. The following gaps found in
`ssh_runner.py`/`webapp.py` have been addressed as described below.

### 7.1 Owner/group are numeric today

`_directory_batch` runs `@ls -lan`; `-n` prints uid/gid, so `owner` comes back
as `1000`, not `deploy` as the design shows. Options: switch to `@ls -la`
(names, but `SFTP_LISTING_RE` must then accept names with unusual characters)
or return both `uid`/`gid` and `owner`/`group`. Keep the regex anchored and
count misses in `unparsed`; don't loosen it into accepting arbitrary lines.

**Implemented:** `uid`/`gid` are returned with the numeric SFTP listing. An
optional, bounded Python3 metadata probe enriches only visible entries with
owner/group names after type, size and numeric IDs match. SFTP-only hosts
continue to show numeric IDs.

### 7.2 Modification time is not machine-readable

`modified` is sftp's display string (`Oct 2 09:30`, or `Oct 2 2025` for older
files), with no year in the recent form and no timezone. The UI needs relative
labels ("Yesterday 14:12") and sorting. Add `modified_at` as ISO 8601 UTC
(e.g. from sftp `ls -l` with a parsed year rule, or a stat-based listing) and
keep `modified` for display compatibility. The remote clock/timezone is
unknown; document which one the value is in.

**Implemented:** `modified_at` is an exact UTC ISO timestamp from remote stat,
or null when the optional metadata probe is unavailable. The display string
is retained. Never guess a year or timezone from SFTP display output.

### 7.3 Symlink target

The design shows `current → 2026-10-08_1412`. OpenSSH `ls -l` output does not
include the link target, so `type: "symlink"` is all we get. If wanted, add
`link_target: str | null` resolved per symlink during the listing (bounded:
only for visible entries, one `readlink` each, skip on error). Remote string,
so length-limit and strip control characters.

**Implemented:** `link_target` is nullable, bounded and sanitized, with one
readlink per visible symlink in the optional metadata probe. Missing enrichment
keeps the symlink icon without target.

### 7.4 Size limit is enforced after the full download

`download_server_file` copies the whole remote file into the staging dir and
only then compares against `MAX_WEB_TRANSFER_BYTES`. A 412 MB file costs a full
transfer and local disk before it is rejected. Check size first (stat / the
listing's `size`) and reject with the same message before transferring; keep
the post-transfer check as a guard in case the file grew.

**Implemented:** the web backend preflights SFTP metadata and retains the
post-transfer guard. It rejects nonregular files and symlink downloads whose
target size cannot be verified; use `serverctl get` for those. SFTP is the
authority even when the SSH shell has a different chroot namespace.

The new UI should disable downloads for
`size > capabilities.max_web_transfer_bytes` from `/api/v1/runtime`;
the backend enforces the limit independently. The current limit is 100 MiB.

### 7.5 Upload overwrites silently

Upload uses `@put -p`, which replaces an existing remote file without asking.
The design's "Upload here" needs a conflict step. Add an `overwrite: bool`
form field (default `false`); when false and the target exists, return 409
a structured file error with the existing entry's size and mtime so the UI can ask.
**Implemented:** uploads default to `overwrite=false` and return 409 with
`error_code: "exists"` and sanitized existing-entry metadata on conflicts.
After preflight, the server uploads to a random file in the same parent and
publishes with an atomic SFTP hardlink, so a concurrently created target cannot
be overwritten. Temporary files are removed after publication or cleaned up
on failure. Unsupported servers return `unsupported` (v1 HTTP 501), without
falling back to direct put. `overwrite=true` explicitly allows direct put.
CLI `serverctl put` keeps its existing default overwrite behavior.

Also: the client builds `path` as `dir + "/" + file.name`. The server should
reject dot/parent path segments and trailing directory paths. The API accepts
one literal full remote path; it cannot infer the browser's intended directory.
Local staging uses fixed names independent of uploaded/remote filenames.

### 7.6 Errors need a stable code

All `SSHError`s map to 502 with only a message. The Files page shows different
states for "permission denied", "no such directory", "not a directory",
"host unreachable" and "timeout". Add an `error_code` field
(`permission_denied`, `not_found`, `not_a_directory`, `unreachable`,
`auth_failed`, `timeout`, `too_large`, `exists`, `unsupported`) parsed from sftp stderr
(`SFTP_OPERATION_ERROR_RE` already isolates these lines). Never put raw stderr
from the remote in the message without the existing sanitisation.

**Implemented:** file failures expose a stable `error_code` on v1 routes,
with bounded, redacted display messages. Metadata subprocess output is bounded
while reading (8 MiB stdout / 64 KiB stderr), with a process deadline.

### 7.7 Large directories

Listings cap at `MAX_DIRECTORY_ENTRIES = 5000` with `truncated: true`, and the
cut is on sftp order, not on the UI's sort. Not blocking: the UI shows
"Showing 5000 of N · narrow with the filter". If server-side filtering is
added, it must be a name filter applied before the cap.

**Implemented:** `GET /api/v1/hosts/{id}/files?name_filter=...` applies a
case-insensitive name filter and directory-first/name sorting before the cap.
`total` counts the entire parsed directory; `matching_total` counts matches.
`truncated` describes the filtered response.

### 7.8 Connection reuse across tabs

Each listing runs a new `sftp` process with `ControlMaster=auto` /
`ControlPersist=300` and `ControlPath=<runtime_dir>/cm-%C`. `%C` already hashes
host+user+port+jump, and `runtime_dir()` is private (`/tmp/ssm-<uid>`), so
tabs on the same host share one master. Already correct; recorded so nobody
"fixes" it:

- Tabs are a UI concept only. Don't add a per-tab socket or a "close tab →
  `ssh -O exit`" call; that would kill a master other tabs (and the CLI) use.
- Keep the runtime path short; OpenSSH limits the Unix socket path length,
  which is why it is not under the data dir on macOS/Linux.
- Every listing click is a full sftp round trip (~one RTT through the master).
  The UI debounces navigation and does not prefetch child folders.

### 7.9 Error status codes

`expected_error` maps every `SSHError` to 502 and every `ValidationError` to
400. With 7.6, map `not_found` → 404, `exists` → 409, `too_large` → 413,
`permission_denied` → 403, `unsupported` → 501 for the v1 file routes.
Legacy `/api/servers/*` keeps 400 for oversize transfers and 502 for file
operation failures; upload conflicts use 409 in both shells.

### 7.10 Contract

New fields (`modified_at`, `link_target`, `uid`/`gid`, `error_code`) and the
`overwrite` form field must be added to `api_models.py` and re-exported to
`contracts/api-v1.openapi.json` via `scripts/export-openapi.py`; all new
response fields optional/nullable so older clients still validate.

**Implemented:** the DTOs and re-exported contract contain these fields,
`matching_total`, conflict metadata, batch results, route hops, and runtime
capabilities (`bulk_checks`, `host_routes`, `file_conflicts`, and bounds).

## 8. Frontend-only (no backend work)

Recorded so they are not re-requested:

- **Hide details** mode: masking is done by not rendering values; search then
  matches only visible fields. Preference stored in browser local storage.
- **Pinned quick actions:** per-browser preference in local storage.
- **Copy SSH command:** copies `serverctl connect ALIAS`, as today, so the
  vault and host-key policy stay in the path.
- **Duplicate host:** existing `GET` + `POST /api/v1/hosts` with a new alias.
- Keyboard navigation and grouping (Route / Tag / None).
