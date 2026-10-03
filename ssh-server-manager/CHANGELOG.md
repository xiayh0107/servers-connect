# Changelog

## Unreleased

- Open the local UI on a searchable Hosts inventory, replacing overview cards
  with a compact actionable list and explicit empty/loading/error states.
- Add read-only host details with full notes, saved connection status, focus and
  browser-history handling, and a one-click managed SSH command with clipboard retry.
- Keep existing connection APIs and loopback/session/CSRF/Origin/vault protections;
  add isolated no-network fixtures and UI interaction/security regressions.
- Fix accent selection being shadowed by the categorical tag-color helper.
- Keep narrow-screen host details inside the available viewport when scrollbars
  are visible, and give the tag filter a readable full-width row.


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
