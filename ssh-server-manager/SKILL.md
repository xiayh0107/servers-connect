---
name: ssh-server-manager
description: Manage multiple SSH connection profiles and secure credentials through a cross-platform CLI and local web UI. Use when Codex needs to add, import, list, edit, test, connect to, or run commands on SSH servers; manage password, private-key, or ssh-agent authentication; work with ProxyJump hosts; inspect SSH connection failures; or open the local server-management UI.
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
- To open a shell, run `serverctl connect <alias>`.
- To execute a remote command, run `serverctl exec <alias> -- <command>`; add `--stdin` when piping UTF-8 text.
- To execute one compound POSIX command string, use `serverctl exec <alias> --shell -- 'command && command'`; this avoids accidentally sending the whole string as an executable name.
- To manage hosts or reveal a stored password locally, run `serverctl ui`. The UI requires reauthentication before revealing a secret.

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
