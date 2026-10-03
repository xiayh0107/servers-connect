# Isolated Hosts UI smoke fixture

This developer-only launcher serves the actual shipped UI and production web
routes. It uses a temporary database, an empty `MemoryVault`, and reserved
`.invalid` hostnames / documentation-only IP addresses. No usable secrets are
seeded. All SSH runner methods fail closed, including check, diagnose, browse,
upload, and download. Import, skill discovery/registration, and credential-file
editing are also denied. No real host, user database, keychain, SSH config, or
skill directory is used.

## Launch

From `ssh-server-manager/` after `scripts/bootstrap`:

```sh
.venv/bin/python tests/manual/hosts_fixture.py --port 8765 --open-browser
```

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

## Modes

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

## Host list / search / detail checklist

1. At desktop and narrow viewport widths, scan all eight hosts and verify status,
   alias/address, username, port, tags and credential summary are understandable
   (the inventory scrolls horizontally on narrow screens). With visible system
   scrollbars, confirm the detail drawer keeps both edges inside the viewport
   and the tag filter still displays its selected value.
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

## Automated checks

```sh
.venv/bin/python -m pytest tests/test_hosts_fixture.py tests/test_webapp_security.py
.venv/bin/python -m pytest tests/
node tests/test_host_details_node.mjs
npm ci --prefix tests/ui
npm test --prefix tests/ui
```

The fixture regressions cover isolation/cleanup, all four data modes, denied
remote/import/file operations, mode-600 one-time URL handling, actual loopback
binding arguments, and no launch token in terminal output. Security regressions
cover unauthenticated host reads, one-use launch, cookie/header safeguards,
foreign Host/Origin rejection, and CSRF checks on host mutations and actions.
The security tests replace the runner with a denial sentinel, so a broken gate
still cannot cause a network operation.
