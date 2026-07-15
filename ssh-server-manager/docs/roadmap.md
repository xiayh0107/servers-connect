# Roadmap

Priorities informed by a July 2026 survey of the SSH-manager landscape
(Termius, XPipe, Tabby, sshs/wishlist, CtrlOps, Termix and the MCP-SSH
ecosystem). Ordered by expected impact.

1. **Published packages** — PyPI release so `pipx install ssh-server-manager`
   works without a checkout; then a Homebrew tap and winget manifest.
   Status: dist passes `twine check`, a Trusted-Publishing workflow
   (`.github/workflows/publish.yml`) triggers on GitHub Release — the only
   remaining step is registering the pending publisher on pypi.org.
2. **File transfer helpers** — `serverctl cp ALIAS:remote local` /
   `serverctl get` wrapping scp/sftp with the same vault auth; the most
   cited feature gap versus every GUI competitor.
3. **Port forwarding profiles** — saved `-L/-R/-D` tunnels per server
   (`serverctl tunnel open db1-local`), with status and teardown.
4. **Audit log** — local, append-only record of secret reveals and
   agent-invoked `exec` commands; `serverctl audit list --json`.
5. **Git-friendly export** — `serverctl export` of host metadata (never
   secrets) for versioning and moving between machines; paired `import`.
6. **Optional MCP wrapper** — a thin MCP server over the same core for
   agents that only speak MCP: read-only by default, command allowlists,
   no secret-bearing tool results. The Skill remains the recommended path.
7. **Session logging for `connect`** — opt-in local transcript with
   secret redaction.
8. **Mosh / jump-host UX niceties** — detect mosh availability; render
   `ProxyJump` chains in the UI graphically.

Non-goals: cloud sync of secrets, telemetry, subscriptions, Electron.
