# Command reference

## Setup and UI

```bash
./scripts/bootstrap
./scripts/serverctl doctor [--json]
./scripts/serverctl web [--port PORT] [--no-open --url-file PATH]
./scripts/serverctl web --credential ALIAS [--port PORT] [--no-open --url-file PATH]
./scripts/serverctl web --status [--json]
./scripts/serverctl web --stop [--json]
./scripts/serverctl ui [--port PORT] [--no-open --url-file PATH]
./scripts/serverctl ui --credential ALIAS [--port PORT] [--no-open --url-file PATH]
./scripts/serverctl ui --status [--json]
./scripts/serverctl ui --stop [--json]
```

`web` is the browser-shell entry point; `ui` is a compatibility alias with
identical options. Both open the bundled modern React UI and share one managed runtime
record. A second launch fails while a managed runtime is active; use `--status`
and the existing window rather than stopping someone else's active session.

In the Web UI, a deliberate host-name hover lazily observes missing/stale facts
with field skeletons, single-flight requests, 60-second freshness and a
30-second failure cooldown. Page load and keyboard focus stay local. Hide
details disables previews and collection, retains fixed masked columns and
disables copying; Shift+H toggles it outside focused controls. Credential
tests require Run test and can use an unbound credential against any saved host
without changing its binding. An unconfirmed TCP/host-key checkpoint or a failed
jump route is not evidence that the target credential was rejected. These are
browser features; they add no CLI commands.

The default `--port 0` selects an available loopback port and opens the browser.
When `--no-open` is used, `--url-file` is required; the file is created with
mode `600` and contains the one-time tokenized URL. The token is never printed.
If an explicitly requested port is occupied, retry with `--port 0` or choose a
different port.

`ui --credential ALIAS` opens the browser directly on that server's edit
dialog with the new-credential form expanded, so entering a password is one
field and one click. Use it right after `server add` when guiding a user
through first-time setup.

`ui --status` reports the managed UI process (pid, port, start time) and exits
`0` while it runs. `ui --stop` terminates it and deletes its `--url-file`; the
command is idempotent, so use it to clean up when the user is done with the UI
instead of tracking pids by hand.

### Agent post-install handoff

`doctor` is a local health check, not the completion condition for an
agent-driven installation. After it succeeds, an installing agent should run
`serverctl server list --json` once, summarize the actual managed hosts, and
teach the user what they can now ask for in natural language using those real
aliases. Do not add remote `server test` calls only for onboarding, do not lead
with database/config paths or raw JSON, and do not require the user to learn
CLI syntax. Make clear that stored secrets remain in the local OS vault and
that a successful `doctor` does not prove a host login has been tested.

## Servers

```bash
./scripts/serverctl server list [--json]
./scripts/serverctl server show ALIAS [--json]
./scripts/serverctl server add ALIAS --hostname HOST --username USER [--port PORT]
  [--credential CREDENTIAL | --password-credential LABEL] [--proxy-jump ALIAS ...] [--notes TEXT]
./scripts/serverctl server edit ALIAS [field options]
./scripts/serverctl server remove ALIAS [--yes]
./scripts/serverctl server import [--config PATH] [--apply] [--overwrite] [--json]
./scripts/serverctl server test ALIAS [--timeout SECONDS] [--json]
./scripts/serverctl server diagnose ALIAS [--timeout SECONDS] [--json]
./scripts/serverctl server note ALIAS (--text TEXT [--append] | --clear) [--json]
```

An import without `--apply` is always a preview. Literal aliases are resolved with `ssh -G`; wildcard-only patterns are reported but not imported.

Import warnings identify authentication directives that are not converted into
managed credentials, configured IdentityFile paths and the selected source
config. Applied hosts retain that config as an OpenSSH fallback, including a
custom `--config` path. Import reads no private-key contents and does not verify
login. `--overwrite` updates imported connection fields while preserving the
latest credential binding, notes, tags, directories and Skill bindings. An
empty credential binding may still authenticate through the source config;
confirm with an explicit `server test`.

`server add --password-credential LABEL` creates a password credential and
attaches it in one step, using the same hidden local prompt as
`credential add-password`. It needs a real terminal; without a TTY it fails
fast and points to `serverctl ui --credential ALIAS` instead.

`diagnose` identifies the host's system: its remote check reports `os`,
`os_id`, `os_version`, `os_family`, and `package_manager` (Linux distros via
os-release, macOS via sw_vers, BSDs, and Windows OpenSSH hosts) plus kernel,
arch, CPU, memory, and disk. Use recent cached facts, or run it when those facts
are missing or stale, before choosing install/admin commands; it is read-only and heavier
than `test`, so run it on demand, not as a routine sweep. A successful remote
summary is saved as sanitized `facts`, including `observed_at`, `os_like`, and
a closed `os_logo` key, for subsequent offline inventory reads.

