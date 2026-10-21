# Changelog

## Unreleased

- Add `serverctl export` / `serverctl import` for moving the inventory between
  machines and versioning it in git. The export is a deterministic JSON document
  (hosts sorted by alias, fixed key order, no timestamps) of host profiles, their
  saved working directories and their forwarding profiles. Secrets, credential
  identifiers and everything observed remotely (facts, last check results,
  resolved IPs, host keys) are left behind: a host carries its credential
  *label*, and an import attaches it only when that label already exists locally,
  reporting a warning instead of inventing one. `import` previews by default and
  writes only with `--apply`; an existing host is left alone unless
  `--overwrite` (the plan names the fields that differ), and saved directories and
  tunnels are merged rather than replaced so a file that omits something cannot
  delete work that exists here. Documents are validated strictly — unknown
  fields, a wrong format, or a future version are refused — and a malformed entry
  is reported while the rest still applies.
- Support mosh as an opt-in transport: `serverctl connect ALIAS --mosh` runs a
  mosh session that bootstraps through the managed ssh config with the same
  askpass environment and `StrictHostKeyChecking=yes`, so it is not a second
  authentication path and host-key verification is not relaxed. mosh needs a
  UTF-8 locale, so one is supplied when the terminal has none. `serverctl doctor`
  gained a `mosh` row that names the missing client instead of failing later at
  connect time, and the command fails rather than falling back to ssh when the
  user asked for mosh. Not offered on Windows.
- Add opt-in session transcripts: `serverctl connect ALIAS --log` (or
  `--log-path FILE`) runs the session through a pty proxy and records it to a
  mode-600 file under `sessions/` in the data directory, named for the host and
  start time. Only the terminal's *output* stream is recorded, so a password
  typed at a no-echo prompt never reaches the file while echoed commands do;
  terminal control sequences are stripped so the transcript can be read and
  diffed, and credential-shaped values (`--password=X`, `MYSQL_PWD=X`, bearer
  headers) are masked as best effort. A warning naming the destination is printed
  before the session starts, the start of a recording is itself audited, `--log`
  is refused together with `--mosh` and on Windows, and an environment that
  denies a pty reports that instead of leaving an empty file behind.
- Add an optional MCP wrapper for agents that only speak MCP: `serverctl mcp
  serve` speaks MCP over stdio, and `serverctl mcp tools` prints what a policy
  exposes. Read-only tools (`list_hosts`, `host_route`, `saved_paths`,
  `host_skills`, `list_tunnels`, `runtime_capabilities`) read local metadata and
  never connect or touch the vault; `test_host` requires `--allow-remote` and
  `run_command` requires `--allow-exec` plus a matching `--allow-command` glob
  (globs match the whole command line or `argv[0]`), taking an argv array rather
  than a shell string. There is no reveal tool and no tool result carries a
  secret; `--allow-exec` without any pattern is refused at startup; acting calls
  are recorded in the audit log under actor `mcp`; and oversized results are
  truncated. The Agent Skill remains the recommended path.
- Add optional local GeoIP: `serverctl geo resolve HOST...|--all [--apply]
  [--overwrite]` resolves a host's address and reads its location from a local
  file only — `SSM_GEOIP_CSV` (CIDR or start/end ranges, with header, comments
  and BOM tolerated) or `SSM_GEOIP_MMDB` when the optional `geoip2` package is
  installed. Nothing is sent to a web service, no database is bundled, and no
  read path performs a lookup or a DNS query: opening the inventory, a host, or
  a route stays local metadata only. A looked-up value is stored with
  `source: geoip` and can only be written through the resolver, so it is never
  mistaken for something a human asserted; an existing manual override is kept
  unless `--overwrite` is given. `serverctl geo status` and a `doctor` row report
  which backend is configured.
- Add recursive transfer: `serverctl put --recursive DIR ALIAS:PATH`,
  `serverctl get --recursive ALIAS:DIR [LOCAL]`, and `cp --recursive` copy a whole
  tree over the same SFTP path and credentials as a single file, creating the
  destination directory. A tree is copied as one batch of `mkdir`/`put` or `get`
  commands built from an explicit walk rather than sftp's `-R`, whose rules for
  destination creation and symlinks differ between OpenSSH versions. Symlinks
  anywhere inside the tree are refused instead of followed (a link can reach a
  private key or another volume), a remote link is refused on download, a
  symlinked root directory is refused on upload, and one call stops at 4000
  entries with a message pointing at a remote archive step.
- Keep a looked-up host location usable. A `geo` value with source `geoip`
  used to make every later profile write fail (editing notes re-validated the
  stored value against the manual-only rule) and made an export fail on import.
  A profile write now *preserves* an unchanged looked-up value without letting it
  mint a new one, `serverctl import` applies a location through the resolver's own
  entry point, and a manual override in the file still wins on import.
