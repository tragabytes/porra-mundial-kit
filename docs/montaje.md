# Montaje de la porra, de cero a producción

Runbook secuencial. Cada paso depende del anterior; no saltes ninguno.
Tiempo estimado total: una tarde para la infraestructura + los días que tardes
en recibir las predicciones de los jugadores.

## 0. Compras y altas (con semanas de antelación)

1. **Excel de matejero** (versión ADMIN 25 jugadores + template de jugador) en
   https://matejero.es/excel-porra-mundial/. Es de pago: **jamás lo publiques**
   en un repo público.
2. **football-data.org**: cuenta gratuita en
   https://www.football-data.org/client/register. Verifica con un
   `GET /v4/competitions/` que el free tier cubre tu torneo y anota su código
   (p. ej. `WC` para el Mundial, `EC` para la Eurocopa).
3. **API key de Anthropic**: https://console.anthropic.com/ → API Keys.
4. **VPS**: Ubuntu 24.04, ~10 €/mes en cualquier proveedor (2 GB+ de RAM;
   whatsapp-web.js arrastra un Chromium). Añade tu clave SSH al crearlo.
5. **eSIM de prepago barata** para el número del bot (nunca el personal).
   ⚠️ **"Aging" del número**: registra WhatsApp en esa línea y úsala de forma
   humana (saludos, algún grupo) durante **≥2 semanas** antes de que el bot
   publique a ritmo de cron, o WhatsApp puede marcarla como spam.
6. **Tokens de GitHub** (fine-grained, solo este repo): `GH_PAT` con
   *Contents: Read and write* y `GH_DISPATCH_TOKEN` con *Actions: Read and
   write*. Apunta sus fechas de caducidad: que no expiren a mitad de torneo.

## 1. Clonar y preparar el entorno local

```bash
git clone https://github.com/<tu-usuario>/<tu-repo>.git
cd <tu-repo>
python -m pip install -r requirements.txt
python -m playwright install chromium   # para renderizar el leaderboard PNG
```

Instala **LibreOffice** (imprescindible: es el "F9 headless" que recalcula las
fórmulas del Excel; sin él nada se actualiza):

- Windows: https://www.libreoffice.org/download/
- Linux: `sudo apt install libreoffice-calc`
- macOS: `brew install --cask libreoffice`

En Windows, lanza los scripts desde una consola con `PYTHONIOENCODING=utf-8`
para evitar problemas de acentos (cp1252).

## 2. Bootstrap del Excel

1. **Mapa partido → fila**: genera `data/match_row_map.json` desde la hoja
   WORLDCUP del ADMIN (fase de grupos; las eliminatorias se resuelven en
   runtime):

   ```bash
   python scripts/bootstrap_match_rows.py
   ```

   ⚠️ El script espera el ADMIN en una ruta hardcodeada en su cabecera —
   revisa/ajusta esa constante antes de ejecutarlo.

2. **Baremo de puntos**: el Excel de matejero viene con la tabla de puntos a
   cero (`ADMIN!D8:D47`). **Sin baremo, ningún acierto puntúa.** El baremo vive
   en el dict `SCORING` de `scripts/set_scoring.py`; acuérdalo con tus
   jugadores, edítalo y aplícalo a **cada** porra:

   ```bash
   python scripts/set_scoring.py --admin pools/<pool>/ADMIN.xlsx
   ```

   Escribe la columna D sin desproteger la hoja y recalcula con LibreOffice.

3. ⚠️ El ADMIN debe estar **cerrado en Excel** siempre que un script vaya a
   escribirlo (si está abierto, Windows bloquea el archivo → `PermissionError`).

## 3. Crear las porras (pools)

Cada porra vive en `pools/<id>/` (identificador corto, kebab-case sin acentos)
con su propio ADMIN y sus jugadores:

```
pools/
└── penya-2030/
    ├── ADMIN.xlsx      # copia del ADMIN comprado, una por porra
    ├── players.json    # lo escribe el organizador
    └── state.json      # lo crea el cron; no lo toques
```

`players.json` es un array con un objeto por jugador. Ejemplo **ficticio**
(schema completo en `pools/example/players.json`):

```json
[
  {"slot": 1, "name": "Lucía",  "club": "Real Betis",    "national": "España",
   "bota_oro": "Mbappé", "balon_oro": "Lamine Yamal", "campeon": "España"},
  {"slot": 2, "name": "Marcos", "club": null,            "national": "Argentina",
   "bota_oro": "Julián Álvarez", "balon_oro": "Messi", "campeon": "Argentina"}
]
```

Reglas: `slot` 1..25 sin duplicados; `name` **idéntico** al que aparecerá en la
hoja CLAS del ADMIN; `club`/`national` pueden ser `null`; `bota_oro`,
`balon_oro` y `campeon` salen del Cuadro de Honor del Excel del jugador. Estos
perfiles alimentan la personalización del comentario de IA.

## 4. Recoger e ingerir predicciones

1. Cada jugador rellena su copia del **template de jugador** de matejero
   (nombre en `Home!C10`, predicciones en WORLDCUP) y te la envía.
2. Guarda cada archivo como `predictions/<pool>/NN_nombre.xlsx`, donde `NN` es
   el slot (01..25). Ej.: `predictions/penya-2030/01_lucia.xlsx`.
   `predictions/` está **gitignored**: las predicciones nunca se suben al repo.
