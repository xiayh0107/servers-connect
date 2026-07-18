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
