"""Lectores y scoring independiente para la auditoría semanal (audit_pool.py).

Este módulo es DELIBERADAMENTE ligero: solo importa lib_excel y set_scoring (sin
LibreOffice/Playwright/Anthropic), para poder correr la auditoría en local sin
las dependencias pesadas del cron. NO importa ingest_match_results.

Responsabilidades:
- Leer los resultados crudos de la hoja WORLDCUP (para contrastarlos con la API).
- Re-calcular puntos de forma independiente del Excel ("marcador en la sombra"):
  por ahora la parte de FASE DE GRUPOS por partido (signo/exacto), que coincide
  con la fórmula del Excel. Las posiciones de grupo, eliminatorias y cuadro de
  honor se añadirán en módulos posteriores.
- Escanear el workbook en busca de celdas de error (#REF!, #NAME?, ...).

Las constantes de columnas de WORLDCUP replican las de ingest_match_results.py
(estables; documentadas ahí). El baremo de grupos sale de set_scoring.SCORING,
la misma fuente que escribe las celdas del Excel.
"""

from __future__ import annotations

import re
from pathlib import Path

from lib_excel import load_values, build_admin_row_map, parse_prediction
from set_scoring import SCORING

# Columnas de la hoja WORLDCUP (1-indexed, openpyxl).
COL_HOME_TEAM = 27   # AA
COL_HOME_PENS = 28   # AB (penaltis local, solo knockouts a penaltis)
COL_HOME_SCORE = 29  # AC (goles local 90'+ET, sin penaltis)
COL_AWAY_SCORE = 30  # AD
COL_AWAY_PENS = 31   # AE
COL_AWAY_TEAM = 32   # AF

# Baremo de un partido de FASE DE GRUPOS (misma fuente que el Excel).
GRUPOS_SIGNO_PTS = SCORING[8]    # 1
GRUPOS_EXACTO_PTS = SCORING[10]  # +3

# Hoja CLAS (clasificación): nombre y total por jugador.
CLAS_FIRST_DATA_ROW = 5
CLAS_NAME_COL = 3   # C
CLAS_TOTAL_COL = 4  # D


def read_ranking(admin_path: str | Path) -> list[dict]:
    """[{name, points, position}] ordenado por puntos desc, desde la hoja CLAS.

    Copia ligera de ingest_match_results.read_ranking (duplicada a propósito para
    no arrastrar las dependencias pesadas del cron). Asume recálculo previo.
    """
    wb = load_values(admin_path)
    ws = wb["CLAS"]
    n_players = int(wb["ADMIN"]["D5"].value or 25)
    rows = []
    for slot in range(1, n_players + 1):
        r = CLAS_FIRST_DATA_ROW + slot - 1
        name = ws.cell(row=r, column=CLAS_NAME_COL).value
        if not name or (isinstance(name, str) and name.startswith("Pegar")):
            continue
        rows.append({
            "name": str(name),
            "points": int(ws.cell(row=r, column=CLAS_TOTAL_COL).value or 0),
        })
    rows.sort(key=lambda x: -x["points"])
    for i, r in enumerate(rows, start=1):
        r["position"] = i
    return rows

# Una celda con error de fórmula tras el recalc de LibreOffice. openpyxl
# (data_only) devuelve el texto del error literal.
_ERROR_RE = re.compile(r"#(REF|NAME|VALUE|DIV/0|N/A|NUM|NULL)[!?]?", re.IGNORECASE)


def result_sign(home: int, away: int) -> str:
    """'1' (gana local), '2' (gana visitante) o 'X' (empate)."""
    return "1" if home > away else ("2" if away > home else "X")


def grupos_match_points(pred: tuple[str, int, int], real_home: int, real_away: int) -> int:
    """Puntos de un jugador en un partido de FASE DE GRUPOS según su predicción.

    `pred` es (signo, goles_local, goles_visitante) de parse_prediction. Idéntico
    a ingest_match_results.grupos_match_points (duplicado a propósito para no
    arrastrar las dependencias pesadas del cron en la auditoría; un test asegura
    que ambos coinciden).
    """
    _sgn, ph, pa = pred
    if ph == real_home and pa == real_away:
        return GRUPOS_SIGNO_PTS + GRUPOS_EXACTO_PTS
    if result_sign(ph, pa) == result_sign(real_home, real_away):
        return GRUPOS_SIGNO_PTS
    return 0


