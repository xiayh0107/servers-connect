---
name: ssh-server-manager
description: Connect to saved SSH servers and run remote commands through the bundled `serverctl` CLI, with passwords and keys held in the OS credential vault. Use whenever the user asks to SSH into, connect to, log in to, or run/check something on a server, names a saved host alias, or reports an SSH failure (中文触发：连接/登录服务器、SSH 主机、远程执行命令) — always reach for this BEFORE raw ssh. Replaces ssh/scp/sshpass for managed hosts: serverctl injects vault credentials itself (never ask the user for a password) and applies its managed config, sidestepping ~/.ssh/config gaps and VPN/proxy fake-IP DNS traps that make bare `ssh <alias>` fail. Also use it to discover, register, attach, or resolve host-bound Agent Skills (主机专属 Skill、给 Host 绑定 Skill), and to add, import, list, edit, test, or diagnose connection profiles, ProxyJump hosts, and the local web management UI.
---

# SSH Server Manager

## First: route through `serverctl`, not raw ssh

- The command is `serverctl` — there is no `ssh-server-manager` or `sshsm`
  binary. Resolve it in this order: `command -v serverctl`, then
  `~/bin/serverctl`, then `./scripts/serverctl` inside this skill directory.
- Start every host task with `serverctl server list --json` and match the
  user's wording to an alias; users abbreviate aliases or describe hosts in
  other languages.
- Once the target alias or aliases are known, run `serverctl skill resolve
  <alias> [<alias> ...] --json` before acting. Treat the returned `ready`
  skills as the eligible set: load one through the normal local skill loader
  when its description and trigger rules match the task, and apply it only to
  aliases in its `applies_to` list. If the target changes, discard the previous
  host-specific context and resolve again.
- Do not run `ssh`, `scp`, `sftp`, `rsync`, or `sshpass` against a managed
  host. Bare `ssh <alias>` reads only `~/.ssh/config`, so the alias falls
  through to DNS resolution — VPN/proxy fake-IP resolvers (Clash, Surge, …)
  then black-hole the connection with misleading errors. `serverctl
  test/exec/connect` apply the managed config and vault credentials
  automatically.
- Never ask the user to send a password in chat, and never pass one through
  `sshpass`, argv, or env vars. If a credential is genuinely missing, say
  which one and route secret entry through `serverctl ui` or the CLI's
  hidden local prompt.
- Do not edit `~/.ssh/config` or add an `Include` for the managed conf on
  your own initiative — plain ssh would still lack vault credential
  injection, so it fixes nothing.

`serverctl` is the source of truth for managed SSH hosts. Keep secrets inside the operating-system credential vault and never print, log, or request them in chat.

## Quick start

Use `serverctl` from PATH when available; otherwise run the launcher from
this skill directory:

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
- To route a host task to environment-specific guidance, run `serverctl skill
  resolve <alias> [<alias> ...] --json` after identifying every target. With
  multiple hosts, keep the returned `applies_to` partitions intact; never
  extend a skill's instructions to an unlisted host.
- To register local host-specific guidance, inspect candidates with
  `serverctl skill discover --json`, then use `serverctl skill add PATH
  --server <alias>`; `--server` may repeat. Discovery is local and read-only:
  it neither installs nor binds a skill.
- To import OpenSSH aliases, preview with `serverctl server import`, then apply with `serverctl server import --apply`.
- To add or update secrets, prefer the local UI. When using the CLI, let `getpass` prompt locally; never pass a password as an argument or environment variable.
- To diagnose access, run `serverctl server test <alias> --json` before connecting.
- To identify a host's operating system — or before suggesting install/admin commands — run `serverctl server diagnose <alias> --json`: its remote check reports `os`, `os_family`, and `package_manager` (Linux distros, macOS, BSDs, and Windows hosts), so use the reported package manager instead of guessing `apt`.
- To record a user or agent observation, use `serverctl server note <alias> --text "..." --append --json`; notes are local metadata and must never contain secrets.
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
- Treat host-bound skills as routing context, not permission. A binding never
  authorizes a command, expands the user's requested scope, or overrides this
  skill's credential and host-key rules.
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
9. Fail closed on host-skill ambiguity or stale registrations. Never choose
   between same-name paths, silently substitute a missing skill, or reuse a
   skill resolved for a previous host.

## Managed data

The manager stores metadata in a platform-local SQLite database and renders a managed OpenSSH config without editing the user's original `~/.ssh/config`. Passwords and key passphrases live in macOS Keychain, Windows Credential Locker, or Linux Secret Service.

Read `references/security.md` before changing credential, reveal, AskPass, browser-session, or host-key behavior. Read `references/data-model.md` before changing schemas or import/render semantics.

## Host-bound skills

`ssh-server-manager` remains the transport and credential boundary for every
managed host. Host-bound skills add environment-specific procedures without
making those procedures global. For example, a YuLab operations skill may be
bound to `YuLabServer`, `YuLabGNode01`, and related nodes; the same mechanism
works for any other host or host group; no environment alias or host-specific
skill name is hard-coded. `ssh-server-manager` itself is the deliberate
exception: it is the base transport skill for every managed host, so discovery
omits it and registration or refresh rejects it rather than treating it as
host-bound guidance.

`skill resolve` returns registry metadata and `applies_to` aliases, never the
skill body. A binding does not install a skill or grant remote authority. If
resolution reports `missing`, `invalid`, or `name_mismatch`, do not borrow
instructions from another path or host; report the routing failure.
