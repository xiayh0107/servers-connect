# Data model and rendering

## Connection profiles

A connection profile contains one case-insensitively unique alias, hostname, port, username, optional credential ID, ordered ProxyJump aliases, notes, source metadata, and the most recent test result. Create another alias when the same endpoint needs another account.

Reject whitespace, shell metacharacters, and OpenSSH wildcard characters in managed aliases. Reject invalid ports, self-jumps, and managed ProxyJump cycles.

## Credential profiles

A credential contains a unique label and one authentication kind:

- `password`: vault-backed password.
- `key`: absolute private-key path plus an optional vault-backed passphrase.
- `agent`: OpenSSH defaults or ssh-agent; no stored secret.

Credentials may be reused by multiple connection profiles. Block deletion while referenced. Never delete the referenced key file.

## SSH config

Render managed entries first and include the original user config last so explicit managed values win while unrelated defaults remain available. Detect and reject an Include cycle involving the generated file. Write atomically with user-only permissions.

## Import

Follow Include files, enumerate literal Host aliases, and resolve each alias with `ssh -G`. Copy imports into the database; do not keep them synchronized automatically. Preview additions, unchanged entries, and conflicts. Skip conflicts unless overwrite is explicit.

