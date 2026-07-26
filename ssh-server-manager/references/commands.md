# Command reference

## Setup and UI

```bash
./scripts/bootstrap
./scripts/serverctl doctor [--json]
./scripts/serverctl ui [--port PORT] [--no-open --url-file PATH]
./scripts/serverctl ui --status [--json]
./scripts/serverctl ui --stop [--json]
```

The default `--port 0` selects an available loopback port and opens the browser.
When `--no-open` is used, `--url-file` is required; the file is created with
mode `600` and contains the one-time tokenized URL. The token is never printed.
If an explicitly requested port is occupied, retry with `--port 0` or choose a
different port.

`ui --status` reports the managed UI process (pid, port, start time) and exits
`0` while it runs. `ui --stop` terminates it and deletes its `--url-file`; the
command is idempotent, so use it to clean up when the user is done with the UI
instead of tracking pids by hand.

## Servers

```bash
./scripts/serverctl server list [--json]
./scripts/serverctl server show ALIAS [--json]
./scripts/serverctl server add ALIAS --hostname HOST --username USER [--port PORT]
  [--credential CREDENTIAL] [--proxy-jump ALIAS ...] [--notes TEXT]
./scripts/serverctl server edit ALIAS [field options]
./scripts/serverctl server remove ALIAS [--yes]
./scripts/serverctl server import [--config PATH] [--apply] [--overwrite] [--json]
./scripts/serverctl server test ALIAS [--timeout SECONDS] [--json]
./scripts/serverctl server diagnose ALIAS [--timeout SECONDS] [--json]
./scripts/serverctl server note ALIAS (--text TEXT [--append] | --clear) [--json]
```

An import without `--apply` is always a preview. Literal aliases are resolved with `ssh -G`; wildcard-only patterns are reported but not imported.

`diagnose` identifies the host's system: its remote check reports `os`,
`os_id`, `os_version`, `os_family`, and `package_manager` (Linux distros via
os-release, macOS via sw_vers, BSDs, and Windows OpenSSH hosts) plus kernel,
arch, CPU, memory, and disk. Use it before suggesting install or admin
commands so you pick the right package manager; it is read-only and heavier
than `test`, so run it on demand, not as a routine sweep.

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

`exec` quotes command arguments for a POSIX remote shell. `--shell` accepts
exactly one command string and runs it through remote `sh -lc`; use it for
pipelines, redirects, command substitutions, or multiple commands. Without
`--shell`, pass the executable and each argument separately. For a non-POSIX
remote shell, pass arguments appropriate for that server and omit `--shell`.

`--stdin` is UTF-8 text. `--stdin-binary` streams raw bytes without a text
decode, which is suitable for artifacts piped to a remote command. Do not put
secrets in command arguments, environment variables, or temporary files.
