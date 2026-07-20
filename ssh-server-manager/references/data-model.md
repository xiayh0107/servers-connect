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

## Host-bound skills

A registered skill contains a case-insensitively unique name, a normalized
absolute path to its local `SKILL.md`, the frontmatter description, and created
and updated timestamps. The registry stores no skill body. The path may be
registered from either a skill root directory or the `SKILL.md` itself.

Skills and connection profiles have a many-to-many relationship. One skill may
apply to several related nodes, and one host may use several complementary
skills. Relationships reference stable server IDs, so renaming an alias keeps
its bindings and separate accounts on the same endpoint can have different
skills. Registration with repeated `--server` aliases and multi-host binding
changes are atomic: an unknown host or invalid association rolls back the
whole mutation. Block registry deletion while hosts are bound. Once detached,
removing the registry entry never deletes the local skill directory.

Resolve host-specific context only after the target aliases are known. The
result includes a per-host view and a deduplicated skill view with an
`applies_to` alias list, so a multi-host task does not leak one environment's
instructions into another. Readiness is computed from the current local file:
`ready`, `missing`, `invalid`, or `name_mismatch`. A non-ready association
fails resolution instead of falling back to another same-name path.

Discovery scans standard local agent skill roots and `SSM_SKILLS_DIRS` without
changing the registry. Same-name candidates at different paths are reported as
conflicts; the registry's case-insensitive uniqueness constraint prevents an
ambiguous registration.

## SSH config

Render managed entries first and include the original user config last so explicit managed values win while unrelated defaults remain available. Detect and reject an Include cycle involving the generated file. Write atomically with user-only permissions.

## Import

Follow Include files, enumerate literal Host aliases, and resolve each alias with `ssh -G`. Copy imports into the database; do not keep them synchronized automatically. Preview additions, unchanged entries, and conflicts. Skip conflicts unless overwrite is explicit.
