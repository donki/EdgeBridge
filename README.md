# sOC Edge Bridge

Maneja las pestañas del **Edge de Josep desde la consola**, con la sesión que ya tiene iniciada
(Partner Center, Play Console…), **sin reiniciar el navegador** y sin Playwright. Una extensión
desempaquetada recibe órdenes de un servidor local y las ejecuta con el protocolo de DevTools
(`chrome.debugger`): leer la página, hacer clic con el ratón, escribir, subir ficheros, capturas.

Es una herramienta interna ([constitución Tools](constitution/CONSTITUCION-TOOLS.md)): no se publica
en ninguna tienda.

## Cómo funciona

```
python eb.py <orden>  ──HTTP + token──▶  eb.py serve (127.0.0.1:41500)  ◀──pide órdenes──  extensión (Edge)
                                                                                        │
                                                                     chrome.debugger ───┘──▶ pestañas
```

- El servidor escucha **solo en 127.0.0.1**. Cada petición lleva el token de `extension/token.txt`
  (lo crea `eb.py serve` la primera vez; está en `.gitignore`). Sin token, **401**; una petición con
  `Origin` de una web (cualquier página abierta en el navegador podría intentar llamar a 127.0.0.1),
  **403**; más de 20 órdenes por segundo, **429**.
- La extensión lee el mismo token de su carpeta y lo manda en cada `/poll` y `/result`.
- Cada orden tiene 60 s como máximo en la extensión y no bloquea a las demás.

## Puesta en marcha (una vez)

1. `python eb.py serve` (déjalo abierto; crea el token si no hay).
2. En Edge, `edge://extensions` › *Modo de desarrollador* › *Cargar desempaquetada* › la carpeta
   `extension/`.
3. `python eb.py status` tiene que decir que la extensión pidió órdenes hace unos segundos.

Tras cambiar algo de `extension/`: `python eb.py reload`.

## Órdenes

`python eb.py --help` las lista todas. Las más usadas:

| Orden | Qué hace |
|---|---|
| `tabs` | pestañas abiertas (id, título, url) |
| `open URL` / `nav TAB URL` / `close TAB` | abrir en segundo plano, navegar, cerrar |
| `text TAB [MAX]` / `eval TAB 'JS'` | texto de la página / evaluar JavaScript (con `return`, como función async) |
| `click TAB 'texto' [N]` / `click TAB 'css=SEL'` / `clickxy TAB X Y` | clic de ratón real |
| `fill TAB 'css' 'texto'` / `select TAB 'css' 'opción'` | rellenar campos y desplegables (los formularios de Angular lo notan) |
| `upload TAB 'css@N' FICH…` / `upload-dt TAB 'css@N' FICH` | subir ficheros a un `<input type=file>` |
| `shot TAB salida.png` / `images TAB CARPETA` | captura de la pestaña / bajar sus imágenes |
| `visible TAB` | la pestaña se porta como visible aunque esté detrás (Play Console no dibuja si no) |
| `cdp TAB Método '{json}'` | cualquier orden del protocolo de DevTools |

Trucos aprendidos en Partner Center: cada imagen ya subida tiene su propio `<input type=file>` para
sustituirla, así que el de «añadir» es el último del grupo (`@N`); la subida tarda en reflejarse
(esperar 15-45 s antes de comprobar); el castellano tiene `languageid` distinto en cada producto.

## Qué necesita

- Python 3.12 o posterior (solo biblioteca estándar).
- Microsoft Edge (o Chrome) con la extensión cargada.
- Para las pruebas: `pip install -r requirements-dev.txt` y Node 20 o posterior.

## A qué accede y qué puede romper

- **Accede** a todas las pestañas del perfil de Edge donde se carga la extensión, con sus sesiones
  iniciadas: puede leer cualquier página, hacer clic, escribir y enviar formularios **como Josep**
  (Partner Center, Play Console, correo…). Por eso el servidor exige token y no admite webs.
- **Mientras trabaja en una pestaña**, Edge enseña la barra «sOC Edge Bridge está depurando este
  navegador»; `detach TAB` la quita.
- **Puede romper**: lo que se haga en esas webs (enviar una versión a revisión, borrar un envío) no
  se deshace desde aquí. La herramienta no hace nada por sí sola: solo lo que se le ordena, orden a
  orden. No guarda contraseñas ni cookies.
- **Registro**: cada orden queda en `edgebridge.log` (fecha, orden, método y pestaña; nunca el texto
  que se escribe en la página). Está en `.gitignore`.
- `SOC_EDGEBRIDGE_PORT`, `SOC_EDGEBRIDGE_TOKEN_FILE` y `SOC_EDGEBRIDGE_LOG` cambian puerto, token y
  registro (las pruebas los usan).

## Pruebas

`pwsh pruebas.ps1` lanza el banco entero (General §8.6):

- **51 pruebas, todas en verde** (2026-11-01): 42 de `eb.py` con pytest (servidor real en un puerto
  libre y una extensión falsa que contesta como Edge: token, 401/403/404/429, cada orden de la
  línea de órdenes) y 9 de `extension/sw.js` con `node --test` y un `chrome` falso (órdenes,
  token, una orden colgada que no bloquea, reintentos, reload).
- **Cobertura de líneas**: `eb.py` 98 % (246/250), `sw.js` 98,1 %; **toda la herramienta, 98 %**.
- **Tiempo del banco**: unos 10 s.

## Licencia

MIT (ver `LICENSE`).
