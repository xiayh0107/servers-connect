---
name: ssh-server-manager
description: Connect to saved SSH servers and run remote commands through the bundled `serverctl` CLI, with passwords and keys held in the OS credential vault. Use whenever the user asks to SSH into, connect to, log in to, or run/check something on a server, names a saved host alias, or reports an SSH failure (中文触发：连接/登录服务器、新建/添加/登记服务器、SSH 主机、远程执行命令) — always reach for this BEFORE raw ssh. Replaces ssh/scp/sshpass for managed hosts: serverctl injects vault credentials itself (never ask the user for a password) and applies its managed config, sidestepping ~/.ssh/config gaps and VPN/proxy fake-IP DNS traps that make bare `ssh <alias>` fail. Also use it to transfer files to or from a managed host (上传/下载文件、传文件), to recall a host's saved working directories (常用目录、工作目录), to mount a remote directory locally with sshfs (挂载远程目录), to discover, register, attach, or resolve host-bound Agent Skills (主机专属 Skill、给 Host 绑定 Skill), and to add, import, list, edit, test, or diagnose connection profiles, ProxyJump hosts, and the local web management UI.
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

## After installation or first setup

Do not treat a successful `serverctl doctor` as the end of an installation or
configuration task. Installation is capability injection; onboarding is
complete only when the user understands what they can now ask you to do.

After `doctor` succeeds:

1. Run `serverctl server list --json` once. This is a local inventory read; do
   not add a remote `server test` just for onboarding.
2. Summarize the actual managed hosts in user language. If there are no hosts,
   say the tool is ready and offer to import an existing OpenSSH config or add
   the first host. If there is one host, name it. If there are several, name a
   useful compact subset or summarize the fleet.
3. Give 3–5 short examples of natural-language requests tied to the real
   inventory, such as “check GPU usage on lab-gpu”, “show me what is in
   /data on web1”, or “upload this file to web1”. Do not make the user learn
   `serverctl` syntax to benefit from the product.
4. State the security benefit in one sentence: saved passwords/passphrases
   stay in the local OS vault and do not need to be sent in chat or exposed to
   the model.
5. Distinguish local readiness from remote reachability. `doctor` proves the
   local installation is healthy; unless a host was explicitly tested, do not
   imply that its login has been verified.

Keep this first-run handoff concise. Do not lead with database paths, rendered
config paths, connection-reuse implementation details, or raw doctor JSON
unless the user asked for diagnostics. Those details remain available for
troubleshooting, but the default post-install response should lead with what
the user can now accomplish.

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
- To add a server that is not managed yet, follow **When the user wants to add a server** below: collect the fields in chat, create the profile, then route secret entry through `serverctl ui --credential ALIAS`.
- To add or update secrets, prefer the local UI — `serverctl ui --credential ALIAS` opens the browser directly on that server's credential form. When using the CLI, let `getpass` prompt locally; never pass a password as an argument or environment variable.
- To diagnose access, run `serverctl server test <alias> --json` before connecting.
- To identify a host's operating system — or before suggesting install/admin commands — run `serverctl server diagnose <alias> --json`: its remote check reports `os`, `os_family`, and `package_manager` (Linux distros, macOS, BSDs, and Windows hosts), so use the reported package manager instead of guessing `apt`.
- To record a user or agent observation, use `serverctl server note <alias> --text "..." --append --json`; notes are local metadata and must never contain secrets.
- To find where a host keeps its files, run `serverctl path resolve <alias> [<alias> ...] --json` before browsing or transferring. It reports the directories the user saved, in one read and without connecting; identical paths across hosts arrive grouped under `applies_to`.
- To browse a host's files visually, run `serverctl ui`, choose the host under **Files**, and start from its remote home directory.
- To copy one file, run `serverctl get <alias>:/remote/path ./local` or `serverctl put ./local <alias>:/remote/path`. Never use `scp`, `rsync`, or `sftp` against a managed host — they cannot see the vault and will prompt or fail.
- To open a remote directory in a local editor, run `serverctl mount <alias>:/remote/path`. Check the `mount` row of `serverctl doctor` first: mounting needs macFUSE, libfuse, or SSHFS-Win, and if it is missing report that rather than retrying.
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

## When the user wants to add a server

