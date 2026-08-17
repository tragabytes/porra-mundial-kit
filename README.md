# porra-mundial-kit

Kit completo, en español, para montar y administrar una **porra automatizada de
un gran torneo de fútbol** (Mundial, Eurocopa) entre amigos o familia, usando
como motor de puntuación el Excel comercial de
[matejero.es](https://matejero.es/excel-porra-mundial/).

La idea: el Excel hace toda la matemática (fórmulas puras, sin macros) y este
kit automatiza **todo lo que lo rodea**. Cuando termina un partido:

1. Un cron de GitHub Actions detecta el resultado con la API de
   [football-data.org](https://www.football-data.org/).
2. Lo escribe en el Excel ADMIN y recalcula la clasificación (LibreOffice headless).
3. Genera una imagen del leaderboard y un comentario socarrón con la API de Anthropic.
4. Lo publica en el grupo de WhatsApp a través de un bot que corre en un VPS.

Sin base de datos, sin frontend pesado, sin microservicios: el estado vive en
el propio repo (el cron commitea el Excel y un `state.json` tras cada partido).

## Qué incluye

- **Cron de resultados** — procesa cada partido terminado y publica al grupo en minutos.
- **Comentarista con IA** — comentario personalizado por jugador (afinidades de
  club y selección), con datos calculados en Python para que la IA no invente cifras.
- **Bot de WhatsApp con comandos** — `!ranking`, `!hoy`, `!proximo`, `!puntos`,
  `!soy`, `!miprediccion`, `!mispuntos`, `!ayuda` y un chat con IA (`!claudio`)
  con rate limit por persona.
- **Panel web** — clasificación, ficha de cada jugador, calendario y cuadro de
  eliminatorias, servido desde el propio VPS bajo una URL secreta por porra.
- **Clasificación diaria** — imagen del leaderboard cada mañana (11:00, hora
  española peninsular — todo el sistema opera en zona `Europe/Madrid`).
- **Auditoría independiente** — script que re-calcula puntos por su cuenta y
  contrasta API ↔ Excel ↔ datos publicados, para dormir tranquilo.
- **Multi-porra** — N porras independientes (cada una con su Excel, sus jugadores
  y su grupo) compartiendo el mismo cron, el mismo bot y el mismo VPS.

## Requisitos

| Qué | Detalle |
|---|---|
| **Excel de matejero (versión ADMIN 25 jugadores)** | **Producto de pago** de Miguel Ángel Tejero, se compra en https://matejero.es/excel-porra-mundial/ — este kit **NO lo incluye ni lo redistribuye**; solo el código de este repo es libre. Compra también el template de jugador que lo acompaña. |
| Cuenta en football-data.org | Gratuita (free tier: 600 peticiones/día, sin tarjeta). |
| API key de Anthropic | Para el comentarista y el `!claudio`. Coste total de un torneo: unos pocos dólares. |
| VPS | Ubuntu 24.04, ~10 €/mes. Aloja el bot de WhatsApp, el dispatcher y el panel web. |
| Número de WhatsApp dedicado | Una eSIM de prepago barata. **NUNCA el número personal del organizador**: el riesgo de baneo por parte de WhatsApp existe (~2-5 % estimado durante un torneo). |
| Cuenta de GitHub | El cron corre en GitHub Actions (free tier suficiente). |

## Coste estimado (torneo de ~5-6 semanas)

| Concepto | Coste |
|---|---|
| GitHub Actions | 0 € (free tier) |
| football-data.org | 0 € (free tier) |
| VPS (~10 €/mes × 2 meses) | ~20 € |
| eSIM de prepago (línea del bot) | ~5 € |
| API de Anthropic (comentarios + bot) | ~5-15 $ |
| Excel de matejero | pago único (ver su web) |
| **Total** | **~30 €** + el Excel |

Al terminar el torneo: dar de baja la eSIM y apagar el VPS para no acumular coste.

## Estructura del repo

```
porra-mundial-kit/
├── pools/                     # 1 carpeta por porra
│   └── example/
│       ├── README.md          # aquí va TU ADMIN.xlsx comprado
│       └── players.json       # jugadores + afinidades (ejemplo ficticio)
├── predictions/               # gitignored: Excels de predicción de los jugadores
├── data/                      # recursos comunes: mapeos de equipos, partido→fila
├── scripts/                   # Python: ingests, orquestador del cron, auditoría
├── design/                    # plantilla HTML/Jinja del leaderboard y carteles
├── web/                       # panel web (SPA autocontenida)
├── vps/                       # bot Node.js de WhatsApp + guía de despliegue
├── .github/workflows/         # cron de partidos, clasificación diaria, salud
└── docs/                      # documentación del kit
```

## Documentación

- [`docs/montaje.md`](./docs/montaje.md) — runbook de montaje completo, de cero a producción.
- [`docs/arquitectura.md`](./docs/arquitectura.md) — cómo funciona por dentro.
- [`docs/administracion-excel.md`](./docs/administracion-excel.md) — el contrato con el Excel de matejero.
- [`vps/README.md`](./vps/README.md) — despliegue del bot de WhatsApp en el VPS.

## Licencia

El **código** de este repo se publica bajo licencia [MIT](./LICENSE).

El **Excel de puntuación NO forma parte de este repo**: es un producto comercial
de [Miguel Ángel Tejero (matejero.es)](https://matejero.es) y debes comprarlo
para usar este kit. No lo subas al repo (los `.xlsx` de `pools/` deben quedar
fuera del control de versiones en un repo público) ni lo redistribuyas.

## Descargo de responsabilidad

Proyecto personal sin ánimo de lucro. Sin afiliación alguna con matejero.es,
FIFA, UEFA, WhatsApp ni ninguna de las APIs utilizadas. El uso de un número de
WhatsApp con un cliente no oficial (whatsapp-web.js) puede violar sus términos
de servicio y acabar en baneo del número: usa siempre una línea dedicada y
asume ese riesgo.
