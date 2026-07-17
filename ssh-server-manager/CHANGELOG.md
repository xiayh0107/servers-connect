# Changelog

## Unreleased

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
