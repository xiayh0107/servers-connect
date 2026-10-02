const ALIAS = /^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$/;
let options, dialog, selectedId = null, selectedAlias = null, trigger = null;
let copyVersion = 0, copying = false, copyError = false, available = false;
let pendingBack = false, queuedOpen = null;
const fields = {};

function text(name, value) {
  const next = String(value ?? "");
  if (fields[name].textContent !== next) fields[name].textContent = next;
}

function state() {
  return options.getState();
}

function routeAlias() {
  const match = /^#\/hosts\/([^/]+)$/.exec(window.location.hash);
  if (!match) return null;
  try {
    const alias = decodeURIComponent(match[1]);
    return ALIAS.test(alias) ? alias : null;
  } catch {
    return null;
  }
}

function hostByAlias(alias) {
  return (state().servers || []).find((server) =>
    String(server.alias).toLowerCase() === alias.toLowerCase());
}

function currentHost() {
  return (state().servers || []).find((server) => server.id === selectedId);
}

function resetCopy() {
  copyVersion += 1;
  copying = false;
  copyError = false;
  updateCopy();
}

function updateCopy() {
  fields.copy.disabled = copying || !available;
  fields.copy.setAttribute("aria-busy", String(copying));
  text("copyLabel", copying ? "Copying…" : copyError ? "Try copying again" : "Copy SSH command");
  fields.error.hidden = !copyError;
}

function writeRoute(alias, replace) {
  const path = alias ? `#/hosts/${encodeURIComponent(alias)}` : "/";
  const marker = { ...window.history.state };
  if (alias && !replace) marker.ssmHostDetails = true;
  if (!alias) delete marker.ssmHostDetails;
  window.history[replace ? "replaceState" : "pushState"](marker, "", path);
}

function restoreFocus(id, source) {
  const replacement = Array.from(document.querySelectorAll("#serverRows button[data-host-details]"))
    .find((button) => button.dataset.hostDetails === id);
  const targets = [source, replacement, document.querySelector('[data-view="servers"]'),
    document.querySelector("#hostSearchInput")];
  const target = targets.find((element) => element?.isConnected && !element.disabled && element.getClientRects().length);
  target?.focus({ preventScroll: true });
}

function dismiss(replaceRoute = true) {
  const id = selectedId, source = trigger;
  selectedId = selectedAlias = trigger = null;
  resetCopy();
  if (replaceRoute && !pendingBack) {
    if (window.history.state?.ssmHostDetails) {
      pendingBack = true;
      window.history.back();
    } else writeRoute(null, true);
  }
  if (dialog.open) dialog.close();
  restoreFocus(id, source);
}

function render() {
  const server = currentHost();
  const wasAvailable = available;
  available = Boolean(server && ALIAS.test(server.alias));
  fields.content.hidden = !available;
  fields.actions.hidden = !available;
  fields.unavailable.hidden = available;
  if (!available) {
    text("title", "Host unavailable");
    text("endpoint", selectedAlias || "Saved host");
    if (wasAvailable) resetCopy();
    updateCopy();
    return;
  }
  if (selectedAlias !== server.alias) {
    selectedAlias = server.alias;
    writeRoute(server.alias, true);
    resetCopy();
  }
  text("title", server.alias);
  text("endpoint", `${server.username}@${server.hostname}`);
  text("hostname", server.hostname);
  text("username", server.username);
  text("port", server.port ?? 22);
  const jumps = Array.isArray(server.proxy_jumps) ? server.proxy_jumps : [];
  text("route", jumps.length ? jumps.join(" → ") : "Direct connection");
  text("routeHint", jumps.length ? `${jumps.length} saved jump host${jumps.length === 1 ? "" : "s"}` : "No jump hosts configured");
  const credential = (state().credentials || []).find((item) => item.id === server.credential_id);
  text("credential", credential?.label || server.credential_label || (server.credential_id ? "Credential unavailable" : "OpenSSH default"));
  text("credentialKind", credential ? ({ key: "SSH key", password: "Password", agent: "SSH agent" }[credential.kind] || "Saved credential")
    : server.credential_id ? "Saved credential" : "SSH agent / local SSH configuration");
  const status = options.serverStatus(server);
  const tone = ["ok", "failed", "stale", "neutral"].includes(status.tone) ? status.tone : "neutral";
  fields.status.className = `hd-status ${tone}`;
  text("statusLabel", status.short || "Unknown");
  text("statusMeta", status.meta === "—" ? "" : status.meta);
  const checked = Date.parse(server.last_test_at || "");
  const checkedText = Number.isFinite(checked)
    ? `Last checked ${new Date(checked).toLocaleString()}` : "No saved connection check";
  text("checked", checkedText);
  fields.status.title = checkedText;
  const tags = Array.isArray(server.tags) ? server.tags : [];
  const tagSignature = JSON.stringify(tags);
  if (fields.tags.dataset.signature !== tagSignature) {
    fields.tags.replaceChildren(...tags.map((tag) => {
      const label = document.createElement("span");
      label.className = "hd-tag";
      label.textContent = tag;
      return label;
    }));
    fields.tags.dataset.signature = tagSignature;
  }
  fields.noTags.hidden = Boolean(tags.length);
  text("tagCount", tags.length ? String(tags.length) : "");
  text("notes", server.notes || "No notes for this host yet.");
  fields.notes.classList.toggle("hd-empty", !server.notes);
  const command = `serverctl connect ${server.alias}`;
  if (fields.command.value !== command) fields.command.value = command;
  updateCopy();
}

