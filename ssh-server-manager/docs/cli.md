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
