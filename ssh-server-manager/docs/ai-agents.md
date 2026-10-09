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

## When an agent installs this for a user

If a user gives you this page and asks you to install or configure SSH Server
Manager, **a successful `serverctl doctor` is not the end of the task**.
The user may never read this documentation or learn the CLI. Treat the install
as capability injection and finish with a short first-run handoff.

After `doctor` succeeds:

1. Run `serverctl server list --json` once and inspect the current local
   inventory. Do not run remote connection tests only for onboarding.
2. Tell the user which hosts are already managed, using their aliases rather
   than leading with IP addresses, database paths, config paths, or raw JSON.
3. Give 3–5 concrete examples of things the user can now ask **in natural
   language**, ideally using the real aliases you just found. Examples:
   - “check GPU usage on `gpu-lab`”
   - “show me what is in `/data` on `web1`”
   - “upload this file to `web1`”
   - “download the latest result from `analysis`”
   - “connect me to `prod`”
4. Explain in one sentence that saved passwords/passphrases stay in the local
   OS credential vault and do not need to be pasted into chat or exposed to
   the model.
5. Be precise about status: `doctor` verifies the **local installation**. If
   you have not run `server test` for a host, do not imply that its remote
   login was verified.

Adapt the handoff to the inventory:

- **No managed hosts:** say the tool is ready, then get the user their first
  host instead of stopping at the offer. Run `serverctl server import`
  (without `--apply` it is always a preview) and show the user which aliases
  their own `~/.ssh/config` already provides; apply only after they confirm.
  With nothing to import, follow the skill's **When the user wants to add a
  server** playbook: collect the non-secret fields in chat, create the
  profile, route credential entry through `serverctl ui --credential ALIAS`
  (or hand the two terminal commands to the user), and run
  `serverctl server test ALIAS --json` before calling that one new host
  usable. Never ask for the password in chat.
- **One managed host:** name it and make the examples about that host.
- **Several managed hosts:** summarize the fleet compactly and use a few real
  aliases in the examples.

A good installation completion message sounds like this:

> SSH Server Manager is ready, and I found your `yulab` host. From now on you
> can just ask me things like “check yulab's GPU”, “show me files under /data”,
> or “upload this file to yulab”; I will route those actions through the
> manager automatically. Your saved SSH secret stays in the local OS vault and
> does not need to be sent to me. The local installation passed its health
> check; I have not tested the remote login unless I explicitly say so.

The CLI is an agent-facing capability surface. Do not turn the first-run
handoff into a CLI tutorial unless the user asks how to operate it manually.

`serverctl web` is now the explicit browser-shell entry point. The existing
`serverctl ui` spelling, flags and credential deep links remain compatible, and
both spellings share `--status`/`--stop`. If the UI is already running, report
its status and use the existing window; do not stop an active session just to
open another. Agent commands use shared Python application services directly,
so host inventory and remote CLI work do not depend on an HTTP runtime.
UI integrations can use the typed `/api/v1` contract described in
[Backend architecture](backend.md); its cookie, CSRF and credential-specific
reveal authentication requirements apply to every shell. The current frontend
remains in place pending its separate Paper design.

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
- drive first-time server onboarding itself: collect the non-secret fields in
  chat, run `server add`, then open `serverctl ui --credential ALIAS` so the
  browser lands directly on that server's credential form — the agent never
  runs the `getpass` commands itself, because its tool calls have no TTY;
- keep interactive shells in the human's terminal: when asked to "connect",
  the agent hands you the `serverctl connect <alias>` command and runs
  follow-up commands on that host through `serverctl exec` on your behalf,
  instead of trying to hold a TTY it does not have.
- resolve host-bound skills after it identifies the target aliases, apply each
  result only to its `applies_to` hosts, and discard that environment-specific
  context when the target changes.

If an agent still reaches for raw `ssh` (typically because it searched for
an `ssh-server-manager` binary, found nothing, and improvised), a one-line
nudge like "use the ssh-server-manager skill" or "use serverctl" re-routes
it; the skill description also carries English and Chinese trigger phrases
so connection requests match it directly.

## Patterns for agent automation

```bash
# structured inventory
serverctl server list --json

# resolve local guidance only after choosing the target hosts
serverctl skill resolve gpu-lab-01 gpu-lab-02 --json

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

# first-time onboarding: create the profile, then land the human on its
# credential form (launch in the background; the URL keeps its one-time token)
serverctl server add web1 --hostname web1.example.com --username deploy --json
serverctl ui --credential web1
```

Exit codes are meaningful (`exec` mirrors the remote command; `test` and
`doctor` return non-zero on failure), so agents can branch without parsing
prose.

## Host-specific Agent Skills

The SSH manager handles inventory, authentication, host-key verification, and
transport. A host-bound skill can then supply the details that should not be
global: cluster topology, approved health checks, scheduler conventions,
service paths, or project runbooks.

The workflow is deliberately target-first:

1. Call `serverctl server list --json` and identify the exact target alias or
   aliases from the user's request.
2. Call `serverctl skill resolve ALIAS [ALIAS ...] --json` once for that target
   set.
3. Treat the `ready` results as the eligible skill set. Load a skill through
   the agent's normal mechanism when its description and trigger rules match
   the current task. For a multi-host task, apply it only to the aliases in
   `applies_to`.
4. Perform only the work the user requested. If the user switches hosts,
   discard the previous host-specific context and resolve again.

For example, `yulab-gpu-node` could be bound to a login node and its GPU
workers, while a storage skill is bound to only the nodes that mount that
filesystem. These are ordinary user-managed relationships; the product does
not special-case YuLab, GPU hosts, or any alias.

Registering and binding are explicit local administration actions:

```bash
# inspect installed candidates without changing anything
serverctl skill discover --json

# register one local skill and bind two hosts atomically
serverctl skill add ~/.agents/skills/gpu-ops \
  --server gpu-lab-01 --server gpu-lab-02 --json

# add or remove more host relationships later
serverctl skill attach gpu-ops gpu-lab-03 --json
serverctl skill detach gpu-ops gpu-lab-01 --json
```

Discovery never downloads or installs a skill. A binding is relevance
metadata, not permission to execute commands or broaden the user's target
scope. Resolution returns the registered name, description, path, status, and
`applies_to` aliases—not the skill body. If a path is missing, invalid, renamed,
or conflicts by name, agents must not guess or reuse a skill resolved for a
different host.

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
5. Never treat a host-skill binding as authorization. It routes local
   instructions after the user has chosen the hosts; it does not choose hosts
   or approve remote changes.