- Add an MCP HTTP transport for clients that cannot spawn a process:
  `serverctl mcp serve --http [--port N] [--token-file PATH]`. It is the same
  server, policy and audit trail as stdio — only the transport differs — and it
  is deliberately narrow: it binds `127.0.0.1` and refuses a non-loopback `Host`,
  requires `Authorization: Bearer` on every request (a per-launch token printed
  once to stderr, or written to a mode-600 file with `--token-file`), refuses any
  request carrying an `Origin` header so a browser page cannot drive it even with
  a stolen token, accepts only `POST /mcp`, and answers a notification with `202`
  and no body. Verified live: 401 without the token, 403 from a browser origin,
  and `lsof` showing a loopback-only listener.
- Add the Web UI for the two surfaces that only had a CLI: a **Ports** page and
  an **Audit** page. Both are GET-only, so neither can start, stop, or rewrite
  anything from a browser. `GET /api/v1/tunnels` joins saved forwarding profiles
  with the live process table (running, pid, uptime, and an `orphan` flag for a
  process whose profile was deleted); a running record now also keeps the bind
  shape so an orphaned row can still show the forward it holds, and the DTO
  allows those fields to be absent for records written before that. `GET
  /api/v1/audit` serves the append-only log newest-first with `limit`, `alias`,
  `event` and `since` filters; the response type has no value field, so a reveal
  can only ever appear as a credential label and slot. The Ports page marks a
  wildcard bind `Exposed` and a loopback bind `Loopback`, and shows an orphaned
  process as such instead of hiding it. The Audit page groups events into the
  same kinds as the CLI filter, names each in plain words, marks a masked command
  `MASKED`, and turns the range control into a `since` query instead of slicing a
  long log in the browser. Both pages are lazy chunks: Ports adds 2.2 KiB gzip and
  Audit 2.6 KiB, leaving the initial load at 86.6 of 150 KiB and the total at
  135.8 of 250 KiB.
- Rotate the audit log instead of letting it grow forever: once the live file
  reaches 5 MiB (`SSM_AUDIT_MAX_BYTES`) it becomes `audit.log.1`, older segments
  shift up, and three are kept. `serverctl audit rotate [--force]` does the same
  on demand, `audit list` and `audit path --json` report the segments in play,
  and reading spans every segment so rotating never hides history. A rotation that
  fails (a read-only data directory) warns once and the event is still appended.
- Prepare Homebrew distribution instead of leaving it as prose: 
  `packaging/homebrew/ssh-server-manager.rb.in` is the formula template and
  `scripts/prepare_packaging SDIST` renders it from the artifact that was built,
  filling the version and the sdist's real sha256 and refusing an sdist whose
  version disagrees with `__version__` or whose archive lacks
  `ssh_server_manager-<version>/`. `docs/packaging.md` documents the tap flow
  (`brew update-python-resources` fills the dependency resources) and records why
  winget is not planned: it installs a Windows installer, not a Python package.
- Record privileged actions in a local, append-only audit log: `exec` and its
  exit status, secret `reveal` (by credential label, never by value),
  `reveal_denied`, `connect`, and tunnel `open`/`close`. `serverctl audit list
  [--alias ALIAS] [--event KIND] [--since TIMESTAMP] [--limit N] [--json]` reads
  it newest-first and `audit path` names the file. The log is one JSON object per
  line, appended with a single `O_APPEND` write to a mode-600 file in the private
  data directory, so a normal database update can never rewrite history and
  concurrent CLI calls cannot interleave lines. Command lines are redacted before
  they are stored (`--password=X`, `--password X`, `MYSQL_PWD=X`,
  `Authorization: Bearer ...`, `--api-key`, `--passphrase`, ...) and fields named
  like secrets are replaced by `***`, so the log never becomes a second place a
  secret lives. A failed write warns once and never blocks the audited action;
  torn or foreign lines are skipped when reading instead of hiding the rest.
- Add saved port-forwarding profiles: `serverctl tunnel add`, `open`, `close`,
  `status`, `list`, `show`, `edit`, and `remove`. A profile stores the parsed
  halves of an OpenSSH `-L`/`-R`/`-D` spelling (never a command string), is unique
  by name across the inventory so `tunnel open db1-local` needs no alias, and
  defaults to a `127.0.0.1` bind because listening on every interface is an
  exposure rather than a convenience. `open` starts one detached `ssh -N` with
  `ExitOnForwardFailure=yes` and `StrictHostKeyChecking=yes`, waits for ssh to
  authenticate so an unknown host key or taken port fails with ssh's own message
  instead of a silent half-open tunnel, and returns the pid; re-opening reports
  `already_open`, and closing a stopped profile is a no-op. Running state lives in
  a mode-600 record in the private runtime directory rather than the database, and
  is pruned as soon as the process is gone, so a copied database never claims a
  tunnel is up. Liveness is a pid *and* the process's command line carrying that
  tunnel's marker (read natively from `/proc` or `sysctl`, with `ps` as a fallback),
  so a recycled pid can never be mistaken for a tunnel or killed by `close`.
  Deleting a host stops its tunnels first. Database schema 8 adds `server_tunnels`.
