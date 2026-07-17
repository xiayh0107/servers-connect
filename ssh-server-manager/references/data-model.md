# Data model and rendering

## Connection profiles

A connection profile contains one case-insensitively unique alias, hostname,
port, username, optional credential ID, ordered ProxyJump aliases, project or
scenario tags, notes, source metadata, and the most recent test result. Tags are
case-insensitively deduplicated; one profile may belong to multiple contexts.
Create another alias when the same endpoint needs another account.

Context names also have a small registry in the `settings` table. This keeps an
empty context available after its last host is unassigned. Creating or updating
a tagged connection registers any new names. Rename, deletion, and bulk host
assignment rewrite the registry and all affected connection tags in one SQLite
transaction, so an invalid host assignment rolls the complete change back.

Connection status is a timestamped observation, not a persistent online flag.
Successful SFTP browsing and explicit SSH tests both update it. The UI treats a
successful result as fresh for two minutes and then presents it as historical;
it does not probe every stored host in the background.

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
