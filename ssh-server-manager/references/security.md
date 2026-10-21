# Security invariants

- Store only vault account identifiers in SQLite. Store password and passphrase values in the selected OS credential backend.
- Reject null, fail, plaintext, and file-based keyring backends.
- Pass only credential identifiers and prompt-matching metadata to AskPass. Do not pass secrets in arguments, environment variables, files, or logs.
- Bind the UI to loopback only. Validate the Host and Origin headers, exchange a one-time launch token for an HttpOnly SameSite session, and require a CSRF header on mutations.
- Select a free loopback port by default. Never print a launch token; when manual launch is required, write it only to an explicitly selected mode-600 file.
- Require WebAuthn user verification before revealing a secret. When WebAuthn is unavailable, require the locally enrolled Argon2id master password.
- Make credential-specific reveal grants single-use and valid for 30 seconds.
  Revoke a deleted credential's grants in every browser session. Mark reveal
  responses `Cache-Control: no-store` and clear rendered secrets after 15
  seconds, on close, or when the page is hidden. Never include reveal views or
  secret input in screenshots, traces, history state or browser storage.
- Allow connections and tests to consume a vault secret without returning it to the caller. UI reveal authentication protects plaintext display, not ordinary SSH use.
- Keep web file *browsing* read-only. Use OpenSSH SFTP with literal quoted paths, reject path control characters, and return metadata rather than file contents.
- Expose transfer as an explicit action, never a side effect of browsing. Download and upload are CSRF-protected POST routes, size-capped, and staged only inside the private runtime directory, which is cleared when the request ends.
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
- Only interactive CLI `connect` may delegate a first-use OpenSSH host-key confirmation to the human's controlling terminal. Require explicit terminal input; fail closed without that terminal. Do not enable this path for agent calls, Web checks, transfers, or independent credential probes, and preserve stricter configured SSH trust policies.
- Redact password-like prompts, vault values, launch tokens, session cookies, and CSRF tokens from diagnostics.
- Record session transcripts only when explicitly requested, to a mode-600 file, and record the display stream only: a password typed at a no-echo prompt is never echoed, so it stays out. Strip terminal control sequences and mask credential-shaped values as best effort, warn about the destination before the session starts, audit the start of a recording, and never write a transcript into a repository, an issue, or a chat reply without the user asking.
- Keep the audit log append-only and secret-free. Open it with `O_APPEND`, mode 600, and one small write per event; record reveals by credential label and slot, never by value; redact credential-shaped arguments before a command line is written; and treat a write failure as an advisory warning, not as an error that blocks the audited action.
- Treat same-user local code as inside the OS account trust boundary. The UI authentication layer is an additional disclosure gate, not a replacement for OS account security.
- Host-skill registration is not content pinning, signing, or sandboxing. Skill
  files remain same-user local code; agents must re-resolve when the target
  changes and apply each skill only to its returned `applies_to` aliases.
- Inspect/validate private keys only on explicit local actions. Load bounded
  OpenSSH/PEM bytes with cryptography in process memory; never send passphrases
  through command arguments, environment variables, temporary files or logs.
  Return public metadata only. Encrypted keys without a passphrase must have
  nullable public metadata rather than an invented fingerprint.
- Bind key revision tokens to the browser session, canonical path and exact
  file snapshot, with a short expiry. Verify the snapshot again before saving;
  a file change aborts database mutation and restores any previous vault value.
  Empty replacement input retains a secret; only explicit clear removes it.
  Preserve spaces exactly and reject ambiguous replacement-plus-clear input.
- Local key-browser scopes authorize a canonical root for one browser session.
  List path/name/type only; skip symlinks/junctions, reject outside paths and
  verify directory identities to prevent replacement races. Expiration of the
  scope or browser session removes scope/revision authorization. Do not expand
  traversal or enumerate another directory implicitly.
- Inspect public agent identities with the local agent listing command only,
  with a timeout and bounded results. Do not load keys into the agent or return
  private material. Unknown permission/vault evidence must stay unknown.
- Independent credential probes may consume secrets internally but must run
  no remote command, preserve host bindings/health records, and restrict auth
  to the selected credential kind. Agent probes must not fall back to default
  private keys. A `none` connection cannot prove credential use. Trust local
  authentication evidence, not a remote banner or unsolicited stdout message.
  Match AskPass prompts to the exact request host/user or key path; reject
  ambiguous prompts even when only one credential is available. Wait for the
  entire request process tree to exit before reading private authentication
  logs, and let the stderr reader own its pipe until EOF.
  On Windows, retain verified handles for the private job's current members,
  wait for their exit within a shared cleanup deadline, release those handles
  and confirm the job has no active processes. Uncertain membership or exit
  must fail the probe and still terminate its private job.
- Serialize credential mutations/tests/reveal/bindings/deletion across processes.
  Default deletion refuses references; explicit detach/delete validates a fresh
  reference snapshot and journals cleanup atomically with the DB deletion.
  Never journal secrets or delete key files. Persist and report failed cleanup
  as pending; retries must remain idempotent and cannot imply erasure until the
  vault/config work actually succeeds. Sanitize vault exceptions and probe
  stages rather than returning backend or SSH output containing secrets.