- Cut remote directory browsing to a single round trip. A listing now runs
  `os.scandir` through one managed `ssh` exec and returns the display fields
  together with precise owner names, UTC timestamps and symlink targets, instead
  of paying for the SFTP subsystem handshake plus one `lstat` round trip per
  entry and then a second probe. Measured on a 104 ms-RTT host: 2973 ms on the
  first folder and ~1.1 s afterwards, against 3.4–5.8 s before; a 387-entry
  directory lists in 84 ms against 335 ms over SFTP. The fast route is only used
  once SFTP and the shell have been shown to resolve `~` to the same directory,
  so a chrooted or jailed shell keeps the SFTP listing, and transfers are still
  authorized by an explicit SFTP `stat_file`. Responses say `enriched: true` when
  they already carry precise metadata so the UI does not probe again. Every
  decision behind the route expires instead of being remembered for the life of
  the process: the namespace verdict lasts ten minutes and is tied to the
  hostname, port and user it was proven for, so a repointed host or a lifted jail
  is noticed while the app runs; a host that cannot answer is retried after two
  minutes rather than pinned to SFTP until a restart; a definitively jailed
  shell is no longer attempted folder by folder; and concurrent folders share one
  in-flight check per host instead of each paying for it.
- Show a remote folder as soon as its SFTP listing arrives instead of waiting
  for the optional precise-metadata probe, and paint a revisited folder from a
  per-host cache before revalidating it in the background. The probe moves to
  `POST /api/v1/hosts/{identifier}/files/metadata`, which repeats each entry's
  SFTP type, size, `uid` and `gid` so the server still refuses metadata from a
  replaced file or a shell namespace that disagrees with the listing. Directory
  listings also report `enrichment_ms` and `total_ms` beside the unchanged
  `latency_ms` (still the SFTP phase, which is the connection health sample), so
  the second round trip is measurable instead of invisible.
- Report a host database or managed SSH config this app cannot write instead of
  failing host actions with an opaque `Request failed (500)`. Both are probed
  once at startup: `GET /api/v1/runtime` reports `config_problem`, the Files
  page shows it once with a retry, and `serverctl web` prints it. A read-only
  SQLite database is now a `DatabaseError` naming the file instead of a raw
  `sqlite3.OperationalError`; a failed config render is an `SSHConfigError`
  naming the directory. Tightening the config directory mode is best effort, so
  a render never depends on `chmod`.
- Keep remote commands working when the managed config's usual directory is
  not writable: they render it in the private cache instead (with a one-line
  warning naming both paths), so browsing and transfers are not blocked by a
  missing Full Disk Access grant.
- Log unexpected UI server failures. Failures no expected-error handler claims
  reach the 500 the dialog shows as `Request failed (500)`; they are now
  appended to a mode-600 `ui-errors.log` in the private runtime directory (with
  method, path and traceback, never request bodies) and the response body states
  `internal server error` instead of a blank payload.
- Load missing/stale system facts automatically after dwelling on a host name,
  replacing the inventory's collection buttons with inline field skeletons.
  Preserve saved values during refresh, serialize hover probes, reuse successful
  snapshots for 60 seconds and back off failures for 30 seconds. Page load,
  restored keyboard focus and hidden details never trigger collection.
- Restore Paper's Hosts route list and privacy mode: white flat lanes, 58px
  rows, parent/child route order and connectors, stable endpoint/credential
  columns with fixed masks, the compact privacy banner and eye toggle.
  Disable copying in privacy mode and implement the advertised Shift+H shortcut.
- Polish the Hosts quick preview: only the host name opens it (row-wide hover
  no longer does), and place it from the measured card height so it stops
  jumping to the top of the viewport on lower rows. Add a header
  **Collect host info** action for every visible host without observations,
  so an upgraded inventory backfills in one click instead of one host at a
  time; the page still never contacts hosts on load.
- Fix #10: replace the misleading OS "run a check" hint with an explicit
  **Collect host info** action; keep connectivity checks separate from system
  observation, label saved system data by observation time, and include the
  Windows OS display name in collected facts.
- Fix #11: add single-host and bulk `/api/v1/hosts/.../observe` actions and a
  selection-toolbar backfill for 1-64 hosts, concurrency 1-4 and bounded probe
  timeouts. Reuse the diagnosis system probe without SFTP, isolate per-host
  failures, share the bulk-check concurrency limit, and retain previous facts
  on failure. Add runner, API, UI and browser regression coverage.
- Fix Paper UI review regressions: suppress hover/focus previews while host
  details are hidden, preserve native keyboard activation and browser shortcuts,
  track the inventory cursor by host identity, and restore saved diagnosis/edit
  row pins. Add unit and browser regression coverage.
- Restore independent tests for unbound credentials. Show unconfirmed probe
  checkpoints rather than inferring TCP/host-key success from timeouts or local
  failures; distinguish route-wide failures from a rejected target credential.
- Refine the Hosts inventory and host profile: a rounded list card with quieter
  route groups and roomier rows, a hover/focus quick-preview card with cached
  telemetry, and a floating profile card made of section cards, icon-led
  connection rows, and segmented memory/disk meters.
