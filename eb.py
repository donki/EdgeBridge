"""sOC Edge Bridge: maneja las pestañas del Edge de Josep desde la consola, con su sesión iniciada y sin
reiniciarlo.

La extensión (carpeta extension/, cargada como «desempaquetada») pide órdenes a este servidor y las
ejecuta con chrome.debugger (protocolo de DevTools). El servidor solo escucha en 127.0.0.1 y exige el
token de extension/token.txt en cada petición (lo crea `serve` la primera vez; no va al repositorio).

  python eb.py serve                         servidor en 127.0.0.1:41500 (dejarlo abierto)
  python eb.py status                        hace cuánto que la extensión pidió órdenes
  python eb.py tabs                          pestañas: id, título, url
  python eb.py open URL                      pestaña nueva en segundo plano; devuelve su id
  python eb.py nav TAB URL                   navegar
  python eb.py close TAB                     cerrar la pestaña
  python eb.py text TAB [MAX]                texto visible de la página
  python eb.py eval TAB 'JS'                 evalúa (con «return», como función async) y devuelve el valor
  python eb.py click TAB 'texto|css=SEL' [N] clic de ratón real en el N-ésimo elemento que coincide
  python eb.py clickxy TAB X Y               clic de ratón real en unas coordenadas
  python eb.py type TAB 'texto'              escribe en el elemento con el foco
  python eb.py fill TAB 'css' 'texto'        vacía un campo y lo escribe como si se tecleara
  python eb.py select TAB 'css' 'opción'     elige una opción de un <select> y avisa a la página
  python eb.py upload TAB 'css[@N]' FICH...  pone ficheros en el N-ésimo <input type=file>
  python eb.py upload-dt TAB 'css[@N]' FICH  igual, creando el fichero dentro de la página (DataTransfer)
  python eb.py shot TAB salida.png           captura de la pestaña
  python eb.py images TAB CARPETA ['css']    guarda como PNG las imágenes de la página
  python eb.py visible TAB                   la pestaña se comporta como visible aunque esté detrás
  python eb.py cdp TAB Metodo '{json}'       cualquier orden de DevTools
  python eb.py detach TAB                    quita la barra «depurando» de esa pestaña
  python eb.py reload                        recarga la extensión (tras cambiar extension/)
"""
import base64
import json
import os
import queue
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "2026.11.01.0"
PORT = int(os.environ.get("SOC_EDGEBRIDGE_PORT", "41500"))
BASE = f"http://127.0.0.1:{PORT}"
HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.environ.get("SOC_EDGEBRIDGE_TOKEN_FILE", os.path.join(HERE, "extension", "token.txt"))
LOG_FILE = os.environ.get("SOC_EDGEBRIDGE_LOG", os.path.join(HERE, "edgebridge.log"))
MAX_CMDS_PER_SECOND = 20


def read_token(create=False):
    """El token compartido con la extensión. `serve` lo crea si no existe."""
    if not os.path.exists(TOKEN_FILE):
        if not create:
            raise SystemExit(f"No hay token en {TOKEN_FILE}: arranca antes `python eb.py serve`.")
        os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
        with open(TOKEN_FILE, "w", encoding="ascii") as f:
            f.write(secrets.token_urlsafe(32))
    with open(TOKEN_FILE, encoding="ascii") as f:
        return f.read().strip()


def log(line):
    """Registro de lo que se ejecuta (Tools §4). Solo la orden, nunca lo que se escribe en la página."""
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- servidor
class Hub:
    """Cola de órdenes para la extensión y resultados que esperan los clientes."""

    def __init__(self, token, rate=MAX_CMDS_PER_SECOND):
        self.token = token
        self.pending = queue.Queue()
        self.results = {}
        self.cv = threading.Condition()
        self.last_poll = 0.0
        self.rate = rate
        self.window = []
        self.lock = threading.Lock()

    def allow(self, now=None):
        """Límite de órdenes por segundo."""
        now = time.monotonic() if now is None else now
        with self.lock:
            self.window = [t for t in self.window if now - t < 1.0]
            if len(self.window) >= self.rate:
                return False
            self.window.append(now)
            return True

    def submit(self, cmd, timeout):
        cmd["id"] = uuid.uuid4().hex
        self.pending.put(cmd)
        with self.cv:
            ok = self.cv.wait_for(lambda: cmd["id"] in self.results, timeout)
            return self.results.pop(cmd["id"], None) if ok else None

    def deliver(self, body):
        with self.cv:
            self.results[body["id"]] = body
            self.cv.notify_all()


