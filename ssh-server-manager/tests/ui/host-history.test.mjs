import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";
import { test } from "node:test";
import { JSDOM } from "jsdom";

const source = await readFile(new URL("../../scripts/ssh_server_manager/assets/ui/hosts.js", import.meta.url), "utf8");
const servers = [
  { id: "a", alias: "alpha", username: "deploy", hostname: "alpha.invalid", port: 22, tags: [], proxy_jumps: [], notes: "Alpha notes" },
  { id: "b", alias: "beta", username: "ops", hostname: "beta.invalid", port: 2222, tags: [], proxy_jumps: [], notes: "Beta notes" },
];
const pause = () => new Promise((resolve) => setTimeout(resolve, 10));
async function until(predicate) {
  for (let i = 0; i < 100; i += 1) { if (predicate()) return; await pause(); }
  throw new Error("History condition timed out");
}

async function start(t, { hash = "", data = servers, previous = false } = {}) {
  const dom = new JSDOM('<button data-view="servers">Hosts</button><table><tbody id="serverRows"><tr><td><button data-host-details="a">Alpha</button><button data-host-details="b">Beta</button></td></tr></tbody></table>', {
    url: `http://localhost:8765/${hash}`, runScripts: "outside-only", pretendToBeVisual: true,
  });
  t.after(() => dom.window.close());
  const w = dom.window, doc = w.document;
  if (previous) {
    w.history.replaceState({ previous: true }, "", "/previous");
    w.history.pushState({ app: true }, "", "/");
  }
  let current = { servers: structuredClone(data), credentials: [] };
  const actions = [], copied = [], errors = [];
  w.addEventListener("error", (event) => errors.push(event.message));
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () {
    this.open = false;
    // Browser native close events are queued, including after a rapid reopen.
    w.setTimeout(() => this.dispatchEvent(new w.Event("close")), 0);
  };
  w.HTMLElement.prototype.getClientRects = function () { return this.hidden ? [] : [{}]; };
  Object.defineProperty(w.navigator, "clipboard", { value: { writeText: async (value) => copied.push(value) } });
  w.fetch = () => { throw new Error("Host details must not fetch"); };
  const options = {
    getState: () => current,
    serverStatus: () => ({ tone: "neutral", short: "Not checked", meta: "—" }),
    toast: () => {},
    openServer: (host) => actions.push(["edit", host.id]),
    openBrowse: (id) => actions.push(["browse", id]),
  };
  const mod = new vm.SourceTextModule(source, { context: dom.getInternalVMContext() });
  await mod.link(() => { throw new Error("No module imports expected"); });
  await mod.evaluate();
  const api = mod.namespace;
  api.syncHostDetails(options);
  const dialog = () => doc.querySelector(".host-details-dialog");
  const field = (name) => doc.querySelector(`[data-hd="${name}"]`);
  const open = (id) => api.openHostDetails(id, doc.querySelector(`[data-host-details="${id}"]`));
  const close = () => field("close").click();
  const closed = () => !dialog()?.open && w.location.hash === "";
  const refresh = (newServers) => { current = { ...current, servers: newServers }; api.syncHostDetails(options); };
  return { w, doc, options, api, open, close, closed, dialog, field, refresh, actions, copied, errors };
}

test("repeated open/close cycles reuse one history slot and Back reaches the previous page", async (t) => {
  const x = await start(t, { previous: true });
  assert.equal(x.w.history.length, 2);
  for (const id of ["a", "b", "a", "b", "a"]) {
    x.open(id);
    assert.equal(x.dialog().open, true);
    x.close();
    assert.equal(x.dialog().open, false, "dialog closes immediately");
    await until(x.closed);
    assert.equal(x.w.history.length, 3, "one reusable Forward detail entry, no duplicate roots");
    assert.equal(x.w.history.state.app, true, "existing base state is preserved");
  }
  x.w.history.back();
  await until(() => x.w.location.pathname === "/previous");
  x.w.history.forward();
  await until(() => x.w.location.pathname === "/");
  assert.equal(x.dialog().open, false);
  x.w.history.forward();
  await until(() => x.dialog().open && x.w.location.hash === "#/hosts/alpha");
  assert.deepEqual(x.errors, []);
});

test("Back and Forward restore details, while Escape returns to the owned base entry", async (t) => {
  const x = await start(t);
  x.open("a");
  x.w.history.back();
  await until(x.closed);
  x.w.history.forward();
  await until(() => x.dialog().open);
  assert.equal(x.field("title").textContent, "alpha");
  x.dialog().dispatchEvent(new x.w.Event("cancel", { cancelable: true }));
  await until(x.closed);
  x.w.history.forward();
  await until(() => x.dialog().open);
  assert.equal(x.field("title").textContent, "alpha");
});

test("a direct host deep link closes by replacing its own entry", async (t) => {
  const x = await start(t, { hash: "#/hosts/alpha" });
  assert.equal(x.dialog().open, true);
  assert.equal(x.w.history.length, 1);
  x.close();
  assert.equal(x.w.location.hash, "");
  assert.equal(x.w.history.length, 1);
  x.api.syncHostDetails(x.options);
  await pause();
  assert.equal(x.dialog().open, false);
  assert.equal(x.w.history.state?.ssmHostDetails, undefined);
});

test("rapid close and reopen queues the latest host until owned Back completes", async (t) => {
  const x = await start(t, { previous: true });
  x.open("a");
  x.close();
  x.api.syncHostDetails(x.options);
  assert.equal(x.dialog().open, false, "refresh cannot reopen a route awaiting dismissal");
  x.open("a");
  x.open("b");
  assert.equal(x.dialog().open, false, "reopening waits for the pending traversal");
  await until(() => x.dialog().open && x.w.location.hash === "#/hosts/beta");
  await pause();
  assert.equal(x.field("title").textContent, "beta", "stale hashchange/close cannot reopen alpha");
  assert.equal(x.w.history.length, 3);
  x.w.history.back();
  await until(x.closed);
  x.w.history.forward();
  await until(() => x.dialog().open);
  assert.equal(x.field("title").textContent, "beta");
  assert.deepEqual(x.errors, []);
});

test("missing deep-link host becomes available on refresh without replacing focus", async (t) => {
  const x = await start(t, { hash: "#/hosts/alpha", data: [] });
  assert.equal(x.field("unavailable").hidden, false);
  x.field("close").focus();
  x.refresh(servers);
  assert.equal(x.field("unavailable").hidden, true);
  assert.equal(x.field("title").textContent, "alpha");
  assert.equal(x.doc.activeElement, x.field("close"));
  x.field("copy").click();
  await until(() => x.copied.length === 1);
  assert.equal(x.copied[0], "serverctl connect alpha");
  x.refresh([]);
  assert.equal(x.field("actions").hidden, true);
  x.refresh([{ ...servers[0], id: "replacement" }]);
  assert.equal(x.field("actions").hidden, false, "same alias with a replacement id also recovers");
  x.field("edit").click();
  assert.deepEqual(x.actions, [["edit", "replacement"]]);
});

test("edit and browse remain closed during the asynchronous route cleanup", async (t) => {
  const x = await start(t);
  x.open("a");
  x.field("edit").click();
  x.api.syncHostDetails(x.options);
  assert.equal(x.dialog().open, false);
  assert.deepEqual(x.actions, [["edit", "a"]]);
  await until(x.closed);
  await pause();
  assert.equal(x.dialog().open, false);
  x.open("b");
  x.field("browse").click();
  x.api.syncHostDetails(x.options);
  await until(x.closed);
  assert.deepEqual(x.actions, [["edit", "a"], ["browse", "b"]]);
});
