const state = { csrf: "", servers: [], credentials: [], auth: {} };
let toastTimer;
let revealTimer;
let masterResolver = null;
let masterRejecter = null;

const $ = (selector) => document.querySelector(selector);

function toast(message, error = false) {
  const node = $("#toast");
  node.textContent = message;
  node.className = error ? "show error" : "show";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.className = ""; }, 4500);
}

async function api(path, options = {}) {
  const method = options.method || "GET";
  const headers = { ...(options.headers || {}) };
  if (options.body && typeof options.body !== "string") {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.body);
  }
  if (method !== "GET") headers["X-CSRF-Token"] = state.csrf;
  const response = await fetch(path, { ...options, method, headers, credentials: "same-origin" });
  const type = response.headers.get("content-type") || "";
  const data = type.includes("json") ? await response.json() : await response.text();
  if (!response.ok) {
    const message = data.message || data.detail || data.error || `Request failed (${response.status})`;
    throw new Error(message);
  }
  return data;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
}

function statusMarkup(server) {
  if (!server.last_test_status) return '<span class="subtle">Not tested</span>';
  const ok = server.last_test_status === "ok";
  const detail = server.last_test_latency_ms == null ? "" : ` · ${server.last_test_latency_ms} ms`;
  return `<span class="status-pill ${ok ? "ok" : "failed"}">${ok ? "Online" : escapeHtml(server.last_test_error_code || "Failed")}${detail}</span>`;
}

function render() {
  $("#serverCount").textContent = state.servers.length;
  $("#credentialCount").textContent = state.credentials.length;
  $("#emptyServers").hidden = state.servers.length > 0;
  $("#emptyCredentials").hidden = state.credentials.length > 0;

  $("#serverRows").innerHTML = state.servers.map((server) => `
    <tr>
      <td><span class="alias">${escapeHtml(server.alias)}</span><br><span class="subtle">${escapeHtml(server.notes || "")}</span></td>
      <td><code>${escapeHtml(server.username)}@${escapeHtml(server.hostname)}:${server.port}</code></td>
      <td>${escapeHtml(server.credential_label || "OpenSSH default")}</td>
      <td>${escapeHtml(server.proxy_jumps.join(", ") || "—")}</td>
      <td>${statusMarkup(server)}</td>
      <td>
        <button class="small secondary test-server" data-id="${server.id}">Test</button>
        <button class="small secondary copy-command" data-alias="${escapeHtml(server.alias)}">Copy command</button>
        <button class="small secondary edit-server" data-id="${server.id}">Edit</button>
        <button class="small danger delete-server" data-id="${server.id}">Delete</button>
      </td>
    </tr>`).join("");

  $("#credentialRows").innerHTML = state.credentials.map((credential) => {
    const secretState = credential.kind === "password"
      ? (credential.has_secret ? "Password stored" : "Missing password")
      : credential.kind === "key"
        ? (credential.has_passphrase ? "Passphrase stored" : "No saved passphrase")
        : "No stored secret";
    const revealable = credential.has_secret || credential.has_passphrase;
    return `<tr>
      <td><span class="alias">${escapeHtml(credential.label)}</span></td>
      <td>${escapeHtml(credential.kind)}</td>
      <td>${secretState}</td>
      <td><code>${escapeHtml(credential.key_path || "—")}</code></td>
      <td>
        ${revealable ? `<button class="small secondary reveal-credential" data-id="${credential.id}">Reveal</button>` : ""}
        <button class="small secondary edit-credential" data-id="${credential.id}">Edit</button>
        <button class="small danger delete-credential" data-id="${credential.id}">Delete</button>
      </td>
    </tr>`;
  }).join("");

  const options = ['<option value="">OpenSSH default / agent</option>']
    .concat(state.credentials.map((item) => `<option value="${item.id}">${escapeHtml(item.label)} (${item.kind})</option>`));
  $("#serverCredential").innerHTML = options.join("");
  const authText = [];
  authText.push(`${state.auth.passkeys || 0} passkey${state.auth.passkeys === 1 ? "" : "s"} enrolled`);
  authText.push(state.auth.master_password_enrolled ? "fallback master password enrolled" : "no fallback master password");
  $("#authDescription").textContent = authText.join(" · ");
  $("#enrollPasskeyButton").disabled = !state.auth.webauthn_available || !window.PublicKeyCredential;
  $("#enrollMasterButton").disabled = state.auth.master_password_enrolled;
}

async function refresh() {
  const data = await api("/api/bootstrap");
  state.csrf = data.csrf;
  state.servers = data.servers;
  state.credentials = data.credentials;
  state.auth = data.auth;
  $("#configPath").textContent = data.managed_config;
  $("#systemStatus").textContent = "Local session active";
  $("#systemStatus").className = "status-pill ok";
  render();
}

