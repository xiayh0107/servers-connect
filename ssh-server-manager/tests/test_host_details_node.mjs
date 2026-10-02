// Dependency-free behavioral checks: node tests/test_host_details_node.mjs
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const source = await readFile(new URL("../scripts/ssh_server_manager/assets/ui/hosts.js", import.meta.url), "utf8");
const events = new Map();
const closeQueue = [];
const nodes = [];
let detailFields = {};
let focused = null;
let rows = [];
const toasts = [];
const actions = [];
const historyCalls = [];
let clipboardCalls = [];
let clipboardWrite = () => Promise.resolve();

class Element {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase();
    this.dataset = {};
    this.listeners = new Map();
    this.children = [];
    this.attributes = {};
    this.className = "";
    this.classList = { toggle() {} };
    this.textContent = "";
    this.value = "";
    this.hidden = false;
    this.isConnected = true;
    this.open = false;
    this.scrollTop = 0;
    nodes.push(this);
  }
  append(child) { this.children.push(child); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, callback) {
    const list = this.listeners.get(name) || [];
    list.push(callback);
    this.listeners.set(name, list);
  }
  dispatch(name, extra = {}) {
    const event = { target: this, preventDefault() {}, ...extra };
    return Promise.all((this.listeners.get(name) || []).map((callback) => callback(event)));
  }
  getClientRects() { return this.isConnected && !this.hidden ? [{}] : []; }
  getBoundingClientRect() { return { left: 100, right: 580, top: 0, bottom: 800 }; }
  focus() { focused = this; }
  select() { this.selected = true; }
  showModal() { this.open = true; }
  close() { this.open = false; closeQueue.push(() => this.dispatch("close")); }
  set innerHTML(value) {
    this.html = value;
    detailFields = {};
    for (const match of value.matchAll(/<([\w-]+)[^>]*data-hd="([^"]+)"[^>]*>/g)) {
      const child = new Element(match[1]);
      child.dataset.hd = match[2];
      child.hidden = /\bhidden\b/.test(match[0]);
      detailFields[match[2]] = child;
      this.children.push(child);
    }
  }
  querySelectorAll(selector) { return selector === "[data-hd]" ? Object.values(detailFields) : []; }
}

const navigation = new Element("button");
const body = new Element("body");
const head = new Element("head");
globalThis.document = {
  body, head,
  createElement: (tag) => new Element(tag),
  querySelector(selector) {
    if (selector === 'link[href="/assets/hosts.css"]') return head.children.find((node) => node.href === "/assets/hosts.css");
    if (selector === '[data-view="servers"]') return navigation;
    return null;
  },
  querySelectorAll: (selector) => selector === "#serverRows button[data-host-details]" ? rows.filter((row) => row.isConnected) : [],
};
globalThis.window = {
  location: { hash: "" },
  history: {
    state: { app: true },
    pushState(value, title, path) { this.state = value; historyCalls.push(["push", path]); window.location.hash = path === "/" ? "" : path; },
    replaceState(value, title, path) { this.state = value; historyCalls.push(["replace", path]); window.location.hash = path === "/" ? "" : path; },
    back() {
      this.state = {};
      historyCalls.push(["back", "/"]);
      window.location.hash = "";
      (events.get("popstate") || []).forEach((callback) => callback({ type: "popstate" }));
    },
  },
  addEventListener(name, callback) {
    const list = events.get(name) || [];
    list.push(callback);
    events.set(name, list);
  },
};
Object.defineProperty(globalThis, "navigator", { configurable: true, value: {
  clipboard: { writeText(value) { clipboardCalls.push(value); return clipboardWrite(value); } },
} });
const server = {
  id: "id-1", alias: "prod-01", username: "deploy", hostname: "2001:db8::1", port: 2222,
  proxy_jumps: ["bastion-01", "jump-02"], credential_id: "credential-1",
  tags: ["production", "<script>alert(1)</script>"], notes: "Line one\n<em>Literal notes</em>\nFinal line",
  last_test_at: "2026-10-02T04:00:00Z", last_test_status: "ok",
};
let appState = {
  servers: [server],
  credentials: [{ id: "credential-1", label: "Production key", kind: "key", key_path: "PRIVATE-KEY-PATH", secret: "PRIVATE-SECRET" }],
};
let status = { tone: "stale", short: "Last reachable", meta: "3m ago", title: "Saved result", label: "Last reachable 3m ago" };
const options = {
  getState: () => appState,
  serverStatus: () => status,
  toast: (...args) => toasts.push(args),
  openServer: (host) => actions.push(["edit", host.id, detail().open]),
  openBrowse: (id) => actions.push(["browse", id, detail().open]),
};
const module = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
const detail = () => body.children.find((node) => node.tagName === "DIALOG");
const makeTrigger = (id = "id-1") => {
  const node = new Element("button");
  node.dataset.hostDetails = id;
  rows.push(node);
  return node;
};
const route = async (hash, event = "popstate") => {
  window.location.hash = hash;
  window.history.state = {};
  await Promise.all((events.get(event) || []).map((callback) => callback({ type: event })));
};
const settleClose = async () => { for (const callback of closeQueue.splice(0)) await callback(); };
const click = (field) => detailFields[field].dispatch("click");