- Finish the Credentials Paper pass: a 420px list plus detail pane with cached
  key-permission status, 12-dot masked secrets, usage/auth history, a Type
  dropdown that reports the API's fixed-kind rule, and 560/440/520
  add/delete/test dialogs (type cards, typed-label deletion, three-step auth
  checklist with the `ssh-copy-id` remedy). Skills now shows `Registered` in
  its list and uses the inline Discover control; Tags creation deliberately
  stays a modal.
- Align the six-page Web UI with the Paper redesign: shared frame primitives
  (eyebrow page headers, 232px sidebar with status-dot recents, relative
  timestamps, key hints, status dots), a rebuilt Hosts table (64px flex lanes,
  tag-scope chips with live counts, pinned row actions with menu sections and
  keyboard shortcuts, details-hidden masking), a floating host-profile card
  (breadcrumb pager, route diagram, telemetry rows, terminal bar), and a Files
  workspace with session tabs, panel toggles, a 240px saved-path rail and
  relative modification dates. Sections use the existing design tokens only;
  Files is reachable from a host instead of the primary navigation.
- Fix SSH config Includes for native Windows and Git/MSYS OpenSSH, and make
  SFTP use the same SSH client. Keep literal Include paths compatible with
  OpenSSH clients before and after 9.9's token expansion. Retain custom import
  sources per host without mixing IdentityFile defaults across sources;
  reject invalid paths and Include cycles before applying an import.
  Preserve literal braces, apostrophes and glob characters across Windows
  MSYS process arguments and bounded UTF-8 path conversion.
- Preserve saved credentials, notes and local metadata on import overwrite.
  Expose authentication warnings, IdentityFile paths and source-config fallback
  in CLI, API v1 and the import dialog; importing does not verify login or read
  private keys.
- End Files loading on errors and independent aborted requests; add a 45-second
  directory-read deadline, cancel and explicit retry. Ignore late responses
  after cancellation or context changes without replaying transfers.
- Fix first-use host-key confirmation for interactive `serverctl connect`
  when AskPass supplies a saved password or key passphrase. Show the OpenSSH
  confirmation in the human's controlling terminal and return only their
  explicit answer; reject missing terminals and preserve configured trust
  policies. Agent calls, Web checks, transfers and credential tests continue
  to refuse unknown keys, and changed or revoked keys are never auto-replaced.

## 0.9.0 — 2026-10-15

- Migrate host metadata to SQLite schema 6: retain sanitized successful system
  facts with `os_like`, closed `os_logo`, and observation time; last-success
  history; locally resolved IP; host-key fingerprint/state; and optional manual
  geography. Inventory and details remain offline, with literal address scope.
  Hostname/port changes invalidate observations for the previous endpoint.
- Serialize partial host/notes/path updates, route validation, schema
  initialization/migrations, and SSH-config rendering with SQLite write locks
  to preserve concurrent changes and prevent stale config replacement.
- Add saved host-route inspection and selected-host bulk tests to API v1:
  validate the full batch before remote work, cap batches at 64 hosts, and share
  four batch-check slots across requests. Advertise capabilities at runtime.
- Add nullable numeric ownership, UTC modification time, and symlink-target
  metadata for remote files when available; filter names before the 5,000-entry
  response cap and expose matching counts. Enforce the existing 100 MiB web
  transfer limit before and after downloads, refuse unverifiable symlink
  downloads, and publish default uploads without replacement using SFTP hard
  links. Fail safely when that operation is unsupported; support explicit
  overwrite and structured v1 file errors. CLI transfer behavior is unchanged.
- Add shared application services for hosts, credentials, files, saved paths,
  diagnostics, contexts, Skills and imports; CLI and legacy HTTP routes reuse
  these use cases and their SSH-config side effects.
- Add typed `/api/v1` runtime, host/workspace, file, credential, path, context,
  Skill, import and authentication contracts, with an offline OpenAPI artifact
  and CI drift check. Keep public API documentation endpoints disabled.
- Add `serverctl web`, retaining `ui` as a compatibility alias. Extract
  `LocalRuntime` with a reserved loopback socket, startup readiness, background
  lifecycle, failure cleanup and a shared status/stop record.
- Filter versioned responses, keep validation errors free of supplied secrets,
  enforce session expiry and share canonical single-use reveal grants between
  legacy and v1 routes. Preserve CSRF, host-key checks and web transfer limits.
- Replace the shipped frontend with a React + TypeScript + Vite application over
  the shared API v1 client. Complete Hosts, host details, Files, Tags, Skills and
  Credentials with browser-history selection and dialogs, explicit actions, draft
  Save/Discard flows, narrow-screen layouts and dated offline metadata.
- Add schema 7 credential revisions, nullable public-key metadata and separate
  authentication history. Stream independent credential tests against any saved
  host without changing its saved credential or health; verify the selected
  authentication method, and disable default-key/password fallback for agent
  tests. Unauthenticated connections do not verify a credential.
- Validate supported OpenSSH/PEM private keys and passphrases in memory before
  save, using session-bound file revisions to reject changed files. Preserve
  blank replacement secrets and literal passphrase whitespace; expose explicit
  passphrase clearing. Add scoped local key browsing, algorithm/fingerprint and
  platform permission metadata, and explicit SSH-agent identity inspection.