function openServer(server = null) {
  $("#serverDialogTitle").textContent = server ? "Edit server" : "Add server";
  $("#serverId").value = server?.id || "";
  $("#serverAlias").value = server?.alias || "";
  $("#serverHostname").value = server?.hostname || "";
  $("#serverPort").value = server?.port || 22;
  $("#serverUsername").value = server?.username || "";
  $("#serverCredential").value = server?.credential_id || "";
  $("#serverProxy").value = server?.proxy_jumps.join(", ") || "";
  $("#serverNotes").value = server?.notes || "";
  hideServerCredentialComposer();
  $("#serverDialog").showModal();
}

function updateServerCredentialFields() {
  const kind = $("#serverCredentialNewKind").value;
  const password = kind === "password";
  const key = kind === "key";
  $("#serverCredentialNewSecretField").hidden = !password;
  $("#serverCredentialNewKeyPathField").hidden = !key;
  $("#serverCredentialNewPassphraseField").hidden = !key;
  $("#serverCredentialNewSecret").required = password;
  $("#serverCredentialNewKeyPath").required = key;
}

function clearServerCredentialComposer() {
  $("#serverCredentialNewLabel").value = "";
  $("#serverCredentialNewKind").value = "password";
  $("#serverCredentialNewSecret").value = "";
  $("#serverCredentialNewKeyPath").value = "";
  $("#serverCredentialNewPassphrase").value = "";
  updateServerCredentialFields();
}

function hideServerCredentialComposer() {
  $("#serverCredentialComposer").hidden = true;
  clearServerCredentialComposer();
}

function showServerCredentialComposer() {
  $("#serverCredentialComposer").hidden = false;
  updateServerCredentialFields();
  $("#serverCredentialNewLabel").focus();
}

async function createServerCredential() {
  const kind = $("#serverCredentialNewKind").value;
  const payload = { label: $("#serverCredentialNewLabel").value.trim(), kind };
  if (!payload.label) throw new Error("Credential label is required");
  if (kind === "password") {
    payload.secret = $("#serverCredentialNewSecret").value;
    if (!payload.secret) throw new Error("Password is required");
  }
  if (kind === "key") {
    payload.key_path = $("#serverCredentialNewKeyPath").value.trim();
    if (!payload.key_path) throw new Error("Private-key path is required");
    if ($("#serverCredentialNewPassphrase").value) payload.passphrase = $("#serverCredentialNewPassphrase").value;
  }
  const created = await api("/api/credentials", { method: "POST", body: payload });
  await refresh();
  $("#serverCredential").value = created.id;
  hideServerCredentialComposer();
  toast("Credential created and selected. Save the server to finish.");
}

function updateCredentialFields() {
  const kind = $("#credentialKind").value;
  $("#secretField").hidden = kind !== "password";
  $("#keyPathField").hidden = kind !== "key";
  $("#passphraseField").hidden = kind !== "key";
  $("#clearPassphraseField").hidden = kind !== "key" || !$("#credentialId").value;
}

function openCredential(credential = null) {
  const editing = Boolean(credential);
  $("#credentialDialogTitle").textContent = editing ? "Edit credential" : "Add credential";
  $("#credentialId").value = credential?.id || "";
  $("#credentialLabel").value = credential?.label || "";
  $("#credentialKind").value = credential?.kind || "password";
  $("#credentialKind").disabled = editing;
  $("#credentialSecret").value = "";
  $("#credentialSecret").required = !editing && $("#credentialKind").value === "password";
  $("#credentialKeyPath").value = credential?.key_path || "";
  $("#credentialPassphrase").value = "";
  $("#credentialClearPassphrase").checked = false;
  $("#credentialEditHint").hidden = !editing;
  updateCredentialFields();
  $("#credentialDialog").showModal();
}

function bytesFromBase64(value) {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
  const binary = atob(normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "="));
  return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}