module.syncHostDetails(options);
module.syncHostDetails(options);
assert.equal(head.children.length, 1, "stylesheet initializes once");
assert.equal(body.children.length, 0, "no dialog before requested");
assert.equal(events.get("popstate").length, 1);
assert.equal(events.get("hashchange").length, 1);

const trigger = makeTrigger();
module.openHostDetails(server.id, trigger);
assert.equal(detail().open, true);
assert.equal(historyCalls.at(-1)[0], "push");
assert.equal(window.location.hash, "#/hosts/prod-01");
assert.equal(detailFields.title.textContent, "prod-01");
assert.equal(focused, detailFields.title);
assert.equal(detailFields.hostname.textContent, "2001:db8::1");
assert.equal(detailFields.port.textContent, "2222");
assert.equal(detailFields.route.textContent, "bastion-01 → jump-02");
assert.equal(detailFields.credential.textContent, "Production key");
assert.equal(detailFields.credentialKind.textContent, "SSH key");
assert.equal(detailFields.notes.textContent, server.notes, "notes remain literal text");
assert.equal(detailFields.tags.children[1].textContent, server.tags[1]);
assert.equal(detailFields.command.value, "serverctl connect prod-01");
assert.equal(detailFields.status.className, "hd-status stale");
assert.match(detailFields.checked.textContent, /^Last checked /);
assert(!JSON.stringify(nodes).includes("PRIVATE-KEY-PATH"));
assert(!JSON.stringify(nodes).includes("PRIVATE-SECRET"));

detailFields.command.focus();
detailFields.scroll.scrollTop = 123;
const commandNode = detailFields.command;
status = { ...status, meta: "4m ago" };
module.syncHostDetails(options);
assert.equal(focused, commandNode, "refresh keeps focus");
assert.equal(detailFields.command, commandNode, "refresh preserves selectable input");
assert.equal(detailFields.scroll.scrollTop, 123);
assert.equal(detailFields.statusMeta.textContent, "4m ago");
module.openHostDetails(server.id, trigger);
assert.equal(historyCalls.at(-1)[0], "replace", "opening while open does not push duplicate history");

let resolveClipboard;
clipboardWrite = () => new Promise((resolve) => { resolveClipboard = resolve; });
const copying = click("copy");
assert.equal(detailFields.copy.disabled, true);
assert.equal(detailFields.copyLabel.textContent, "Copying…");
await click("copy");
assert.equal(clipboardCalls.length, 1, "pending copy cannot be duplicated");
module.syncHostDetails(options);
assert.equal(detailFields.copy.disabled, true, "refresh preserves copy pending");
resolveClipboard();
await copying;
assert.equal(detailFields.copy.disabled, false);
assert.deepEqual(toasts.at(-1), ["SSH command copied"]);