- Add credential deletion previews and explicit detach/delete with snapshot
  checks, per-credential cross-process locks and atomic host detachment.
  Persist unfinished vault cleanup for idempotent retry; retain default deletion
  refusal for referenced credentials and leave private-key files intact.
- Report real session expiry and server time, and observed vault name/status
  without implicit vault access. Revoke deleted credentials' reveal grants in
  every session; keep grants single-use for 30 seconds and display secrets for
  at most 15 seconds, clearing them on hide/close.
- Commit generated API types, the npm lockfile and packaged frontend assets;
  check type/contract drift and reproducible builds with gzip size budgets.
  Add frontend interaction tests and disposable browser acceptance fixtures for
  actual direct/multi-hop/external-jump SSH, authentication failure, SFTP
  conflicts, explicit overwrite, large directories and metadata fallback.
  Desktop packaging and automatic GeoIP remain optional future work.
- Allow an immediate fixed-`--port` restart after `--stop` on macOS/Linux, hold
  an exclusive OS lock so simultaneous launches cannot both serve, and write the
  runtime record before the launch URL file.
- API v1 updates are `PATCH`-only for hosts and credentials; contexts are
  renamed/rebound with `PATCH` and deleted without a request body at
  `/api/v1/contexts/{name}`.
  Pin the schema toolchain in `contracts/constraints.txt` for the drift check.
- Close SQLite connections after initialization and reads, including error paths,
  so disposable UI fixtures release database files before cleanup on Windows.
- Preserve global original SSH-config defaults after the final managed host.
  Parse Windows SFTP listings without inventing unavailable permission bits;
  keep binary transfers intact. Verify credential authentication after releasing
  OpenSSH's private logs, including Windows logs that prohibit concurrent reads.
- Test Python 3.10/3.13 on macOS, Linux and Windows, check the offline API
  contract and frontend build, and validate wheel/sdist installations outside
  the checkout. Upgrade populated published 0.8.0 databases through schema 7
  while retaining host/credential IDs, routes and authentication settings.
  Include the public frontend sources, test fixtures and launchers in the sdist.
- Wait for every Windows credential-test process to exit before reading private
  authentication logs, and keep stderr reads and closure in the same thread.
  Verify private-job member handles and wait for their exit within one shared
  cleanup deadline; fail closed when process ownership or exit is uncertain.
  Reject ambiguous AskPass prompts for the selected host or key. Keep idle test
  tunnels alive until explicit teardown and verify their worker cleanup.


## 0.8.0 — 2026-08-26

### Added
- `serverctl ui --credential ALIAS` deep-links first-time credential entry:
  the browser opens directly on that server's edit dialog with the
  new-credential form expanded and the password field focused, instead of
  landing on the dashboard. The target travels in the URL fragment, which
  browsers never send to the server; the one-time token flow is unchanged.
- `serverctl server add --password-credential LABEL` creates a password
  credential through the hidden local prompt and attaches it in one command.
  Without a TTY it fails fast and points to the UI path, so agent tool calls
  get a clear error instead of a hanging `getpass`.
- SKILL.md gained a "When the user wants to add a server" playbook: agents
  now drive onboarding proactively — collect non-secret fields in chat,
  create the profile, route password entry through
  `serverctl ui --credential ALIAS` (or hand terminal commands to the user),
  and finish with `server test` — instead of leaving users to discover the
  commands.

### Changed
- Agent-driven installation now treats `serverctl doctor` as a local health
  checkpoint rather than the end of onboarding. After a healthy install,
  agents are instructed to read the managed host inventory, summarize the
  actual aliases, give a few natural-language examples of what the user can
  ask next, and explain that saved SSH secrets stay in the local OS vault.
- `SKILL.md`, `docs/ai-agents.md`, both one-line installers, and the CLI/command
  references now explicitly avoid first-run CLI tutorials and diagnostic dumps
  by default. They also distinguish local readiness from an untested remote
  login and forbid adding remote tests merely to make onboarding look complete.
- Duplicate-name errors now name the way out: `server add` with an existing
  alias answers with the matching `serverctl server edit` command, and a
  taken credential label points to `credential edit` (failing *before* the
  hidden password prompt, so correcting a typo no longer means typing the
  secret twice for nothing). The `add`/`add-*` help texts say the same up
  front, and the FAQ gained the two most-hit confusion entries, so the
  edit path is discoverable before the error ever happens.
- The UI's default accent is now **teal** instead of indigo — a deliberate
  brand choice rather than the statistical default of generated interfaces.
  Indigo remains available as an accent option (now backed by explicit
  `themes.css` blocks for every color theme), and existing users keep the
  accent already stored in their browser. The landing page makes the same
  choice: teal accents, flat primary buttons without the glow halo, solid
  headline color instead of gradient text, and the embedded product demo
  now defaults to the accent the product actually ships.
