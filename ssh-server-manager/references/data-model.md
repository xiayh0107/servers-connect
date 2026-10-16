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
Successful SFTP browsing and explicit host SSH tests both update it. The UI
shows saved status and observation times; it does not probe every stored host
in the background. Independent credential probes have separate history and
never change a host's health record.

SQLite schema 6 adds nullable `facts`, `last_success_at`, `last_resolved_ip`,
`host_key`, and `geo` fields. Successful diagnostics retain sanitized system
facts with their `observed_at`, `os_like`, and closed `os_logo` key. Failed
checks replace the latest result but keep previous successful facts and
`last_success_at`. A locally resolved IP comes from an explicit check and may
differ from the peer address through ProxyJump. Host-key state is `verified`,
`unknown`, `changed`, or `revoked`; stored fingerprints do not replace OpenSSH
verification. Manual geography stores an ISO country code and optional city.
`address_scope` is derived from the literal hostname without DNS. Inventory and
detail reads perform no DNS, SSH, or vault operations.

Changing hostname or port clears observations and test history for the old
endpoint, and delayed results from that endpoint cannot repopulate them.
User-entered geography, notes, tags, paths, and skill bindings remain. Schema
initialization and migrations, partial updates, and route validation hold a
SQLite immediate write transaction so separate processes cannot apply stale
read/merge/write snapshots or race a schema upgrade.

Reject whitespace, shell metacharacters, and OpenSSH wildcard characters in managed aliases. Reject invalid ports, self-jumps, and managed ProxyJump cycles.
When an alias changes, validate the complete graph with dependent references
rewritten before committing. Creating or renaming a profile into a previously
external hop also validates the resulting managed graph.

## Credential profiles

A credential contains a unique label and one authentication kind:

- `password`: vault-backed password.
- `key`: absolute private-key path plus an optional vault-backed passphrase.
- `agent`: OpenSSH defaults or ssh-agent; no stored secret.

Credentials may be reused by multiple connection profiles. Default deletion
refuses references. A separate explicit preview/detach-delete operation checks
the expected revision/timestamp and exact host ID set, then detaches references,
deletes the credential and records cleanup work in one transaction. Never
delete the referenced key file.

Schema 7 adds an integer credential `revision`, nullable cached `key_metadata`,
`credential_tests` and `credential_deletions`. Earlier schemas migrate in order
without discarding host observations, existing credentials or bindings. Public
key metadata includes the observed format, algorithm, fingerprint, bits,
encryption and permissions assessment; encrypted keys can have nullable public
fields until validated. Private-key bytes and passwords/passphrases are never
stored in SQLite. Read-only credential summaries use this cache, not a fresh
file/vault lookup.

Credential updates increment the revision. Test results record that revision
and are bounded to the latest 50 completed results; the current `last_test`
ignores older revisions while their history remains available. Credential
deletion cascades its test history. Tests temporarily override the target
host's credential without modifying its saved binding or health record.

The deletion journal survives removal of its credential. It stores an operation
ID, sanitized credential metadata, detached host references, remaining vault
slots/config cleanup, attempt count and a sanitized error code. Secret values
are never journaled. A completed DB deletion with failed cleanup remains
committed/pending rather than appearing fully erased. Retry is idempotent and
does not re-detach hosts or remove the user's key file. Partial credential
creation failures use the same journal if compensating vault cleanup fails.

Per-credential cross-process locks serialize vault mutation, tests, reveal,
binding changes and deletion. Private-key saves verify the same bounded file
snapshot before database commit; a changed key aborts and restores the prior
vault value. Browser key revision tokens and directory scopes are ephemeral,
session-bound metadata, never persisted credential material.

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

## Saved working directories

Each record belongs to exactly one connection profile and holds a label, a
remote path, and an optional note. The relationship is one-to-many rather than
the many-to-many shape skills use, because a path describes one machine's
filesystem: `/srv/app` on `web1` and `/srv/app` on `web2` are two records, and
deleting a host deletes its own.

Labels are unique per host under Unicode case folding, so `app` and `APP` are
the same directory, and either spelling resolves it. Paths are unique per host
as well, which stops the same directory being saved twice under two names.
Both uniqueness rules are enforced by the database, not by the caller.

Every record carries a last-used timestamp that listing sorts on, so the
directories someone actually works in rise to the top while untouched ones stay
alphabetical. Replacing a host's whole set preserves those timestamps by path,
so renaming a directory in the UI does not reset what is known about it.

Saved directories are local metadata and nothing more. They record where a
person chose to work; they are not a cache of the remote filesystem, are never
verified against it, and their presence is not evidence that a path still
exists.

## SSH config

Render managed entries first, then include each imported host's selected source
config after its explicit managed values. Hosts imported from a custom source
are excluded from the original config's fallback so additive IdentityFile
directives from unrelated configs do not mix. External aliases remain available
through fallback Includes. Format absolute paths for the actual SSH client;
Git/MSYS uses its own bounded `cygpath` conversion on Windows, while native
OpenSSH uses forward slashes. Reject a conversion failure or Include cycle
before applying an import. Write atomically with user-only permissions.
Hold the database write lock across the saved-profile snapshot and atomic
replacement so a concurrent render cannot publish an older configuration last.

## Import

Follow Include files, enumerate literal Host aliases, and resolve each alias with `ssh -G`. Copy imports into the database; do not keep them synchronized automatically. Preview additions, unchanged entries, and conflicts. Skip conflicts unless overwrite is explicit.

Store each applied host's canonical source path under its stable ID in the
`ssh_config_import_sources` setting, in the same transaction as a create or
update. Renaming retains the source; deleting removes its association. The
existing settings table needs no schema migration. Imported connection fields
are snapshots, while authentication directives continue to be read by OpenSSH
from the source config. Warnings expose this distinction and public IdentityFile
paths without reading keys or creating credentials. Overwrite merges only
imported connection fields with the latest record, preserving credential
bindings, notes and other local metadata.
