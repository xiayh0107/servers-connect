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

If your client can only speak MCP, the optional wrapper in
[As an MCP server](#as-an-mcp-server-optional) exposes the same local
inventory — read-only by default, with no tool that can reveal a secret.

Here, the boundary is structural: the model calls a deterministic CLI;
authentication happens out-of-band through AskPass and the OS vault. The
secret value never enters the process output the model reads. AskPass refuses
host-key confirmation during agent calls and Web operations, and never
answers OTP/2FA prompts. Only interactive `serverctl connect` can show a
first-use host-key prompt in the human's controlling terminal and return
their explicit answer; the human must check its fingerprint with a trusted
source. It preserves the configured SSH trust policy and never automatically
accepts or replaces a host key.

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
  Explain the import's authentication warning: existing IdentityFile and other
  directives remain in the source config, rather than becoming saved
  credentials. Show the selected fallback path, preserve existing bindings on
  overwrite, and explicitly test login before claiming success.
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
reveal authentication requirements apply to every shell. The bundled React
frontend implements Hosts, host details, Files, Tags, Skills and Credentials
over that contract. Users do not need Node.js to run the installed package. The
runtime reports the actual session expiry and server time; vault status stays
unknown until an explicit vault operation observes a result.

Host inventory and details read saved local metadata without DNS, SSH, or vault
access. Successful diagnostics and explicit host-info collection retain
sanitized system `facts` with an `observed_at` timestamp and closed `os_logo`
key; JSON also exposes
`last_success_at`, the last locally resolved IP, host-key state, optional manual
geography, and literal address scope. These are dated observations: a failed
check keeps the previous successful facts, and cached metadata never proves
that a host is currently reachable. Check only when the requested task needs
fresh information.

For UI integrations, `/api/v1/runtime` advertises bulk-check, route, file-conflict,
and transfer-limit capabilities. `GET /api/v1/hosts/{id}/route` reads the saved
ProxyJump route without probing; `POST /api/v1/hosts/test` checks connectivity
only. `POST /api/v1/hosts/observe` explicitly backfills sanitized system facts
using the diagnosis system probe, without an SFTP check. Both accept
`{identifiers: [...], timeout: 8, concurrency: 4}` with 1-64 hosts, concurrency
1-4 and timeout 1-120 seconds per probe, plus bounded SSH transport grace.
Every selected host must exist before any remote work starts; aliases/IDs are
deduplicated, results keep selection order, failures are isolated per host,
and overlapping batches share a four-worker limit. A failed system probe
retains prior facts and does not turn a successful handshake into a failed
connectivity status. `POST /api/v1/hosts/{id}/observe` accepts `{timeout: 8}`
for a single host. A deliberate 260ms host-name hover automatically observes
missing/stale facts, with inline skeletons and saved values during refresh.
Hover probes are serialized; successes have 60-second freshness and failures
have a 30-second cooldown. Keyboard focus, page load, history restoration and
Hide details never initiate them. Explicit **Collect host info** remains in
host details and the selection toolbar, not inventory rows or the page header.
Agents must not start a fleet backfill merely
because upgraded hosts have empty observations. The file API supports optional
name filtering, nullable precise metadata, structured errors, and uploads that refuse
existing destinations unless `overwrite=true` is supplied for an authorized
replacement. These are HTTP contracts; agents continue using the existing CLI
commands and the same credential boundary.

Credentials inventory is local metadata: opening the page does not read private
keys or access the vault. Explicit key inspection and validation parse supported
OpenSSH/PEM files in memory, bind validation to the file saved, and return public
metadata rather than private-key contents. A blank replacement secret preserves
the saved value; clearing a key passphrase is a separate explicit choice.
The browser's independent credential test streams authentication stages against
any saved host without changing its binding or health observations. Agent tests
use only the agent, with default-file/password fallback disabled; ordinary host
connections retain their configured OpenSSH behavior. A connection accepted
without authentication does not count as verification of the selected credential.
No prior host binding is needed. Inconclusive results leave TCP/host-key
checkpoints unconfirmed; a route-wide failure does not prove that the target
credential was rejected. Hide details preserves endpoint/credential lanes using
fixed masks and disables copying. It also suppresses host hover/focus
previews. Inventory shortcuts leave focused controls, open dialogs and browser
shortcuts to their native handlers.

The CLI still refuses to remove a credential referenced by a host. The Web UI
previews all affected hosts and explicitly confirms detaching and deleting.
Database changes commit together; incomplete vault cleanup is kept in a durable
journal and can be retried without repeating the detach. Reveal requires a
credential-specific, single-use grant that expires after 30 seconds; the UI
displays a revealed value for at most 15 seconds and clears it when hidden or
closed. Never include reveal, password entry, launch URLs, cookies, CSRF values
or passphrases in screenshots, traces, logs or agent messages.

The skill instructs the agent to:

- treat `serverctl` as the only SSH path for managed hosts — no raw
  `ssh`/`scp`/`sshpass`, no password requests in chat, no editing
  `~/.ssh/config` — and to resolve the binary as `serverctl` on PATH,
  `~/bin/serverctl`, or `scripts/serverctl` inside the skill directory;
- use `--json` output for every query (`server list`, `server test`, …);
- never request, print, or store secret values;
- use a recent saved successful check before connecting, test when needed,
  and use classified error codes
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

# a port forward the agent starts and finishes with the task
serverctl tunnel add db1-local --server db1 --local 15432:localhost:5432 --json
serverctl tunnel open db1-local --json     # returns the pid, or fails with ssh's reason
serverctl tunnel status --json             # what this machine is actually running
serverctl tunnel close db1-local --json    # closing a stopped profile is a no-op

# move an inventory between machines without copying the database
serverctl export --output inventory.json
serverctl import inventory.json              # preview the plan
serverctl import inventory.json --apply      # create/update hosts, paths, tunnels

# first-time onboarding: create the profile, then land the human on its
# credential form (launch in the background; the URL keeps its one-time token)
serverctl server add web1 --hostname web1.example.com --username deploy --json
serverctl ui --credential web1
```

Exit codes are meaningful (`exec` mirrors the remote command; `test` and
`doctor` return non-zero on failure), so agents can branch without parsing
prose.

## As an MCP server (optional)

The Skill is still the recommended path: an agent that can read `SKILL.md` gets
the reasoning, the interaction rules, and every command. The MCP wrapper exists
for clients that cannot read a Skill and only speak MCP.

```bash
# read-only: inventory, routes, saved paths, skills, tunnel profiles, capabilities
serverctl mcp serve

# allow a connectivity check that contacts a host
serverctl mcp serve --allow-remote

# allow specific commands, one glob per --allow-command
serverctl mcp serve --allow-exec --allow-command 'uptime' --allow-command 'journalctl -u *'
```

Configure the client to run that command over stdio (stdout carries protocol
messages only). `serverctl mcp tools` prints what a given policy would expose,
which is the fastest way to confirm a client will see the tools you expect.

If the client cannot spawn a process, serve the same tools over loopback HTTP
instead:

```bash
serverctl mcp serve --http --port 0 --token-file ~/.cache/ssm-mcp-token
# stderr: MCP endpoint: http://127.0.0.1:PORT/mcp
```

Point the client at that URL with `Authorization: Bearer <token>` read from the
token file. Same tools, same policy, same audit actor. Three refusals are fixed
and not configurable: the listener binds loopback only and rejects a non-loopback
`Host`; every request needs the bearer token; and any request carrying an
`Origin` header is refused, so a browser page cannot drive your servers even if
the token leaks into it. That last rule is the point — it is what separates this
from "an MCP server on a port".

What it does not do, by construction:

- **No secret tools.** There is no reveal tool and no result carries a vault
  value; read tools are built from local metadata.
- **Structured arguments only.** `run_command` takes an argv array, never a
  shell string, so there is no local shell parsing.
- **Two switches, both off by default.** `test_host` needs `--allow-remote`;
  `run_command` needs `--allow-exec` *and* a matching `--allow-command` pattern.
  `--allow-exec` with no pattern is refused at startup rather than silently
  enabling nothing.
- **Audited.** Acting calls are recorded with actor `mcp` in the same local
  audit log, so `serverctl audit list` answers "what did the agent do here".

Host-key confirmation does not change: an MCP client cannot approve a host key,
so a new host must be verified once with `serverctl server test ALIAS` in a
human's terminal first.

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
