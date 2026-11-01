"""Banco de pruebas de sOC Edge Bridge: servidor real en un puerto libre y una extensión falsa que
contesta como lo haría Edge. Sin navegador, sin red externa y sin tocar el token de verdad."""
import base64
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import eb  # noqa: E402

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfalso").decode()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeEdge:
    """Hace de extensión: pide órdenes y contesta. `answers` decide qué devuelve cada orden."""

    def __init__(self, base, token):
        self.base, self.token = base, token
        self.calls = []
        self.eval_value = None
        self.eval_error = None
        self.stop = False
        self.thread = threading.Thread(target=self.run, daemon=True)

    def req(self, path, body=None, timeout=25):
        r = urllib.request.Request(self.base + path, None if body is None else json.dumps(body).encode(),
                                   {"X-EB-Token": self.token, "Origin": "chrome-extension://prueba"})
        resp = urllib.request.urlopen(r, timeout=timeout)
        return resp.status, resp.read()

    def run(self):
        while not self.stop:
            try:
                status, data = self.req("/poll")
            except Exception:
                time.sleep(0.05)
                continue
            if status == 204:
                continue
            cmd = json.loads(data)
            self.calls.append(cmd)
            try:
                body = {"id": cmd["id"], "result": self.answer(cmd)}
            except Exception as e:  # la extensión de verdad también devuelve el error como texto
                body = {"id": cmd["id"], "error": str(e)}
            self.req("/result", body)

    def answer(self, cmd):
        op = cmd["op"]
        if op == "tabs":
            return [{"id": 7, "windowId": 1, "active": True, "title": "Partner Center", "url": "https://x"}]
        if op == "create":
            return {"id": 8}
        if op in ("navigate", "close", "detach", "reload"):
            return {"ok": True}
        if op == "boom":
            raise ValueError("falló en la extensión")
        m, p = cmd["method"], cmd["params"]
        if m == "Runtime.evaluate":
            if self.eval_error:
                return {"exceptionDetails": {"exception": {"description": self.eval_error}}}
            v = self.eval_value(p["expression"]) if callable(self.eval_value) else self.eval_value
            return {"result": {"value": v}}
        if m == "DOM.getDocument":
            return {"root": {"nodeId": 1}}
        if m == "DOM.querySelectorAll":
            return {"nodeIds": [11, 12]}
        if m == "Page.captureScreenshot":
            return {"data": PNG}
        return {}