# Desglose por categoría de la hoja CLAS (cols E..S). Lo expone el propio
# matejero: cada jugador, sus puntos por concepto, que suman el total (col D).
# Clave -> (columna, ¿re-calculable de forma independiente desde marcadores?).
# Las "Pos. Grupos" y "Equipos N/M" las DERIVA el Excel de las predicciones de
# partido con su propia lógica de desempates (hoja Combinaciones3), opaca; por eso
# no se re-calculan de forma independiente, solo se leen y se cruzan por consistencia.
CLAS_CATEGORIES: dict[str, tuple[int, bool]] = {
    "F. Grupos":       (5, True),
    "Pos. Grupos":     (6, False),
    "Equipos 1/16":    (7, False),
    "Partidos 1/16":   (8, True),
    "Equipos 1/8":     (9, False),
    "Partidos 1/8":    (10, True),
    "Equipos 1/4":     (11, False),
    "Partidos 1/4":    (12, True),
    "Equipos 1/2":     (13, False),
    "Partidos 1/2":    (14, True),
    "Equipos 3-4":     (15, False),
    "Equipos Final":   (16, False),
    "Partido 3-4":     (17, True),
    "Partido Final":   (18, True),
    "Cuadro de Honor": (19, False),
}
CLAS_TOTAL_COL_BREAKDOWN = 4  # D


def read_clas_breakdown(admin_path: str | Path) -> dict[str, dict]:
    """{nombre: {"total": int, "cats": {categoria: int}}} desde la hoja CLAS.

    Permite verificar que el total (col D) == suma de categorías (cols E..S) y
    contrastar las categorías re-calculables de forma independiente. Asume recalc
    previo (valores cacheados).
    """
    wb = load_values(admin_path)
    ws = wb["CLAS"]
    n_players = int(wb["ADMIN"]["D5"].value or 25)
    out: dict[str, dict] = {}
    for slot in range(1, n_players + 1):
        r = CLAS_FIRST_DATA_ROW + slot - 1
        name = ws.cell(row=r, column=CLAS_NAME_COL).value
        if not name or (isinstance(name, str) and name.startswith("Pegar")):
            continue
        cats = {}
        for cat, (col, _indep) in CLAS_CATEGORIES.items():
            v = ws.cell(row=r, column=col).value
            cats[cat] = int(v) if isinstance(v, (int, float)) else 0
        total = ws.cell(row=r, column=CLAS_TOTAL_COL_BREAKDOWN).value
        out[str(name)] = {"total": int(total) if isinstance(total, (int, float)) else 0,
                          "cats": cats}
    return out


def read_worldcup_results(admin_path: str | Path) -> list[dict]:
    """Resultados crudos de cada fila de partido de la hoja WORLDCUP.

    Usa build_admin_row_map() para saber qué filas de WORLDCUP son partidos.
    Devuelve, por fila con ambos equipos resueltos:
      [{row, home, away, home_score, away_score, home_pens, away_pens, played}]
    `played` = True si hay ambos goles. Los nombres de equipo van en español
    (la hoja WORLDCUP ya los trae resueltos). Eliminatorias sin resolver (equipos
    None/placeholder) se omiten.
    """
    wb = load_values(admin_path)
    wc = wb["WORLDCUP"]
    rows = sorted(build_admin_row_map(admin_path))  # filas de WORLDCUP con partido
    out: list[dict] = []
    for r in rows:
        home = wc.cell(row=r, column=COL_HOME_TEAM).value
        away = wc.cell(row=r, column=COL_AWAY_TEAM).value
        home = home.strip() if isinstance(home, str) and home.strip() else None
        away = away.strip() if isinstance(away, str) and away.strip() else None
        if not home or not away:
            continue
        hs = wc.cell(row=r, column=COL_HOME_SCORE).value
        as_ = wc.cell(row=r, column=COL_AWAY_SCORE).value
        hp = wc.cell(row=r, column=COL_HOME_PENS).value
        ap = wc.cell(row=r, column=COL_AWAY_PENS).value
        played = isinstance(hs, (int, float)) and isinstance(as_, (int, float))
        out.append({
            "row": r,
            "home": home,
            "away": away,
            "home_score": int(hs) if isinstance(hs, (int, float)) else None,
            "away_score": int(as_) if isinstance(as_, (int, float)) else None,
            "home_pens": int(hp) if isinstance(hp, (int, float)) else None,
            "away_pens": int(ap) if isinstance(ap, (int, float)) else None,
            "played": played,
        })
    return out


