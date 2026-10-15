# Development and release validation

Run these commands from `ssh-server-manager/`. Use the project `.venv`, not an
existing user installation. On Windows, replace `.venv/bin/python` with
`.venv\Scripts\python.exe`. Frontend development needs Node.js 22.12 or newer;
CI pins Node.js 22.22.3. Installed wheels and sdists serve the committed build
and do not require Node.js.

## Backend, contract and frontend checks

```sh
.venv/bin/python -m pytest tests/
.venv/bin/python scripts/export-openapi.py --check contracts/api-v1.openapi.json
npm ci --prefix frontend
npm run check-types --prefix frontend
npm run typecheck --prefix frontend
npm test --prefix frontend
npm run check-build --prefix frontend
```

The OpenAPI check detects a stale committed API contract. `check-types`
generates TypeScript types in a temporary directory and compares them with
`frontend/src/generated.ts`; it never updates the source as a side effect.
When changing a contract, update the OpenAPI file and generated types explicitly
before rerunning the checks.

`check-build` makes a fresh temporary production build and compares every output
with `scripts/ssh_server_manager/assets/ui/`. It checks the manifest graph,
local asset references, missing/extra files and the absence of source maps.
The initial HTML, entry JavaScript and static dependencies must fit within
**150 KiB gzip**; all shipped JavaScript/CSS and HTML must fit within
**250 KiB gzip**. Compression uses level 9, and deferred page chunks count
toward the total. `tests/test_ui_assets.py` independently validates the shipped
manifest and asset budgets.

After changing frontend source, build the committed assets explicitly:

```sh
npm run build --prefix frontend
npm run check-build --prefix frontend -- --allow-uncommitted
```

The local flag permits uncommitted assets while still requiring byte-for-byte
agreement with a fresh build. The normal check requires the assets to have no
Git changes; run it again after committing. Do not hand-edit hashed build files.

## Browser and real SSH/SFTP acceptance

Install the test extra and Chromium before running the browser projects:

```sh
.venv/bin/python -m pip install -e ".[test]" -c contracts/constraints.txt
npm exec --prefix frontend -- playwright install chromium
npm run e2e --prefix frontend -- --project=smoke
npm run e2e --prefix frontend -- --project=ssh
```

The projects start their own Vite server on `127.0.0.1:4173`. Keep that port free
and run them in one Playwright invocation or sequentially. `npm run e2e
--prefix frontend` runs both projects together. The SSH project also needs
OpenSSH `ssh` and `sftp` on PATH and Paramiko from the test extra. It defaults to
the project `.venv` interpreter; set `SSM_E2E_PYTHON` to an explicit Python
executable when using a different test environment.

The `smoke` project uses controlled `/api/v1` responses. It checks the six views,
read-only host details, literal remote-path encoding, file upload cancellation
and explicit overwrite, saved-directory CRUD, session/CSRF integration,
Back/Forward and rapid dialog reopening, empty workspaces, and desktop/narrow
Files layouts. Its screenshots are visual evidence of a local fixture only.

The `ssh` project runs `tests/manual/modern_fixture.py`: disposable Paramiko
SSH/SFTP servers and the actual Web backend, OpenSSH runner and SFTP client.
It uses temporary database/runtime/configuration paths, an in-memory test vault,
temporary keys and an explicit trusted known-hosts file. No user's database,
OS vault, SSH configuration or saved infrastructure is used. Forwarding is
restricted to the fixture's loopback ports; no interactive shell or PTY is
available. Fixed health/metadata probes are supported for the normal host checks.

| Acceptance area | Exercised behavior |
|---|---|
| Hosts and routes | Direct, two managed jumps, external jump alias, authentication failure, diagnosis, cached facts/fingerprint and batch checks |
| Credential test | A selected key authenticates against a host bound to a different key; the saved binding and host observations stay unchanged |
| Transfers | Routed SFTP metadata, download, conflict cancellation, explicit replacement, and publication of a new file |
| Large folders and fallbacks | 5,005 entries, filtering before the 5,000-entry cap, truncation/count hints, empty folders and nullable metadata |
| Limits | Disabled oversized downloads in the UI and a real backend size-limit refusal |

The backend integration tests independently check strict host-key verification,
credential separation across hops, password/encrypted-key authentication,
authentication without a remote session or command, cancellation/deadline cleanup,
exact AskPass identity matching, idle jump forwarding, process-tree exit and
stderr ownership, atomic publication and filesystem isolation. Rerun only those
integrations with:

