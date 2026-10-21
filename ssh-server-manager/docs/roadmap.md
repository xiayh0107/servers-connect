# Roadmap

Priorities informed by a July 2026 survey of the SSH-manager landscape
(Termius, XPipe, Tabby, sshs/wishlist, CtrlOps, Termix and the MCP-SSH
ecosystem). Ordered by expected impact.

1. ~~**Published packages** — PyPI release~~ **Done (0.2.0):**
   `pipx install ssh-server-manager` is live on PyPI, and the
   Trusted-Publishing workflow (`.github/workflows/publish.yml`) automates
   future releases. A Homebrew tap is now prepared rather than open: the formula
   template and `scripts/prepare_packaging` render it from the real sdist
   (`docs/packaging.md`), and publishing it is a copy into a tap the maintainer
   hosts. **winget is not planned**: it installs a Windows installer, not a
   Python package, so a manifest would promise an install path nothing in this
   repo builds.
2. ~~**Host-bound Agent Skills**~~ **Done (0.6.0):** the local registry
   attaches one Skill to one or many saved hosts and resolves only the guidance
   in scope for the target hosts. The first version is deliberately local and
   explicit: it discovers installed Skills but never downloads or installs
   them.
3. ~~**Remote file workspace and transfer helpers**~~ **Done (0.7.0):**
   `serverctl cp` / `get` / `put` move single files over the same SFTP path and
   vault credentials, the web browser gained explicit download and upload
   actions, and per-host saved working directories (`serverctl path`) let
   people and agents recall where the work happens. Also shipped:
   `serverctl mount` for sshfs, gated behind a `doctor` capability check
   because the FUSE layer cannot be installed for the user. Recursive transfer
   of a whole tree landed later as `--recursive` (unreleased).
4. ~~**Port forwarding profiles**~~ **Done (unreleased):** saved `-L/-R/-D`
   tunnels per server (`serverctl tunnel add` / `open` / `close` / `status`),
   opened as detached background ssh processes with status and teardown.
   Runtime state lives in the private runtime directory, not the database.
5. ~~**Audit log**~~ **Done (unreleased):** `serverctl audit list [--json]`
   appends one JSON object per event to a mode-600 `audit.log` under the private
   data directory: `exec` and `exec_result`, `reveal` and `reveal_denied`,
   `connect`, `tunnel_open` and `tunnel_close`. Credentials in a recorded command
   line are masked and a reveal is recorded by label, never by value. The log
   rotates itself (5 MiB, three segments, and reads span every segment).
6. ~~**Git-friendly export**~~ **Done (unreleased):** `serverctl export` writes
   a deterministic JSON document of hosts, saved directories and forwarding
   profiles (never secrets or credential ids; a host carries its credential
   *label*), and `serverctl import` previews by default, applies with `--apply`,
   and merges rather than replaces.
7. ~~**Optional MCP wrapper**~~ **Done (unreleased):** `serverctl mcp serve`
   speaks MCP over stdio with read-only inventory tools by default; `test_host`
   needs `--allow-remote` and `run_command` needs `--allow-exec` plus one
   `--allow-command` glob per shape. No reveal tool, argv-only commands, and
   acting calls are audited. The Skill remains the recommended path.
8. ~~**Session logging for `connect`**~~ **Done (unreleased):**
   `serverctl connect ALIAS --log` records a mode-600 transcript through a pty
   proxy, stripping control sequences and masking credential-shaped values. Only
   the display stream is recorded, so a password typed at a no-echo prompt never
   reaches the file. POSIX only.
9. ~~**Mosh support**~~ **Done (unreleased):** `serverctl doctor` reports a
   `mosh` capability row and `serverctl connect ALIAS --mosh` runs a session that
   bootstraps over the managed config with host-key verification intact. The
   0.9.0 Hosts details already render managed and external `ProxyJump` hops from
   the backend route.
10. ~~**Local GeoIP**~~ **Done (unreleased):** `serverctl geo resolve` fills a
    host's location from a local range file (`SSM_GEOIP_CSV`) or a local
    `.mmdb` when the optional `geoip2` package is installed — no web service, and
    never a lookup on a read path. A manual override is kept unless
    `--overwrite`. **Desktop shell: not planned.** The six-page Web UI already
    runs on loopback and can be wrapped by any browser or launcher, so a bundled
    Electron/Tauri shell would add a second toolchain and a second update path to
    reach the same page. That stays a non-goal, recorded here so the item is
    closed rather than forever pending.

## Deliberately not planned

Recorded so these stay decisions rather than indefinitely pending work.

- **MCP over HTTP for remote agents.** The MCP wrapper speaks stdio because the
  inventory, the vault and the audit log all belong to one OS account on one
  machine. An HTTP listener reachable by another machine is the opposite of that
  boundary, and the loopback Web UI already covers same-machine use.
- **Tunnel and audit surfaces in the Web UI.** Both have complete CLI surfaces
  (`serverctl tunnel`, `serverctl audit`), which is what agents and scripts use.
  Adding them to the six-page UI means new pages inside the 150/250 KiB gzip
  budgets for information a person rarely needs interactively — a tunnel is
  opened and a log is read far more often from a shell. Revisit if real use shows
  people opening tunnels from the browser.
- **winget manifest and a bundled desktop shell.** Covered above: winget needs a
  Windows installer this repo does not build, and a desktop shell would add a
  second toolchain to reach a page a browser already serves.

Non-goals: cloud sync of secrets, telemetry, subscriptions, Electron.