def find_error_cells(admin_path: str | Path, sheets: list[str],
                     exclude: dict[str, set[str]] | None = None) -> list[str]:
    """Lista de 'Hoja!Celda' cuyo valor cacheado es un error de fórmula.

    `sheets` = hojas a escanear. `exclude` = {hoja: {coordenada, ...}} de celdas
    rotas CONOCIDAS que se ignoran (p.ej. Stats N:T con funciones Excel 2021+).
    Para excluir una columna entera se pasa su letra (p.ej. 'N') además de
    coordenadas concretas.
    """
    exclude = exclude or {}
    wb = load_values(admin_path)
    bad: list[str] = []
    for sheet in sheets:
        if sheet not in wb.sheetnames:
            continue
        ws = wb[sheet]
        ex_cells = exclude.get(sheet, set())
        for row in ws.iter_rows():
            for cell in row:
                v = cell.value
                if not isinstance(v, str) or not _ERROR_RE.search(v):
                    continue
                col_letter = cell.column_letter
                if cell.coordinate in ex_cells or col_letter in ex_cells:
                    continue
                bad.append(f"{sheet}!{cell.coordinate} = {v}")
    return bad


# ---------------------------------------------------------------------------
# Eliminatorias: re-cálculo independiente de "Equipos N/M" (clasificados).
#
# El matejero puntúa, por ronda, los EQUIPOS que cada jugador predijo que se
# clasifican: en ADMIN, la columna de cada jugador (S, V, Y…) lleva su lista de
# equipos predichos en un bloque de filas; la celda de puntos hace
# COUNTIF(<clasificados_reales>, <equipo_predicho>) * <baremo>. Esto re-calcula
# esa cuenta de forma independiente para la auditoría (módulo 9) y alimenta las
# features de fase final (sello ×N, aciertos por jugador).
#
# Bloque por categoría: (categoría CLAS, fila_inicio, fila_fin, fila_baremo_D).
# Verificado leyendo las fórmulas COUNTIF reales del ADMIN.
# ---------------------------------------------------------------------------

KO_EQUIPOS_BLOCKS: list[tuple[str, int, int, int]] = [
    ("Equipos 1/16", 130, 161, 15),  # clasificados a dieciseisavos
    ("Equipos 1/8",  182, 197, 19),  # a octavos
    ("Equipos 1/4",  210, 217, 23),  # a cuartos
    ("Equipos 1/2",  226, 229, 27),  # a semifinales
    ("Equipos 3-4",  236, 237, 31),  # al partido por el 3º/4º puesto
    ("Equipos Final", 240, 241, 32), # a la final
]

# Etapa de la API (lib_football_api) -> categoría CLAS de clasificados.
KO_STAGE_TO_EQUIPOS: dict[str, str] = {
    "LAST_32": "Equipos 1/16",
    "LAST_16": "Equipos 1/8",
    "QUARTER_FINALS": "Equipos 1/4",
    "SEMI_FINALS": "Equipos 1/2",
    "THIRD_PLACE": "Equipos 3-4",
    "FINAL": "Equipos Final",
}

# Columna M (clasificados reales) en ADMIN; col de slot del jugador 1 = S (19).
_KO_REAL_COL = 13          # M
_KO_NAME_ROW = 5
_KO_FIRST_SLOT_COL = 19    # S


def _norm_team(s) -> str:
    return str(s).strip() if isinstance(s, str) else ""


def knockout_equipos_baremo() -> dict[str, int]:
    """{categoría: puntos por equipo clasificado acertado}, desde set_scoring."""
    return {cat: SCORING[row] for cat, _, _, row in KO_EQUIPOS_BLOCKS}


def read_knockout_qualifier_picks(admin_path: str | Path) -> dict[str, dict[str, list[str]]]:
    """{nombre: {categoría: [equipos que predijo que se clasifican]}}.

    Lee, por jugador, los bloques S de cada ronda (los nombres de país que el
    re-ingest pega desde Pool!C). Asume recálculo previo (valores cacheados).
    """
    wb = load_values(admin_path)
    admin = wb["ADMIN"]
    n_players = int(wb["ADMIN"]["D5"].value or 25)
    out: dict[str, dict[str, list[str]]] = {}
    for slot in range(1, n_players + 1):
        col = _KO_FIRST_SLOT_COL + (slot - 1) * 3
        name = admin.cell(row=_KO_NAME_ROW, column=col).value
        if not name or (isinstance(name, str) and name.startswith("Pegar")):
            continue
        cats: dict[str, list[str]] = {}
        for cat, r0, r1, _ in KO_EQUIPOS_BLOCKS:
            teams = [_norm_team(admin.cell(row=r, column=col).value)
                     for r in range(r0, r1 + 1)]
            cats[cat] = [t for t in teams if t]
        out[str(name)] = cats
    return out


