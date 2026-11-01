// Pruebas de extension/sw.js con un `chrome` y un `fetch` falsos (node --test).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

import { fileURLToPath } from "node:url";
const SW_PATH = fileURLToPath(new URL("../extension/sw.js", import.meta.url));
const SRC = readFileSync(SW_PATH, "utf8");
const tick = (ms = 20) => new Promise((ok) => setTimeout(ok, ms));

// Carga sw.js en un contexto nuevo. `polls` es la cola de respuestas a /poll; cuando se vacía, el
// bucle se queda esperando sin hacer nada.
function load({ polls = [], tokens = ["tok"], debuggerFails = null, slow = false } = {}) {
  const posted = [], headers = [], listeners = {}, attached = [];
  let reloaded = false, tokenReads = 0;
  const ev = (name) => ({ addListener: (f) => (listeners[name] = f) });
  const chrome = {
    runtime: {
      getURL: (p) => "ext://" + p, getPlatformInfo: () => {}, reload: () => (reloaded = true),
      onStartup: ev("startup"), onInstalled: ev("installed"),
    },
    alarms: { create: () => {}, onAlarm: ev("alarm") },
    debugger: {
      onDetach: ev("detach"),
      attach: async ({ tabId }) => {
        if (debuggerFails) throw new Error(debuggerFails);
        attached.push(tabId);
      },
      detach: async () => {},
      sendCommand: async (_t, method, params) =>
        slow ? new Promise(() => {}) : { method, params },
    },
    tabs: {
      onRemoved: ev("removed"),
      query: async () => [{ id: 1, windowId: 2, active: true, title: "T", url: "u", extra: 1 }],
      create: async ({ url }) => ({ id: 9, url }),
      update: async () => {}, remove: async () => {},
    },
  };
  const pending = [...polls];
  const fetch = async (url, opts = {}) => {
    if (url.startsWith("ext://token.txt")) {
      tokenReads++;
      return { text: async () => (tokens[Math.min(tokenReads - 1, tokens.length - 1)] + "\n") };
    }
    headers.push(opts.headers?.["X-EB-Token"]);
    if (url.endsWith("/result")) {
      posted.push(JSON.parse(opts.body));
      return { ok: true };
    }
    const next = pending.shift();
    if (next === undefined) return new Promise(() => {}); // sin más órdenes: el bucle espera
    if (next instanceof Error) throw next;
    if (typeof next === "number") return { status: next, ok: next < 300, json: async () => ({}) };
    return { status: 200, ok: true, json: async () => next };
  };
  const timers = [];
  const ctx = {
    chrome, fetch, console,
    setTimeout: (f, ms) => (ms >= 3000 ? (timers.push(f), 0) : setTimeout(f, ms)),
  };
  vm.runInNewContext(SRC, ctx, { filename: SW_PATH });
  return { posted, headers, listeners, attached, get reloaded() { return reloaded; },
           get tokenReads() { return tokenReads; }, timers, chrome };
}

test("lista pestañas, crea, navega, cierra y suelta la depuración", async () => {
  const s = load({ polls: [
    { id: "a", op: "tabs" }, { id: "b", op: "create", url: "https://x" },
    { id: "c", op: "navigate", tab: 1, url: "y" }, { id: "d", op: "close", tab: 1 },
    { id: "e", op: "detach", tab: 1 },
  ] });
  await tick(80);
  const by = Object.fromEntries(s.posted.map((p) => [p.id, p]));
  assert.deepEqual(by.a.result, [{ id: 1, windowId: 2, active: true, title: "T", url: "u" }]);
  assert.deepEqual(by.b.result, { id: 9 });
  assert.deepEqual(by.c.result, { ok: true });
  assert.deepEqual(by.d.result, { ok: true });
  assert.deepEqual(by.e.result, { ok: true });
});

test("cada petición lleva el token", async () => {
  const s = load({ polls: [{ id: "a", op: "tabs" }] });
  await tick();
  assert.ok(s.headers.length >= 2);
  assert.ok(s.headers.every((h) => h === "tok"));
});

test("las órdenes de DevTools se adjuntan una vez por pestaña y devuelven el resultado", async () => {
  const s = load({ polls: [
    { id: "a", op: "cdp", tab: 5, method: "Runtime.evaluate", params: { x: 1 } },
    { id: "b", op: "cdp", tab: 5, method: "Page.reload" },
    { id: "c", op: "detach", tab: 5 },
  ] });
  await tick(80);
  assert.deepEqual(s.attached, [5]);
  const a = s.posted.find((p) => p.id === "a");
  assert.deepEqual(a.result, { method: "Runtime.evaluate", params: { x: 1 } });
  assert.deepEqual(s.posted.find((p) => p.id === "b").result.params, {});
  s.listeners.detach({ tabId: 5 });
  s.listeners.removed(5);
});

test("«Already attached» no es un error; otro fallo sí", async () => {
  let s = load({ polls: [{ id: "a", op: "cdp", tab: 3, method: "M" }], debuggerFails: "Already attached to tab" });
  await tick();
  assert.ok(s.posted[0].result);
  s = load({ polls: [{ id: "a", op: "cdp", tab: 3, method: "M" }], debuggerFails: "No se puede depurar" });
  await tick();
  assert.match(s.posted[0].error, /No se puede depurar/);
});

test("una orden desconocida devuelve el error", async () => {
  const s = load({ polls: [{ id: "a", op: "vuela" }] });
  await tick();
  assert.match(s.posted[0].error, /op desconocida/);
});

test("reload contesta y recarga la extensión", async () => {
  const s = load({ polls: [{ id: "a", op: "reload" }] });
  await tick(300);
  assert.deepEqual(s.posted[0].result, { ok: true });
  assert.ok(s.reloaded);
});

test("una orden colgada no bloquea las siguientes", async () => {
  const s = load({ polls: [{ id: "lenta", op: "cdp", tab: 1, method: "Page.captureScreenshot" },
                           { id: "rapida", op: "tabs" }], slow: true });
  await tick(60);
  assert.deepEqual(s.posted.map((p) => p.id), ["rapida"]);
});

test("204 vuelve a pedir; 401 vuelve a leer el token; fallos de red esperan y reintentan", async () => {
  const s = load({ polls: [204, 401, new Error("sin red"), { id: "a", op: "tabs" }], tokens: ["viejo", "nuevo"] });
  await tick();
  assert.equal(s.timers.length, 1); // la espera de 3 s tras el 401
  s.timers.shift()();
  await tick();
  assert.equal(s.timers.length, 1); // y la del fallo de red
  s.timers.shift()();
  await tick();
  assert.equal(s.tokenReads, 2);
  assert.equal(s.posted[0].id, "a");
  assert.equal(s.headers.at(-1), "nuevo");
});

test("las alarmas y el arranque no lanzan un segundo bucle", async () => {
  const s = load();
  s.listeners.alarm();
  s.listeners.startup();
  s.listeners.installed();
  await tick();
  assert.equal(s.headers.length, 1); // un solo /poll pendiente
});
