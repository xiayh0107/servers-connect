# AGENTS.md

Guidance for AI coding agents working on this repository. (If you are an
agent *using* the product to manage SSH servers, read
`ssh-server-manager/SKILL.md` instead — this file is about developing it.)

## What this repo is

- `ssh-server-manager/` — the product: `serverctl` CLI + loopback FastAPI web
  UI + OS-keychain vault, shipped as a Python package and as an Agent Skill.
- `website/` — the landing page published by the `pages.yml` workflow.
- `install.sh` / `install.ps1` — one-line agent-skill installers users pipe
  from GitHub; treat their public URLs as a stable contract.
- Gitignored directories at the repo root are private local workspaces:
  never read them into changes, publish them, or clean them up.

## Build and test

Work from `ssh-server-manager/`. A project venv already exists at `.venv/`
(create it with `./scripts/bootstrap` if missing).

```bash
.venv/bin/python -m pytest tests/        # full suite, sub-second
./scripts/serverctl doctor --json        # end-to-end health check
```

For manual testing, isolate state with `SSM_DATA_DIR` and `SSM_RUNTIME_DIR`
env vars so you never touch the developer's real host database, keychain
entries, or a UI instance they have running. CI (`test.yml`) runs the suite
on macOS, Linux, and Windows — keep changes portable (e.g. no POSIX-only
path or signal assumptions without an `os.name` branch).

## Source map

- `scripts/ssh_server_manager/cli.py` — argument parsing and `handle()`
  dispatch; `scripts/serverctl` is the launcher.
- `scripts/ssh_server_manager/ssh_runner.py` — test/connect/exec over the
  system OpenSSH client; error classification lives here.
- `scripts/ssh_server_manager/webapp.py` — FastAPI UI, launch-token flow,
  and the UI runtime state file behind `ui --status/--stop`.
- `scripts/ssh_server_manager/vault.py`, `askpass.py` — secret storage and
  out-of-band authentication. Read `references/security.md` before touching.

## Web UI assets are committed minified

`scripts/ssh_server_manager/assets/ui/` holds the shipped (minified) files —
there is no separate source tree. Edit them only through
`scripts/build-ui beautify <workdir>` → edit → `scripts/build-ui minify
<workdir>`; the script pins the verified toolchain (terser round-trips the
committed JS byte-identically, lightningcss with modern targets for CSS) and
enforces the size budgets from `tests/test_ui_assets.py`. Two traps it
guards against: csso silently deletes `@media (width<=NNNpx)` blocks — never
substitute it — and `webapp.py` injects lazy assets by string-replacing the
literal `<div class=app-shell>`, which the minify step asserts is preserved.
Always finish with `tests/test_ui_assets.py`: it greps the minified output
for exact tokens.

## Documentation must stay in sync

Any change to the CLI surface or agent-facing behavior updates all of:

1. `SKILL.md` (+ `references/commands.md`) — what agents load; includes
   interaction-style rules, not just command syntax.
2. `docs/cli.md` and the relevant `docs/*.md` page.
3. `docs/ai-agents.md` — the install/integration page users hand to agents.
4. `CHANGELOG.md` under `## Unreleased`.

The skill's conversation-level UX is a product feature: judge SKILL.md
changes against real agent transcripts (would an agent following this reply
in seconds, without option menus, without hanging on a TTY?), not just
against command coverage.

## Safety rules (mirror SKILL.md — they bind development too)

- Never print, log, or commit secrets, launch tokens, or vault values;
  secret entry happens only via hidden local prompts or the UI.
- Never weaken host-key verification, even in tests.
- Keep the UI loopback-only and the launch token out of process output.

## Releases

Version lives in **four places** — bump all of them together:
`ssh-server-manager/pyproject.toml`, `scripts/ssh_server_manager/__init__.py`,
and two spots in `website/index.html` (version badge + footer). Then:

1. Move `CHANGELOG.md` `## Unreleased` into a dated `## X.Y.Z` section.
2. Run the full test suite; build with
   `.venv/bin/python -m build --outdir dist .` and smoke-test the wheel in a
   clean venv (`serverctl --version`).
3. Commit, push, tag `vX.Y.Z`, and `gh release create vX.Y.Z` with the wheel
   and sdist attached (`dist/` is not tracked by git).
4. Publishing the GitHub Release triggers `publish.yml`, which uploads to
   PyPI via Trusted Publishing (OIDC, environment `pypi`, `skip-existing`
   enabled). No local credentials are involved; twine is a manual fallback.
