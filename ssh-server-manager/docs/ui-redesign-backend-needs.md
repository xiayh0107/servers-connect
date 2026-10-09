# UI redesign: backend follow-ups

Working list of backend support that the Paper redesign (file
"SSH Server Manager · UI 重新设计") relies on. Frontend work continues
without it; each item says what the UI shows until the backend lands.
Not published to the docs site yet.

Guiding constraints: opening the inventory or a host's details must not start
SSH, touch the vault, or call a third-party service; secrets never reach the
browser outside the existing reveal flow.

## 1. Persist the detected operating system

**Needed by:** Hosts list OS logo + "Ubuntu 24.04" line; host details System
section.

`diagnose` already collects `os_id`, `os_version`, `os_pretty`, family, kernel,
arch, CPU, memory, disk and uptime (`_summarize_remote_identity`), but nothing
is stored. Store the last successful identity per host (new columns or a
`host_facts` table, schema migration) with its `observed_at`, and expose it on
the v1 `Host`/`Workspace` DTOs as read-only fields.

- Map `os_id`/`ID_LIKE` to a closed logo key (`debian`, `ubuntu`, `rocky`,
  `alpine`, `rhel`, `fedora`, `arch`, `macos`, `freebsd`, `windows`,
  `unknown`) so the UI never renders arbitrary remote strings as asset names.
- Remote strings are untrusted: length-limit and strip control characters.

**Until then:** generic server icon and "OS unknown · run a check".

## 2. Address scope and location

**Needed by:** flag / network icon before each endpoint, "Tokyo, JP · direct".

- **Scope** (no network access needed): classify the configured hostname or
  literal IP as `public`, `private` (RFC 1918 / ULA), `lan` (`.lan`, `.local`,
  `.home.arpa`), `loopback`, or `unresolved`. Add `address_scope` to `Host`.
- **Country/city:** only from an optional local GeoIP database
  (e.g. GeoLite2 file configured by the user) or a manual per-host override.
  Never call an online lookup API — it would disclose every host IP.
  Expose `geo: {country_code, city, source: "geoip"|"manual"} | null`.
- Hostnames resolve to an IP only during an explicit check; record the IP
  seen by the last check (`last_resolved_ip`) instead of resolving on read.

**Until then:** network icon by scope computed in the UI from the literal
hostname; no flags.

## 3. Last successful check

**Needed by:** "Blocked · lab-gateway is down" and "Last OK 9 days ago".

`record_test` overwrites `last_test_*`. Add `last_success_at` (and keep
`last_test_*` as the latest attempt). The UI derives *blocked* when any hop in
`proxy_jumps` has a failed latest attempt newer than the host's own check; no
stored state is needed for that.

## 4. Resolved route

**Needed by:** route chain in host details (`You → bastion → prod-web-01`) and
the "Jump for N" badge.

The UI can compute both from `proxy_jumps` across `/api/v1/hosts`. Two gaps:

- Jump entries that are not managed aliases (allowed by the graph validator
  when the node is absent) should come back flagged as `external: true`.
- Optional: `GET /api/v1/hosts/{id}/route` returning ordered hops with each
  hop's latest status, so very large inventories need not load everything.

## 5. Host key fingerprint

**Needed by:** "Host key verified · SHA256:…" in host details.

Return the known-hosts fingerprint and match state recorded by the last check
(read from the managed known_hosts file, never by connecting on read). Never
add any "accept new key" action to the web UI; host-key changes stay a CLI
decision.

## 6. Bulk checks

**Needed by:** "Run checks" on a multi-row selection.

The UI can call `POST /hosts/{id}/test` per host with a small concurrency cap.
A `POST /api/v1/hosts/test` batch endpoint with server-side concurrency and a
per-host result list would avoid browser-side fan-out. Not blocking.

## 7. Frontend-only (no backend work)

Recorded so they are not re-requested:

- **Hide details** mode: masking is done by not rendering values; search then
  matches only visible fields. Preference stored in browser local storage.
- **Pinned quick actions:** per-browser preference in local storage.
- **Copy SSH command:** copies `serverctl connect ALIAS`, as today, so the
  vault and host-key policy stay in the path.
- **Duplicate host:** existing `GET` + `POST /api/v1/hosts` with a new alias.
- Keyboard navigation and grouping (Route / Tag / None).