function base64FromBytes(value) {
  const bytes = new Uint8Array(value);
  let binary = "";
  bytes.forEach((byte) => { binary += String.fromCharCode(byte); });
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function preparePublicKey(options) {
  options.challenge = bytesFromBase64(options.challenge);
  if (options.user?.id) options.user.id = bytesFromBase64(options.user.id);
  if (options.excludeCredentials) options.excludeCredentials.forEach((item) => { item.id = bytesFromBase64(item.id); });
  if (options.allowCredentials) options.allowCredentials.forEach((item) => { item.id = bytesFromBase64(item.id); });
  return options;
}

function serializeCredential(credential) {
  const response = {};
  for (const key of ["clientDataJSON", "attestationObject", "authenticatorData", "signature", "userHandle"]) {
    if (credential.response[key]) response[key] = base64FromBytes(credential.response[key]);
  }
  if (credential.response.getTransports) response.transports = credential.response.getTransports();
  return { id: credential.id, rawId: base64FromBytes(credential.rawId), type: credential.type, response };
}

async function enrollPasskey() {
  const options = preparePublicKey(await api("/api/auth/passkey/register/options", { method: "POST" }));
  const credential = await navigator.credentials.create({ publicKey: options });
  await api("/api/auth/passkey/register/verify", { method: "POST", body: serializeCredential(credential) });
  toast("Passkey enrolled");
  await refresh();
}

async function authorizeReveal(credentialId) {
  if (state.auth.passkeys > 0 && window.PublicKeyCredential) {
    try {
      const options = preparePublicKey(await api("/api/auth/reveal/options", { method: "POST", body: { credential_id: credentialId } }));
      const assertion = await navigator.credentials.get({ publicKey: options });
      await api("/api/auth/reveal/verify", {
        method: "POST",
        body: { credential_id: credentialId, response: serializeCredential(assertion) },
      });
      return;
    } catch (error) {
      if (!state.auth.master_password_enrolled) throw error;
      toast("Passkey was unavailable; use the fallback master password", true);
    }
  }
  if (!state.auth.master_password_enrolled) throw new Error("Enroll a passkey or fallback master password first");
  const password = await requestMasterPassword(false);
  await api("/api/auth/master/verify", { method: "POST", body: { credential_id: credentialId, password } });
}

function requestMasterPassword(enrolling) {
  return new Promise((resolve, reject) => {
    masterResolver = resolve;
    masterRejecter = reject;
    $("#masterDialogTitle").textContent = enrolling ? "Set fallback master password" : "Authenticate to reveal";
    $("#masterConfirmField").hidden = !enrolling;
    $("#masterPasswordConfirm").required = enrolling;
    $("#masterPassword").autocomplete = enrolling ? "new-password" : "current-password";
    $("#masterPassword").value = "";
    $("#masterPasswordConfirm").value = "";
    $("#masterDialog").dataset.enrolling = enrolling ? "true" : "false";
    $("#masterDialog").showModal();
    $("#masterPassword").focus();
  });
}

function cancelMaster() {
  if ($("#masterDialog").open) $("#masterDialog").close();
  if (masterRejecter) masterRejecter(new Error("Authentication cancelled"));
  masterResolver = null;
  masterRejecter = null;
  $("#masterPassword").value = "";
  $("#masterPasswordConfirm").value = "";
}

function showSecret(value) {
  clearInterval(revealTimer);
  let seconds = 15;
  $("#revealedSecret").textContent = value;
  $("#revealCountdown").textContent = seconds;
  $("#revealDialog").showModal();
  revealTimer = setInterval(() => {
    seconds -= 1;
    $("#revealCountdown").textContent = seconds;
    if (seconds <= 0) closeReveal();
  }, 1000);
}

function closeReveal() {
  clearInterval(revealTimer);
  $("#revealedSecret").textContent = "";
  if ($("#revealDialog").open) $("#revealDialog").close();
}

async function guarded(action) {
  try { await action(); }
  catch (error) { toast(error.message || String(error), true); }
}

document.addEventListener("DOMContentLoaded", () => {
  history.replaceState({}, "", "/");
  guarded(refresh);

  $("#refreshButton").addEventListener("click", () => guarded(refresh));
  $("#addServerButton").addEventListener("click", () => openServer());
  $("#addCredentialButton").addEventListener("click", () => openCredential());
  $("#newServerCredentialButton").addEventListener("click", showServerCredentialComposer);
  $("#cancelServerCredentialButton").addEventListener("click", hideServerCredentialComposer);
  $("#serverCredentialNewKind").addEventListener("change", updateServerCredentialFields);
  $("#saveServerCredentialButton").addEventListener("click", () => guarded(createServerCredential));
  $("#credentialKind").addEventListener("change", () => {
    updateCredentialFields();
    $("#credentialSecret").required = !$("#credentialId").value && $("#credentialKind").value === "password";
  });
  document.querySelectorAll(".close-dialog").forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));
  document.querySelectorAll(".close-reveal").forEach((button) => button.addEventListener("click", closeReveal));
  document.querySelectorAll(".cancel-master").forEach((button) => button.addEventListener("click", cancelMaster));
  $("#masterDialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    cancelMaster();
  });
  $("#masterForm").addEventListener("submit", (event) => {
    event.preventDefault();
    const password = $("#masterPassword").value;
    const enrolling = $("#masterDialog").dataset.enrolling === "true";
    if (enrolling && password !== $("#masterPasswordConfirm").value) {
      toast("Master passwords do not match", true);
      return;
    }
    $("#masterDialog").close();
    $("#masterPassword").value = "";
    $("#masterPasswordConfirm").value = "";
    const resolve = masterResolver;
    masterResolver = null;
    masterRejecter = null;
    resolve(password);
  });

  $("#serverForm").addEventListener("submit", (event) => {
    event.preventDefault();
    guarded(async () => {
      const id = $("#serverId").value;
      const payload = {
        alias: $("#serverAlias").value,
        hostname: $("#serverHostname").value,
        port: Number($("#serverPort").value),
        username: $("#serverUsername").value,
        credential_id: $("#serverCredential").value || null,
        proxy_jumps: $("#serverProxy").value.split(",").map((item) => item.trim()).filter(Boolean),
        notes: $("#serverNotes").value,
      };
      await api(id ? `/api/servers/${id}` : "/api/servers", { method: id ? "PUT" : "POST", body: payload });
      $("#serverDialog").close();
      toast(id ? "Server updated" : "Server added");
      await refresh();
    });
  });

  $("#credentialForm").addEventListener("submit", (event) => {
    event.preventDefault();
    guarded(async () => {
      const id = $("#credentialId").value;
      const kind = $("#credentialKind").value;
      const payload = { label: $("#credentialLabel").value, kind };
      if (kind === "password" && $("#credentialSecret").value) payload.secret = $("#credentialSecret").value;
      if (kind === "key") {
        payload.key_path = $("#credentialKeyPath").value;
        if ($("#credentialPassphrase").value) payload.passphrase = $("#credentialPassphrase").value;
        if (id && $("#credentialClearPassphrase").checked) payload.clear_passphrase = true;
      }
      await api(id ? `/api/credentials/${id}` : "/api/credentials", { method: id ? "PUT" : "POST", body: payload });
      $("#credentialSecret").value = "";
      $("#credentialPassphrase").value = "";
      $("#credentialDialog").close();
      toast(id ? "Credential updated" : "Credential added");
      await refresh();
    });
  });

  $("#serverRows").addEventListener("click", (event) => guarded(async () => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.classList.contains("test-server")) {
      button.disabled = true;
      const result = await api(`/api/servers/${button.dataset.id}/test`, { method: "POST" });
      toast(result.ok ? `Connected in ${result.latency_ms} ms` : result.message, !result.ok);
      await refresh();
    } else if (button.classList.contains("copy-command")) {
      await navigator.clipboard.writeText(`serverctl connect ${button.dataset.alias}`);
      toast("Connection command copied");
    } else if (button.classList.contains("edit-server")) {
      openServer(state.servers.find((item) => item.id === button.dataset.id));
    } else if (button.classList.contains("delete-server")) {
      const server = state.servers.find((item) => item.id === button.dataset.id);
      if (confirm(`Delete server ${server.alias}?`)) {
        await api(`/api/servers/${button.dataset.id}`, { method: "DELETE" });
        toast("Server deleted");
        await refresh();
      }
    }
  }));

  $("#credentialRows").addEventListener("click", (event) => guarded(async () => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.classList.contains("reveal-credential")) {
      await authorizeReveal(button.dataset.id);
      const result = await api(`/api/credentials/${button.dataset.id}/reveal`, { method: "POST" });
      showSecret(result.value);
    } else if (button.classList.contains("edit-credential")) {
      openCredential(state.credentials.find((item) => item.id === button.dataset.id));
    } else if (button.classList.contains("delete-credential")) {
      const credential = state.credentials.find((item) => item.id === button.dataset.id);
      if (confirm(`Delete credential ${credential.label}? Stored secrets will also be removed.`)) {
        await api(`/api/credentials/${button.dataset.id}`, { method: "DELETE" });
        toast("Credential deleted");
        await refresh();
      }
    }
  }));

  $("#importButton").addEventListener("click", () => guarded(async () => {
    const preview = await api("/api/import/preview", { method: "POST", body: {} });
    const adds = preview.items.filter((item) => item.action === "add").length;
    const conflicts = preview.items.filter((item) => item.action === "conflict").length;
    const message = `Import ${adds} new host(s)? ${conflicts} conflict(s) will be skipped.`;
    if (confirm(message)) {
      const result = await api("/api/import/apply", { method: "POST", body: { overwrite: false } });
      toast(`Imported ${result.added.length} host(s); skipped ${result.skipped.length}`);
      await refresh();
    }
  }));

  $("#enrollPasskeyButton").addEventListener("click", () => guarded(enrollPasskey));
  $("#enrollMasterButton").addEventListener("click", () => guarded(async () => {
    const password = await requestMasterPassword(true);
    await api("/api/auth/master/enroll", { method: "POST", body: { password } });
    toast("Fallback master password enrolled");
    await refresh();
  }));
  $("#copySecretButton").addEventListener("click", () => guarded(async () => {
    await navigator.clipboard.writeText($("#revealedSecret").textContent);
    toast("Secret copied to clipboard");
  }));
});