def knockout_actuals_from_api(api_matches: list[dict], teams_es: dict) -> dict[str, set]:
    """{categoría: set(equipos ES que realmente pasan)} desde los partidos KO de la API.

    Los cruces aún sin resolver llegan con home/away None y no suman (ronda parcial).
    """
    out: dict[str, set] = {cat: set() for cat, *_ in KO_EQUIPOS_BLOCKS}
    for m in api_matches:
        cat = KO_STAGE_TO_EQUIPOS.get(m.get("stage"))
        if not cat:
            continue
        for k in ("home", "away"):
            es = teams_es.get(m.get(k)) if m.get(k) else None
            if es:
                out[cat].add(es)
    return out


def knockout_actuals_from_worldcup(admin_path: str | Path) -> dict[str, set]:
    """{categoría: set(equipos)} desde la columna M del ADMIN (clasificados que el
    Excel resuelve por INDEX/MATCH). Menos independiente que la API, pero offline.
    Descarta placeholders no resueltos (contienen dígitos: '1I', '3ABCDF', …).
    """
    admin = load_values(admin_path)["ADMIN"]
    out: dict[str, set] = {}
    for cat, r0, r1, _ in KO_EQUIPOS_BLOCKS:
        teams = set()
        for r in range(r0, r1 + 1):
            t = _norm_team(admin.cell(row=r, column=_KO_REAL_COL).value)
            if t and not any(ch.isdigit() for ch in t):
                teams.add(t)
        out[cat] = teams
    return out


# Bloques de MARCADOR KO ("Partidos N/M"): el cruce + resultado que predijo cada
# jugador, en celdas "Local-Visitante·signo|gl-gv". Para la feature 4 (consultar
# las predicciones propias de la fase en curso).
KO_MATCH_BLOCKS: list[tuple[str, int, int]] = [
    ("Dieciseisavos", 164, 179),
    ("Octavos", 200, 207),
    ("Cuartos", 220, 223),
    ("Semifinales", 232, 233),
    ("3º y 4º puesto", 244, 244),
    ("Final", 247, 247),
]


def read_knockout_matchup_picks(admin_path: str | Path) -> dict[str, dict[str, list[dict]]]:
    """{nombre: {ronda: [{"cruce": "Local-Visitante", "marcador": "gl-gv"}]}}.

    Lee las celdas de marcador KO de cada jugador. El cruce es el prefijo antes del
    separador '·'; el marcador sale de parse_prediction (último token signo|gl-gv).
    """
    wb = load_values(admin_path)
    admin = wb["ADMIN"]
    n_players = int(wb["ADMIN"]["D5"].value or 25)
    out: dict[str, dict[str, list[dict]]] = {}
    for slot in range(1, n_players + 1):
        col = _KO_FIRST_SLOT_COL + (slot - 1) * 3
        name = admin.cell(row=_KO_NAME_ROW, column=col).value
        if not name or (isinstance(name, str) and name.startswith("Pegar")):
            continue
        rounds: dict[str, list[dict]] = {}
        for rlabel, r0, r1 in KO_MATCH_BLOCKS:
            items = []
            for r in range(r0, r1 + 1):
                v = admin.cell(row=r, column=col).value
                if not isinstance(v, str) or "·" not in v:
                    continue
                cruce = v.rsplit("·", 1)[0].strip()
                p = parse_prediction(v)
                if cruce:
                    items.append({"cruce": cruce,
                                  "marcador": f"{p[1]}-{p[2]}" if p else ""})
            if items:
                rounds[rlabel] = items
        out[str(name)] = rounds
    return out


# Etapa de la API -> etiqueta de ronda de KO_MATCH_BLOCKS (bloques de marcador).
KO_STAGE_TO_RONDA: dict[str, str] = {
    "LAST_32": "Dieciseisavos",
    "LAST_16": "Octavos",
    "QUARTER_FINALS": "Cuartos",
    "SEMI_FINALS": "Semifinales",
    "THIRD_PLACE": "3º y 4º puesto",
    "FINAL": "Final",
}

