// Pide órdenes al servidor local (eb.py serve) y las ejecuta: pestañas y protocolo de DevTools.
// Cada petición lleva el token de token.txt (lo crea `eb.py serve`; no va al repositorio).
const SERVER = "http://127.0.0.1:41500";
const attached = new Set();
let running = false;
let token = null;

chrome.debugger.onDetach.addListener((src) => attached.delete(src.tabId));
chrome.tabs.onRemoved.addListener((id) => attached.delete(id));

async function getToken() {
  if (!token) token = (await (await fetch(chrome.runtime.getURL("token.txt"))).text()).trim();
  return token;
}

async function attach(tabId) {
  if (attached.has(tabId)) return;
  try {
    await chrome.debugger.attach({ tabId }, "1.3");
  } catch (e) {
    if (!String(e).includes("Already attached")) throw e;
  }
  attached.add(tabId);
}

async function run(cmd) {
  switch (cmd.op) {
    case "tabs":
      return (await chrome.tabs.query({})).map((t) => ({
        id: t.id, windowId: t.windowId, active: t.active, title: t.title, url: t.url,
      }));
    case "create": {
      const t = await chrome.tabs.create({ url: cmd.url, active: cmd.active ?? false });
      return { id: t.id };
    }
    case "navigate":
      await chrome.tabs.update(cmd.tab, { url: cmd.url });
      return { ok: true };
    case "close":
      await chrome.tabs.remove(cmd.tab);
      return { ok: true };
    case "detach":
      if (attached.has(cmd.tab)) await chrome.debugger.detach({ tabId: cmd.tab });
      attached.delete(cmd.tab);
      return { ok: true };
    case "cdp":
      await attach(cmd.tab);
      return await chrome.debugger.sendCommand({ tabId: cmd.tab }, cmd.method, cmd.params || {});
    default:
      throw new Error("op desconocida: " + cmd.op);
  }
}

async function post(body) {
  await fetch(SERVER + "/result", {
    method: "POST", body: JSON.stringify(body), headers: { "X-EB-Token": await getToken() },
  }).catch(() => {});
}

async function handle(cmd) {
  let body;
  try {
    if (cmd.op === "reload") {
      setTimeout(() => chrome.runtime.reload(), 200);
      body = { id: cmd.id, result: { ok: true } };
    } else {
      // Una orden que se cuelga (una captura con un menú abierto, una página bloqueada) no puede
      // dejar al cliente esperando para siempre.
      const limit = new Promise((_, ko) => setTimeout(() => ko(new Error("tiempo agotado (60 s)")), 60000));
      body = { id: cmd.id, result: await Promise.race([run(cmd), limit]) };
    }
  } catch (e) {
    body = { id: cmd.id, error: String(e && e.message ? e.message : e) };
  }
  await post(body);
}

async function loop() {
  if (running) return;
  running = true;
  try {
    for (;;) {
      chrome.runtime.getPlatformInfo(); // cualquier llamada a la API mantiene vivo el service worker
      let cmd;
      try {
        const r = await fetch(SERVER + "/poll", { cache: "no-store", headers: { "X-EB-Token": await getToken() } });
        if (r.status === 204) continue;
        if (r.status === 401) token = null; // el token cambió: se vuelve a leer
        if (!r.ok) throw new Error(String(r.status));
        cmd = await r.json();
      } catch {
        await new Promise((ok) => setTimeout(ok, 3000));
        continue;
      }
      // Sin await: una orden lenta no bloquea las demás.
      handle(cmd);
    }
  } finally {
    running = false;
  }
}

chrome.alarms.create("keepalive", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => loop());
chrome.runtime.onStartup.addListener(() => loop());
chrome.runtime.onInstalled.addListener(() => loop());
loop();
