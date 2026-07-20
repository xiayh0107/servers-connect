# Security invariants

- Store only vault account identifiers in SQLite. Store password and passphrase values in the selected OS credential backend.
- Reject null, fail, plaintext, and file-based keyring backends.
- Pass only credential identifiers and prompt-matching metadata to AskPass. Do not pass secrets in arguments, environment variables, files, or logs.
- Bind the UI to loopback only. Validate the Host and Origin headers, exchange a one-time launch token for an HttpOnly SameSite session, and require a CSRF header on mutations.
- Select a free loopback port by default. Never print a launch token; when manual launch is required, write it only to an explicitly selected mode-600 file.
- Require WebAuthn user verification before revealing a secret. When WebAuthn is unavailable, require the locally enrolled Argon2id master password.
- Make reveal grants single-use and short-lived. Mark reveal responses `Cache-Control: no-store` and clear rendered secrets from the DOM after 15 seconds.
- Allow connections and tests to consume a vault secret without returning it to the caller. UI reveal authentication protects plaintext display, not ordinary SSH use.
- Keep the web file browser read-only. Use OpenSSH SFTP with literal quoted paths, reject path control characters, and return metadata rather than file contents.
- Treat host-skill bindings as routing metadata only. They do not authorize
  remote actions, install software, execute hooks, or expand the target hosts
  selected by the user.
- Keep skill discovery offline and read-only. Store only the normalized local
  `SKILL.md` path plus frontmatter metadata; do not copy or return the skill body
  from registration or resolution APIs.
- Fail closed on ambiguous or stale skill identities. Names are
  case-insensitively unique; same-name paths conflict, and missing, invalid, or
  renamed files must never be silently replaced with another candidate.
- Require normal OpenSSH known-host validation. Never silently accept or discard host keys.
- Redact password-like prompts, vault values, launch tokens, session cookies, and CSRF tokens from diagnostics.
- Treat same-user local code as inside the OS account trust boundary. The UI authentication layer is an additional disclosure gate, not a replacement for OS account security.
- Host-skill registration is not content pinning, signing, or sandboxing. Skill
  files remain same-user local code; agents must re-resolve when the target
  changes and apply each skill only to its returned `applies_to` aliases.
