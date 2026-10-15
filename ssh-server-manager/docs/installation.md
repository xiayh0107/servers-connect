# Installation

## Requirements

- Python **3.10+**
- An **OpenSSH client** on `PATH` (`ssh -V` to check)
  - macOS / most Linux distros: preinstalled
  - Windows 10/11: `Settings → Apps → Optional features → OpenSSH Client`,
    or `winget install Microsoft.OpenSSH.Beta`
- An unlocked **OS credential vault** (see [platforms.md](platforms.md))

## Option A — pipx / uv (recommended)

Installs `serverctl` as an isolated global command:

```bash
pipx install ssh-server-manager
# or
uv tool install ssh-server-manager
```

Then from anywhere:

```bash
serverctl doctor
serverctl ui
```

## Option B — run from a source checkout

```bash
git clone https://github.com/xiayh0107/servers-connect.git
cd servers-connect/ssh-server-manager
./scripts/bootstrap        # creates .venv and installs dependencies
./scripts/serverctl doctor
```

On Windows:

```bat
scripts\bootstrap.cmd
scripts\serverctl.cmd doctor
```

The `scripts/serverctl` launcher prefers the project `.venv` and falls back
to the system `python3`.

## Option C — as an AI Agent Skill (one line)

macOS / Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/xiayh0107/servers-connect/main/install.sh | sh
```

Windows:

```powershell
powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/xiayh0107/servers-connect/main/install.ps1 | iex"
```

The installer fetches the source (or uses the checkout it runs from —
`./install.sh` works too), installs dependencies, symlinks the skill into
every detected agent skills directory (`~/.claude/skills`,
`~/.codex/skills`; extend with `SSM_SKILLS_DIRS`), links `serverctl` into
`~/.local/bin` when that directory exists, and finishes with
`serverctl doctor`. Re-running updates an existing install; `doctor`'s
`agent_skill` check warns when a linked skill copy is older than the CLI,
so agents never keep following stale instructions silently. See
[ai-agents.md](ai-agents.md).

## Verify the install

```bash
serverctl doctor --json
```

Every check should report `"ok": true`. The most common failure is the
vault check on Linux — see [platforms.md](platforms.md#linux).

## Uninstall

- `pipx uninstall ssh-server-manager` (or `uv tool uninstall`)
- Data lives in a platform data directory (`serverctl doctor` prints the
  database path); secrets live in the OS vault under the service name
  `ssh-server-manager`. Remove credentials with `serverctl credential remove`
  *before* uninstalling if you want the vault entries cleaned up.
- The rendered config at `~/.ssh/ssh-server-manager.conf` can be deleted;
  your original `~/.ssh/config` was never modified.

## Upgrade to 0.9.0

Stop the running Web UI before upgrading:

```bash
serverctl web --stop
pipx upgrade ssh-server-manager
# or: uv tool upgrade ssh-server-manager
```

Back up the SQLite database in the platform data directory before upgrading.
With the Web UI and other serverctl processes stopped, copy `manager.db` to a
private backup directory. Keep the OS vault entries and referenced private-key
files in place; the database contains references and metadata, not their secrets.
Opening the database migrates schema 5 (0.8.0) or development schema 6 to schema 7
transactionally. IDs, routes, tags, notes, directories, Skills and reveal settings
are preserved. A newer unsupported schema is rejected. To return to 0.8.0, stop
all processes and restore the pre-upgrade database backup before starting it.

The wheel and source distribution include the fingerprinted production Web
assets and their manifest. Running an installed package or a source checkout
does not require Node.js or an internet connection for the UI. Node 22 is needed
only when changing the frontend source; see [frontend development](https://github.com/xiayh0107/servers-connect/blob/main/ssh-server-manager/frontend/README.md).
If the native vault is unavailable, credential operations report that state;
there is no fallback that saves secrets in a file.