@pytest.fixture()
def bridge(tmp_path, monkeypatch):
    port = free_port()
    token_file = tmp_path / "token.txt"
    monkeypatch.setattr(eb, "TOKEN_FILE", str(token_file))
    monkeypatch.setattr(eb, "LOG_FILE", str(tmp_path / "eb.log"))
    monkeypatch.setattr(eb, "BASE", f"http://127.0.0.1:{port}")
    server, hub = eb.make_server(port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    edge = FakeEdge(eb.BASE, hub.token)
    edge.thread.start()
    yield edge, hub, tmp_path
    edge.stop = True
    server.shutdown()
    server.server_close()


def raw(path, headers=None, body=None):
    r = urllib.request.Request(eb.BASE + path, body, headers or {})
    try:
        resp = urllib.request.urlopen(r, timeout=10)
        return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


# ---------------------------------------------------------------- token y registro
def test_token_se_crea_una_vez_y_se_reutiliza(tmp_path, monkeypatch):
    monkeypatch.setattr(eb, "TOKEN_FILE", str(tmp_path / "sub" / "token.txt"))
    t1 = eb.read_token(create=True)
    assert len(t1) >= 40
    assert eb.read_token() == t1


def test_sin_token_el_cliente_explica_que_hay_que_arrancar_el_servidor(tmp_path, monkeypatch):
    monkeypatch.setattr(eb, "TOKEN_FILE", str(tmp_path / "no-existe.txt"))
    with pytest.raises(SystemExit, match="serve"):
        eb.read_token()


def test_el_registro_guarda_la_orden_pero_no_el_texto(bridge):
    edge, _, tmp = bridge
    edge.eval_value = "hola"
    eb.cdp(7, "Input.insertText", {"text": "secreto"})
    log = (tmp / "eb.log").read_text(encoding="utf-8")
    assert "cdp Input.insertText tab=7" in log
    assert "secreto" not in log


def test_registro_que_no_se_puede_escribir_no_rompe(monkeypatch, tmp_path):
    monkeypatch.setattr(eb, "LOG_FILE", str(tmp_path))  # es una carpeta: open falla
    eb.log("nada")


# ---------------------------------------------------------------- seguridad del servidor
def test_sin_token_401(bridge):
    assert raw("/status")[0] == 401
    assert raw("/cmd", {"X-EB-Token": "malo"}, b"{}")[0] == 401


def test_una_web_no_puede_mandar_ordenes_403(bridge):
    _, hub, _ = bridge
    code, body = raw("/cmd", {"X-EB-Token": hub.token, "Origin": "https://malvada.example"}, b'{"op":"tabs"}')
    assert code == 403 and "origen" in body["error"]


def test_ruta_desconocida_404(bridge):
    _, hub, _ = bridge
    assert raw("/nada", {"X-EB-Token": hub.token})[0] == 404
    assert raw("/nada", {"X-EB-Token": hub.token}, b"{}")[0] == 404


def test_limite_de_ordenes_por_segundo():
    hub = eb.Hub("t", rate=3)
    assert [hub.allow(10.0) for _ in range(4)] == [True, True, True, False]
    assert hub.allow(11.5)  # pasado el segundo, vuelve a dejar


def test_429_cuando_se_pasa_del_limite(bridge):
    _, hub, _ = bridge
    hub.rate = 0
    code, body = raw("/cmd", {"X-EB-Token": hub.token}, b'{"op":"tabs"}')
    assert code == 429


def test_sin_extension_el_cliente_recibe_un_error_claro(tmp_path, monkeypatch):
    port = free_port()
    monkeypatch.setattr(eb, "TOKEN_FILE", str(tmp_path / "t.txt"))
    monkeypatch.setattr(eb, "LOG_FILE", str(tmp_path / "eb.log"))
    monkeypatch.setattr(eb, "BASE", f"http://127.0.0.1:{port}")
    server, _ = eb.make_server(port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(RuntimeError, match="sin respuesta"):
            eb.call("tabs", timeout=1)
    finally:
        server.shutdown()
        server.server_close()


def test_status_antes_y_despues_de_ver_la_extension(tmp_path, monkeypatch):
    port = free_port()
    monkeypatch.setattr(eb, "TOKEN_FILE", str(tmp_path / "t.txt"))
    monkeypatch.setattr(eb, "BASE", f"http://127.0.0.1:{port}")
    server, hub = eb.make_server(port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert eb._request("/status") == {"version": eb.VERSION, "extension_seen_s_ago": None}
        hub.last_poll = time.time() - 5
        assert eb._request("/status")["extension_seen_s_ago"] >= 5
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------- cliente
def test_error_de_la_extension_llega_al_cliente(bridge):
    with pytest.raises(RuntimeError, match="falló en la extensión"):
        eb.call("boom")


def test_wrap_js():
    assert eb.wrap_js("document.title") == "document.title"
    assert eb.wrap_js("return 1").startswith("(async()=>")


def test_evaluate_devuelve_valor_y_excepciones(bridge):
    edge, _, _ = bridge
    edge.eval_value = 42
    assert eb.evaluate(7, "return 6*7") == 42
    edge.eval_error = "TypeError: x"
    with pytest.raises(RuntimeError, match="TypeError"):
        eb.evaluate(7, "x.y")


def test_click_usa_el_raton_en_el_centro(bridge):
    edge, _, _ = bridge
    edge.eval_value = {"found": 1, "x": 10, "y": 20, "tag": "BUTTON", "text": "Save"}
    assert eb.click(7, "Save")["tag"] == "BUTTON"
    mouse = [c["params"]["type"] for c in edge.calls if c.get("method") == "Input.dispatchMouseEvent"]
    assert mouse == ["mouseMoved", "mousePressed", "mouseReleased"]


def test_click_no_encontrado(bridge):
    edge, _, _ = bridge
    edge.eval_value = {"found": 0}
    with pytest.raises(RuntimeError, match="no encontrado"):
        eb.click(7, "Nada")


def test_fill_borra_escribe_y_devuelve_el_valor(bridge):
    edge, _, _ = bridge
    edge.eval_value = "texto nuevo"
    assert eb.fill(7, "#notas", "texto nuevo") == "texto nuevo"
    methods = [c.get("method") for c in edge.calls]
    assert "Input.dispatchKeyEvent" in methods and "Input.insertText" in methods


def test_select_lanza_change(bridge):
    edge, _, _ = bridge
    edge.eval_value = lambda js: "Utilities + tools" if "dispatchEvent(new Event('change'" in js else None
    assert eb.select(7, "select[name=Category]", "Utilities + tools") == "Utilities + tools"


def test_split_target():
    assert eb.split_target("input[type=file]@3") == ("input[type=file]", 3)
    assert eb.split_target("input") == ("input", 0)


def test_upload_elige_el_input_por_indice(bridge, tmp_path):
    edge, _, _ = bridge
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    assert eb.upload(7, "input[type=file]@1", [str(f)]) == {"ok": True, "files": 1}
    sent = [c for c in edge.calls if c.get("method") == "DOM.setFileInputFiles"][0]["params"]
    assert sent["nodeId"] == 12 and sent["files"] == [str(f)]


def test_upload_indice_fuera_de_rango(bridge, tmp_path):
    with pytest.raises(RuntimeError, match="hay 2"):
        eb.upload(7, "input@5", [str(tmp_path)])


def test_upload_dt_mete_el_fichero_en_la_pagina(bridge, tmp_path):
    edge, _, _ = bridge
    f = tmp_path / "captura.png"
    f.write_bytes(b"\x89PNG")
    edge.eval_value = lambda js: "captura.png" if base64.b64encode(b"\x89PNG").decode() in js else None
    assert eb.upload_dt(7, "input[type=file]@2", str(f)) == "captura.png"


def test_images_guarda_png(bridge, tmp_path):
    edge, _, _ = bridge
    data = "data:image/png;base64," + PNG
    edge.eval_value = lambda js: 2 if ".length" in js and "canvas" not in js else [3, 4, data]
    saved = eb.images(7, str(tmp_path / "img"))
    assert len(saved) == 2 and saved[0].endswith("3x4")
    assert (tmp_path / "img" / "01.png").read_bytes().startswith(b"\x89PNG")


def test_visible(bridge):
    edge, _, _ = bridge
    assert eb.visible(7) == {"ok": True}
    assert {"Emulation.setFocusEmulationEnabled", "Page.setWebLifecycleState"} <= {c.get("method") for c in edge.calls}


# ---------------------------------------------------------------- línea de órdenes
@pytest.mark.parametrize("argv,expect", [
    (["tabs"], "Partner Center"),
    (["open", "https://x"], '"id": 8'),
    (["nav", "7", "https://x"], '"ok": true'),
    (["close", "7"], '"ok": true'),
    (["detach", "7"], '"ok": true'),
    (["reload"], '"ok": true'),
    (["eval", "7", "return 1"], "valor"),
    (["text", "7", "3"], "val"),
    (["text", "7"], "valor"),
    (["clickxy", "7", "1", "2"], '"ok": true'),
    (["type", "7", "hola"], "{}"),
    (["cdp", "7", "Page.reload", "{}"], "{}"),
    (["cdp", "7", "Page.reload"], "{}"),
    (["visible", "7"], '"ok": true'),
    (["status"], "version"),
])
def test_main_ordenes(bridge, capsys, argv, expect):
    edge, _, _ = bridge
    edge.eval_value = "valor"
    eb.main(argv)
    assert expect in capsys.readouterr().out


def test_main_click_fill_select_upload_shot_images(bridge, capsys, tmp_path):
    edge, _, _ = bridge
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    edge.eval_value = {"found": 1, "x": 1, "y": 1, "tag": "A", "text": "t"}
    eb.main(["click", "7", "t", "0"])
    edge.eval_value = "ok"
    eb.main(["fill", "7", "#a", "b"])
    eb.main(["select", "7", "#s", "o"])
    eb.main(["upload", "7", "input", str(f)])
    eb.main(["upload-dt", "7", "input", str(f)])
    eb.main(["shot", "7", str(tmp_path / "s.png")])
    assert (tmp_path / "s.png").read_bytes().startswith(b"\x89PNG")
    edge.eval_value = lambda js: 0
    eb.main(["images", "7", str(tmp_path / "i"), "img.x"])
    assert capsys.readouterr().out.count("\n") >= 6


def test_main_ayuda_y_orden_desconocida(capsys):
    eb.main([])
    assert "eb.py serve" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="desconocida"):
        eb.main(["vuela"])


def test_serve_arranca_el_servidor(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(eb, "TOKEN_FILE", str(tmp_path / "t.txt"))

    class Fake:
        def serve_forever(self):
            print("sirviendo")

    monkeypatch.setattr(eb, "make_server", lambda: (Fake(), None))
    eb.main(["serve"])
    assert "sirviendo" in capsys.readouterr().out
