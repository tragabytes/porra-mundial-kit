# El contrato con el Excel de matejero

Los scripts de este kit **no reimplementan** la puntuación: leen y escriben
celdas concretas del Excel comercial de [matejero.es](https://matejero.es) y
dejan que sus fórmulas hagan el resto. Eso convierte el layout del Excel en un
**contrato**: si compras otra versión (u otra edición del torneo) con celdas en
otro sitio, hay que revalidar cada coordenada antes de fiarse.

## Qué comprar

- **Versión ADMIN de 25 jugadores** (sin macros, fórmulas puras, hojas
  protegidas) + el **template de jugador** que la acompaña, en
  https://matejero.es/excel-porra-mundial/.
- Es un producto de pago: cómpralo, no lo redistribuyas y no lo subas a ningún
  repo público. Este kit solo documenta cómo interactuar con él.

## Layout que los scripts esperan

Coordenadas verificadas contra la edición Mundial 2026 (48 equipos, 104
partidos). Otras ediciones pueden variar: validar contra el Excel real.

### Hoja `ADMIN`

- `D5` — **capacidad** de la plantilla (25). Ojo: es la capacidad, no el número
  real de jugadores inscritos.
- `D8:D47` — **baremo de puntos**. Viene a cero de fábrica; se escribe con
  `set_scoring.py` (las celdas están desbloqueadas: no hace falta desproteger).
- Bloques **"Pegar Valores"** por jugador: el slot `k` empieza en la columna
  `19 + (k-1)*3`. Ahí inyecta `ingest_predictions.py` nombre, predicciones de
  todas las rondas y cuadro de honor.
- **Cuadro de Honor**: filas 250-258 (campeón/subcampeón/3º y los tres
  escalones de bota y balón de oro). Los picks de cada jugador van en su
  columna de slot; los **resultados oficiales** de botas/balones se escriben a
  mano en `Idiomas!G41:G46`. ⚠️ Las celdas `Idiomas!D41:D46` NO son de texto:
  son `=IF($B$2,G4x,H4x)` (selector de idioma) — **escribe en la columna G,
  nunca en la D**.

### Hoja `WORLDCUP`

- Un partido por fila: equipos en las columnas AA/AF, goles oficiales en AC/AD
  (y penaltis en AB/AE cuando un cruce se decide en la tanda). El mapeo
  partido→fila lo precalcula `bootstrap_match_rows.py` en
  `data/match_row_map.json`; las eliminatorias se resuelven en runtime.
- La **final** está en la fila 147 y el **3er/4º puesto** en la 143: de ahí se
  autoderivan campeón, subcampeón y tercero para el Cuadro de Honor.

### Hoja `CLAS`

- Clasificación calculada: nombre en la columna C, total en la D y **desglose
  por categorías en las columnas E..S** (signo, exacto, posiciones de grupo,
  clasificados por ronda, honor...). La auditoría contrasta que D == suma(E..S).

## Reglas de oro

1. **Cierra el Excel antes de que escriba cualquier script** (`ingest_*`,
   `set_scoring`, recalc). Con el archivo abierto, Windows lo bloquea y el
   script muere con `PermissionError`.
2. **Recalcula siempre con LibreOffice tras escribir.** openpyxl no ejecuta
   fórmulas: escribe valores y deja los cacheados obsoletos. Los helpers del
   kit ya invocan LibreOffice headless; si tocas celdas a mano con openpyxl,
   recalcula tú.
3. **No leas la hoja `Stats`.** Usa funciones de Excel 2021+ que LibreOffice no
   calcula: sus valores cacheados pueden ser basura tras un recalc headless.
   Los scripts del kit la ignoran a propósito.
4. **Los COUNTIF del honor son sensibles a acentos y grafías.** "Mbappé" y
   "Mbappe" NO cuentan igual: al escribir los oficiales en `Idiomas!G41:G46`,
   usa exactamente la misma grafía que aparece en los picks de los jugadores
   (o normaliza los picks antes).
5. Los scripts solo tocan lo mínimo (goles en WORLDCUP, bloques de pegado,
   baremo): CLAS, Stats y el resto son fórmulas del producto y no se escriben
   jamás.

## Copyright

El Excel es propiedad de Miguel Ángel Tejero (matejero.es). Este documento
describe únicamente las coordenadas con las que interactúa el código libre del
kit; no reproduce fórmulas ni contenido del producto.
