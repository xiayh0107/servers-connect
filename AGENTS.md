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
.venv/bin/python -m pytest tests/        # isolated Python regression suite
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

## Web UI source and generated assets

`ssh-server-manager/frontend/` is the React + TypeScript + Vite source tree.
Edit `frontend/src/`, never hand-edit generated files in
`scripts/ssh_server_manager/assets/ui/`. Commit source, `package-lock.json`,
generated API types, and built assets together. Node.js 22.12 or newer is
required for development; installed wheels serve the bundled build without
Node.js, a development server, external fonts, or CDN scripts.

From `ssh-server-manager/`:

```bash
npm ci --prefix frontend
npm run check-types --prefix frontend
npm test --prefix frontend
npm run build --prefix frontend
npm run check-build --prefix frontend -- --allow-uncommitted
.venv/bin/python -m pytest tests/test_ui_assets.py tests/test_webapp.py tests/test_webapp_security.py
```

After an API change, regenerate `contracts/api-v1.openapi.json` with the
pinned contract toolchain, then run `npm run typegen --prefix frontend` and
review both diffs. `check-types` verifies generated types; `check-build`
rebuilds in a temporary directory, compares every asset and manifest byte,
rejects source maps, and checks the initial 150 KiB / total 250 KiB gzip
budgets. Its default also rejects uncommitted assets; the flag above is for
local validation before committing. CI runs the default check on committed
output. Run `npm run e2e --prefix frontend -- --project=smoke` for disposable
browser fixtures and the SSH project for real loopback authentication/SFTP
coverage; see `frontend/README.md` for prerequisites. Never capture secrets,
launch URLs, session cookies, or reveal views in screenshots or traces.

Radius and spacing are tokenized: use the `--radius-sm/md/lg/xl` and
`--space-1..7` scales from `styles.css` `:root` instead of adding new pixel
literals, and keep the shipped default accent teal (Indigo stays a
user-selectable option). The rationale
lives in `docs/ui-ux-research.md` ("Design tokens").

Keep opening host details free of DNS/SSH/SFTP/vault operations. Host routes,
cached observations, workspace paths and credential summaries are local
metadata. Remote checks, file browsing, credential tests, local key Browse,
key validation and agent inspection require their explicit controls. The Hosts
name-hover preview is a deliberate exception: after its dwell delay it may
observe that host's missing/stale facts, with a single flight, freshness cache,
failure cooldown and inline loading. Inventory load, focus/history restoration
and Hide details must never initiate it. Use the
shared `/api/v1` client for cookie/CSRF/error handling and generated DTOs.
Browser history may restore a metadata dialog but must never replay a write,
transfer or authentication action. Do not persist secret form values in URLs,
browser storage, dialog-history metadata, tests, or screenshots.

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

Version lives in **three places** — bump all of them together:
`ssh-server-manager/pyproject.toml`, `scripts/ssh_server_manager/__init__.py`,
and the footer in `website/index.html` (guarded by
`test_marketing_page_footer_version_tracks_the_package`).
`website/build.py` derives its docs-footer version from the package — never
hand-edit it there. Then:

1. Move `CHANGELOG.md` `## Unreleased` into a dated `## X.Y.Z` section.
2. Run the full test suite; build with
   `.venv/bin/python -m build --outdir dist .` and smoke-test the wheel in a
   clean venv (`serverctl --version`).
3. Render the packaging files from that sdist:
   `scripts/prepare_packaging dist/ssh_server_manager-X.Y.Z.tar.gz` (see
   `docs/packaging.md`; never hand-edit the formula's hash).
4. Commit, push, tag `vX.Y.Z`, and `gh release create vX.Y.Z` with the wheel
   and sdist attached (`dist/` is not tracked by git).
5. Publishing the GitHub Release triggers `publish.yml`, which uploads to
   PyPI via Trusted Publishing (OIDC, environment `pypi`, `skip-existing`
   enabled). No local credentials are involved; twine is a manual fallback.
