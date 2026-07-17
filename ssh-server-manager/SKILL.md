---
name: ssh-server-manager
description: Manage multiple SSH connection profiles and secure credentials through a cross-platform CLI and local web UI. Use when an AI agent needs to add, import, list, edit, test, connect to, or run commands on SSH servers; manage password, private-key, or ssh-agent authentication; work with ProxyJump hosts; inspect SSH connection failures; or open the local server-management UI.
---

# SSH Server Manager

Use the bundled `serverctl` command as the source of truth for managed SSH hosts. Keep secrets inside the operating-system credential vault and never print, log, or request them in chat.

## Quick start

Run commands from this skill directory:

```bash
./scripts/serverctl doctor
./scripts/serverctl server list --json
./scripts/serverctl ui
```

If the launcher reports missing dependencies, run `./scripts/bootstrap` once. Read `references/commands.md` for the complete command surface.
Use `references/troubleshooting.md` when a UI port, remote command, file stream,
or externally published service behaves unexpectedly.

`serverctl ui` chooses an available loopback port and opens the browser itself. It
never prints the one-time launch token. If the browser must not be opened, write
the tokenized URL to a local mode-600 file instead:

```bash
./scripts/serverctl ui --no-open --url-file /tmp/ssh-server-manager-url
```

Do not redirect UI output to a shared log or copy the URL into chat. A fixed port
can be requested with `--port PORT`; use `--port 0` to return to automatic port
selection.

## Choose the workflow

- To inspect configured hosts, run `serverctl server list --json`.
- To import OpenSSH aliases, preview with `serverctl server import`, then apply with `serverctl server import --apply`.
- To add or update secrets, prefer the local UI. When using the CLI, let `getpass` prompt locally; never pass a password as an argument or environment variable.
- To diagnose access, run `serverctl server test <alias> --json` before connecting.
- To browse a host's files visually, run `serverctl ui`, choose the host under **Files**, and start from its remote home directory.
- To open a live shell, hand `serverctl connect <alias>` to the user's own terminal — see **When the user says "connect"** below. Do not run `connect` from an agent tool call.
- To execute a remote command, run `serverctl exec <alias> -- <command>`; add `--stdin` when piping UTF-8 text.
- To execute one compound POSIX command string, use `serverctl exec <alias> --shell -- 'command && command'`; this avoids accidentally sending the whole string as an executable name.
- To manage hosts or reveal a stored password locally, run `serverctl ui`. The UI requires reauthentication before revealing a secret.

## When the user says "connect"

`serverctl connect` opens a real interactive SSH shell and needs a TTY that
stays attached to the user's keyboard. Agent tool-execution environments run
one command at a time without one, so never run `connect` yourself and never
run a `server test` first just to stall — respond in seconds, not minutes:

1. Check the host's `last_test` in `server list --json`. Only run
   `serverctl server test <alias> --json --timeout 10` when there is no recent
   successful result.
2. Reply briefly: give the exact absolute `serverctl connect <alias>` command
   for the user to paste into their own terminal, and say you can instead run
   commands on that host directly.
3. Treat the host as the active context: interpret follow-up requests
   ("check the load", "what's in /var/log") as `serverctl exec <alias> -- …`
   calls and report the results. Use `--reuse 300` when several commands will
   run in a row.

Keep the whole explanation to two or three sentences. Do not present a
numbered menu of connection modes.

## Interaction style

- Answer inventory questions from one `serverctl server list --json` call.
  Lead with a one-line summary (total hosts, how many tested ok, failed,
  untested), then a compact table with only alias, host:port, user, and last
  test status. Keep per-field detail, speculation about causes, and raw JSON
  for follow-up questions.
- Run only the commands the user's request needs. Do not add unrequested
  tests, doctor runs, or fleet-wide sweeps; offer them as a next step instead.
- End with at most one suggested next step, phrased concretely. Never end
  with a numbered menu of options — a short reply like "sure" or "let me see"
  then forces a guess about which item the user meant.
- The UI is the heavyweight path: it starts a persistent local process.
  Launch it only when the user explicitly asks for the UI or a workflow needs
  it (entering a secret, revealing a password). Check whether one is already
  running with `serverctl ui --status`, and when the user is done, clean up
  with `serverctl ui --stop` (it also removes the `--url-file`).

## Safety rules

1. Never disable host-key verification or add `StrictHostKeyChecking=no`.
2. Never read or display a private-key file. Store only its absolute path.
3. Never include a password, key passphrase, recovery secret, or vault value in model-visible output.
4. Treat a failed or unavailable OS credential backend as a hard error. Never fall back to plaintext files.
5. Preview imports and destructive operations. Do not overwrite an existing alias unless the user explicitly requests it.
6. Use connection profiles for separate accounts on the same endpoint; keep one username and one default credential per alias.
7. Treat UI launch tokens like credentials: keep them in the local browser or an explicitly requested mode-600 file, never in terminal output, logs, screenshots, or chat.
8. Keep remote file browsing read-only unless the user explicitly requests a separate file mutation or transfer workflow.

## Managed data

The manager stores metadata in a platform-local SQLite database and renders a managed OpenSSH config without editing the user's original `~/.ssh/config`. Passwords and key passphrases live in macOS Keychain, Windows Credential Locker, or Linux Secret Service.

Read `references/security.md` before changing credential, reveal, AskPass, browser-session, or host-key behavior. Read `references/data-model.md` before changing schemas or import/render semantics.

## YuLab integration

Use the managed aliases `YuLabServer` and `YuLabGNode01` when available. Keep YuLab ports, service paths, GPU topology, and operational checks in the YuLab-specific skills; use this skill only for host inventory, authentication, and SSH transport.