# Puntos por acertar el RESULTADO EXACTO de un partido KO, por ronda (signo +
# exacto, filas de SCORING). Lo usa el mensaje ⚽💥 de eliminatorias.
KO_RONDA_EXACTO_PTS: dict[str, int] = {
    "Dieciseisavos": SCORING[16] + SCORING[18],
    "Octavos": SCORING[20] + SCORING[22],
    "Cuartos": SCORING[24] + SCORING[26],
    "Semifinales": SCORING[28] + SCORING[30],
    "3º y 4º puesto": SCORING[33] + SCORING[35],
    "Final": SCORING[36] + SCORING[38],
}

# Puntos por acertar SOLO EL SIGNO de un partido KO, por ronda (fila de signo de
# SCORING, sin el extra de exacto). Mismas claves que KO_RONDA_EXACTO_PTS.
KO_RONDA_SIGNO_PTS: dict[str, int] = {
    "Dieciseisavos": SCORING[16],
    "Octavos": SCORING[20],
    "Cuartos": SCORING[24],
    "Semifinales": SCORING[28],
    "3º y 4º puesto": SCORING[33],
    "Final": SCORING[36],
}


def knockout_match_points(pred: tuple[str, int, int], real_home: int, real_away: int,
                          ronda: str) -> int:
    """Puntos de un jugador en un partido de ELIMINATORIA según su predicción.

    Espejo de grupos_match_points pero con el baremo KO de `ronda` (clave de
    KO_RONDA_*_PTS). `pred` es (signo, goles_local, goles_visitante) en la
    orientación real del cruce (la que devuelve knockout_predicciones). La
    referencia real_home/real_away es el marcador a 120' (cols AC/AD), igual que
    el Excel para el exacto. Presupone que `pred` es de quien predijo ESTE cruce:
    solo puntúa signo/exacto, no los "equipos clasificados" (categoría aparte).
    """
    _sgn, ph, pa = pred
    if ph == real_home and pa == real_away:
        return KO_RONDA_EXACTO_PTS[ronda]
    if result_sign(ph, pa) == result_sign(real_home, real_away):
        return KO_RONDA_SIGNO_PTS[ronda]
    return 0


def ronda_for_admin_row(arow: int) -> str | None:
    """Etiqueta de ronda KO (KO_MATCH_BLOCKS) a la que pertenece una fila ADMIN,
    o None si la fila no es de marcador de eliminatoria (p.ej. fase de grupos)."""
    for label, r0, r1 in KO_MATCH_BLOCKS:
        if r0 <= arow <= r1:
            return label
    return None


def _flip_marcador(mar: str) -> str:
    """'1-2' -> '2-1'. Devuelve mar sin tocar si no tiene el formato esperado."""
    parts = mar.split("-")
    return f"{parts[1]}-{parts[0]}" if len(parts) == 2 else mar


def knockout_marcadores(matchups: dict[str, dict[str, list[dict]]], ronda: str,
                        home_es: str, away_es: str,
                        rh: int | None = None, ra: int | None = None) -> dict[str, dict]:
    """Predicciones de un CRUCE concreto de eliminatoria, fieles al baremo.

    `matchups` es la salida de read_knockout_matchup_picks. Compara el cruce de
    cada jugador (string completo "Local-Visitante", sin trocear: hay países con
    guion) contra el cruce real en ambas orientaciones, igual que el COUNTIF de la
    columna T del ADMIN. Devuelve SOLO a quien predijo este cruce:
      {nombre: {"marcador": "<gl-gv en orientación real>", "exacto": bool|None}}
    El marcador se normaliza a la orientación real (home_es-away_es). `exacto` es
    None si no hay resultado (rh/ra None, p.ej. previa pre-partido); si lo hay,
    True cuando el marcador predicho coincide con el real. Penaltis: rh/ra es el
    120' (AC/AD), la misma referencia que usa el Excel para el exacto."""
    ha = f"{home_es}-{away_es}"
    ah = f"{away_es}-{home_es}"
    real = f"{rh}-{ra}" if rh is not None and ra is not None else None
    out: dict[str, dict] = {}
    for name, rounds in matchups.items():
        for p in rounds.get(ronda, []):
            cruce = (p.get("cruce") or "").strip()
            mar = (p.get("marcador") or "").strip()
            if cruce == ha:
                disp = mar
            elif cruce == ah:
                disp = _flip_marcador(mar)
            else:
                continue
            out[name] = {"marcador": disp,
                         "exacto": (disp == real) if real is not None else None}
            break
    return out