```sh
.venv/bin/python -m pytest tests/test_modern_fixture.py tests/test_credential_probe.py
```

Windows CI additionally runs the unbound-jump credential probe ten times with
fresh isolated topologies. Any failed attempt stops the check; failed probes are
never retried to obtain a passing result. Failure evidence contains only fixed
stage flags, counts and authentication-method names.

The loopback topology proves the application/client flows with a controlled
server. It does not certify every remote SSH/SFTP implementation. In particular,
servers without the required no-replace hard-link publication operation must
return the explicit unsupported error covered by backend tests.

## Clean package installations

Build both distributions with the candidate version already set consistently:

```sh
.venv/bin/python -m pip install build
.venv/bin/python -m build --outdir dist .
.venv/bin/python scripts/packaging-smoke.py dist/ssh_server_manager-VERSION-py3-none-any.whl --version VERSION
.venv/bin/python scripts/packaging-smoke.py dist/ssh_server_manager-VERSION.tar.gz --version VERSION
```

Replace `VERSION` in filenames and arguments with the built package's version.
Remove obsolete distributions from the build output before selecting artifacts
for release or use an empty output directory.

`packaging-smoke.py` installs each distribution into a fresh virtual environment
outside the checkout. It confirms the installed package import, CLI version,
empty host listing, normal one-use Web launch, runtime contract, every manifest
asset, a CSRF-protected local tag write/delete, and Web status/stop cleanup.
This also verifies that a source checkout cannot mask omitted package data.
For the sdist, it also checks the public development inputs and rejects cached
dependencies or runtime state, then unpacks the source and collects its tests.
Only the two repository-level website test modules are excluded from that
collection; they still run in checkout CI, where the website sources exist.
All state is disposable, no SSH is requested, and ordinary metadata reads never
open the OS credential vault.

## Upgrade from the published 0.8.0 wheel

Run the migration smoke against the exact candidate wheel intended for release:

```sh
.venv/bin/python scripts/upgrade-smoke.py dist/ssh_server_manager-VERSION-py3-none-any.whl --version VERSION
```

This check needs package-index access. It downloads the actual published 0.8.0
wheel and installs it in a fresh virtual environment, then uses that installed
package to create schema 5 with:

- Four hosts, explicit IDs, saved credentials of all three kinds, managed and
  external jump references, successful/failed/unrun check history.
- Unicode tags and reusable empty/assigned contexts, multiline notes, used and
  unused saved directories, and one Skill bound to two hosts.
- An enrolled Argon2 master-password hash, a fixture WebAuthn user/credential
  record, and arbitrary application settings.

It force-reinstalls the candidate wheel selected for the 0.9.0 release. Worker
interpreters run in isolated mode and assert that the package comes from the
temporary virtual environment. Every original column, row, ID, association and
timestamp is compared exactly. The check also
requires schema 7, correct last-success migration, nullable observation/key
metadata defaults, initial credential revisions, empty new history tables,
working master-password verification, SQLite integrity, and unchanged data
after repeated startup.

Credentials are metadata fixtures: no real password/passphrase is put in a vault
and no usable private key or passkey is seeded. Vault construction and network
connection/process launches are denied inside the migration workers. This tests
preservation of credential references and authentication settings without
accessing the developer's OS keychain. Temporary environments and snapshots are
removed on completion or failure.

## CI scope and evidence

`.github/workflows/test.yml` defines the following checks. A local pass is not
evidence that an unrun CI job passed; confirm each required check on the exact
commit being released.

| Job | Environment | Checks |
|---|---|---|
| `cross-platform` | macOS, Ubuntu Linux, Windows; Python 3.10 and 3.13 | Backend tests, API/type freshness, TypeScript/component checks, reproducible UI build, wheel and sdist installation |
| Upgrade smoke | macOS, Ubuntu Linux, Windows within `cross-platform`; Python 3.13 | Published 0.8.0 schema 5 to candidate schema 7 |
| `browser-topology` | Ubuntu Linux; Python 3.13 | Six-view Chromium smoke and isolated real SSH/SFTP browser topology |

Browser tests use the normal one-use launch-file flow, with no authentication
bypass route. The real SSH project's trace, screenshot and video capture are
disabled because browser navigation initially contains the launch token. Its
fixture output and readiness metadata do not contain that token. Do not enable
request-body logging or attach private launch files, browser cookies, CSRF
values, credential input, reveal results or test database snapshots to reports.
Screenshots for documentation should use synthetic metadata and blank secret
fields. Never use a real user's workspace to create release screenshots.
