# FAQ & troubleshooting

## Install & environment

**`serverctl doctor` says the vault check failed (Linux).**
You need an unlocked Secret Service provider — see
[platforms.md](platforms.md#linux). On a headless server either run an
unlocked `gnome-keyring-daemon` or stick to `agent`-kind credentials.

**`doctor` can't find `ssh` (Windows).**
Install the *OpenSSH Client* optional feature
(`Settings → Apps → Optional features`) and reopen the terminal.

**`fastapi`/`uvicorn` marked missing.**
Run `./scripts/bootstrap` (source checkout) or reinstall with
pipx/uv — the UI dependencies are part of the standard install.

## Connections

**`server test` returns `host-key-untrusted`.**
The host key is unknown or changed. Verify the fingerprint out-of-band,
accept it once with plain `ssh`, or clean the stale entry from
`~/.ssh/known_hosts`. The manager never bypasses verification for you.

**`authentication-failed` but the password is right.**
Check the username on the profile (`server show ALIAS`); confirm the server
allows password auth (`PasswordAuthentication yes` on the server side); if
the account uses 2FA/OTP the automated password flow will stop at the OTP
prompt by design — connect interactively with `serverctl connect`.

**`exec` says "No such file or directory" for a command that exists.**
You passed a compound command as one string without `--shell`. Either
`serverctl exec a -- ls -la /tmp` (separate args) or
`serverctl exec a --shell -- 'ls -la /tmp | head'` (one string).

**`--reuse` doesn't speed anything up on Windows.**
ControlMaster needs Unix sockets; Windows OpenSSH doesn't support it. The
warning is expected — auth is still non-interactive, only slower.

**Interactive `connect` asks for the password even though it's stored.**
That usually means the AskPass helper couldn't classify the prompt (unusual
server banner) or the vault is locked. Run `server test ALIAS --json` and
check `error_code`; run `doctor` to confirm the vault backend.

## Web UI

**The URL says "use the one-time URL opened by serverctl ui".**
The launch token was already used or the server restarted. Stop and rerun
`serverctl ui` for a fresh URL.

**Port already in use.**
`serverctl ui --port 0` (automatic selection) or pick another port.

**Passkey registration fails.**
Use a browser with WebAuthn platform-authenticator support (Safari, Chrome,
Edge) on `localhost`, not an IP address. Enroll the master password as a
fallback factor.

## Data & migration

**Where is everything stored?**
`serverctl doctor` prints the paths — database in the platform data dir,
rendered config at `~/.ssh/ssh-server-manager.conf`, secrets in the OS
vault under service `ssh-server-manager`.

**How do I move to a new machine?**
Copy the database (it holds no secrets), re-add secrets on the new machine,
or simply re-import from `~/.ssh/config` and re-create credentials.
A git-friendly export command is on the roadmap.

**Does it ever modify my `~/.ssh/config`?**
No. It renders a separate file that `Include`s your config last. Never
Include the rendered file back from your own config — that's a cycle and
rendering will refuse.