- Border radii and spacing moved from ~90 ad-hoc literals onto design-token
  scales (`--radius-sm/md/lg/xl`, `--space-1..7`), consolidating eleven
  radius values and the odd one-off spacings into coherent steps; the
  lazily loaded contexts/skills/workspace/notes/diagnostics modules then
  moved onto the same scales (~190 more declarations). The raw
  asset budget grew ~2.6% for the longer `var()` names; the gzipped wire
  size grew only ~0.4%. Rationale is recorded in
  `docs/ui-ux-research.md` ("Design tokens").
- The font stack no longer names Inter, which was never bundled — the UI
  renders with the system sans font it was already using in practice.

### Fixed
- The generated docs pages' footer showed a hand-maintained version that had
  gone stale (`v0.6.0` under a `0.7.0` product); `website/build.py` now
  derives it from the package, and a test pins the two together.
- The landing page's agent install prompt stopped at "installed and checked",
  leaving first-time users with an empty inventory and no next step. The
  prompt (both languages) now asks the agent to walk the user through first
  use — previewing what their `~/.ssh/config` could import or guiding a
  first server with proper credential routing — and `docs/ai-agents.md`'s
  no-managed-hosts branch spells out that procedure instead of a vague
  "offer to import".
- **"Save server" silently did nothing** when adding a server with an
  existing credential (or OpenSSH default): the inline credential composer's
  password/key inputs kept `required` while hidden, and an invisible required
  control blocks form submission with no visible feedback. `required` now
  follows composer visibility. This was the real wall behind "adding a server
  is hard" for anyone not creating a credential inline.
- The alias `pattern` attribute never validated anything in current Chrome:
  since M136 Chrome compiles `pattern` with the RegExp `v` flag, under which
  a bare class hyphen is a syntax error that also aborts interactive
  validation. The hyphen is now escaped.

## 0.7.0 — 2026-07-26

### Added
- Per-host saved working directories. `serverctl path list|add|edit|remove`
  records where you actually work on a host, and the Host Workspace shows them
  as chips ordered by recent use, with one click to save the directory you are
  in. A directory belongs to one host, and removing the host removes its own.
- `serverctl path resolve ALIAS [ALIAS ...] --json` for agents: one read, no
  connection, and identical paths across hosts collapse into a single entry
  listing every host that has them — so "where does this fleet keep its
  application" is answered before anything is browsed or transferred.
- File transfer: `serverctl cp`, `get`, and `put` move one file over the same
  SFTP path and vault credentials as every other command. Previously the only
  documented option was raw `scp`/`rsync`, which cannot see the vault and
  prompts for a password the tool already holds.
- The web UI browses read-only as before and now offers explicit per-file
  download and upload-into-this-directory actions.
- `serverctl mount ALIAS[:REMOTE] [MOUNTPOINT]`, `--list`, and
  `serverctl unmount` expose a remote directory locally through sshfs, reusing
  the managed config and credential boundary. `serverctl doctor` gained a
  `mount` row that names the missing component — macFUSE, libfuse, or
  SSHFS-Win — because sshfs installed without its FUSE layer looks installed
  and fails every mount.
- New `docs/mounting.md`, including when *not* to mount: a mount makes remote
  latency look local, so repo-wide searches and builds belong in
  `serverctl exec`.

### Security
- The web file browser is no longer read-only, and the four documents that
  promised it were rewritten rather than left to drift. Browsing still returns
  metadata only. Transfer is a separate, explicit action: CSRF-protected `POST`
  routes (download included, since a link cannot carry the CSRF header), capped
  at 100 MB, staged inside the 0700 runtime directory and cleared when the
  request ends. Credential handling is unchanged — a transfer cannot reach a
  secret that a listing could not.
- Transfers validate the remote path before any process spawns, and treat both
  of sftp's silent failures as errors: a diagnostic on stderr alongside exit
  code 0, and a download that "succeeds" without writing the destination.

### Changed
- UI colour tokens renamed for purpose rather than appearance: the generic
  `--tint-soft`, `--tint-line`, `--tint-text`, and `--sidebar-soft` are replaced
  by `--sidebar-hover`, `--sidebar-active`, `--sidebar-line`, `--sidebar-input`,
  `--sidebar-input-line`, and `--terminal-bg` / `--terminal-text` /
  `--terminal-line`. A token now says where it belongs, so changing the sidebar
  cannot silently repaint the terminal block.
- Runtime `color-mix()` calls folded into literal colours (23 to 10 in
  `styles.css`, 4 to 3 in `contexts.css`), removing per-paint colour computation
  from the busiest surfaces.
- All six accent palettes re-tuned for contrast (66 of 97 colour values in
  `themes.css` changed; for example teal moves from `#0b8277` to `#0f766e`).
  Token names, theme modes, and every selector are unchanged, so this is a
  visual refresh rather than an API change.
- Database schema 5 adds `server_paths`. Existing databases upgrade in place;
  all four upgrade paths from schema 1 were verified.
- `python-multipart` is now a dependency, required for the upload endpoint.

## 0.6.0 — 2026-07-20

