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

The launch token is single-use: once a browser session is established, the
URL stops working (and the `--url-file` is deleted). Restart `serverctl ui`
to get a new one. Sessions last 12 hours.

## What you can do

- **Servers** — add, edit, remove; ordered ProxyJump chains; live
  connection tests with latency and classified errors.
- **Credentials** — create password / key / agent credentials, edit or
  replace secrets, see which servers use them. Secret input fields post
  directly to the loopback API; values are never rendered back.
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

## If something misbehaves

See [faq.md](faq.md) and the
[troubleshooting playbook](../references/troubleshooting.md).