`test` only checks connectivity; it does not collect system facts. The UI's
**Collect host info** action explicitly runs a handshake and the same system
probe as `diagnose`, without SFTP. It supports one host or a selected batch
of 1-64 hosts with concurrency 1-4 and timeout 1-120 seconds per probe (plus
the SSH runner's bounded transport grace). Failed probes keep prior facts.
This is also available via `POST /api/v1/hosts/{id}/observe` and
`POST /api/v1/hosts/observe`; the name-hover preview uses the single-host endpoint
on demand, then reads the updated local Host DTO. The inventory no longer has
per-row or page-wide collection buttons; explicit collection remains in host
details and the selected-host toolbar. There is no new CLI batch command.

`list --json` and `show --json` read local metadata without DNS, SSH, or vault
access. They include nullable `facts`, `last_success_at`, `last_resolved_ip`,
`host_key`, and `geo`, plus the literal `address_scope`. A failed test updates
the latest test result but retains the last successful timestamp and facts.
The resolved IP is a local DNS snapshot from an explicit check, not proof of
the peer IP through ProxyJump. Host-key states are `verified`, `unknown`,
`changed`, or `revoked`; saved state never bypasses OpenSSH verification.
Changing hostname or port clears the old endpoint's observations.

`note` changes only local server metadata. Use `--text` to replace a note,
`--append` to add an agent or user observation without losing existing text, or
`--clear` to remove it. Never put passwords or key passphrases in notes.

## Credentials

```bash
./scripts/serverctl credential list [--json]
./scripts/serverctl credential add-password LABEL
./scripts/serverctl credential add-key LABEL --key-path PATH [--store-passphrase]
./scripts/serverctl credential add-agent LABEL
./scripts/serverctl credential edit LABEL [--label NEW_LABEL] [--key-path PATH]
  [--replace-secret] [--replace-passphrase] [--clear-passphrase]
./scripts/serverctl credential remove LABEL [--yes]
```

Secret input uses a local hidden prompt. There is deliberately no CLI command that reveals a saved secret.

Keys are locally parsed before saving, supporting OpenSSH/PEM RSA, ECDSA and
Ed25519 identities. Unsupported formats fail explicitly. An encrypted key needs
its passphrase through the hidden prompt or local UI; never provide one in chat,
argv or environment variables. The private-key path and public metadata are
saved, not its contents. Empty replacement input preserves the existing value;
`--clear-passphrase` is the explicit removal action. Whitespace is significant.

`remove` still refuses a credential referenced by any host. The Web Credentials
view provides a separate preview and explicit **Detach and delete** transaction,
with durable pending vault/config cleanup and **Retry cleanup** when needed.
Neither operation removes a private-key file. A committed deletion with pending
cleanup must not be presented as completed secret removal.

The Web view also exposes local key Browse/Inspect/Validate, agent identities
and **Test on host**. There is no new credential-test CLI command. The Web test
authenticates the selected credential against any saved host without rebinding
it, executing a remote command, or updating host health history. Password/key
tests restrict authentication to the selected kind; agent tests use only agent
keys without default private-key fallback, unlike ordinary connections that
follow the managed OpenSSH configuration. A `none` authentication result is not
credential verification. Progress/results never return secrets. See
[the local UI](../docs/web-ui.md) and [API contracts](../docs/backend.md).

## Host-bound skills

```bash
./scripts/serverctl skill discover [--json]
./scripts/serverctl skill list [--server ALIAS] [--json]
./scripts/serverctl skill show NAME|ID [--json]
./scripts/serverctl skill add PATH [--server ALIAS ...] [--json]
./scripts/serverctl skill refresh NAME|ID [--path PATH] [--json]
./scripts/serverctl skill attach NAME|ID SERVER [SERVER ...] [--json]
./scripts/serverctl skill detach NAME|ID SERVER [SERVER ...] [--json]
./scripts/serverctl skill remove NAME|ID [--yes] [--json]
./scripts/serverctl skill resolve ALIAS [ALIAS ...] [--json]
```

`discover` scans the standard local skill roots (`~/.agents/skills`,
`~/.codex/skills`, and `~/.claude/skills`) plus `SSM_SKILLS_DIRS`. It is
read-only and offline: candidates are not installed, registered, or bound.
The base `ssh-server-manager` transport skill is omitted and cannot be
registered as host-bound guidance.
`add` accepts either a skill directory or its `SKILL.md`; each repeated
`--server` creates a binding in the same transaction. Skill names are
case-insensitively unique, so same-name candidates at different paths are an
explicit conflict rather than an arbitrary choice.

`discover --json` returns `candidates` and `conflicts`. Candidate status is
`available`, `registered`, `conflict`, or `invalid`; valid candidates include
`name`, `description`, and `path`, registered matches include `registered_id`,
and invalid candidates put the validation reason in `error`. Name conflicts
contain `name` plus `paths`; path conflicts contain `path` plus `names`;
registry conflicts also include `registered_id`.

`refresh` re-reads the registered `SKILL.md`, optionally from a replacement
path. `attach` and `detach` update bindings for all positional server aliases.
`remove` is blocked while any host is bound; detach those hosts first.
`remove --yes` skips only the confirmation and never deletes local skill
files.

Agents should call `resolve` after determining the target hosts and again
whenever the target changes. The JSON contains both per-host skills and a
deduplicated skill list whose `applies_to` aliases preserve multi-host scope.
It returns metadata and paths, not skill bodies. `missing`, `invalid`, and
`name_mismatch` registrations make `ok` false and the command exit `1`; do not
silently reuse a previous host's skill context. `list` and `resolve` add
`status_message` to non-ready entries; `show` returns stored metadata and host
bindings without calculating readiness.

## Saved working directories

```bash
./scripts/serverctl path list ALIAS [--json]
./scripts/serverctl path add ALIAS PATH --label LABEL [--note TEXT] [--json]
./scripts/serverctl path edit ALIAS LABEL|ID [--label NEW] [--path NEW] [--note TEXT] [--json]
./scripts/serverctl path remove ALIAS LABEL|ID [--yes] [--json]
./scripts/serverctl path resolve ALIAS [ALIAS ...] [--json]
```

Call `path resolve` after choosing the target hosts and before browsing or
transferring anything. It returns, in one read and without connecting, the
directories the user has marked as theirs on those hosts — which is the
difference between working in `/srv/app` and guessing at `/var/www`. The
`paths` array collapses identical paths across hosts and lists every host that
has one in `applies_to`, so a fleet-wide action can be scoped in a single step.

Saved directories are one-to-many, unlike skills: each belongs to one host, and
labels are unique per host case-insensitively. They are metadata the user
wrote, not a directory listing — never present them as proof a path exists.

## File transfer

```bash
./scripts/serverctl cp SOURCE DEST [--force] [--timeout SECONDS] [--json]
./scripts/serverctl get ALIAS:REMOTE [LOCAL] [--force] [--timeout SECONDS] [--json]
./scripts/serverctl put LOCAL ALIAS:REMOTE [--force] [--timeout SECONDS] [--json]
```

Use these instead of `scp`, `rsync`, or `sftp`. They run the same SFTP path as
the rest of the tool, so a vault password or key passphrase is injected without
prompting; the raw tools cannot see the vault and will prompt or fail.

Exactly one side is `ALIAS:PATH`. One file per invocation. `get` refuses to
overwrite an existing local file without `--force` — treat that refusal as a
question for the user, not an obstacle to route around.

The browser's separate HTTP transfer interface is capped at 100 MiB per file
(advertised as `max_web_transfer_bytes` by `/api/v1/runtime`). Its uploads
default to refusing an existing remote destination with HTTP `409`; an
authorized overwrite uses the API's explicit `overwrite=true` form field.
Default web uploads require the server's SFTP hard-link operation for safe
no-replace publication; unsupported servers return an error. Web downloads
refuse symlinks when the target size cannot be verified. These HTTP options add
no CLI commands or flags: CLI transfers have no web size cap, and CLI `put`
retains its existing remote-replacement behavior.

## Mounting

```bash
./scripts/serverctl mount ALIAS[:REMOTE] [MOUNTPOINT] [--read-only] [--json]
./scripts/serverctl mount --list [--json]
./scripts/serverctl unmount ALIAS|MOUNTPOINT [--json]
```

Mounting needs a FUSE stack that may not be installed: macFUSE, libfuse, or
SSHFS-Win with WinFsp. Check the `mount` row of `serverctl doctor` before
offering it, and report the missing component instead of retrying. Prefer
`--read-only` unless the user asked to write.

## Connections

```bash
./scripts/serverctl connect ALIAS
./scripts/serverctl exec ALIAS [--stdin|--stdin-binary] [--reuse SECONDS] [--shell] [--json] -- COMMAND [ARG ...]
./scripts/serverctl config render [--json]
```

`connect` opens an interactive shell and is meant for a human terminal, not
for agent tool calls (see SKILL.md). Without a TTY it fails fast with exit
code `2` and a pointer to `exec`.

With the normal OpenSSH `ask` policy, an unknown host key is shown in the
human's controlling terminal even when AskPass supplies a saved password or
key passphrase. Compare its fingerprint with a trusted source before typing
`yes` or the matching fingerprint; `no`, EOF, or an unavailable terminal
rejects confirmation. Explicit stricter SSH policies remain in force. This
does not authorize agent calls, checks, transfers, or credential tests to
accept keys, and it does not replace a changed or revoked key.

`exec` quotes command arguments for a POSIX remote shell. `--shell` accepts
exactly one command string and runs it through remote `sh -lc`; use it for
pipelines, redirects, command substitutions, or multiple commands. Without
`--shell`, pass the executable and each argument separately. For a non-POSIX
remote shell, pass arguments appropriate for that server and omit `--shell`.

`--stdin` is UTF-8 text. `--stdin-binary` streams raw bytes without a text
decode, which is suitable for artifacts piped to a remote command. Do not put
secrets in command arguments, environment variables, or temporary files.
