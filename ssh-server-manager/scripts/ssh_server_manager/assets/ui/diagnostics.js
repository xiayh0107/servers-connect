(() => {
  let csrf = "";
  let dialog;

  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));

  async function request(path, options = {}) {
    const method = options.method || "GET";
    const headers = { ...(options.headers || {}) };
    if (method !== "GET") {
      if (!csrf) {
        const bootstrap = await fetch("/api/bootstrap", { credentials: "same-origin" });
        const data = await bootstrap.json();
        csrf = data.csrf;
      }
      headers["X-CSRF-Token"] = csrf;
    }
    const response = await fetch(path, { ...options, method, headers, credentials: "same-origin" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.message || data.detail || data.error || `Request failed (${response.status})`);
    return data;
  }

  function ensureDialog() {
    if (dialog) return dialog;
    dialog = document.createElement("dialog");
    dialog.className = "diagnostics-dialog";
    dialog.setAttribute("aria-labelledby", "diagnosticsTitle");
    dialog.innerHTML = `
      <div class="dialog-heading">
        <h2 id="diagnosticsTitle">Host diagnostics</h2>
        <button type="button" class="icon-button diagnostics-close" aria-label="Close">×</button>
      </div>
      <div class="diagnostics-body" aria-live="polite"></div>
      <div class="dialog-actions"><button type="button" class="secondary diagnostics-close">Close</button></div>`;
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog || event.target.closest(".diagnostics-close")) dialog.close();
    });
    document.body.append(dialog);
    return dialog;
  }

  function setBody(html) {
    ensureDialog().querySelector(".diagnostics-body").innerHTML = html;
  }

  function render(result) {
    const summary = result.summary || {};
    const tone = result.overall === "ok" ? "ok" : result.overall === "warning" ? "warning" : "failed";
    const checked = result.checked_at ? new Date(result.checked_at).toLocaleString() : "—";
    const checks = (result.checks || []).map((check) => {
      const details = Object.entries(check.details || {})
        .map(([key, value]) => `<span><b>${escapeHtml(key.split("_").join(" "))}</b> ${escapeHtml(Array.isArray(value) ? value.join(" → ") : value)}</span>`)
        .join("");
      const latency = check.latency_ms == null ? "" : ` · ${escapeHtml(check.latency_ms)} ms`;
      const code = check.error_code ? ` · ${escapeHtml(check.error_code)}` : "";
      return `<article class="diagnostic-check ${escapeHtml(check.status)}">
        <div class="diagnostic-check-head"><strong>${escapeHtml(check.label)}</strong><span>${escapeHtml(check.status)}${latency}${code}</span></div>
        <p>${escapeHtml(check.message)}</p>${details ? `<div class="diagnostic-details">${details}</div>` : ""}
      </article>`;
    }).join("");
    setBody(`<div class="diagnostics-summary ${tone}">
      <div><strong>${escapeHtml(result.alias)} · ${escapeHtml(result.overall)}</strong><span>${escapeHtml(summary.passed || 0)}/${escapeHtml(summary.total || 0)} checks passed</span></div>
      <small>Checked ${escapeHtml(checked)}</small>
    </div><section class="diagnostics-checks">${checks || "<p class=diagnostics-empty>No checks returned.</p>"}</section>`);
  }

  async function diagnose(id, label, button) {
    if (!id || button?.dataset.busy === "true") return;
    if (button) {
      button.dataset.busy = "true";
      button.disabled = true;
    }
    const current = ensureDialog();
    current.querySelector("#diagnosticsTitle").textContent = `Checking ${label}`;
    setBody('<div class="diagnostics-loading"><span></span><span></span><span></span><p>Running read-only SSH, SFTP, and remote-shell checks…</p></div>');
    if (!current.open) current.showModal();
    try {
      const result = await request(`/api/servers/${encodeURIComponent(id)}/diagnose`, { method: "POST" });
      current.querySelector("#diagnosticsTitle").textContent = `${result.alias} diagnostics`;
      render(result);
      document.querySelector("#refreshButton")?.click();
    } catch (error) {
      setBody(`<div class="diagnostics-error"><strong>Diagnostics failed</strong><p>${escapeHtml(error.message || error)}</p></div>`);
    } finally {
      if (button) {
        button.disabled = false;
        button.dataset.busy = "false";
      }
    }
  }

  function decorate() {
    document.querySelectorAll("#serverRows tr[data-server-id]").forEach((row) => {
      const actions = row.querySelector(".connection-actions");
      if (!actions || actions.querySelector(".host-diagnostics")) return;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "button small subtle-button host-diagnostics";
      button.textContent = "Diagnostics";
      button.title = "Run read-only host diagnostics";
      button.dataset.id = row.dataset.serverId;
      actions.insertBefore(button, actions.querySelector(".row-menu"));
    });
    const workspaceActions = document.querySelector("#workspaceActiveActions");
    if (workspaceActions && !workspaceActions.querySelector(".host-diagnostics")) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "button subtle-button host-diagnostics";
      button.textContent = "Diagnostics";
      button.title = "Run read-only host diagnostics";
      button.dataset.workspace = "true";
      workspaceActions.append(button);
    }
  }

  document.addEventListener("click", (event) => {
    const button = event.target.closest(".host-diagnostics");
    if (!button) return;
    event.preventDefault();
    const id = button.dataset.workspace
      ? document.querySelector("#fileServerSelect")?.value
      : button.dataset.id;
    const row = id
      ? [...document.querySelectorAll("#serverRows tr[data-server-id]")].find((item) => item.dataset.serverId === id)
      : null;
    const label = button.dataset.workspace
      ? document.querySelector("#fileConnectionLabel strong")?.textContent || "host"
      : row?.querySelector(".alias")?.textContent || "host";
    diagnose(id, label, button);
  });

  document.addEventListener("DOMContentLoaded", () => {
    decorate();
    const rows = document.querySelector("#serverRows");
    if (rows) new MutationObserver(decorate).observe(rows, { childList: true });
    const workspace = document.querySelector("#workspaceActiveActions");
    if (workspace) new MutationObserver(decorate).observe(workspace, { childList: true });
  });
})();
