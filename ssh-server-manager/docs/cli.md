# `serverctl` command reference

All commands accept `--json` for machine-readable output unless noted.
Errors exit with code `2` and, under `--json`, print
`{"ok": false, "error": "<Type>", "message": "..."}` on stdout.

## Global

```
serverctl --version
serverctl doctor [--json]
```

`doctor` checks: serverctl version, platform (and whether connection reuse
is available), database path, ssh / sftp / ssh-keygen presence and client version,
original and managed config paths, vault backend safety, and the Python
dependencies needed by the UI. Exit code is `0` only if every check passes.

## UI

```
serverctl ui [--port PORT] [--no-open --url-file PATH]
serverctl ui --status [--json]
serverctl ui --stop [--json]
```

- Default `--port 0` picks a free loopback port and opens your browser with
  a one-time tokenized URL. The token is never printed to the terminal.
- `--no-open` requires `--url-file`; the URL is written to a mode-600 file
  that is deleted once the token is used or the server stops.
- If an explicit port is busy the command fails fast; retry with `--port 0`.
- `--status` reports the UI process started by the most recent `serverctl ui`
  (pid, port, start time). Exit code `0` while it runs, `1` otherwise; stale
  records are cleaned up automatically.
- `--stop` terminates that process and deletes its `--url-file` if one was
  written. It is idempotent: exit code `0` whenever no UI is left running.

## Servers

```
serverctl server list [--json]
serverctl server show ALIAS [--json]
serverctl server add ALIAS --hostname HOST --username USER [--port PORT]
    [--credential LABEL|ID] [--proxy-jump ALIAS ...] [--tag TAG ...] [--notes TEXT]
serverctl server edit ALIAS [--new-alias A] [--hostname H] [--username U]
    [--port P] [--credential LABEL|'none'] [--proxy-jump ALIAS ...]
    [--tag TAG ... | --clear-tags] [--notes TEXT]
serverctl server remove ALIAS [--yes]
serverctl server import [--config PATH] [--apply] [--overwrite] [--json]
serverctl server test ALIAS [--timeout SECONDS] [--json]
serverctl server diagnose ALIAS [--timeout SECONDS] [--json]
serverctl server note ALIAS (--text TEXT [--append] | --clear) [--json]
```

Notes:

- Aliases are case-insensitively unique; whitespace, shell metacharacters,
  and OpenSSH wildcards are rejected.
- `--proxy-jump` may repeat to build an ordered hop chain; self-jumps and
  cycles are rejected. Passing `--proxy-jump` during `edit` replaces the
  whole chain.
- `--tag` may repeat to assign project, environment, or role tags. On
  `edit`, supplied tags replace the existing set; `--clear-tags` removes all.
- `import` without `--apply` is always a preview. Conflicting aliases are
  skipped unless `--overwrite`.
- `test` runs a real `ssh` handshake (`NumberOfPasswordPrompts=1`,
  StrictHostKeyChecking) and reports `latency_ms` plus one of:
  `authentication-failed`, `host-key-untrusted`, `timeout`,
  `connection-refused`, `dns-failed`, `network-unreachable`,
  `ssh-not-found`, `ssh-failed`. Exit code `1` on failure.
- `diagnose` runs read-only checks for the local profile, SSH handshake, SFTP
  home-directory access, and a fixed remote system-summary command. The result
  identifies the operating system across Linux distributions (os-release with
  legacy-release fallbacks), macOS (`sw_vers`), BSDs, and Windows OpenSSH
  hosts, reporting structured `os`, `os_id`, `os_version`, `os_family`, and
  `package_manager` fields (e.g. `debian`/`apt`, `rhel`/`dnf`, `suse`/`zypper`,
  `arch`/`pacman`, `alpine`/`apk`, `macos`/`brew`) alongside kernel, CPU,
  uptime/load, memory, and root-disk details when the host exposes them. It
  never returns credentials or raw remote output; `--json` includes structured
  check results. Exit code `1` only when at least one check fails.
- `note` lets a user or agent set, append, or clear the server's human-readable
  note without changing its SSH connection settings. Notes are local metadata;
  never put passwords, key passphrases, or other secrets in them.
- `remove` prompts unless `--yes`; in non-interactive contexts (no TTY)
  it fails closed instead of assuming consent.

## Credentials

```
serverctl credential list [--json]
serverctl credential add-password LABEL
serverctl credential add-key LABEL --key-path PATH [--store-passphrase]
serverctl credential add-agent LABEL
serverctl credential edit LABEL|ID [--label NEW] [--key-path PATH]
    [--replace-secret] [--replace-passphrase] [--clear-passphrase]
serverctl credential remove LABEL|ID [--yes]
```

Notes:

- Secrets are always collected through a hidden local prompt (`getpass`),
  never through arguments or environment variables.
- There is **no** command that prints a stored secret. Reveal requires the
  web UI plus passkey / master-password re-authentication.
- Only the private key **path** is stored; the key file itself is never
  read or displayed, and removing the credential never deletes the file.
- A credential referenced by any server cannot be removed.

## Host-bound skills