Drive the whole onboarding yourself instead of waiting for the user to
discover the commands. Reach for this flow whenever the user asks to add,
register, or set up a server (中文：新建/添加/登记服务器) — and offer it when a
task names a host that is not in `serverctl server list --json`, instead of
falling back to raw ssh.

1. Collect the non-secret fields in chat: alias, hostname or IP, port,
   username, and any ProxyJump hop. Ask only for what is missing. If the host
   already lives in the user's `~/.ssh/config`, offer `server import` instead.
2. Create the profile:
   `serverctl server add ALIAS --hostname HOST --username USER --json`.
   `add` only creates: if it reports the alias already exists, the profile is
   already there — switch to `serverctl server edit ALIAS ...` for the change
   the user actually wants (for example `--credential LABEL`). Never remove
   and re-create to work around a conflict, and never treat a taken
   credential label as anything other than a `credential edit` task.
3. Route the credential by kind. You may create `agent` credentials and
   passphrase-less key credentials yourself (`credential add-agent`,
   `credential add-key` without `--store-passphrase`) and attach them with
   `server edit ALIAS --credential LABEL`. A password or key passphrase is
   different: the CLI prompt needs a local TTY an agent tool call does not
   have, and asking for the secret in chat is forbidden. Launch
   `serverctl ui --credential ALIAS` as a background command — the browser
   opens directly on that server's edit dialog with the new-credential form
   expanded, so the user types the password once and clicks **Save and use**.
   When no browser is available, hand two commands to the user's own terminal
   (the same delivery rule as `connect`): `serverctl credential add-password
   LABEL`, then `serverctl server edit ALIAS --credential LABEL`.
4. Once the user says the secret is saved, run
   `serverctl server test ALIAS --json` and report the result. Do not treat
   the host as usable before the test passes.

Keep the guidance to a sentence or two per step, and never paste a menu of
onboarding options.

## Interaction style

- Treat `serverctl` as an agent-facing capability API, not something the user
  must learn. Prefer “I can check GPU usage on `lab-gpu`” over teaching the
  corresponding CLI command. Show exact commands when the user asks for them,
  needs an interactive terminal command, or is troubleshooting the tool.
- Do not assume the user has read this README, the web UI, or any product
  documentation. Introduce capabilities at the moment they become useful,
  using the user's host names and task language rather than feature names.
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
8. Keep remote file browsing read-only. Move a file only when the user asked for that file to move, and use `serverctl cp`, `get`, or `put` — never raw `scp`, `rsync`, or `sftp`, which bypass the vault. Confirm before overwriting anything; `get` refuses an existing local file unless `--force` is passed, and that refusal is a prompt for the user, not an obstacle to work around.
9. Fail closed on host-skill ambiguity or stale registrations. Never choose
   between same-name paths, silently substitute a missing skill, or reuse a
   skill resolved for a previous host.

## Managed data

The manager stores metadata in a platform-local SQLite database and renders a managed OpenSSH config without editing the user's original `~/.ssh/config`. Passwords and key passphrases live in macOS Keychain, Windows Credential Locker, or Linux Secret Service.

Read `references/security.md` before changing credential, reveal, AskPass, browser-session, or host-key behavior. Read `references/data-model.md` before changing schemas or import/render semantics.

## Working with remote files

Three tools, in the order to reach for them:

1. `serverctl path resolve` tells you where the user works on a host. Saved
   directories are metadata the user wrote — they are not proof a path still
   exists, so treat a resolve result as a starting point, not a guarantee.
2. `serverctl get` / `put` / `cp` move one file at a time over the same SFTP
   path and vault credentials as everything else. Exactly one side is
   `ALIAS:PATH`; host-to-host copies are refused. `get` will not overwrite an
   existing local file without `--force`, and that refusal is a question for
   the user, not something to force past.
3. `serverctl mount` is for editing a handful of files in a local editor. It
   depends on a FUSE stack that is often absent, and it makes remote latency
   look local — anything that walks a large tree (a repo-wide search, a build,
   an editor indexing a project) belongs in `serverctl exec` instead.

Recursive transfer is deliberately unsupported. If the user needs a whole tree,
say so and offer `serverctl exec` with a remote archive step, or a mount.

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