function show(alias, source = null) {
  ensureDialog();
  const server = hostByAlias(alias);
  const changed = selectedAlias !== alias || selectedId !== (server?.id ?? null);
  if (changed) {
    selectedAlias = alias;
    selectedId = server?.id ?? null;
    trigger = source;
    resetCopy();
  } else if (source) trigger = source;
  render();
  if (!dialog.open) {
    dialog.showModal();
    fields.title.focus({ preventScroll: true });
    fields.scroll.scrollTop = 0;
  } else if (changed) fields.scroll.scrollTop = 0;
}

function followRoute(event) {
  if (pendingBack) {
    if (event?.type !== "popstate") return;
    pendingBack = false;
    const next = queuedOpen;
    queuedOpen = null;
    if (next) return openHostDetails(next.id, next.source);
  }
  const alias = routeAlias();
  if (alias) show(alias);
  else if (dialog?.open) dismiss(false);
}

async function copyCommand() {
  if (copying || !available) return;
  const server = currentHost();
  if (!server || !ALIAS.test(server.alias)) return;
  const version = ++copyVersion;
  copying = true;
  copyError = false;
  updateCopy();
  try {
    if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
    await navigator.clipboard.writeText(`serverctl connect ${server.alias}`);
    if (version !== copyVersion || !dialog.open) return;
    options.toast("SSH command copied");
  } catch {
    if (version !== copyVersion || !dialog.open) return;
    copyError = true;
  } finally {
    if (version === copyVersion) {
      copying = false;
      updateCopy();
    }
  }
}

function ensureStyle() {
  if (!document.querySelector('link[href="/assets/hosts.css"]')) {
    const style = document.createElement("link");
    style.rel = "stylesheet";
    style.href = "/assets/hosts.css";
    document.head.append(style);
  }
}