```
serverctl skill discover [--json]
serverctl skill list [--server ALIAS] [--json]
serverctl skill show NAME|ID [--json]
serverctl skill add PATH [--server ALIAS ...] [--json]
serverctl skill refresh NAME|ID [--path PATH] [--json]
serverctl skill attach NAME|ID SERVER [SERVER ...] [--json]
serverctl skill detach NAME|ID SERVER [SERVER ...] [--json]
serverctl skill remove NAME|ID [--yes] [--json]
serverctl skill resolve ALIAS [ALIAS ...] [--json]
```

Host-bound skills let environment-specific operating guidance follow the
managed hosts where it is relevant. The relationship is many-to-many: one
skill can cover a group of related nodes, and one host can use several skills.
Nothing is hard-coded for a particular provider, cluster, or alias.

- `discover` scans `~/.agents/skills`, `~/.codex/skills`,
  `~/.claude/skills`, and directories in `SSM_SKILLS_DIRS`. It makes no
  network requests and does not install, register, or bind anything. The base
  `ssh-server-manager` transport skill is deliberately omitted and cannot be
  registered as host-bound guidance.
- `list` shows registered skills; `--server` filters by a bound host. `show`
  accepts a case-insensitive name or skill ID and includes its host bindings.
- `add` accepts a skill directory or its `SKILL.md`. It stores the normalized
  absolute `SKILL.md` path and frontmatter metadata, not the skill body.
  Repeated `--server` options register and bind atomically.
- `refresh` re-reads one registered skill. `--path` moves the registration to
  another local skill directory or `SKILL.md` after validation.
- `attach` and `detach` accept one or more positional server aliases and
  change all requested bindings atomically.
- `remove` prompts unless `--yes` and is blocked while any host is still
  bound; detach those hosts first. It removes only the registry entry, never
  the local skill files. In a non-interactive context it fails closed without
  `--yes`.
- Names are case-insensitively unique. Discovery reports same-name candidates
  at different paths as conflicts, and registration refuses to guess between
  them.

`discover --json` returns `{"candidates": [...], "conflicts": [...]}`. Every
candidate has `path` and `status`; a valid candidate also has `name` and
`description`. Candidate status is:

- `available` — valid local skill that is not registered;
- `registered` — its name and path match a registry entry, whose ID is in
  `registered_id`;
- `conflict` — its name or path disagrees with another candidate or registry
  entry;
- `invalid` — its frontmatter could not be validated; the reason is in
  `error`.

Each conflict has a `type`. A name conflict reports `name` and `paths`; a path
conflict reports `path` and `names`. Conflicts against an existing registry
entry also include `registered_id`. Discovery status describes candidates,
not whether a registered skill is currently usable.

Agents call `resolve` only after selecting the target alias or aliases. Its
JSON has both a per-host view and a deduplicated view:

```json
{
  "ok": true,
  "hosts": [
    {"id": "...", "alias": "gpu-lab-01", "skills": [{"id": "...", "name": "gpu-ops", "path": "/.../gpu-ops/SKILL.md", "description": "...", "status": "ready"}]}
  ],
  "skills": [
    {"id": "...", "name": "gpu-ops", "path": "/.../gpu-ops/SKILL.md", "description": "...", "status": "ready", "applies_to": ["gpu-lab-01"]}
  ]
}
```

The response contains metadata and local paths, never skill bodies. Each
skill's `applies_to` list is authoritative for a multi-host task. Current file
status is `ready`, `missing`, `invalid`, or `name_mismatch`; any non-ready
registration makes `ok` false and exits `1` instead of silently substituting a
different path. `skill list` and both views in `skill resolve` include a
`status_message` for non-ready entries; ready entries omit it. `skill show`
returns stored registry metadata and host bindings without calculating
readiness.

## Connect and execute

```
serverctl connect ALIAS [--timeout SECONDS]
serverctl exec ALIAS [--shell] [--stdin | --stdin-binary]
    [--timeout SECONDS] [--reuse SECONDS] [--json] -- COMMAND [ARG ...]
```

- `connect` opens an interactive shell and requires a real terminal. Without
  a TTY (pipes, agent tool calls, cron) it fails fast with exit code `2` and
  points to `exec` instead of hanging.
- Everything after `--` is the remote command. Without `--shell`, each
  argument is quoted for a POSIX remote shell — write
  `exec web1 -- ls -la /var/log`, not a single string.
- `--shell` accepts exactly one string and runs it via remote `sh -lc`;
  use it for pipelines, redirects, globs, and `&&` chains.
- `--stdin` streams UTF-8 text; `--stdin-binary` streams raw bytes.
- `--reuse N` enables OpenSSH ControlMaster for `N` seconds so consecutive
  `exec` calls skip re-authentication. macOS/Linux only; on Windows a
  warning is printed and the option is ignored.
- Exit code mirrors the remote command's return code. `--json` adds
  `stdout`, `stderr` (redacted), `latency_ms`, and `error_code`.

## Config

```
serverctl config render [--json]
```

Re-renders the managed OpenSSH config (also done automatically after every
mutation) and prints its path. The file is written atomically with
user-only permissions and `Include`s your original config last.
