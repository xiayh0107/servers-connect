# Frontend development

The shipped UI is built from `src/` with React, TypeScript and Vite. The Python
package serves the generated assets in `../scripts/ssh_server_manager/assets/ui/`;
installed users need Python and the system SSH client, not Node.js.

Use Node.js 22.12 or newer. From `ssh-server-manager/`:

```sh
npm ci --prefix frontend
npm run check-types --prefix frontend
npm test --prefix frontend
npm run build --prefix frontend
npm run check-build --prefix frontend -- --allow-uncommitted
.venv/bin/python -m pytest tests/test_ui_assets.py tests/test_webapp.py tests/test_webapp_security.py
```

Edit the source files, not generated assets or `src/generated.ts`. Commit the
source, `package-lock.json`, generated types and packaged asset output together.
`check-build` builds in a temporary directory and compares the exact asset names
and bytes with the packaged output. It validates the manifest, rejects source
maps and enforces gzip budgets of 150 KiB for initial assets and 250 KiB overall.
The default check also requires packaged assets to be committed; use
`--allow-uncommitted` while reviewing a local rebuild, then run without it for a
clean release check. `../scripts/build-ui` is a compatibility launcher for this
build workflow; the old beautify/minify commands are removed.

## API contract and generated types

All page requests use the shared `/api/v1` client, which handles the session,
CSRF header and structured errors. The saved OpenAPI artifact is generated
offline against disposable state. When changing DTOs or routes, use the pinned
Python schema toolchain before exporting:

```sh
.venv/bin/python -m pip install -e '.[test]' -c contracts/constraints.txt
.venv/bin/python scripts/export-openapi.py --output contracts/api-v1.openapi.json
npm run typegen --prefix frontend
npm run check-types --prefix frontend
```

Review both the contract and type diffs. `check-types` regenerates into a
temporary location and checks drift. Pages should read runtime capabilities and
handle nullable metadata explicitly. Opening inventories or details reads local
saved metadata; SSH, DNS, filesystem inspection and vault work require explicit
actions, except the deliberate 260ms host-name hover: its preview may observe
missing/stale facts using one in-flight request, 60-second freshness and a
30-second failure cooldown. Missing fields use skeletons; saved values remain
during refresh. Keyboard focus and Hide details never initiate collection.
Privacy mode masks values in the existing lanes, never removes those columns,
and disables copying. Browser-history restoration may reopen a dialog, but must not repeat
its mutation. Keep secrets out of URL fragments, history and persistent browser
storage.

## Browser acceptance

Install the Python test extra with `../scripts/bootstrap` (from this directory)
and Chromium before running browser tests. On Windows, use
`..\scripts\bootstrap.cmd` and `.venv\Scripts\python.exe` for Python commands.
From `ssh-server-manager/`:

```sh
npm exec --prefix frontend playwright -- install chromium
npm run e2e --prefix frontend -- --project=smoke
npm run e2e --prefix frontend -- --project=ssh
```

The smoke project uses synthetic API responses and safe fixture data to cover
the six views, empty/error states, navigation, dialogs and desktop/mobile
screenshots. Its Vite server is started automatically; it is not a proxy to a
developer's running UI. For manual testing of the actual API, rebuild the assets
and use the disposable fixtures described in
[`../tests/manual/README.md`](../tests/manual/README.md).

The SSH project starts a real loopback SSH/SFTP fixture using Paramiko from the
Python test extra and the system OpenSSH client. It uses temporary databases,
keys, trusted host keys and a `MemoryVault`; it exercises the production
launch-token/session flow. It never uses saved user hosts or the OS vault. Set
`SSM_E2E_PYTHON` to an isolated test interpreter if it is not the product's
`.venv`. The project disables screenshots, videos and traces because the normal
login and authentication flows contain ephemeral secrets. Do not enable those
artifacts or print private launch files, CSRF values, passwords or passphrases.

Use the design tokens in `src/styles.css`; keep teal as the default accent.
Automatic GeoIP and a native desktop file dialog are outside the Web UI scope.
