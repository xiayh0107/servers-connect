# Using SSH Server Manager with AI agents

SSH Server Manager was designed so that a coding agent can operate your
servers **without ever being able to see a secret**. This page covers the
integration options and why the design is safer than the common
alternatives.

## Why not just give the agent `ssh` or an MCP SSH server?

- Raw `ssh` + password helpers (`sshpass`) put secrets in argv, env vars,
  or files the agent can read.
- Generic MCP SSH servers hold credentials in their own config and execute
  whatever the model asks; 2026 security research documented tool-poisoning
  attacks exfiltrating SSH keys through exactly this channel, and real
  shell-injection CVEs in popular MCP SSH servers.

Here, the boundary is structural: the model calls a deterministic CLI;
authentication happens out-of-band through AskPass and the OS vault. The
secret value never enters the process output the model reads. Prompts that
should go to a human (host-key confirmation, OTP/2FA) are refused by the
AskPass helper rather than auto-answered.

## As an Agent Skill (recommended)

The project ships a [SKILL.md](../SKILL.md) following the open Agent Skills
format supported by Claude Code, Codex CLI, Gemini CLI, Cursor, and others.
Deploy it with one line:

```bash
# macOS / Linux — fetches, installs deps, links into every detected agent, runs doctor
curl -fsSL https://raw.githubusercontent.com/xiayh0107/servers-connect/main/install.sh | sh

# Windows
powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/xiayh0107/servers-connect/main/install.ps1 | iex"

# or manually, from a checkout
ln -s /path/to/ssh-server-manager ~/.claude/skills/ssh-server-manager
```

**Keeping the skill current:** the skill directories are symlinks into one
installed copy, and agents follow whatever that copy says. Re-run the same
one-liner to update it — the installer pulls the latest release, re-links,
and also puts `serverctl` on PATH via `~/.local/bin`. `serverctl doctor`
prints an `agent_skill` warning whenever a linked copy is older than the
CLI you are running, which is the usual reason an agent falls back to
hand-rolled `ssh`.

The skill instructs the agent to:

- treat `serverctl` as the only SSH path for managed hosts — no raw
  `ssh`/`scp`/`sshpass`, no password requests in chat, no editing
  `~/.ssh/config` — and to resolve the binary as `serverctl` on PATH,
  `~/bin/serverctl`, or `scripts/serverctl` inside the skill directory;
- use `--json` output for every query (`server list`, `server test`, …);
- never request, print, or store secret values;
- prefer `server test` before connecting, and use classified error codes
  (`authentication-failed`, `host-key-untrusted`, `timeout`, …) to
  diagnose failures;
- route secret entry to the local UI or hidden `getpass` prompts, both of
  which happen outside the model's context;
- keep interactive shells in the human's terminal: when asked to "connect",
  the agent hands you the `serverctl connect <alias>` command and runs
  follow-up commands on that host through `serverctl exec` on your behalf,
  instead of trying to hold a TTY it does not have.

If an agent still reaches for raw `ssh` (typically because it searched for
an `ssh-server-manager` binary, found nothing, and improvised), a one-line
nudge like "use the ssh-server-manager skill" or "use serverctl" re-routes
it; the skill description also carries English and Chinese trigger phrases
so connection requests match it directly.

## Patterns for agent automation

```bash
# structured inventory
serverctl server list --json

# server notes (local metadata only; never include secrets)
serverctl server note web1 --text "Primary web node" --json
serverctl server note web1 --text "Agent checked disk usage" --append --json

# health check across a fleet (agent loops over aliases)
serverctl server test web1 --json

# run a command, get structured results
serverctl exec web1 --json -- systemctl is-active nginx

# compound commands need --shell
serverctl exec web1 --json --shell -- 'du -sh /var/log/* | sort -h | tail -5'

# stream a locally generated script
cat check.sh | serverctl exec web1 --stdin -- sh -s

# several commands in a row without re-auth (macOS/Linux)
serverctl exec web1 --reuse 300 --json -- uptime

# UI lifecycle without pid bookkeeping
serverctl ui --status --json
serverctl ui --stop --json
```

Exit codes are meaningful (`exec` mirrors the remote command; `test` and
`doctor` return non-zero on failure), so agents can branch without parsing
prose.

## What agents must not do

These rules are enforced by the skill instructions and, where possible, by
the tool itself:

1. Never echo a password/passphrase into `credential add-*` — the commands
   only read from a local hidden prompt.
2. Never work around a failed host-key check; report `host-key-untrusted`
   to the human.
3. Never print the UI launch URL or token into chat or logs
   (`serverctl ui` withholds the token from stdout by design).
4. Never store secrets in notes fields, files, or environment variables.
