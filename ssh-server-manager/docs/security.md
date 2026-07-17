# Security model

This page describes what SSH Server Manager protects, what it deliberately
does not, and the invariants the code enforces. The developer-facing rules
live in [references/security.md](../references/security.md).

## Trust boundary

The tool assumes **your OS user account is trusted**. Anything running as
your user is inside the boundary — the same assumption OpenSSH itself makes
about `~/.ssh`. The UI's re-authentication layer is an *additional
disclosure gate* for humans standing at your screen, not a sandbox against
malicious local code.

What it defends against:

- Secrets at rest on disk, in dotfiles, in backups, or in a synced repo
- Secrets leaking through argv, environment variables, logs, shell history,
  or an AI agent's context window
- Other machines on your network reaching the management UI
- Cross-origin browser attacks against the local API (CSRF, DNS rebinding)
- Shoulder-surfing and casual disclosure of stored secrets
- MITM via silently accepted host keys

What it does not defend against:

- Malware running as your user
- An attacker with your unlocked OS session
- A compromised remote server

## Where secrets live

| Secret | Storage |
|---|---|
| Passwords, key passphrases | macOS Keychain / Windows Credential Locker / Linux Secret Service, service name `ssh-server-manager` |
| Private keys | Never stored, never read — only the absolute path |
| Master password | Argon2id hash in the local SQLite database |
| Passkey (WebAuthn) | Your platform authenticator (Touch ID, Windows Hello, security key) |
| Database, rendered config | User-only file permissions; contain **no** secret values |

The remote Files panel is deliberately read-only. It invokes the OpenSSH SFTP
subsystem through the same rendered config and AskPass boundary as other
connections, rejects control characters in paths, quotes paths as literal SFTP
arguments, and returns directory metadata rather than file contents. Remote
write operations are not exposed by the web API.

Unsafe keyring backends (null, plaintext file, `keyrings.alt`) are rejected
as a hard error — the tool refuses to run rather than degrade to plaintext.

## How SSH authentication works without exposing secrets

`serverctl` never types your password. It sets `SSH_ASKPASS` to a small
helper and passes it only *credential identifiers* plus prompt-matching
metadata. When OpenSSH asks for a password or passphrase, the helper:

1. Classifies the prompt (it refuses to answer host-key confirmation or
   OTP prompts — those must go to a human);
2. Matches it to the right credential by username/hostname/key path;
3. Fetches the secret from the OS vault and writes it directly to the ssh
   process.

The secret exists only in the memory of the helper and ssh. It never
appears in command lines, environment variables, files, logs, or the
calling process's output.

## The web UI's layered defenses

1. **Loopback only** — binds `127.0.0.1`; Host and Origin headers are
   validated to stop DNS-rebinding.
2. **One-time launch token** — the URL `serverctl ui` opens carries a
   single-use token; it is exchanged for an `HttpOnly`, `SameSite=Strict`
   session cookie and cannot be replayed.
3. **CSRF token on every mutation**, checked with constant-time comparison.
4. **Strict CSP** (`default-src 'self'`, no frames, no external origins)
   and `no-store` on all API responses.
5. **Reveal gating** — displaying a stored secret requires WebAuthn user
   verification (or the enrolled Argon2id master password, rate-limited to
   5 attempts/minute). Grants are single-use, expire in 30 seconds, and the
   rendered secret is cleared from the DOM after 15 seconds.

Connecting or testing a server *uses* the vault secret without ever
returning it to the caller — reveal authentication protects display, not
normal operation.

## Host keys

`StrictHostKeyChecking` is never weakened. An unknown or changed host key
fails the connection with the `host-key-untrusted` error code; you resolve
it deliberately (e.g. verify the fingerprint out-of-band, connect once with
plain `ssh` to accept it, or clean `~/.ssh/known_hosts`). There is no flag
to bypass verification — by design.

## Reporting

If you find a vulnerability, open a private security advisory or contact
the maintainers directly rather than filing a public issue.
