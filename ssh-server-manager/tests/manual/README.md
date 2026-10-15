# Disposable Web UI fixtures

Build the React frontend before launching either Python fixture. Neither fixture
uses the developer's host database, SSH configuration or OS credential vault.
The automated browser suite is described below; its real SSH project disables
screenshots, traces and videos because the standard login flow contains secrets.

## Hosts inventory and denial fixture

This developer-only launcher serves the actual shipped UI and production web
routes. It uses a temporary database, an empty `MemoryVault`, and reserved
`.invalid` hostnames / documentation-only IP addresses. No usable secrets are
seeded. All SSH runner methods fail closed, including check, diagnose, browse,
upload, and download. Import, skill discovery/registration, and credential-file
editing are also denied. No real host, user database, keychain, SSH config, or
skill directory is used.

### Launch

From `ssh-server-manager/` after `scripts/bootstrap` (or `scripts/bootstrap.cmd`
on Windows) and `npm ci --prefix frontend`:

```sh
npm run build --prefix frontend
.venv/bin/python tests/manual/hosts_fixture.py --port 8765 --open-browser
```

Use `.venv\Scripts\python.exe` instead of `.venv/bin/python` on Windows.

The browser receives the normal one-time launch URL. The terminal prints only
the mode and private URL file's path, never the token. For browser tooling, omit
`--open-browser` and optionally choose an explicit `--url-file` path; consume the
file privately without echoing its contents or saving it in logs, screenshots,
or test artifacts. The standard launcher writes mode 600, removes the file after
session creation, binds to `127.0.0.1`, and disables access logging. There is no
test-only auto-login or session-bypass route. Navigate using `localhost`, since
production Origin validation expects that origin.

Use `--port 0` (the default) to choose an available loopback port. The private URL
file contains the selected port; the normal runtime-state file is inside the
announced temporary directory's `runtime/` folder. Stop with Ctrl-C; the entire
temporary directory is removed. Restart to reset data or get another one-time
launch URL. Choose a distinct unused URL-file path for each concurrent instance.

### Modes

- `--mode demo`: eight hosts; production/staging/development/legacy contexts;
  successful, failed, and never-run checks; agent/password/no credential;
  proxy jump; non-default ports; saved directory; long, multiline and Unicode
  notes; literal HTML-like text that must render as text
- `--mode empty`: no hosts, credentials, or contexts
- `--mode bootstrap-error`: initial page loads and login succeeds, but every
  bootstrap data request fails with an explicit synthetic error
- `--mode bootstrap-retry`: first bootstrap request fails; next one succeeds,
  for testing Retry without changing the server or dropping the session

The last two modes fail `database.list_servers()` after seeding and should be
used only for bootstrap/error/retry checks. They do not simulate remote health.

### Host list / search / detail checklist

1. At desktop and narrow viewport widths, scan all eight hosts and verify status,
   alias/address, username, port, tags and credential summary are understandable.
   With visible system scrollbars, confirm dialogs stay inside the viewport and
   filters remain readable; wide tables may scroll horizontally.
2. Search by alias, address, username and context where supported; try mixed
   case, whitespace, a long query, and a query with no matches; clear search
3. Filter production, staging and the empty `archived-empty` context; verify
   counts, empty-state wording, and returning to the complete host list
4. Open `api-prod-eu-01`: check proxy jump, credential label and full notes; open
   `worker-staging-long-context-name-01`: check long notes, wrapping, Unicode,
   text escaping and no horizontal overflow
5. Open details by keyboard and move focus between rows and controls; verify
   active/selected state, Escape/back behavior, and no invisible trapped focus
6. Switch hosts repeatedly; verify the selected host's information is current
   and one host's note/status never carries into another host's details
7. Run empty/error/retry modes; verify they remain distinct from zero search
   results and no failed bootstrap presents a misleading empty inventory
8. Try Check or remote tools only to verify the fixture's explicit blocked
   error. Those actions can never validate real connection behavior here
9. Reload the same authenticated tab; verify the session survives. A fresh
   cookie-less tab cannot reuse the consumed token or fetch bootstrap data

## Real loopback SSH/SFTP fixture

`modern_fixture.py` serves the shipped frontend and production API against
temporary, real SSH/SFTP listeners implemented with Paramiko. The Python test
extra supplies Paramiko; the machine must also have the system OpenSSH client.
It generates disposable client/server keys and a trusted `known_hosts` file,
keeps strict host-key checking enabled, and uses a `MemoryVault`. Connections and
forwarding are restricted to fixture loopback ports. There is no general remote
shell or PTY: only the product's fixed health and metadata probes are accepted.

The fixture seeds direct, two-managed-hop and external-jump routes, a rejected
key, a metadata fallback host, a directory with 5,005 entries, empty and Unicode
paths, an existing upload target, and a file above the Web transfer limit. It
supports SFTP's atomic no-replacement and explicit overwrite operations. Local
imports, Skill discovery/registration and credential-file edits are denied.
Generated fixture keys may be used for independent credential testing.

The Playwright SSH project starts and stops this fixture automatically. For
manual browser testing, choose a new private directory, then run:

```sh
.venv/bin/python tests/manual/modern_fixture.py \
  --url-file PATH_TO_PRIVATE_DIR/launch-url \
  --ready-file PATH_TO_PRIVATE_DIR/ready.json
```

Consume the launch file privately in browser tooling; do not echo its contents,
copy it into a screenshot or save it in a trace. The normal one-use launch flow
creates the session and removes the launch file. `ready.json` contains only the
base URL, host IDs and transfer limit. Stop with Ctrl-C; temporary SSH listeners,
keys, data and runtime state are cleaned up. `--stdin-stop` is available for
automation, including Windows, and stops on `stop` or EOF.

## Automated checks

```sh
.venv/bin/python -m pytest tests/test_hosts_fixture.py tests/test_webapp_security.py
.venv/bin/python -m pytest tests/
npm ci --prefix frontend
npm run check-types --prefix frontend
npm test --prefix frontend
npm run build --prefix frontend
npm run check-build --prefix frontend -- --allow-uncommitted
npm exec --prefix frontend playwright -- install chromium
npm run e2e --prefix frontend -- --project=smoke
npm run e2e --prefix frontend -- --project=ssh
```

The frontend source and lockfile are in `frontend/`; old sidecar Node tests and
the former `tests/ui` package have been replaced. The smoke project uses safe
synthetic API data for six-page interaction and desktop/mobile screenshot
coverage. The SSH project exercises real direct/multi-hop/external routing,
diagnosis and authentication failure, independent credential verification,
routed SFTP browse/download/upload conflicts, explicit overwrite, large-directory
filtering, empty folders, nullable metadata and the transfer cap. Its interpreter
defaults to the product `.venv`; override it with `SSM_E2E_PYTHON` if needed.
See [`../../frontend/README.md`](../../frontend/README.md) for build/contract checks.

The fixture regressions cover isolation/cleanup, all four data modes, denied
remote/import/file operations, mode-600 one-time URL handling, actual loopback
binding arguments, and no launch token in terminal output. Security regressions
cover unauthenticated host reads, one-use launch, cookie/header safeguards,
foreign Host/Origin rejection, and CSRF checks on host mutations and actions.
The security tests replace the runner with a denial sentinel, so a broken gate
still cannot cause a network operation.
