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
- A malicious or compromised local skill file chosen by the same OS user

## Where secrets live

| Secret | Storage |
|---|---|
| Passwords, key passphrases | macOS Keychain / Windows Credential Locker / Linux Secret Service, service name `ssh-server-manager` |
| Private keys | Never stored, never read — only the absolute path |
| Master password | Argon2id hash in the local SQLite database |
| Passkey (WebAuthn) | Your platform authenticator (Touch ID, Windows Hello, security key) |
| Database, rendered config | User-only file permissions; contain **no** secret values |

The remote Files panel browses read-only and transfers only when you ask it to.
Browsing invokes the OpenSSH SFTP subsystem through the same rendered config and
AskPass boundary as other connections, rejects control characters in paths,
quotes paths as literal SFTP arguments, and returns directory metadata rather
than file contents.

Downloading and uploading are separate, explicit actions with their own rules:

- Both are `POST` routes that require the browser session **and** a matching
  CSRF token. Download is deliberately not a `GET`: a link cannot carry the CSRF
  header, and a request that moves bytes off one of your hosts is exactly what a
  hostile page would try to forge.
- Both are capped at 100 MB. Larger files belong on `serverctl get` / `put`,
  which stream straight through sftp without staging.
- Bytes are staged in a per-transfer temporary directory inside the private
  runtime directory (`0700`, owner-checked) and removed when the request ends,
  whether it succeeded or failed.
- Nothing about credential handling changes. Transfers reuse the same rendered
  config, host-key policy, ProxyJump chain, and AskPass boundary as every other
  connection, so a transfer cannot see a secret that a listing could not.

Unsafe keyring backends (null, plaintext file, `keyrings.alt`) are rejected
as a hard error — the tool refuses to run rather than degrade to plaintext.

## How SSH authentication works without exposing secrets

`serverctl` never types your password. It sets `SSH_ASKPASS` to a small
helper and passes it only *credential identifiers* plus prompt-matching
metadata. When OpenSSH asks for a password or passphrase, the helper:

1. Classifies the prompt (it refuses non-interactive host-key confirmation
   and OTP prompts; interactive first-use confirmation goes to the human's
   controlling terminal);
2. Matches it to the right credential by username/hostname/key path;
3. Fetches the secret from the OS vault and writes it directly to the ssh
   process.

The secret exists only in the memory of the helper and ssh. It never
appears in command lines, environment variables, files, logs, or the
calling process's output.

### Changing a password

Saving a new password and changing the password on a server are different
operations, and the tool keeps them apart. `credential edit --replace-secret`
(and the Web UI's **Replace**) changes only the copy saved in the vault.
`credential rotate ALIAS` is for a host whose login password is changing, and it
never runs anything on the server — the human changes the password with the
account's own tools, because a scripted `passwd` dialogue cannot be made
reliable across systems.

What `rotate` guarantees:

- The new password is read at a hidden prompt and held in that process only.
  It is checked through the same isolated credential probe as **Test
  credential**: password authentication only, no agent, no key files, no
  `ControlMaster` reuse, strict host-key checking, and no remote session. The
  probe serves the candidate to the target hop alone, under an identity no
  saved credential has, so a jump host that shares the credential is still
  offered the saved password.
- Nothing is written until that login succeeds. A rejection saves nothing and
  is followed by one check of the saved password, so the result can say whether
  the server still has the old one. Interrupting at any point before success
  leaves the vault as it was; running `rotate` again with the password the
  server now has is the recovery.
- A credential that other hosts also use is never overwritten: they keep the
  old password, and the rotated host is bound to a new credential.
- Each check is appended to the audit log as `password_rotate` with the host,
  the credential label and the outcome — never a value.

## Host-bound skill routing

Host-skill bindings are local routing metadata, not a capability or policy
grant. The user or agent still selects the target hosts and operations through
the normal workflow; binding a skill cannot authorize a command, install
software, execute a hook, or weaken credential and host-key rules.

`skill discover` scans only local agent skill roots and `SSM_SKILLS_DIRS`; it
makes no network request and changes no state. Registration stores a normalized
absolute `SKILL.md` path plus its name and description. The database and
`skill resolve` do not copy or return the instruction body. Removing a
registration is blocked until every host has been detached and never deletes
the source files.

Names are case-insensitively unique. Same-name candidates at different paths
are reported as a conflict rather than selected by search order. At resolution
time, missing, invalid, or renamed files make the operation fail; agents must
not substitute another file or reuse context from the previously selected
host. Multi-host results include `applies_to` so one host's procedures are not
implicitly applied to another.

This mechanism is not code signing, content pinning, or a sandbox. A local
skill file is trusted to the same extent as other code and configuration owned
by the OS user, and it may change after registration. Review skills before
registering them and protect their directories with normal filesystem
permissions.

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

`StrictHostKeyChecking` is never weakened. Automated checks and transfers
reject unknown or changed keys with `host-key-untrusted`. Under OpenSSH's
normal `ask` policy, interactive `serverctl connect ALIAS` shows a first-use
fingerprint in the human's controlling terminal even when AskPass supplies
a saved password or passphrase. Verify it out-of-band before explicitly
confirming; missing terminal input fails closed. Stricter configured policies
still apply. Investigate changed or revoked keys with the server administrator
before modifying trust records; the manager never automatically removes or
replaces them. There is no flag to bypass verification.

## Reporting

If you find a vulnerability, open a private security advisory or contact
the maintainers directly rather than filing a public issue.
