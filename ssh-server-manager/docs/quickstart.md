# Quickstart

Ten minutes from install to a managed fleet. Commands below assume an
installed `serverctl`; from a source checkout use `./scripts/serverctl`.

## 1. Check your environment

```bash
serverctl doctor
```

Fix anything not `ok` before continuing ([installation.md](installation.md),
[platforms.md](platforms.md)).

## 2. Bring in the servers you already have

```bash
serverctl server import            # preview: what would be imported
serverctl server import --apply    # copy literal Host aliases into the manager
```

The importer follows `Include` directives and resolves each alias with
`ssh -G`. Wildcard patterns are reported but not imported. Your original
`~/.ssh/config` is read, never written.

## 3. Store a credential

```bash
serverctl credential add-password prod-password      # hidden local prompt, stored in OS vault
serverctl credential add-key deploy-key --key-path ~/.ssh/id_ed25519 --store-passphrase
serverctl credential add-agent just-use-my-agent     # ssh-agent / OpenSSH defaults
```

There is deliberately **no CLI command that prints a stored secret**.
Revealing one requires the web UI plus passkey / master-password re-auth.

## 4. Add a server and attach the credential

```bash
serverctl server add web1 \
  --hostname web1.example.com --username deploy \
  --credential prod-password --notes "primary web node"
```

Behind a bastion? Model the hop as its own alias and chain it:

```bash
serverctl server add bastion --hostname bastion.example.com --username jump
serverctl server add db1 --hostname 10.0.2.7 --username dba --proxy-jump bastion
```

## 5. Test, connect, execute

```bash
serverctl server test web1            # latency + classified error code
serverctl connect web1                # interactive shell
serverctl exec web1 -- uptime         # one command, args passed safely
serverctl exec web1 --shell -- 'df -h | sort -k5 -r | head'   # pipelines need --shell
```

Stream a file or script:

```bash
cat deploy.sh | serverctl exec web1 --stdin -- sh -s
cat build.tar.gz | serverctl exec web1 --stdin-binary -- sh -c 'cat > /tmp/build.tar.gz'
```

Run several commands without re-authenticating each time (macOS/Linux):

```bash
serverctl exec web1 --reuse 300 -- uptime   # keeps the connection alive 300 s
```

## 6. Optional: attach environment-specific Agent Skills

Discover local Skills, register one, and bind it only to the Hosts where its
guidance applies:

```bash
serverctl skill discover --json
serverctl skill add ~/.agents/skills/web-operations --server web1 --json
serverctl skill resolve web1 --json
```

Replace the example path with a candidate returned by `discover`. Discovery is
local and read-only. A binding makes a Skill eligible for that Host; normal
Skill trigger rules still decide whether it matches the task, and the binding
does not authorize remote changes. See
[ai-agents.md](ai-agents.md#host-specific-agent-skills).

## 7. Optional: the web UI

```bash
serverctl ui
```

Opens the local management interface with a one-time tokenized URL —
add/edit servers and credentials, discover and register Skills in the Skill
Library, manage a Host's Skill assignments, run tests, import config, and
reveal a stored secret after re-authentication. See [web-ui.md](web-ui.md).

## 8. Plain `ssh` still works

Every managed alias is rendered into `~/.ssh/ssh-server-manager.conf`, which
`Include`s your original `~/.ssh/config` at the end — so managed values win
while your existing defaults keep applying. To use managed aliases with plain
OpenSSH tools, point them at the rendered file:

```bash
ssh  -F ~/.ssh/ssh-server-manager.conf web1
scp  -F ~/.ssh/ssh-server-manager.conf ./file web1:/tmp/
rsync -e 'ssh -F ~/.ssh/ssh-server-manager.conf' -avz ./dir/ web1:/srv/dir/
```

Do **not** add `Include ~/.ssh/ssh-server-manager.conf` to your original
config — that would create an Include cycle, and `serverctl` will refuse to
render. Password/passphrase injection only happens through `serverctl`;
plain `ssh` prompts as usual.
