# Changelog

## 0.2.0 — 2026-07-15

First release prepared for standalone distribution.

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
