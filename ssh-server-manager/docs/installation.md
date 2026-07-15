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
`~/.codex/skills`; extend with `SSM_SKILLS_DIRS`), and finishes with
`serverctl doctor`. Re-running updates an existing install. See
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
