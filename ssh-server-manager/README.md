# SSH Server Manager

**A local-first SSH host and credential manager for humans and AI agents.**
One CLI (`serverctl`) and a loopback-only web UI to add, import, test, and
connect to your servers — with every password and passphrase stored in your
operating system's native credential vault, never in a plaintext file.

[![test](https://github.com/xiayh0107/servers-connect/actions/workflows/test.yml/badge.svg)](https://github.com/xiayh0107/servers-connect/actions/workflows/test.yml)
[![release](https://img.shields.io/github/v/release/xiayh0107/servers-connect)](https://github.com/xiayh0107/servers-connect/releases)
[![license](https://img.shields.io/badge/license-MIT-green.svg)](../LICENSE)

[简体中文说明](README.zh-CN.md) · [Documentation](docs/) · [Security model](docs/security.md) · [Website](https://xiayh0107.github.io/servers-connect/)

> 🤖 **Using an AI agent?** The skill deploys to every agent on your machine
> in one line — dependencies, links, and health check included. See the
> [agent deployment guide](docs/ai-agents.md).

---

## Why this exists

Most SSH managers make you choose between convenience and custody:

- **Cloud-synced clients** (subscription GUIs) keep your credentials on
  someone else's infrastructure.
- **Plain `~/.ssh/config`** keeps you in control but handles no passwords,
  no vault, and gives AI coding agents an easy way to leak secrets.
- **`sshpass`-style wrappers** put passwords in files, env vars, or shell
  history.

SSH Server Manager is different by construction:

| Property | How |
|---|---|
| Secrets never touch disk in plaintext | macOS Keychain / Windows Credential Locker / Linux Secret Service via `keyring`; unsafe backends are rejected as a hard error |
| Your `~/.ssh/config` is never modified | A separate managed config is rendered and included alongside it |
| The web UI is unreachable from the network | Loopback-only bind, one-time launch token, CSRF + Origin checks, strict CSP |
| Revealing a stored secret requires re-auth | WebAuthn passkey (Touch ID / Windows Hello) or an Argon2id master password; grants are single-use and expire in 30 s |
| AI agents can drive it without seeing secrets | Every command has `--json`; SSH authentication happens through AskPass so secrets never appear in argv, env, logs, or model context |
| Host keys are always verified | `StrictHostKeyChecking` is never weakened, by policy and by code |

## Quick start

```bash
git clone https://github.com/xiayh0107/servers-connect.git
cd servers-connect/ssh-server-manager
./scripts/bootstrap          # Windows: scripts\bootstrap.cmd
./scripts/serverctl doctor   # verify ssh, vault backend, and dependencies
```

Add a server and connect:

```bash
./scripts/serverctl credential add-password work-password   # prompts locally, stores in OS vault
./scripts/serverctl server add web1 --hostname web1.example.com --username deploy --credential work-password
./scripts/serverctl server test web1
./scripts/serverctl connect web1
```

Or import everything you already have:

```bash
./scripts/serverctl server import          # preview only
./scripts/serverctl server import --apply
```

Prefer a GUI? `./scripts/serverctl ui` opens the local web interface in your
browser with a one-time tokenized URL.

## Features

- **Connection profiles** — alias, host, port, user, notes, ordered
  ProxyJump chains (with cycle detection).
- **Three credential kinds** — vault-backed passwords, private keys with
  optional vault-backed passphrases, and ssh-agent/OpenSSH defaults.
  Credentials are reusable across servers and protected against deletion
  while referenced.
- **OpenSSH import** — parses your `~/.ssh/config` (following `Include`),
  resolves each literal alias with `ssh -G`, previews before applying.
- **Managed config rendering** — atomic, user-only-permission writes to
  `~/.ssh/ssh-server-manager.conf`; your original config always loads last
  so unrelated defaults keep working.
- **Connection testing** — `server test` reports latency and a classified
  error code (`authentication-failed`, `host-key-untrusted`, `timeout`,
  `dns-failed`, …) and records history.
- **Remote execution** — `serverctl exec alias -- cmd args`, with `--shell`
  for pipelines, `--stdin`/`--stdin-binary` for streaming files, `--reuse N`
  for ControlMaster connection sharing (macOS/Linux), and `--json` for
  machine-readable results.
- **Local web UI** — manage servers and credentials, test connections,
  import config, and reveal a stored secret after passkey or master-password
  re-authentication.
- **Diagnostics** — `serverctl doctor` checks ssh availability and version,
  vault backend safety, database and config paths, and Python dependencies.

## Platform support

| | macOS | Linux | Windows |
|---|---|---|---|
| CLI + web UI | ✅ | ✅ | ✅ |
| Credential vault | Keychain | Secret Service (gnome-keyring / KWallet / KeePassXC) | Credential Locker |
| Secret reveal re-auth | Touch ID passkey or master password | passkey or master password | Windows Hello passkey or master password |
| Connection reuse (`--reuse`) | ✅ | ✅ | — (OpenSSH ControlMaster needs Unix sockets; a warning is printed) |

Details and headless-server notes: [docs/platforms.md](docs/platforms.md).
CI runs the full test suite on all three platforms.

## For AI agents

This project ships as an Agent Skill ([SKILL.md](SKILL.md)) so Claude Code,
Codex, and other agents can manage servers safely: agents get structured
JSON output and connection error classification, while the AskPass
architecture keeps secrets out of the model's context by design.

Deploy the skill with one line:

```bash
curl -fsSL https://raw.githubusercontent.com/xiayh0107/servers-connect/main/install.sh | sh
```

It links the skill into every detected agent (`~/.claude/skills`,
`~/.codex/skills`, …), installs dependencies, and runs `doctor`. Windows
uses `install.ps1`. See [docs/ai-agents.md](docs/ai-agents.md).

## Documentation

| Doc | Contents |
|---|---|
| [docs/installation.md](docs/installation.md) | Per-platform install, requirements, bootstrap |
| [docs/quickstart.md](docs/quickstart.md) | First 10 minutes, common workflows |
| [docs/cli.md](docs/cli.md) | Complete `serverctl` reference |
| [docs/web-ui.md](docs/web-ui.md) | Web UI walkthrough and its security gates |
| [docs/security.md](docs/security.md) | Threat model and security invariants |
| [docs/platforms.md](docs/platforms.md) | Platform-specific behavior, headless Linux |
| [docs/ai-agents.md](docs/ai-agents.md) | Using with Claude Code / Codex / MCP |
| [docs/faq.md](docs/faq.md) | Troubleshooting and common questions |

## License

[MIT](../LICENSE)