def make_handler(hub):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj=None):
            data = b"" if obj is None else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self):
            # Una web cualquiera abierta en el navegador podría llamar a 127.0.0.1: solo se admiten
            # peticiones sin Origin (la consola) o de una extensión, y siempre con el token.
            origin = self.headers.get("Origin")
            if origin and not origin.startswith("chrome-extension://"):
                self._send(403, {"error": "origen no permitido"})
                return False
            if not secrets.compare_digest(self.headers.get("X-EB-Token", ""), hub.token):
                self._send(401, {"error": "falta el token o no es el bueno"})
                return False
            return True

        def _body(self):
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

        def do_GET(self):
            if not self._authorized():
                return
            if self.path == "/poll":
                hub.last_poll = time.time()
                try:
                    self._send(200, hub.pending.get(timeout=20))
                except queue.Empty:
                    self._send(204)
            elif self.path == "/status":
                seen = round(time.time() - hub.last_poll, 1) if hub.last_poll else None
                self._send(200, {"version": VERSION, "extension_seen_s_ago": seen})
            else:
                self._send(404, {"error": "no existe"})

        def do_POST(self):
            if not self._authorized():
                return
            if self.path == "/result":
                hub.deliver(self._body())
                self._send(200, {})
            elif self.path == "/cmd":
                if not hub.allow():
                    self._send(429, {"error": "demasiadas órdenes por segundo"})
                    return
                body = self._body()
                timeout = body.pop("timeout", 120)
                log(f"{body.get('op')} {body.get('method', '')} tab={body.get('tab', '')}".rstrip())
                res = hub.submit(body, timeout)
                self._send(200, res or {"error": "sin respuesta de la extensión (¿cargada? ¿Edge abierto?)"})
            else:
                self._send(404, {"error": "no existe"})

    return Handler


def make_server(port=PORT, token=None):
    hub = Hub(token or read_token(create=True))
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(hub)), hub


def serve():
    server, _ = make_server()
    print(f"sOC Edge Bridge {VERSION} escuchando en {BASE} (token en {TOKEN_FILE})")
    server.serve_forever()


# ---------------------------------------------------------------- cliente
def _request(path, payload=None, timeout=30):
    req = urllib.request.Request(BASE + path, None if payload is None else json.dumps(payload).encode(),
                                 {"Content-Type": "application/json", "X-EB-Token": read_token()})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read() or b"{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(json.loads(e.read() or b"{}").get("error", str(e))) from None


def call(op, timeout=120, **kw):
    res = _request("/cmd", {"op": op, "timeout": timeout, **kw}, timeout + 10)
    if "error" in res:
        raise RuntimeError(res["error"])
    return res["result"]


def cdp(tab, method, params=None, timeout=120):
    return call("cdp", tab=int(tab), method=method, params=params or {}, timeout=timeout)


def wrap_js(js):
    """Con «return» el código va dentro de una función async (admite await)."""
    return f"(async()=>{{ {js} }})()" if "return" in js else js


def evaluate(tab, js):
    r = cdp(tab, "Runtime.evaluate", {"expression": wrap_js(js), "awaitPromise": True,
                                      "returnByValue": True, "userGesture": True})
    if "exceptionDetails" in r:
        raise RuntimeError(r["exceptionDetails"].get("exception", {}).get("description")
                           or r["exceptionDetails"].get("text"))
    return r["result"].get("value")