clipboardWrite = () => Promise.reject(new Error("Denied"));
await click("copy");
assert.equal(detailFields.error.hidden, false);
assert.equal(detailFields.copyLabel.textContent, "Try copying again");
clipboardWrite = () => Promise.resolve();
await click("copy");
assert.equal(detailFields.error.hidden, true);
assert.equal(detailFields.copyLabel.textContent, "Copy SSH command");
assert.equal(clipboardCalls.at(-1), "serverctl connect prod-01");

trigger.isConnected = false;
const replacement = makeTrigger();
await click("close");
assert.equal(detail().open, false);
assert.equal(historyCalls.at(-1)[1], "/");
assert.equal(focused, replacement, "close restores rerendered row trigger");
module.syncHostDetails(options);
assert.equal(detail().open, false, "refresh after explicit close does not reopen");
await settleClose();

module.openHostDetails(server.id, replacement);
await route("");
assert.equal(detail().open, false, "Back dismisses");
await route("#/hosts/prod-01");
assert.equal(detail().open, true, "Forward restores details");
await settleClose();
assert.equal(detail().open, true, "stale native close does not close reopened dialog");
await detail().dispatch("cancel");
assert.equal(detail().open, false, "Escape dismisses");
assert.equal(focused, replacement);

await route("#/hosts/missing-alias");
assert.equal(detailFields.unavailable.hidden, false);
assert.equal(detailFields.content.hidden, true);
assert.equal(detailFields.actions.hidden, true);
await click("close");
for (const invalid of ["#/hosts/%3Cscript%3E", "#/hosts/../x", "#/hosts/%E0%A4%A", "#/hosts/-option", `#/hosts/${"a".repeat(64)}`]) {
  await route(invalid);
  assert.equal(detail().open, false, `invalid route ${invalid} must not open`);
}

await route("#/hosts/prod-01");
appState = { ...appState, servers: [] };
module.syncHostDetails(options);
assert.equal(detailFields.unavailable.hidden, false, "deleted host becomes unavailable");
assert.equal(detailFields.actions.hidden, true);
await click("copy");
assert.equal(detail().open, true);
await click("close");
appState = { ...appState, servers: [server] };

module.openHostDetails(server.id, replacement);
clipboardWrite = () => new Promise((resolve) => { resolveClipboard = resolve; });
const lateCopy = click("copy");
const toastCount = toasts.length;
await click("close");
module.openHostDetails(server.id, replacement);
resolveClipboard();
await lateCopy;
assert.equal(toasts.length, toastCount, "stale copy does not report success in another open");
assert.equal(detailFields.copy.disabled, false);
await settleClose();
assert.equal(detail().open, true);

appState = { ...appState, servers: [{ ...server, alias: "renamed-host", notes: "Updated notes" }] };
module.syncHostDetails(options);
assert.equal(window.location.hash, "#/hosts/renamed-host", "rename updates route by stable id");
assert.equal(detailFields.command.value, "serverctl connect renamed-host");
assert.equal(detailFields.notes.textContent, "Updated notes");
await click("edit");
assert.deepEqual(actions.at(-1), ["edit", server.id, false], "edit closes before app callback");
assert.equal(window.location.hash, "");
module.syncHostDetails({ ...options, openBrowse: (id) => actions.push(["new-browse", id, detail().open]) });
module.openHostDetails(server.id, replacement);
await click("browse");
assert.deepEqual(actions.at(-1), ["new-browse", server.id, false], "latest options used, browse closes first");
assert.equal(window.location.hash, "");

await route("#/hosts/renamed-host", "hashchange");
assert.equal(detail().open, true);
await detail().dispatch("click", { clientX: 20, clientY: 200 });
assert.equal(detail().open, false, "backdrop click dismisses");
rows.forEach((row) => { row.isConnected = false; });
module.openHostDetails(server.id);
await click("close");
assert.equal(focused, navigation, "removed trigger has a safe navigation focus fallback");
assert(!/\bfetch\s*\(|XMLHttpRequest|WebSocket/.test(source), "details must not issue network requests");
console.log("Host detail behavior checks passed (routing, clipboard, refresh, safety, focus, actions)");