3. Ingesta (idempotente: si un jugador corrige, sobreescribe su `.xlsx` y
   repite):

   ```bash
   POOL_ID=penya-2030 python scripts/ingest_predictions.py
   ```

   Inyecta los valores en el bloque de cada slot del ADMIN, reprotege y
   recalcula. Verifica abriendo el ADMIN en Excel real: los nombres aparecen en
   CLAS y no hay celdas `#REF!`/`#ERROR`.
4. Fija la **fecha de congelación** (freeze) en
   `.github/workflows/manual-predictions.yml`: la del primer partido. Las
   predicciones tardías no se procesan.

## 5. Secrets de GitHub Actions

Settings → Secrets and variables → Actions. Crear:

| Secret | De dónde sale |
|---|---|
| `FOOTBALL_API_KEY` | Registro gratuito en football-data.org. |
| `ANTHROPIC_API_KEY` | console.anthropic.com → API Keys. |
| `VPS_URL` | URL pública del bot: `https://<tu-dominio>/publish`. |
| `VPS_WEBHOOK_TOKEN` | Token aleatorio generado al desplegar el bot (paso 6). |
| `WHATSAPP_GROUP_ID_<POOL>` | Uno por porra, sufijo en MAYÚSCULAS (`..._PENYA-2030` → usa `_` en lugar de `-` si tu id lleva guiones). El bot imprime los IDs de todos sus grupos en consola al arrancar. |
| `GH_PAT` | PAT fine-grained *Contents: Read and write* (el cron commitea ADMIN + state.json de vuelta al repo). |
| `VPS_HEALTH_URL` | `https://<tu-dominio>/health` — lo usan el workflow de vigilancia y la auditoría. |

## 6. VPS y bot de WhatsApp

Guía completa en [`vps/README.md`](../vps/README.md). Resumen del camino feliz:

1. Node 22 + pm2 + dependencias de Chromium en el VPS; copiar `vps/*` y
   `npm install`.
2. HTTPS obligatorio (GitHub Actions lo exige): un **subdominio dinámico
   gratuito** (p. ej. DuckDNS) apuntando a la IP del VPS + **Caddy** como
   reverse proxy con certificado Let's Encrypt automático.
3. `.env` del VPS — copia `kit/.env.example` y rellena:
   `VPS_WEBHOOK_TOKEN`, `POOL_GROUPS` (⚠️ **entre comillas simples**, o el
   `source .env` se come las dobles del JSON), `ORGANIZER_JID`,
   `GH_DISPATCH_TOKEN`, `GH_REPO=owner/repo`, `ANTHROPIC_API_KEY`.
4. Primer arranque interactivo (`set -a && source .env && set +a && node
   server.js`): aparece un QR en consola → escanéalo desde WhatsApp en el móvil
   de la línea dedicada (Dispositivos vinculados). Anota los IDs de grupo que
   lista el bot.
5. Servicio permanente con pm2. ⚠️ **Gotcha**: pm2 no honra `env_file`; para
   que el bot vea el `.env` hay que cargarlo en la shell:

   ```bash
   pm2 delete porra-bot 2>/dev/null
   set -a && source .env && set +a
   pm2 start ecosystem.config.cjs --update-env && pm2 save
   ```

6. La sesión de WhatsApp caduca a ~14 días sin abrir la app: ritual semanal de
   abrir WhatsApp en el móvil del bot.

## 7. Smoke test E2E

Antes del primer partido real, un pase completo en seco (no publica nada, no
gasta tokens):

```bash
gh workflow run cron-matches.yml -f dry_run=true
gh run list --workflow=cron-matches.yml --limit 3   # todos los jobs verdes
```

Después, una prueba real acotada: mapea temporalmente un grupo de prueba en
`POOL_GROUPS` y verifica los comandos (`!ranking`, `!proximo`...). Los grupos
NO mapeados se ignoran en silencio — si el bot "no contesta", revisa el mapping.

## 8. Operación durante el torneo

- **El disparo del cron lo hace el dispatcher del VPS**, no el `schedule` de
  GitHub (GitHub retrasa o salta los schedules): dispara el workflow ~18 min
  antes de cada kickoff (mensaje de previa) y desde kickoff+110' cada 30 min
  hasta que el partido queda procesado en todas las porras. El calendario le
  llega vía `/sync`. Estado visible en `GET /health`.
- **Auditoría** (1 vez por semana, manual):

  ```bash
  VPS_HEALTH_URL=https://<tu-dominio>/health VPS_SSH=root@<ip-del-vps> \
    python scripts/audit_pool.py --pool <pool>
  ```

  Re-calcula puntos de forma independiente y contrasta API ↔ Excel ↔ datos
  publicados. Solo lectura: nunca corrige nada, solo señala.
- ⚠️ **Gotcha estructural**: la API puede **revisar un marcador días después**
  de que el cron lo procesara (p. ej. goles reasignados). Como el cron no
  reprocesa partidos ya anunciados, el Excel queda desfasado sin que nada
  falle — **solo la auditoría lo detecta**. Si pasa: corrige el ADMIN a mano,
  recalcula, commitea y avisa al grupo.
- Monitorización rápida: `gh run list --workflow=cron-matches.yml`,
  `pm2 logs porra-bot` en el VPS, `curl https://<tu-dominio>/health`.
