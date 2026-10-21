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

## Decisions (owner, 2026-10-21)

Recorded so these stay decisions with a date and an owner, rather than an
inherited default. Earlier revisions of this file listed the first two as "not
planned" on the maintainer's engineering judgement; the owner has since decided
otherwise and they are now in scope.

- **Web UI for ports and audit — done (unreleased).** Read-only first: both
  views are GET-only, so a browser cannot start a trace or rewrite history. The
  budget concern that motivated the earlier "not planned" note did not hold: the
  pages are lazy chunks and cost 2.2 and 2.6 KiB gzip.
- **MCP over HTTP — in scope.** The stdio server stays the default; an HTTP
  listener is added for agents that cannot spawn a process, and it must stay
  loopback-only with a bearer token and the same allowlist policy as stdio.
- **winget — not planned.** winget installs a Windows installer, not a Python
  package; a manifest would promise an install path nothing in this repo builds.
  The supported Windows install is `install.ps1` or `pipx`.
- **A bundled desktop shell — not planned.** The six-page UI already runs on
  loopback and any browser can wrap it; a second toolchain would exist only to
  reach the same page. (Electron remains a non-goal.)

Non-goals: cloud sync of secrets, telemetry, subscriptions, Electron.