### Added
- Host-bound Agent Skills: a local skill registry and many-to-many bindings let
  one environment skill serve several related nodes and let one host compose
  several skills. The `serverctl skill` `discover`, `list`, `show`, `add`,
  `refresh`, `attach`, `detach`, and `remove` subcommands manage the
  relationships; registration can bind repeated `--server` aliases atomically.
- `serverctl skill resolve ALIAS [ALIAS ...] --json` gives agents per-host and
  deduplicated views with explicit `applies_to` partitions. Resolution returns
  metadata and local paths rather than skill bodies, and fails on missing,
  invalid, or renamed skills. Discovery reports ambiguous same-name copies
  explicitly instead of choosing by scan order.
- A Skill Library in the loopback UI supports offline discovery, explicit
  registration, readiness status, and bulk host binding. Each Connections row
  and active Host Workspace now also exposes that host's current assignments
  and opens a host-scoped picker directly.

### Changed
- Existing databases migrate automatically to schema 4 while preserving Hosts,
  credentials, tags, notes, and other saved data. Skill identity now uses a
  persisted Unicode-aware casefold key, so case variants cannot create
  ambiguous registrations across platforms.
- PyYAML 6.0 or newer is now a runtime dependency for bounded, safe parsing of
  `SKILL.md` frontmatter; normal package installation and the bundled
  installers add it automatically.
- Skill manifest paths preserve their resolved filesystem spelling, while
  discovery and validation consistently honor Windows filename case rules,
  including case-sensitive NTFS directories.

### Security
- Host-skill bindings are routing metadata, not authorization. Discovery never
  installs or executes a skill, deletion is blocked while hosts remain bound,
  and same-name paths fail closed instead of depending on discovery order.
- Frontmatter parsing enforces both a 64 KiB size limit and a nesting-depth
  limit; malformed or pathological YAML becomes an `invalid` Skill instead of
  escaping resolution or breaking the UI bootstrap.

## 0.5.0 — 2026-07-18

### Added
- Web UI accent palettes: a new header picker offers seven accent colors
  (indigo default, teal, emerald, amber, rose, violet, graphite) served from a
  dedicated `themes.css` asset. Accents layer onto the light, dark, and
  high-contrast themes, persist in browser storage, and the hardcoded indigo
  tints in the sidebar and primary buttons now follow the chosen accent. The
  core-bundle performance budget grew 100 kB → 104 kB raw (25 kB → 26 kB
  gzipped) to account for the picker markup and accent plumbing.

- `serverctl doctor` gained an `agent_skill` check: it inspects the standard
  agent skill directories (plus `SSM_SKILLS_DIRS`) and warns — with the
  update command — when a linked skill copy is older than the running CLI,
  so agents stop silently following stale SKILL.md guidance.
- The POSIX installer now links `serverctl` into `~/.local/bin` when that
  directory exists, so humans and agents can invoke it by name, and its
  closing message documents that re-running the installer updates an
  existing install.
- Troubleshooting entries for the two failure modes seen in a real agent
  transcript: bare `ssh ALIAS` black-holed by VPN/proxy fake-IP resolvers
  while `serverctl` works, and an agent hand-rolling ssh/sshpass instead of
  using the manager.
- Dev tooling: `scripts/build-ui` (beautify → edit → minify round trip for
  the committed-minified web UI assets, with budget reporting) plus an
  AGENTS.md section documenting the pipeline and its traps.

### Fixed
- Tag chips now reuse each tag's sidebar color everywhere a tag renders —
  connections table, workspace host cards, tag library, host detail rows, and
  the server editor's tag picker — instead of drawing every chip in the accent
  color. The lazy tag-manager bundle budget grew 11 kB → 11.5 kB gzipped.
- Web UI responsive layout: management tables now scroll inside their surface
  instead of overflowing the viewport, summary cards and toolbars wrap on
  narrow windows, and fixed-width tracks that caused clipped content or dead
  whitespace between 640 px and 1180 px were replaced with fluid ones.

### Changed
- SKILL.md now front-loads agent routing: the description is trigger-phrased
  (English and Chinese), names the `serverctl` binary with a resolution
  order, and a new lead section forbids the observed failure paths — raw
  `ssh`/`scp`/`sshpass` against managed hosts, asking for passwords in chat,
  and editing `~/.ssh/config`. Driven by a real transcript where an agent
  searched for a nonexistent `ssh-server-manager` binary, fell back to
  `sshpass`, and asked the user for a root password.
- Added `server note` plus UI Note actions so users and agents can set, append,
  or clear local server notes without changing SSH settings.
- `server diagnose` now identifies the host system across Linux
  distributions, macOS, BSDs, and Windows OpenSSH hosts. The remote check
  reports structured `os`, `os_id`, `os_version`, `os_family`,
  `package_manager`, and `arch` fields (os-release ID/ID_LIKE mapping with
  sw_vers, freebsd-version, and legacy release-file fallbacks) instead of
  only `PRETTY_NAME`, and no longer relies on the non-portable `uname -o`.
  Windows hosts whose default shell rejects the POSIX probe are detected via
  a `cmd.exe /c ver` fallback instead of failing the check.