# Busca elementos visibles por texto (o css=…) y devuelve el centro del N-ésimo.
FIND_JS = r"""
const q = %s, n = %d;
const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 &&
  getComputedStyle(e).visibility !== 'hidden'; };
let els;
if (q.startsWith('css=')) els = [...document.querySelectorAll(q.slice(4))].filter(vis);
else {
  const t = q.toLowerCase();
  const all = [...document.querySelectorAll('button,a,[role=button],[role=tab],[role=menuitem],[role=link],[role=option],input,label,select,summary,li,span,div,td,h1,h2,h3')];
  const hit = e => ((e.innerText || e.value || e.getAttribute('aria-label') || '').trim().toLowerCase());
  els = all.filter(e => vis(e) && hit(e) === t);
  if (!els.length) els = all.filter(e => vis(e) && hit(e).includes(t));
  // el más interno: quita los que contienen a otro de la lista
  els = els.filter(e => !els.some(o => o !== e && e.contains(o)));
}
const e = els[n];
if (!e) return {found: els.length};
e.scrollIntoView({block: 'center'});
const r = e.getBoundingClientRect();
return {found: els.length, x: r.left + r.width / 2, y: r.top + r.height / 2,
        tag: e.tagName, text: (e.innerText || e.value || '').trim().slice(0, 80)};
"""


def click_xy(tab, x, y):
    for t in ("mouseMoved", "mousePressed", "mouseReleased"):
        cdp(tab, "Input.dispatchMouseEvent", {"type": t, "x": float(x), "y": float(y), "button": "left",
                                               "clickCount": 1})


def click(tab, query, n=0):
    hit = evaluate(tab, FIND_JS % (json.dumps(query), n))
    if "x" not in hit:
        raise RuntimeError(f"no encontrado: {query!r} (coincidencias: {hit['found']})")
    click_xy(tab, hit["x"], hit["y"])
    return hit


def key(tab, name, code, vk):
    for t in ("keyDown", "keyUp"):
        cdp(tab, "Input.dispatchKeyEvent", {"type": t, "key": name, "code": code, "windowsVirtualKeyCode": vk})


def fill(tab, css, text):
    """Vacía el campo y escribe el texto con eventos reales (los formularios de Angular lo ven)."""
    sel = json.dumps(css)
    evaluate(tab, f"const e=document.querySelector({sel}); if(!e) throw new Error('no hay '+{sel}); "
                  "e.scrollIntoView({block:'center'}); e.focus(); e.select(); return 1")
    key(tab, "Delete", "Delete", 46)
    cdp(tab, "Input.insertText", {"text": text})
    return evaluate(tab, f"const e=document.querySelector({sel}); e.blur(); return e.value")


def select(tab, css, option):
    sel, opt = json.dumps(css), json.dumps(option)
    return evaluate(tab, f"""const s=document.querySelector({sel}); if(!s) throw new Error('no hay '+{sel});
const o=[...s.options].find(o=>o.text.trim()==={opt}) || [...s.options].find(o=>o.text.includes({opt}));
if(!o) throw new Error('sin opción: '+[...s.options].map(o=>o.text.trim()).join('|'));
s.value=o.value; s.dispatchEvent(new Event('change',{{bubbles:true}})); s.dispatchEvent(new Event('input',{{bubbles:true}}));
return s.options[s.selectedIndex].text.trim()""")


def split_target(target):
    """«css@N» → («css», N). Varios <input type=file> iguales se distinguen por su orden."""
    css, _, idx = target.partition("@")
    return css, int(idx or 0)


def upload(tab, target, files):
    css, idx = split_target(target)
    root = cdp(tab, "DOM.getDocument", {"depth": 0})["root"]["nodeId"]
    nodes = cdp(tab, "DOM.querySelectorAll", {"nodeId": root, "selector": css})["nodeIds"]
    if len(nodes) <= idx:
        raise RuntimeError(f"no hay {target!r} (hay {len(nodes)})")
    cdp(tab, "DOM.setFileInputFiles", {"nodeId": nodes[idx], "files": [os.path.abspath(f) for f in files]})
    return {"ok": True, "files": len(files)}


