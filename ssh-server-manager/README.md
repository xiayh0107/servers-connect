# SSH Server Manager

**A local-first SSH host and credential manager for humans and AI agents.**
One CLI (`serverctl`) and a loopback-only web UI to add, import, test, and
connect to your servers — with every password and passphrase stored in your
operating system's native credential vault, never in a plaintext file.

[![test](https://github.com/xiayh0107/servers-connect/actions/workflows/test.yml/badge.svg)](https://github.com/xiayh0107/servers-connect/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/ssh-server-manager)](https://pypi.org/project/ssh-server-manager/)
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
| Environment guidance stays with the right hosts | Local Agent Skills have explicit many-to-many host bindings; resolution partitions multi-host tasks without installing skills or returning their bodies |
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

- **Connection profiles** — alias, host, port, user, project/scenario tags,
  notes, ordered ProxyJump chains (with cycle detection).
- **Host-bound Agent Skills** — discover and register local skills, bind one
  skill to several related nodes or several skills to one host, and resolve
  exact `applies_to` partitions before an agent acts. Bindings are routing
  metadata, not remote authorization. Manage a host's assignments directly
  from Hosts details; use the Skills page for
  discovery and registration.
- **Three credential kinds** — vault-backed passwords, private keys with
  optional vault-backed passphrases, and ssh-agent/OpenSSH defaults.
  Credentials are reusable across servers. Default deletion rejects references;
  the Web UI can preview references and explicitly detach before deleting.
  Inspect and validate private keys locally, or test a selected credential
  independently without changing host bindings or health records.
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
- **Saved working directories** — `serverctl path add web1 /srv/app --label
  app` records where you work on a host; `serverctl path resolve` answers
  "where does this fleet keep its application" in one read, with no connection,
  grouping hosts that share a layout.
- **File transfer** — `serverctl get web1:/srv/app/config.yml .` and
  `serverctl put ./config.yml web1:/srv/app/` move files over the same SFTP path
  and vault credentials, and `--recursive` copies a whole tree, so no password
  prompt appears where `scp` and `rsync` would ask for one they cannot supply.
- **Local mounting** — `serverctl mount web1:/srv/app` exposes a remote
  directory to Finder, Explorer, and your editor through sshfs. Needs a FUSE
  layer that `serverctl doctor` checks for; see
  [docs/mounting.md](docs/mounting.md).
- **Local web UI** — manage servers and credentials, test connections,
  create/rename/delete tags and bulk-assign searchable hosts, choose
  system/light/dark/contrast themes and selectable accents, import config, and
  browse remote directories with saved paths, tabs, server-side name filtering,
  and explicit download/upload actions. Conflicts require an explicit overwrite.
  Hosts distinguish connection checks from system diagnosis and show the last
  successful observation. Reveal a stored secret for 15 seconds after passkey
  or master-password re-authentication. The session shows its actual expiry. Inspect or shut down a background instance with
  `serverctl ui --status` / `--stop` — no pid bookkeeping.
- **Port forwarding** — save a forward per host (`serverctl tunnel add db1-local
  --server db1 --local 15432:localhost:5432`) and open it as a detached `ssh -N`
  (`tunnel open`), with `status` and `close`. Binds to `127.0.0.1` unless you say
  otherwise, and `status` proves a forward is live against the real process.
- **Audit log** — a local, append-only, mode-600 record of remote commands and
  secret reveals (`serverctl audit list --json`). It rotates itself at 5 MiB,
  masks credential-shaped arguments, and never stores a secret value.
- **Portable inventory** — `serverctl export` writes a deterministic JSON
  document of hosts, saved paths, and tunnels for git or another machine;
  `serverctl import` previews, then merges. Never secrets or credential ids.
- **Agent integration** — a host-bound Agent Skill, plus an optional MCP server
  (`serverctl mcp serve`) that is read-only unless started with `--allow-remote`
  or `--allow-exec --allow-command PATTERN`, and never reveals a secret.
- **Mosh and session transcripts** — `serverctl connect ALIAS --mosh` for
  roaming links, and `connect --log` for an opt-in, redacted transcript.
- **Local GeoIP** — `serverctl geo resolve --all --apply` fills host locations
  from a local range file; no web service, and never a lookup when reading the
  inventory.
- **Diagnostics** — `serverctl doctor` checks ssh availability and version,
  SFTP availability, mount backend availability, mosh and GeoIP support, vault
  backend safety, database and config paths, and Python dependencies.

## Platform support

| | macOS | Linux | Windows |
|---|---|---|---|
| CLI + web UI | ✅ | ✅ | ✅ |
| Credential vault | Keychain | Secret Service (gnome-keyring / KWallet / KeePassXC) | Credential Locker |
| Secret reveal re-auth | Touch ID passkey or master password | passkey or master password | Windows Hello passkey or master password |
| Connection reuse (`--reuse`) | ✅ | ✅ | — (OpenSSH ControlMaster needs Unix sockets; a warning is printed) |
| Background tunnels (`serverctl tunnel`) | ✅ | ✅ | ✅ |
| Mosh (`connect --mosh`) | ✅ | ✅ | — (the Windows client is not offered; use `connect`) |
| Session transcripts (`connect --log`) | ✅ | ✅ | — (needs a POSIX pty; the command says so) |

Details and headless-server notes: [docs/platforms.md](docs/platforms.md).
CI runs the full test suite on all three platforms.

## For AI agents

This project ships as an Agent Skill ([SKILL.md](SKILL.md)) so Claude Code,
Codex, and other agents can manage servers safely: agents get structured
JSON output and connection error classification, while the AskPass
architecture keeps secrets out of the model's context by design.

The skill also encodes conversation UX, not just commands: agents answer
inventory questions from a single `--json` call, hand interactive shells to
*your* terminal and proxy follow-up commands through `exec` (`connect`
refuses to run without a TTY instead of hanging), and clean up a background
UI with `serverctl ui --stop`.

Environment-specific skills can follow only the hosts where they belong. The
agent identifies the target first, runs `serverctl skill resolve ALIAS
[ALIAS ...] --json`, then selects relevant ready skills by their normal trigger
rules and applies them only to their returned `applies_to` aliases. Switching
hosts discards the previous host-specific context; no cluster name or provider
is hard-coded.

```bash
serverctl skill discover --json
serverctl skill add ~/.agents/skills/gpu-ops \
  --server gpu-lab-01 --server gpu-lab-02 --json
serverctl skill resolve gpu-lab-01 gpu-lab-02 --json
```

Discovery is local and read-only. Registration does not install or execute a
skill, and resolution returns metadata and paths rather than instruction
bodies. See the [agent deployment guide](docs/ai-agents.md#host-specific-agent-skills).

Deploy the skill with one line:

```bash
curl -fsSL https://raw.githubusercontent.com/xiayh0107/servers-connect/main/install.sh | sh
```

It links the skill into every detected agent (`~/.claude/skills`,
`~/.codex/skills`, …), installs dependencies, and runs `doctor`. Windows
uses `install.ps1`. See [docs/ai-agents.md](docs/ai-agents.md).

Only the CLI and Web UI: `pipx install ssh-server-manager`, or
`brew install xiayh0107/tap/ssh-server-manager`.

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
| [docs/mounting.md](docs/mounting.md) | Mounting a host locally with sshfs |
| [docs/faq.md](docs/faq.md) | Troubleshooting and common questions |

## License

[MIT](../LICENSE)
