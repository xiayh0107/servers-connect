# Troubleshooting playbook

## Local UI does not open

Run the default launcher first; it selects a free loopback port and opens the
browser:

```bash
./scripts/serverctl ui
```

If a fixed port is required and it is occupied, either stop the process that
owns it or use automatic selection:

```bash
./scripts/serverctl ui --port 0
```

For a manually opened browser, keep the one-time URL in a private file rather
than printing it:

```bash
./scripts/serverctl ui --no-open --url-file /tmp/ssh-server-manager-url
```

The file is mode `600` and is intended for the same local user only.

## Remote command reports “No such file or directory”

`serverctl exec` treats arguments literally. A full command string such as
`printf ...; uname -a` becomes one executable name unless shell mode is explicit:

```bash
./scripts/serverctl exec ALIAS --shell -- 'printf "host=%s\\n" "$(hostname)"; uname -a'
```

For a normal executable, pass the executable and arguments separately:

```bash
./scripts/serverctl exec ALIAS -- uname -a
```

## Sending a file or script

For a text script, use:

```bash
cat local-script.sh | ./scripts/serverctl exec ALIAS --stdin -- sh -s
```

For a binary artifact, stream raw bytes directly:

```bash
cat artifact.tar.gz | ./scripts/serverctl exec ALIAS --stdin-binary -- sh -c 'cat > artifact.tar.gz'
```

Keep streamed data out of logs and do not use this path for credentials.

## Remote Files panel does not load

Test the same alias first:

```bash
./scripts/serverctl server test ALIAS --json
./scripts/serverctl doctor --json
```

The browser uses the local OpenSSH `sftp` executable and the remote SFTP
subsystem. A successful interactive shell does not guarantee that SFTP is
enabled. Directory permission errors are surfaced in the panel; unknown host
keys remain a hard failure.

## SSH succeeds but a web port is unreachable

First verify the service on the server itself, then verify the container or
process is bound to the intended interface. If localhost works but the public
address times out, the cloud security group or upstream firewall needs to allow
that TCP port; changing OpenSSH settings will not fix it.

## Bare `ssh ALIAS` fails while `serverctl` succeeds

Managed aliases live in the manager's own rendered config, not in
`~/.ssh/config`, so plain `ssh ALIAS` treats the alias as a DNS hostname.
Under a VPN/proxy fake-IP resolver (Clash, Surge, …) that lookup gets a
synthetic answer such as `198.18.x.x` and the handshake is dropped — the
failure looks like a dead server even though nothing is wrong. Use
`serverctl connect ALIAS` (interactive) or `serverctl exec ALIAS -- CMD`
instead; they apply the managed config and inject vault credentials
automatically. Do not add an `Include` for the managed config to
`~/.ssh/config`: key-based hosts would half-work, but password auth flows
only through serverctl's AskPass, so bare ssh would still prompt.

## An AI agent hand-rolls ssh/sshpass instead of using the manager

Three checks, in order:

1. `serverctl doctor` — the `agent_skill` line lists which agent skill
   directories are linked and warns when a linked copy is older than the
   running CLI. Re-run `install.sh` to update stale copies, then restart
   the agent session so it reloads the skill text.
2. Make sure `serverctl` resolves by name (`command -v serverctl`); the
   installer links it into `~/.local/bin` when that directory exists.
3. A one-line nudge — "use serverctl" or "use the ssh-server-manager
   skill" — re-routes an agent that already started down the raw-ssh path.

Never give an agent a password in chat; if a credential is missing, add it
through `serverctl ui` so it lands in the OS vault.