def knockout_predicciones(matchups: dict[str, dict[str, list[dict]]], ronda: str,
                          home_es: str, away_es: str) -> dict[str, tuple]:
    """{nombre: (signo, gl, gv)} de quien predijo este cruce KO, en orientación
    real. Mismo formato que lib_excel.read_match_predictions, para alimentar la
    previa y los refrescos del bot en eliminatorias sin la confusión por fila."""
    out: dict[str, tuple] = {}
    for name, v in knockout_marcadores(matchups, ronda, home_es, away_es).items():
        try:
            gl, gv = (int(x) for x in v["marcador"].split("-"))
        except (ValueError, AttributeError):
            continue
        signo = "1" if gl > gv else "2" if gl < gv else "X"
        out[name] = (signo, gl, gv)
    return out


def _team_in_cruce(cruce: str, team: str) -> bool:
    """True si `team` es uno de los dos lados de `cruce` ('Local-Visitante').

    No trocea por '-' (hay países con guion, p.ej. 'Bosnia-Herzegovina'): comprueba
    que el nombre completo del equipo está en el lado local (prefijo 'team-') o
    visitante (sufijo '-team'), mismo criterio sin ambigüedad que knockout_marcadores."""
    return cruce.startswith(f"{team}-") or cruce.endswith(f"-{team}")


def knockout_clasificacion(matchups: dict[str, dict[str, list[dict]]], ronda: str,
                           home_es: str, away_es: str,
                           rh: int | None = None, ra: int | None = None) -> dict:
    """Clasifica a TODOS los jugadores frente a un cruce KO en 4 grupos excluyentes.

    `matchups` es la salida de read_knockout_matchup_picks (incluye a todos los
    jugadores nombrados, aunque tengan la ronda vacía → caen en "nada"). Devuelve:
      {
        "cruce":     {nombre: {"marcador", "signo", "exacto"}},  # acertaron el cruce
        "ambos":     [nombres],          # predijeron los dos equipos, en cruces distintos
        "un_equipo": {nombre: "equipo"}, # solo uno de los dos (se guarda cuál)
        "nada":      [nombres],          # ninguno de los dos
      }
    El grupo "cruce" sale de knockout_marcadores (fiel al baremo: solo quien predijo
    este cruce), con `signo` derivado del marcador. `exacto` es None si no hay
    resultado (rh/ra None, p.ej. previa pre-partido)."""
    cruce: dict[str, dict] = {}
    for name, v in knockout_marcadores(matchups, ronda, home_es, away_es, rh, ra).items():
        try:
            gl, gv = (int(x) for x in v["marcador"].split("-"))
            signo = "1" if gl > gv else "2" if gl < gv else "X"
        except (ValueError, AttributeError):
            signo = None
        cruce[name] = {**v, "signo": signo}

    ambos: list[str] = []
    un_equipo: dict[str, str] = {}
    nada: list[str] = []
    for name, rounds in matchups.items():
        if name in cruce:
            continue
        cruces = [(p.get("cruce") or "").strip() for p in rounds.get(ronda, [])]
        h = any(_team_in_cruce(c, home_es) for c in cruces)
        a = any(_team_in_cruce(c, away_es) for c in cruces)
        if h and a:
            ambos.append(name)
        elif h:
            un_equipo[name] = home_es
        elif a:
            un_equipo[name] = away_es
        else:
            nada.append(name)
    return {"cruce": cruce, "ambos": ambos, "un_equipo": un_equipo, "nada": nada}


def knockout_qualifier_recompute(picks: dict[str, dict[str, list[str]]],
                                 actual_by_cat: dict[str, set],
                                 baremo: dict[str, int]) -> dict[str, dict[str, dict]]:
    """Por jugador/categoría: {n, puntos, aciertos[], fallos[], resuelta}.

    `aciertos` = equipos predichos que SÍ pasan (firme en cuanto el equipo aparece).
    `fallos` = predichos que no pasan, SOLO si la ronda está resuelta (actual no vacío);
    si no, [] (un equipo aún no aparecido no es fallo). `puntos` = n * baremo (cota
    inferior mientras la ronda no esté 100% resuelta).
    """
    out: dict[str, dict[str, dict]] = {}
    for name, cats in picks.items():
        res: dict[str, dict] = {}
        for cat, teams in cats.items():
            actual = actual_by_cat.get(cat, set())
            tset = set(teams)
            aciertos = sorted(tset & actual)
            fallos = sorted(tset - actual) if actual else []
            res[cat] = {
                "n": len(aciertos),
                "puntos": len(aciertos) * baremo.get(cat, 0),
                "aciertos": aciertos,
                "fallos": fallos,
                "resuelta": bool(actual),
            }
        out[name] = res
    return out