def upload_dt(tab, target, path):
    css, idx = split_target(target)
    b64 = base64.b64encode(open(path, "rb").read()).decode()
    return evaluate(tab, f"""
const bin=atob({json.dumps(b64)}); const u=new Uint8Array(bin.length); for(let i=0;i<bin.length;i++) u[i]=bin.charCodeAt(i);
const inp=document.querySelectorAll({json.dumps(css)})[{idx}]; if(!inp) throw new Error('no hay el input');
const dt=new DataTransfer(); dt.items.add(new File([u], {json.dumps(os.path.basename(path))}));
inp.files=dt.files; inp.dispatchEvent(new Event('input',{{bubbles:true}})); inp.dispatchEvent(new Event('change',{{bubbles:true}}));
return inp.files[0].name""")


def images(tab, out, css="img"):
    os.makedirs(out, exist_ok=True)
    sel = json.dumps(css)
    saved = []
    for i in range(evaluate(tab, f"return document.querySelectorAll({sel}).length")):
        w, h, data = evaluate(tab, f"""const im=document.querySelectorAll({sel})[{i}];
const c=document.createElement('canvas'); c.width=im.naturalWidth; c.height=im.naturalHeight;
try {{ c.getContext('2d').drawImage(im,0,0); return [c.width,c.height,c.toDataURL('image/png')]; }}
catch(e) {{ const b=await (await fetch(im.src)).blob(); return await new Promise(ok=>{{const f=new FileReader();
  f.onload=()=>ok([im.naturalWidth,im.naturalHeight,f.result]); f.readAsDataURL(b);}}); }}""")
        p = os.path.join(out, f"{i:02d}.png")
        with open(p, "wb") as f:
            f.write(base64.b64decode(data.split(",", 1)[1]))
        saved.append(f"{p} {w}x{h}")
    return saved


def visible(tab):
    """Las webs que no dibujan en una pestaña de fondo (Play Console) se comportan como si tuvieran foco."""
    cdp(tab, "Emulation.setFocusEmulationEnabled", {"enabled": True})
    cdp(tab, "Page.setWebLifecycleState", {"state": "active"})
    return {"ok": True}


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    c, a = argv[0], argv[1:]
    if c == "serve":
        return serve()
    if c == "status":
        out = _request("/status")
    elif c == "tabs":
        for t in call("tabs"):
            print(f"{t['id']:>10} {'*' if t['active'] else ' '} {t['title'][:70]:<70} {t['url'][:110]}")
        return
    elif c == "open":
        out = call("create", url=a[0])
    elif c == "nav":
        out = call("navigate", tab=int(a[0]), url=a[1])
    elif c == "close":
        out = call("close", tab=int(a[0]))
    elif c == "detach":
        out = call("detach", tab=int(a[0]))
    elif c == "reload":
        out = call("reload", timeout=15)
    elif c == "text":
        txt = evaluate(a[0], "document.body.innerText")
        print(txt[: int(a[1])] if len(a) > 1 else txt)
        return
    elif c == "eval":
        out = evaluate(a[0], a[1])
    elif c == "click":
        out = click(a[0], a[1], int(a[2]) if len(a) > 2 else 0)
    elif c == "clickxy":
        click_xy(a[0], a[1], a[2])
        out = {"ok": True}
    elif c == "type":
        out = cdp(a[0], "Input.insertText", {"text": a[1]})
    elif c == "fill":
        out = fill(a[0], a[1], a[2])
    elif c == "select":
        out = select(a[0], a[1], a[2])
    elif c == "upload":
        out = upload(a[0], a[1], a[2:])
    elif c == "upload-dt":
        out = upload_dt(a[0], a[1], a[2])
    elif c == "shot":
        png = cdp(a[0], "Page.captureScreenshot", {"format": "png"})["data"]
        with open(a[1], "wb") as f:
            f.write(base64.b64decode(png))
        out = a[1]
    elif c == "images":
        out = images(a[0], a[1], *a[2:3])
    elif c == "visible":
        out = visible(a[0])
    elif c == "cdp":
        out = cdp(a[0], a[1], json.loads(a[2]) if len(a) > 2 else {})
    else:
        raise SystemExit(f"orden desconocida: {c} (python eb.py --help)")
    print(out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":  # pragma: no cover
    sys.stdout.reconfigure(encoding="utf-8")
    main(sys.argv[1:])
