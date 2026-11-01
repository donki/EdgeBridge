# Changelog — sOC Edge Bridge

Formato de versión `AAAA.MM.DD.N`.

## 2026.11.01.0 — Primera versión en sOCProjects

- Servidor local (`eb.py serve`, 127.0.0.1:41500) y extensión de Edge que ejecuta sus órdenes con el
  protocolo de DevTools sobre la sesión ya iniciada, sin reiniciar el navegador.
- **Token obligatorio** en cada petición (`extension/token.txt`, fuera del repositorio): sin él, 401;
  desde una web, 403; más de 20 órdenes por segundo, 429 (Tools §3).
- **Registro** de cada orden en `edgebridge.log`, sin el texto que se escribe (Tools §4).
- Una orden colgada (una captura con un menú abierto) ya no bloquea a las demás: cada una tiene 60 s.
- Órdenes nuevas que estaban en scripts sueltos: `fill`, `select`, `upload-dt`, `images`,
  `clickxy`, `visible`; `upload` admite `css@N`.
- Banco de pruebas: 51 pruebas (pytest y node --test), cobertura del 98 %.
- Nació el 2026-11-01 en `D:\dev\edge-bridge` para gestionar Partner Center; con esta versión pasa a
  `Tools/EdgeBridge` (que queda enlazado desde la ruta antigua para no tener que volver a cargar la
  extensión).
