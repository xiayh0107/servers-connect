# Roadmap

Priorities informed by a July 2026 survey of the SSH-manager landscape
(Termius, XPipe, Tabby, sshs/wishlist, CtrlOps, Termix and the MCP-SSH
ecosystem). Ordered by expected impact.

1. ~~**Published packages** — PyPI release~~ **Done (0.2.0):**
   `pipx install ssh-server-manager` is live on PyPI, and the
   Trusted-Publishing workflow (`.github/workflows/publish.yml`) automates
   future releases. Still open: a Homebrew tap and winget manifest.
2. ~~**Host-bound Agent Skills**~~ **Done (0.6.0):** the local registry
   attaches one Skill to one or many saved hosts and resolves only the guidance
   in scope for the target hosts. The first version is deliberately local and
   explicit: it discovers installed Skills but never downloads or installs
   them.
3. **Remote file workspace and transfer helpers** — the local UI now has a
   read-only SFTP directory browser with Agent-friendly path references.
   Still open: `serverctl cp ALIAS:remote local` / `serverctl get` plus
   explicit upload and download actions using the same vault auth.
4. **Port forwarding profiles** — saved `-L/-R/-D` tunnels per server
   (`serverctl tunnel open db1-local`), with status and teardown.
5. **Audit log** — local, append-only record of secret reveals and
   agent-invoked `exec` commands; `serverctl audit list --json`.
6. **Git-friendly export** — `serverctl export` of host metadata (never
   secrets) for versioning and moving between machines; paired `import`.
7. **Optional MCP wrapper** — a thin MCP server over the same core for
   agents that only speak MCP: read-only by default, command allowlists,
   no secret-bearing tool results. The Skill remains the recommended path.
8. **Session logging for `connect`** — opt-in local transcript with
   secret redaction.
9. **Mosh / jump-host UX niceties** — detect mosh availability; render
   `ProxyJump` chains in the UI graphically.

Non-goals: cloud sync of secrets, telemetry, subscriptions, Electron.
