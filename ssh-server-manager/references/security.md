# Security invariants

- Store only vault account identifiers in SQLite. Store password and passphrase values in the selected OS credential backend.
- Reject null, fail, plaintext, and file-based keyring backends.
- Pass only credential identifiers and prompt-matching metadata to AskPass. Do not pass secrets in arguments, environment variables, files, or logs.
- Bind the UI to loopback only. Validate the Host and Origin headers, exchange a one-time launch token for an HttpOnly SameSite session, and require a CSRF header on mutations.
- Select a free loopback port by default. Never print a launch token; when manual launch is required, write it only to an explicitly selected mode-600 file.
- Require WebAuthn user verification before revealing a secret. When WebAuthn is unavailable, require the locally enrolled Argon2id master password.
- Make reveal grants single-use and short-lived. Mark reveal responses `Cache-Control: no-store` and clear rendered secrets from the DOM after 15 seconds.
- Allow connections and tests to consume a vault secret without returning it to the caller. UI reveal authentication protects plaintext display, not ordinary SSH use.
- Require normal OpenSSH known-host validation. Never silently accept or discard host keys.
- Redact password-like prompts, vault values, launch tokens, session cookies, and CSRF tokens from diagnostics.
- Treat same-user local code as inside the OS account trust boundary. The UI authentication layer is an additional disclosure gate, not a replacement for OS account security.
