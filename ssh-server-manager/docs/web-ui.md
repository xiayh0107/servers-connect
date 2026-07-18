# The local web UI

```bash
serverctl ui
```

A management interface served from `127.0.0.1` only — it is not a cloud
dashboard and cannot be reached from the network. The command picks a free
port, prints only the port number, and opens your browser with a one-time
tokenized URL.

## Launching

| Situation | Command |
|---|---|
| Normal use | `serverctl ui` |
| Fixed port | `serverctl ui --port 8422` |
| Browser can't be auto-opened (e.g. remote desktop) | `serverctl ui --no-open --url-file /path/to/private-url` then open the URL from that mode-600 file yourself |
| Check whether the UI is running | `serverctl ui --status` |
| Stop a UI left running in the background | `serverctl ui --stop` |

The launch token is single-use: once a browser session is established, the
URL stops working (and the `--url-file` is deleted). Restart `serverctl ui`
to get a new one. Sessions last 12 hours.

## What you can do

- **Servers** — add, edit, remove; assign reusable project/environment tags;
  manage ordered ProxyJump chains; run individual or selected-host connection
  tests with latency and classified errors; open **Diagnostics** on a host to
  inspect its local profile, SSH, SFTP, and remote system summary. Use **Note**
  on a host row or the active workspace to append or edit local server notes.
  The inventory keeps host, endpoint,
  tags, status, and actions as its five primary columns; credential and jump
  details stay under the endpoint instead of widening the table.
- **Files** — choose a server and browse its remote home or another directory
  over SFTP; use breadcrumbs, show/hide dotfiles, and copy an
  `ALIAS:/absolute/path` reference for an agent. Visible file references can be
  copied in one batch. Modification values use one visual format, and access
  bits include owner/group/others labels and a full explanatory tooltip. This
  browser is read-only.
- **Tags** — tag a host by project, environment, role, or any other useful
  dimension, then use the global tag selector to scope both the workspace and
  connection inventory to that subset. The last selection is stored on this
  device. **Tags** is a dedicated library for inline creation, rename, deletion,
  usage counts, and host previews. In **Connections**, select one or more hosts
  and open the single **Edit tags** picker: checked, mixed, and empty states
  show current membership, and clicking an item adds or removes it. Search for
  an existing tag or type a new name and choose **Create** to create and assign
  it in one operation. The same picker opens from every Tags table cell. Tags
  remain clickable filters, and every tag keeps one categorical color across
  the sidebar, tables, chips, and pickers. In the server editor, tags
  are searchable chips: Enter adds a typed value, Backspace removes the last
  chip, and suggestions reuse existing tags. The Files sidebar also shows each
  host's first tag and remaining count; click that summary to open the
  same add/remove/create picker without leaving the workspace.
- **Themes** — follow the operating-system appearance or explicitly choose the
  light, dark, or high-contrast token set, then pick one of seven accent
  palettes (indigo, teal, emerald, amber, rose, violet, graphite) from the
  header. Accent choices persist locally, adapt to every base theme, and keep
  the high-contrast theme's yellow focus ring. No theme assets or external
  fonts are downloaded.
- **Credentials** — create password / key / agent credentials, edit or
  replace secrets, copy referenced key paths, and see which servers use them.
  Vault and reveal-protection status live together above the credential list.
  Secret input fields post directly to the loopback API; values are never
  rendered back.
- **Import** — preview your `~/.ssh/config` and apply selected aliases.
- **Reveal** — display a stored password/passphrase, gated as below.

## Revealing a secret

1. Click reveal on a credential.
2. Authenticate with a **passkey** (Touch ID / Windows Hello / security
   key) — or the **master password** if you enrolled one (Argon2id-hashed,
   rate-limited to 5 attempts per minute).
3. The grant is single-use and expires in 30 seconds; the revealed value is
   wiped from the page after 15 seconds.

Set up either factor in the UI's auth section: register a passkey (works
on `localhost` without HTTPS) and/or enroll a master password as fallback.

Ordinary operations — connecting, testing, executing — never require
reveal: they consume vault secrets without returning them.

Remote file browsing follows the same rule. It uses the managed OpenSSH
configuration, normal host-key verification, ProxyJump chain, and AskPass
credential flow. The browser returns directory metadata only; it does not read
file contents or provide remote write actions.

Status indicators are deliberately time-aware. Green means an SSH test or SFTP
operation succeeded within the last two minutes, and the label includes how
recently it was verified. Older results become an amber “Last reachable” state;
they never remain indefinitely green. Failed connection attempts immediately
replace the previous result. The UI avoids automatically probing every host,
which would be slow and could trigger many authentication prompts.

## Performance baseline

The local UI deliberately stays framework-free and self-contained: its first
view loads one stylesheet and one deferred JavaScript file, uses system fonts,
and makes no CDN or third-party network requests. Automated tests keep those
initial assets below 100 KB uncompressed and 25 KB compressed. Tag
management, connection bulk actions, and the chip picker are separate
dependency-free assets loaded only when one of those controls is opened (below
48 KB uncompressed / 11 KB compressed together).

Opening the UI does not initiate an SSH connection. Remote work begins only
after a host is selected. Directory filtering and sorting happen locally, while
large results render in batches of 250 rows so a directory with thousands of
entries does not create thousands of DOM nodes at once. Motion is limited to a
lightweight loading pulse and is disabled by the reduced-motion preference.

See [ui-ux-research.md](ui-ux-research.md) for the evidence and task-model
audit behind the host and tag interaction.

## If something misbehaves

See [faq.md](faq.md) and the
[troubleshooting playbook](../references/troubleshooting.md).
