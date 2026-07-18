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
        csrf = (await bootstrap.json()).csrf;
      }
      headers["X-CSRF-Token"] = csrf;
      headers["Content-Type"] = "application/json";
    }
    const response = await fetch(path, {
      ...options,
      method,
      headers,
      credentials: "same-origin",
      body: options.body && typeof options.body !== "string" ? JSON.stringify(options.body) : options.body,
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.message || data.detail || data.error || `Request failed (${response.status})`);
    return data;
  }

  async function getServer(id) {
    const bootstrap = await request("/api/bootstrap");
    csrf = bootstrap.csrf || csrf;
    return bootstrap.servers.find((server) => server.id === id);
  }

  function ensureDialog() {
    if (dialog) return dialog;
    dialog = document.createElement("dialog");
    dialog.className = "server-notes-dialog";
    dialog.innerHTML = `
      <div class="dialog-heading">
        <h2 id="serverNotesTitle">Server notes</h2>
        <button type="button" class="icon-button notes-close" aria-label="Close">×</button>
      </div>
      <p class="notes-current-label">Current note</p>
      <pre class="notes-current">No note yet.</pre>
      <label class="notes-editor"><span>Add a note</span><textarea id="serverNoteInput" maxlength="10000" rows="6" placeholder="What should a user or agent remember about this server?"></textarea></label>
      <p class="notes-error" role="alert" hidden></p>
      <div class="dialog-actions">
        <button type="button" class="secondary notes-clear">Clear</button>
        <button type="button" class="secondary notes-replace">Replace</button>
        <button type="button" class="notes-append">Add note</button>
      </div>`;
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog || event.target.closest(".notes-close")) dialog.close();
    });
    dialog.querySelector(".notes-append").addEventListener("click", () => save("append"));
    dialog.querySelector(".notes-replace").addEventListener("click", () => {
      const input = dialog.querySelector("#serverNoteInput");
      if (!input.value && dialog.dataset.currentNote) {
        input.value = dialog.dataset.currentNote;
        input.focus();
        return;
      }
      save("set");
    });
    dialog.querySelector(".notes-clear").addEventListener("click", () => {
      if (confirm("Clear this server's note?")) save("clear");
    });
    document.body.append(dialog);
    return dialog;
  }

  function setServer(server) {
    const current = server?.notes || "";
    const root = ensureDialog();
    root.dataset.serverId = server?.id || "";
    root.dataset.currentNote = current;
    root.querySelector("#serverNotesTitle").textContent = `${server?.alias || "Server"} notes`;
    root.querySelector(".notes-current").textContent = current || "No note yet.";
    root.querySelector("#serverNoteInput").value = "";
    root.querySelector(".notes-error").hidden = true;
  }

  async function save(mode) {
    const root = ensureDialog();
    const text = root.querySelector("#serverNoteInput").value;
    if (mode === "append" && !text.trim()) {
      root.querySelector(".notes-error").textContent = "Enter note text before adding it.";
      root.querySelector(".notes-error").hidden = false;
      root.querySelector("#serverNoteInput").focus();
      return;
    }
    const buttons = root.querySelectorAll(".dialog-actions button");
    buttons.forEach((button) => { button.disabled = true; });
    try {
      await request(`/api/servers/${encodeURIComponent(root.dataset.serverId)}/notes`, {
        method: "PUT",
        body: { mode, text },
      });
      root.close();
      document.querySelector("#refreshButton")?.click();
    } catch (error) {
      root.querySelector(".notes-error").textContent = error.message || String(error);
      root.querySelector(".notes-error").hidden = false;
    } finally {
      buttons.forEach((button) => { button.disabled = false; });
    }
  }

  async function openNotes(id) {
    if (!id) return;
    try {
      const server = await getServer(id);
      if (!server) throw new Error("Server not found");
      setServer(server);
      ensureDialog().showModal();
      ensureDialog().querySelector("#serverNoteInput").focus();
    } catch (error) {
      window.alert(error.message || String(error));
    }
  }

  function decorate() {
    document.querySelectorAll("#serverRows tr[data-server-id]").forEach((row) => {
      const actions = row.querySelector(".connection-actions");
      if (!actions || actions.querySelector(".host-note")) return;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "button small subtle-button host-note";
      button.textContent = "Note";
      button.title = "Add or edit server notes";
      button.dataset.id = row.dataset.serverId;
      actions.insertBefore(button, actions.querySelector(".host-diagnostics, .row-menu"));
    });
    const workspaceActions = document.querySelector("#workspaceActiveActions");
    if (workspaceActions && !workspaceActions.querySelector(".host-note")) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "button subtle-button host-note";
      button.textContent = "Note";
      button.title = "Add or edit server notes";
      button.dataset.workspace = "true";
      workspaceActions.insertBefore(button, workspaceActions.querySelector(".host-diagnostics"));
    }
  }

  document.addEventListener("click", (event) => {
    const button = event.target.closest(".host-note");
    if (!button) return;
    event.preventDefault();
    const id = button.dataset.workspace
      ? document.querySelector("#fileServerSelect")?.value
      : button.dataset.id;
    openNotes(id);
  });

  document.addEventListener("DOMContentLoaded", () => {
    decorate();
    const rows = document.querySelector("#serverRows");
    if (rows) new MutationObserver(decorate).observe(rows, { childList: true });
    const workspace = document.querySelector("#workspaceActiveActions");
    if (workspace) new MutationObserver(decorate).observe(workspace, { childList: true });
  });
})();