## 0.4.1 — 2026-07-17

### Fixed
- Windows: `serverctl ui --status`/`--stop` no longer reports a terminated UI
  process as still running when another process holds a handle to it —
  liveness is now checked with `WaitForSingleObject` instead of assuming a
  successful `OpenProcess` means the process is alive.

## 0.4.0 — 2026-07-17

### Added
- `serverctl ui --status` and `serverctl ui --stop`: the UI records its pid
  and port in a private runtime state file, so a background instance can be
  inspected and shut down (including `--url-file` cleanup) without manual
  pid bookkeeping. Stale records are detected and removed automatically.
- Agent-experience guidance in SKILL.md: how to respond when the user asks to
  "connect" (hand the command to the human terminal, proxy follow-ups through
  `exec`), concise inventory summaries, no option menus, and UI lifecycle
  rules.

### Changed
- `serverctl connect` now fails fast with a clear message and exit code `2`
  when run without a terminal (pipes, agent tool calls, cron) instead of
  starting an interactive session that can never receive input.

## 0.3.0 — 2026-07-17

### Added
- A read-only remote Files workspace in the local UI with host selection,
  home/up navigation, breadcrumbs, direct path entry, dotfile filtering,
  refresh, and copyable `ALIAS:/absolute/path` references.
- Remote directory listing over the system OpenSSH SFTP client, reusing the
  managed config, ProxyJump chain, host-key checks, AskPass vault auth, and
  short-lived ControlMaster connections where supported.
- `serverctl doctor` now verifies that the local `sftp` executable is present.
- Multi-tag host organization, workspace tag filtering, and persisted
  system/light/dark appearance preferences.
- A lazy-loaded Tags workspace for inline creation, rename, deletion, usage
  previews, and clickable filters; Connections now supports persistent host
  selection and one searchable tag picker with checked/mixed states. New
  tags can be created and assigned atomically from that picker, which is
  also available directly in every host row. The host editor includes an
  autocomplete chip picker.

### Changed
- Reworked the local UI around a full-height workspace: hosts are always
  available in a searchable sidebar, while connection and credential setup
  live in focused secondary views. File browsing now includes local filtering,
  sortable columns, responsive layouts, and explicit loading/error/empty states.
- Tightened the visual hierarchy and 8-pixel spacing rhythm across the shell.
  Connections now uses five primary columns with grouped endpoint metadata,
  icon-and-text status semantics, focused row actions, and selected-host tests.
  Files adds labeled refresh, normalized time/access presentation, zebra rows,
  and batch reference copy. Credential protection is contextual, key paths are
  copyable, tag creation sits in the list header, and a high-contrast theme
  joins the light/dark/system choices.
- Made host tags directly editable from the Files sidebar. Each host shows
  a compact tag summary that opens the same searchable add/remove/create
  picker used by the Connections inventory.
- Added a tested frontend performance budget, early deferred script loading,
  frame-coalesced filtering, and 250-row progressive rendering for large remote
  directories. Optional tag tools load only when opened. The UI remains
  framework-free with no third-party requests.
- Made host status time-aware: explicit SSH tests and real SFTP activity update
  the observation, green expires after two minutes, and stale successes are
  shown as historical instead of indefinitely online.

### Fixed
- Periodic status refreshes now restore the enhanced host-selection column, so
  connection rows remain aligned with their headers after the UI has been open.

## 0.2.0 — 2026-07-15

First public release: on [PyPI](https://pypi.org/project/ssh-server-manager/)
(`pipx install ssh-server-manager`) and
[GitHub](https://github.com/xiayh0107/servers-connect/releases/tag/v0.2.0).

### Added
- One-line Agent Skill deployment: `install.sh` (curl | sh) and
  `install.ps1` (irm | iex) fetch the source, install dependencies, link
  the skill into every detected agent skills directory, and run `doctor`.
- `serverctl --version`.
- `serverctl doctor` now reports the serverctl version, platform, Python
  version, OpenSSH client version, and whether connection reuse
  (ControlMaster) is available.
- Installable via `pipx install` / `uv tool install`: UI assets ship inside
  the Python package and the AskPass helper is invoked as a module, so an
  installed `serverctl` no longer depends on the source checkout layout.
- `exec --reuse` on Windows now prints an explicit warning instead of being
  silently ignored.
- Vault errors on Linux now include a hint about installing and unlocking a
  Secret Service provider.
- MIT license, user documentation under `docs/`, English and Chinese READMEs,
  and a product website under `website/`.

### Fixed
- Windows: OpenSSH config parsing (`Include`, `Host`) no longer mangles
  backslash paths; verified by CI on windows-latest.

### Changed
- UI assets moved from `assets/ui/` to `scripts/ssh_server_manager/assets/ui/`.
- Published at https://github.com/xiayh0107/servers-connect with the website
  at https://xiayh0107.github.io/servers-connect/.

## 0.1.0

Initial internal version: `serverctl` CLI, loopback-only web UI, OS-vault
credential storage, OpenSSH config import/render, ProxyJump support,
WebAuthn/master-password reveal gating, cross-platform CI.