function ensureDialog() {
  if (dialog) return;
  dialog = document.createElement("dialog");
  dialog.className = "host-details-dialog";
  dialog.setAttribute("aria-labelledby", "hostDetailTitle");
  dialog.setAttribute("aria-describedby", "hostDetailDescription");
  dialog.innerHTML = `
<header class="hd-header">
<div class="hd-heading"><p class="hd-eyebrow">HOST DETAILS</p>
<h2 id="hostDetailTitle" data-hd="title" tabindex="-1"></h2>
<p class="hd-endpoint" data-hd="endpoint"></p>
</div>
<button type="button" class="icon-button hd-close" data-hd="close" aria-label="Close host details" title="Close (Esc)">×</button>
</header>
<div class="hd-scroll" data-hd="scroll">
<p id="hostDetailDescription" class="hd-description">Saved configuration and the latest connection check.</p>
<div class="hd-unavailable" data-hd="unavailable" hidden>
<span class="hd-unavailable-icon" aria-hidden="true">◇</span>
<h3>This host is unavailable</h3>
<p>It may have been removed or renamed. Choose a host from the list.</p>
</div>
<div data-hd="content">
<section class="hd-status-card" aria-label="Saved connection status">
<div class="hd-status-row"><span class="hd-section-label">LAST SAVED CHECK</span>
<span class="hd-status" data-hd="status"><i aria-hidden="true"></i><b data-hd="statusLabel"></b><span data-hd="statusMeta"></span></span>
</div>
<p class="hd-checked" data-hd="checked"></p>
<p class="hd-status-note">Saved result. Opening details does not check the connection.</p>
</section>
<section class="hd-section" aria-labelledby="hostConnectionTitle">
<h3 id="hostConnectionTitle">Connection</h3>
<dl class="hd-fields">
<div class="hd-field hd-wide"><dt>Hostname</dt><dd class="hd-mono" data-hd="hostname"></dd></div>
<div class="hd-field"><dt>Username</dt><dd class="hd-mono" data-hd="username"></dd></div>
<div class="hd-field"><dt>Port</dt><dd class="hd-mono" data-hd="port"></dd></div>
<div class="hd-field hd-wide"><dt>Proxy route</dt><dd class="hd-mono" data-hd="route"></dd><dd class="hd-field-hint" data-hd="routeHint"></dd></div>
<div class="hd-field hd-wide"><dt>Credential</dt><dd data-hd="credential"></dd><dd class="hd-field-hint" data-hd="credentialKind"></dd></div>
</dl>
</section>
<section class="hd-section" aria-labelledby="hostTagsTitle">
<h3 id="hostTagsTitle">Tags <span class="hd-count" data-hd="tagCount"></span></h3>
<div class="hd-tags" data-hd="tags"></div><p class="hd-empty" data-hd="noTags">No tags assigned</p>
</section>
<section class="hd-section" aria-labelledby="hostNotesTitle">
<h3 id="hostNotesTitle">Notes</h3><p class="hd-notes" data-hd="notes"></p>
</section>
<section class="hd-section hd-command-section" aria-labelledby="hostCommandTitle">
<h3 id="hostCommandTitle">Connect from your terminal</h3>
<label class="hd-command"><span aria-hidden="true">$</span><input data-hd="command" aria-label="SSH command" readonly spellcheck="false"></label>
<p class="hd-command-hint">Uses this host’s saved SSH configuration.</p>
</section>
</div>
</div>
<footer class="hd-footer" data-hd="actions">
<p class="hd-copy-error" role="alert" data-hd="error" hidden>Couldn’t copy. Select the command above and copy it, or try again.</p>
<button type="button" class="hd-copy" data-hd="copy"><svg aria-hidden="true" width="16" height="16" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="7" y="7" width="10" height="10" rx="2"/><path d="M4 13H3V3h10v1"/></svg><span data-hd="copyLabel">Copy SSH command</span></button>
<div class="hd-secondary-actions"><button type="button" class="secondary" data-hd="edit">Edit host</button><button type="button" class="secondary" data-hd="browse">Browse files <span aria-hidden="true">↗</span></button></div>
</footer>`;
  dialog.querySelectorAll("[data-hd]").forEach((element) => { fields[element.dataset.hd] = element; });
  fields.close.addEventListener("click", () => dismiss());
  fields.copy.addEventListener("click", copyCommand);
  fields.command.addEventListener("click", () => fields.command.select());
  for (const action of ["edit", "browse"]) {
    fields[action].addEventListener("click", () => {
      const server = currentHost();
      if (!server) return render();
      dismiss();
      if (action === "edit") options.openServer(server);
      else Promise.resolve(options.openBrowse(server.id)).catch(() => options.toast("Could not open the file browser", true));
    });
  }
  dialog.addEventListener("cancel", (event) => { event.preventDefault(); dismiss(); });
  dialog.addEventListener("close", () => {
    if (!dialog.open && selectedAlias !== null) dismiss();
  });
  dialog.addEventListener("click", (event) => {
    if (event.target !== dialog) return;
    const rect = dialog.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dismiss();
  });
  document.body.append(dialog);
}

export function openHostDetails(id, source = null) {
  const server = (state().servers || []).find((item) => item.id === id);
  if (!server || !ALIAS.test(server.alias)) return;
  if (pendingBack) { queuedOpen = { id, source }; return; }
  writeRoute(server.alias, Boolean(dialog?.open));
  show(server.alias, source);
}

export function syncHostDetails(nextOptions) {
  const first = !options;
  options = nextOptions;
  if (first) {
    ensureStyle();
    window.addEventListener("popstate", followRoute);
    window.addEventListener("hashchange", followRoute);
  }
  if (pendingBack) return;
  if (dialog?.open) {
    const alias = routeAlias();
    if (!alias) dismiss(false);
    else if (alias !== selectedAlias) show(alias);
    else {
      if (!currentHost()) selectedId = hostByAlias(alias)?.id ?? null;
      render();
    }
  } else followRoute();
}
