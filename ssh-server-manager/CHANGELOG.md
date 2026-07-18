# Changelog

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
