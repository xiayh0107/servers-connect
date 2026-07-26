// Saved working directories and explicit file transfer for the Host Workspace.
//
// Loaded eagerly and committed readable, like diagnostics.js and notes.js. It
// attaches itself to the DOM that app.js renders rather than being called from
// it, which keeps the core bundle untouched.
(() => {
  let csrf = "";
  let cachedServerId = "";
  let cachedPaths = [];

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
    }
    if (options.json !== undefined) {
      headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(options.json);
    }
    const response = await fetch(path, { ...options, method, headers, credentials: "same-origin" });
    if (!response.ok) {
      let message = `Request failed (${response.status})`;
      try {
        message = (await response.json()).message || message;
      } catch (error) {
        /* a non-JSON body means the status line is all we have */
      }
      throw new Error(message);
    }
    return response;
  }

  let toastTimer = 0;

  function toast(message, isError = false) {
    // Reuse the app's own toast host and its "show"/"show error" class contract
    // so our messages cannot end up styled differently from everything else.
    const host = document.querySelector("#toast");
    if (!host) return;
    host.textContent = message;
    host.className = isError ? "show error" : "show";
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      host.className = "";
    }, 4500);
  }

  const activeServerId = () => document.querySelector("#fileServerSelect")?.value || "";
  const currentPath = () => document.querySelector("#filePathInput")?.value || "";

  function navigate(path) {
    const input = document.querySelector("#filePathInput");
    const form = document.querySelector("#filePathForm");
    if (!input || !form) return;
    input.value = path;
    // Let app.js own the loading, error handling and history; we only steer it.
    form.requestSubmit();
  }

  // --- saved working directories -------------------------------------------

  function renderPaths() {
    const mount = document.querySelector("#hostPathsMount");
    if (!mount) return;
    const serverId = activeServerId();
    if (!serverId) {
      mount.replaceChildren();
      return;
    }
    const here = currentPath();
    const saved = cachedPaths.some((item) => item.path === here);
    const chips = cachedPaths
      .map(
        (item) => `<button type="button" class="ws-chip${item.path === here ? " current" : ""}"
          data-ws-go="${escapeHtml(item.path)}" data-ws-id="${escapeHtml(item.id)}"
          title="${escapeHtml(item.notes || item.path)}">${escapeHtml(item.label)}<span
          class="ws-chip-forget" data-ws-forget="${escapeHtml(item.id)}"
          role="button" tabindex="0" aria-label="Forget ${escapeHtml(item.label)}">×</span></button>`,
      )
      .join("");
    mount.innerHTML = `<div class="ws-paths">
      <span class="ws-paths-label">Saved</span>
      <div class="ws-chip-row">${chips || '<span class="ws-empty">No saved directories yet</span>'}</div>
      <button type="button" class="toolbar-button text-toolbar-button ws-save" ${
        saved || !here ? "disabled" : ""
      }>${saved ? "Saved" : "Save this directory"}</button>
    </div>`;
  }

  async function refreshPaths(force = false) {
    const serverId = activeServerId();
    if (!serverId) {
      cachedServerId = "";
      cachedPaths = [];
      renderPaths();
      return;
    }
    if (!force && serverId === cachedServerId) {
      renderPaths();
      return;
    }
    cachedServerId = serverId;
    try {
      cachedPaths = await (await request(`/api/servers/${encodeURIComponent(serverId)}/paths`)).json();
    } catch (error) {
      cachedPaths = [];
    }
    renderPaths();
  }

  async function saveCurrentDirectory() {
    const serverId = activeServerId();
    const path = currentPath();
    if (!serverId || !path) return;
    // Default the label to the directory name; the full path is the tooltip.
    const suggested = path.replace(/\/+$/, "").split("/").pop() || path;
    const label = window.prompt("Name this directory", suggested);
    if (label === null) return;
    try {
      await request(`/api/servers/${encodeURIComponent(serverId)}/paths`, {
        method: "POST",
        json: { label: label.trim() || suggested, path },
      });
      await refreshPaths(true);
      toast("Directory saved");
    } catch (error) {
      toast(error.message, true);
    }
  }

  async function forgetDirectory(pathId) {
    const serverId = activeServerId();
    if (!serverId) return;
    try {
      await request(
        `/api/servers/${encodeURIComponent(serverId)}/paths/${encodeURIComponent(pathId)}`,
        { method: "DELETE" },
      );
      await refreshPaths(true);
    } catch (error) {
      toast(error.message, true);
    }
  }

  async function goToSaved(path, pathId) {
    navigate(path);
    const serverId = activeServerId();
    if (!serverId || !pathId) return;
    try {
      // Recency ordering is the whole point of the list, so record the visit.
      await request(
        `/api/servers/${encodeURIComponent(serverId)}/paths/${encodeURIComponent(pathId)}/used`,
        { method: "POST" },
      );
      cachedServerId = "";
    } catch (error) {
      /* ordering is a convenience; a failed touch must not block navigation */
    }
  }

  // --- transfers ------------------------------------------------------------

  async function download(path) {
    const serverId = activeServerId();
    if (!serverId) return;
    const name = path.replace(/\/+$/, "").split("/").pop() || "download";
    toast(`Downloading ${name}…`);
    try {
      const response = await request(`/api/servers/${encodeURIComponent(serverId)}/download`, {
        method: "POST",
        json: { path },
      });
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = name;
      document.body.append(link);
      link.click();
      link.remove();
      // Revoke on the next tick; revoking synchronously races the download.
      setTimeout(() => URL.revokeObjectURL(url), 0);
      toast(`Downloaded ${name}`);
    } catch (error) {
      toast(error.message, true);
    }
  }

  async function upload(file) {
    const serverId = activeServerId();
    const directory = currentPath();
    if (!serverId || !file) return;
    const target = `${directory.replace(/\/+$/, "")}/${file.name}`;
    toast(`Uploading ${file.name}…`);
    try {
      const body = new FormData();
      body.append("file", file);
      body.append("path", target);
      await request(`/api/servers/${encodeURIComponent(serverId)}/upload`, { method: "POST", body });
      toast(`Uploaded ${file.name}`);
      document.querySelector("#fileRefreshButton")?.click();
    } catch (error) {
      toast(error.message, true);
    }
  }

  function decorateRows() {
    document.querySelectorAll("#fileRows tr").forEach((row) => {
      const copy = row.querySelector(".copy-file-reference");
      if (!copy || row.querySelector(".ws-download")) return;
      // Only regular files are downloadable; directories render an open button.
      if (row.querySelector(".open-directory")) return;
      const path = copy.dataset.path;
      if (!path) return;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "row-icon-button ws-download";
      button.dataset.wsDownload = path;
      button.title = "Download this file";
      button.setAttribute("aria-label", `Download ${path.split("/").pop()}`);
      button.innerHTML =
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v10"/><path d="M8 12l4 4 4-4"/><path d="M5 19h14"/></svg>';
      copy.parentElement.insertBefore(button, copy);
    });
  }

  function decorateCommandBar() {
    const actions = document.querySelector(".file-command-actions");
    if (!actions || actions.querySelector(".ws-upload")) return;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "toolbar-button text-toolbar-button ws-upload";
    button.innerHTML =
      '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20V10"/><path d="M8 12l4-4 4 4"/><path d="M5 5h14"/></svg><span>Upload</span>';
    actions.append(button);
  }

  function decorate() {
    decorateCommandBar();
    decorateRows();
    refreshPaths();
  }

  document.addEventListener("click", (event) => {
    const go = event.target.closest("[data-ws-go]");
    const forget = event.target.closest("[data-ws-forget]");
    const downloadButton = event.target.closest("[data-ws-download]");
    if (forget) {
      event.preventDefault();
      event.stopPropagation();
      forgetDirectory(forget.dataset.wsForget);
      return;
    }
    if (go) {
      goToSaved(go.dataset.wsGo, go.dataset.wsId);
      return;
    }
    if (downloadButton) {
      download(downloadButton.dataset.wsDownload);
      return;
    }
    if (event.target.closest(".ws-save")) saveCurrentDirectory();
    if (event.target.closest(".ws-upload")) document.querySelector("#wsUploadInput")?.click();
  });

  document.addEventListener("DOMContentLoaded", () => {
    const input = document.createElement("input");
    input.type = "file";
    input.id = "wsUploadInput";
    input.hidden = true;
    input.addEventListener("change", () => {
      const [file] = input.files || [];
      if (file) upload(file);
      input.value = "";
    });
    document.body.append(input);

    decorate();
    const rows = document.querySelector("#fileRows");
    if (rows) new MutationObserver(decorate).observe(rows, { childList: true });
    const breadcrumbs = document.querySelector("#fileBreadcrumbs");
    // The breadcrumbs re-render on every navigation, which is the cheapest
    // signal that the current directory changed.
    if (breadcrumbs) new MutationObserver(renderPaths).observe(breadcrumbs, { childList: true });
    document.querySelector("#fileServerSelect")?.addEventListener("change", () => refreshPaths(true));
  });
})();
