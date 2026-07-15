# Platform notes

CI runs the full test suite on macOS, Ubuntu, and Windows on every push.

## macOS

- **Vault**: Keychain, no setup needed. The first secret access may show a
  Keychain permission dialog — click "Always Allow" for your Python/serverctl.
- **Passkey reveal**: Touch ID via Safari/Chrome WebAuthn.
- **OpenSSH**: preinstalled (`/usr/bin/ssh`).
- Connection reuse (`--reuse`) fully supported.

## Linux

- **Vault**: any freedesktop **Secret Service** provider:
  - GNOME: `gnome-keyring` (default on most desktops)
  - KDE: KWallet (5.97+ provides Secret Service; enable it in settings)
  - Anything else: KeePassXC with *Enable Secret Service integration*
  The collection must be **unlocked**; normally your desktop login unlocks it.
- **Headless / SSH-only servers**: there is no unlocked Secret Service by
  default, so `serverctl doctor` will fail the vault check. Options:
  - Run `gnome-keyring-daemon` unlocked in your session
    (e.g. `dbus-run-session` + `gnome-keyring-daemon --unlock`)
  - Use only `agent`-kind credentials (ssh-agent / key files without stored
    passphrases) — those never touch the vault
  - Plaintext fallback is deliberately **not** offered.
- **Passkey reveal**: requires a WebAuthn-capable browser + authenticator;
  otherwise enroll the master password.
- Connection reuse fully supported. Runtime sockets live in `/tmp/ssm-<uid>`
  (kept short because OpenSSH limits `ControlPath` length).

## Windows

- **Vault**: Credential Locker (Windows Credential Manager), no setup.
- **OpenSSH**: install the optional *OpenSSH Client* feature or
  `winget install Microsoft.OpenSSH.Beta`; `serverctl doctor` verifies it.
- **AskPass**: password/passphrase injection uses the
  `ssh-server-manager-askpass.exe` console script installed next to your
  Python — present automatically under pipx/uv or after
  `scripts\bootstrap.cmd`.
- **Connection reuse (`--reuse`)**: not available — Windows OpenSSH lacks
  ControlMaster Unix-socket support. `serverctl` prints a warning and each
  `exec` authenticates independently (vault auth is non-interactive, so
  this costs latency, not typing).
- **Passkey reveal**: Windows Hello via Edge/Chrome.
- Use `scripts\serverctl.cmd` from a source checkout; plain `serverctl`
  after pipx/uv install.

## Data locations

`serverctl doctor` prints the effective paths. Defaults:

| | Database | Managed config |
|---|---|---|
| macOS | `~/Library/Application Support/ssh-server-manager/manager.db` | `~/.ssh/ssh-server-manager.conf` |
| Linux | `$XDG_DATA_HOME/ssh-server-manager/manager.db` | `~/.ssh/ssh-server-manager.conf` |
| Windows | `%LOCALAPPDATA%\ssh-server-manager\manager.db` | `~\.ssh\ssh-server-manager.conf` |

Overrides for testing/isolation: `SSM_DATA_DIR`, `SSM_RUNTIME_DIR`,
`SSM_ORIGINAL_SSH_CONFIG`, `SSM_MANAGED_SSH_CONFIG`.
