"""Auditoría semanal de una porra, end-to-end (lánzala tú; no hay nada programado).

Verifica de forma INDEPENDIENTE que el pipeline (resultados → Excel → puntuaciones →
datos que ve la gente) está sano. El Excel sigue siendo la fuente de verdad para
los pagos: esta auditoría solo CONTRASTA y marca lo que no cuadra para revisión
humana — nunca corrige nada.

Es de SOLO LECTURA sobre producción: el recalc se hace sobre una COPIA temporal;
no toca pools/<pool>/ADMIN.xlsx ni state.json. Sus únicas escrituras son sus
propios artefactos en audit/ (informe + snapshot de totales).

Uso:
  python scripts/audit_pool.py --pool familia
  python scripts/audit_pool.py --pool amigos --no-recalc   # más rápido, sin LibreOffice
  python scripts/audit_pool.py --pool familia --no-api      # sin red (offline)

Salida:
  audit/reports/audit_<pool>_<YYYYMMDD-HHMM>.md   (informe legible)
  audit/snapshots/<pool>.json                      (totales por jugador, para monotonía)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_excel import load, load_values, build_admin_row_map, read_matches_predictions
import lib_scoring as sc
from lib_scoring import read_ranking

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
AUDIT_DIR = PROJECT_ROOT / "audit"

# Caducidad conocida de tokens: opcional, en data/token_expiries.json (gitignored),
# formato {"GH_PAT": "YYYY-MM-DD", ...}. Avisar si quedan <14 días.
try:
    TOKEN_EXPIRIES = json.loads((DATA_DIR / "token_expiries.json").read_text(encoding="utf-8"))
except FileNotFoundError:
    TOKEN_EXPIRIES = {}

# Salud del VPS: por entorno. Sin ellas, los checks correspondientes se saltan con aviso.
VPS_HEALTH_URL = os.environ.get("VPS_HEALTH_URL")  # p.ej. https://<tu-dominio>/health
VPS_SSH = os.environ.get("VPS_SSH")  # p.ej. root@<ip-del-vps> — para --with-vps-log

# Celdas/columnas rotas CONOCIDAS que el escáner de errores debe ignorar
# (Stats usa funciones Excel 2021+ que LibreOffice no calcula; ver historial 11/06).
KNOWN_BROKEN = {
    "WORLDCUP": {"Z100"},
    "Stats": {"N", "O", "P", "Q", "R", "S", "T", "I8", "AD35", "AE35"},
}

# --- Framework de secciones -------------------------------------------------

OK, WARN, FAIL, INFO = "OK", "WARN", "FAIL", "INFO"
ICON = {OK: "✅", WARN: "⚠️", FAIL: "❌", INFO: "ℹ️"}
_SEV = {OK: 0, INFO: 0, WARN: 1, FAIL: 2}


class Section:
    def __init__(self, title: str, *, id: str = "", que: str = "", como: str = ""):
        self.title = title
        self.id = id          # slug estable para el JSON
        self.que = que        # QUÉ se comprueba (lenguaje natural)
        self.como = como      # CÓMO: fuentes, columnas, baremo (para reproducir)
        self.status = OK
        self.lines: list[tuple[str, str]] = []
        self.evidencia: list = []   # datos concretos para re-verificar de forma independiente

    def add(self, status: str, msg: str) -> None:
        self.lines.append((status, msg))
        if _SEV[status] > _SEV[self.status]:
            self.status = status

    def ok(self, msg): self.add(OK, msg)
    def warn(self, msg): self.add(WARN, msg)
    def fail(self, msg): self.add(FAIL, msg)
    def note(self, msg): self.add(INFO, msg)
    def evid(self, item): self.evidencia.append(item)

    def to_json(self) -> dict:
        oks = [m for st, m in self.lines if st == OK]
        return {
            "id": self.id,
            "title": self.title,
            "que": self.que,
            "como": self.como,
            "status": self.status,
            "resultado": "; ".join(oks) if oks else (self.lines[0][1] if self.lines else ""),
            "hallazgos": [{"status": st, "msg": m} for st, m in self.lines if st in (WARN, FAIL)],
            "evidencia": self.evidencia,
            "lineas": [{"status": st, "msg": m} for st, m in self.lines],
        }


# --- Carga de contexto ------------------------------------------------------

class Ctx:
    def __init__(self, pool: str, args):
        self.pool = pool
        self.args = args
        self.pool_dir = PROJECT_ROOT / "pools" / pool
        self.admin = self.pool_dir / "ADMIN.xlsx"
        self.state_path = self.pool_dir / "state.json"
        self.players_path = self.pool_dir / "players.json"
        self.state = _load_json(self.state_path, default={})
        self.players = _load_json(self.players_path, default=[])
        self.teams_en_es = _load_json(DATA_DIR / "teams_en_es.json", default={})
        self.match_row_map = _load_json(DATA_DIR / "match_row_map.json", default={})
        self.broadcast = _load_json(DATA_DIR / "broadcast_2026.json", default={})
        # Datos de la API (se rellenan en setup si hay red).
        self.api_finished: list[dict] | None = None
        self.api_next: dict | None = None
        self.api_today: list[dict] | None = None
        self.api_all: list[dict] | None = None  # todos los partidos (para clasificados KO)

    def fetch_api(self) -> None:
        if self.args.no_api:
            return
        from lib_football_api import (fetch_finished_matches,
                                      fetch_next_scheduled_match, fetch_today_matches,
                                      fetch_all_matches)
        from datetime import date
        self.api_finished = fetch_finished_matches(
            date_from=date(2026, 6, 1), date_to=datetime.now(timezone.utc).date())
        self.api_next = fetch_next_scheduled_match()
        self.api_today = fetch_today_matches()
        self.api_all = fetch_all_matches()

    def es(self, en_name: str) -> str | None:
        return self.teams_en_es.get(en_name)


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except Exception as e:
        print(f"WARN: no pude leer {path.name}: {e}", file=sys.stderr)
        return default


# --- Módulo 1: Resultados del Excel vs API ----------------------------------

def check_results_vs_api(ctx: Ctx) -> Section:
    s = Section(
        "1. Resultados del Excel vs API oficial",
        id="resultados_vs_api",
        que="Que cada marcador y penaltis del Excel coincidan con la API oficial, y que no falten ni sobren partidos.",
        como="WORLDCUP cols AA/AC/AD/AB/AE/AF (equipos, goles 90'+ET, penaltis); fetch_finished_matches(rango de fechas) de football-data.org; mapear EN→ES con teams_en_es.json; comparar marcador y penaltis; FINISHED en la API no registrados = faltantes; registrados sin estar en la API = sobrantes.")
    if ctx.api_finished is None:
        s.note("Saltado (--no-api o sin red).")
        return s

    excel = [m for m in sc.read_worldcup_results(ctx.admin) if m["played"]]
    # API → resultados en español, indexados por par (home_es, away_es).
    api_by_pair: dict[tuple[str, str], dict] = {}
    unmapped = []
    for m in ctx.api_finished:
        h, a = ctx.es(m["home"]), ctx.es(m["away"])
        if not h or not a:
            unmapped.append(f"{m['home']} vs {m['away']}")
            continue
        api_by_pair[(h, a)] = m

    mismatches = 0
    for em in excel:
        key = (em["home"], em["away"])
        am = api_by_pair.get(key)
        # Evidencia para re-verificar de forma independiente.
        s.evid({
            "partido": f"{em['home']}-{em['away']}",
            "excel": f"{em['home_score']}-{em['away_score']}",
            "api": (f"{am['home_score']}-{am['away_score']}" if am else None),
        })
        if am is None:
            s.warn(f"En el Excel pero NO en la API (FINISHED): {em['home']} {em['home_score']}-{em['away_score']} {em['away']} (fila {em['row']})")
            mismatches += 1
            continue
        if em["home_score"] != am["home_score"] or em["away_score"] != am["away_score"]:
            s.fail(f"MARCADOR DISTINTO: {em['home']}-{em['away']} → Excel {em['home_score']}-{em['away_score']} vs API {am['home_score']}-{am['away_score']}")
            mismatches += 1
        # Penaltis (solo si la API los da).
        if am.get("home_penalties") is not None:
            if em["home_pens"] != am["home_penalties"] or em["away_pens"] != am["away_penalties"]:
                s.fail(f"PENALTIS DISTINTOS: {em['home']}-{em['away']} → Excel {em['home_pens']}-{em['away_pens']} vs API {am['home_penalties']}-{am['away_penalties']}")
                mismatches += 1

    # Partidos FINISHED en la API que NO están registrados en el Excel (perdidos).
    excel_pairs = {(m["home"], m["away"]) for m in excel}
    missing = [k for k in api_by_pair if k not in excel_pairs]
    for h, a in missing:
        am = api_by_pair[(h, a)]
        s.fail(f"FINISHED en la API pero SIN registrar en el Excel: {h} {am['home_score']}-{am['away_score']} {a}")
        mismatches += 1

    if unmapped:
        s.warn(f"{len(unmapped)} partido(s) de la API con equipos no mapeados EN→ES: {', '.join(unmapped[:5])}")

    if mismatches == 0 and not unmapped:
        s.ok(f"{len(excel)} resultados del Excel coinciden con la API. {len(api_by_pair)} FINISHED en la API, todos registrados.")
    return s


# --- Módulo 2: puntuaciones — independiente + consistencia por categoría -----

def _independent_group_points(ctx: Ctx) -> dict[str, int]:
    """Re-cálculo independiente de F. Grupos (signo/exacto) por jugador, desde
    predicciones (Excel) + resultados (Excel WORLDCUP) + baremo. No usa las
    fórmulas del Excel."""
    admin_map = build_admin_row_map(ctx.admin)
    row_to_res = {m["row"]: m for m in sc.read_worldcup_results(ctx.admin) if m["played"]}
    arows = [admin_map[w] for w in row_to_res if w in admin_map]
    preds_by_arow = read_matches_predictions(ctx.admin, arows) if arows else {}
    indep: dict[str, int] = {}
    group_keys = ctx.match_row_map.get("matches", {})
    for wrow, res in row_to_res.items():
        arow = admin_map.get(wrow)
        if not arow or f"{res['home']}|{res['away']}" not in group_keys:
            continue  # solo partidos de fase de grupos
        for name, pred in preds_by_arow.get(arow, {}).items():
            indep[name] = indep.get(name, 0) + sc.grupos_match_points(
                pred, res["home_score"], res["away_score"])
    return indep


def check_scoring(ctx: Ctx) -> Section:
    s = Section(
        "2. Puntuaciones: re-cálculo independiente + consistencia por categoría",
        id="puntuaciones",
        que="Que el total de cada jugador sea la suma de sus categorías (CLAS E..S) y que el re-cálculo independiente de F. Grupos coincida con el Excel.",
        como="read_clas_breakdown (CLAS: D=total, E..S=categorías); re-cálculo independiente de F. Grupos con grupos_match_points (baremo signo=1 / exacto=+3 de set_scoring.SCORING) sobre las predicciones (ADMIN) y los resultados (WORLDCUP); comparar por jugador. Las posiciones de grupo/clasificados las DERIVA el Excel (Combinaciones3, opaco): no se re-calculan, se verifican por consistencia.")
    try:
        breakdown = sc.read_clas_breakdown(ctx.admin)
    except Exception as e:
        s.fail(f"No pude leer el desglose por categorías de CLAS: {e}")
        return s

    # (a) Consistencia interna del Excel: total (D) == suma de categorías (E..S).
    inconsist = [f"{n}: suma {sum(d['cats'].values())} ≠ total {d['total']}"
                 for n, d in breakdown.items() if sum(d["cats"].values()) != d["total"]]
    if inconsist:
        s.fail("El total del Excel NO es la suma de sus categorías (¿fórmula rota?): " + "; ".join(inconsist[:6]))
    else:
        s.ok(f"Consistencia interna OK: para los {len(breakdown)} jugadores, total == suma de categorías (E..S de CLAS).")

    # (b) Re-cálculo INDEPENDIENTE de F. Grupos vs la columna del Excel, por jugador.
    indep = _independent_group_points(ctx)
    for n, d in breakdown.items():
        s.evid({
            "jugador": n,
            "total": d["total"],
            "suma_cats": sum(d["cats"].values()),
            "f_grupos_indep": indep.get(n, 0),
            "f_grupos_excel": d["cats"]["F. Grupos"],
            "cats": d["cats"],
        })
    difs = [f"{n}: independiente {indep.get(n, 0)} vs Excel {d['cats']['F. Grupos']}"
            for n, d in breakdown.items() if indep.get(n, 0) != d["cats"]["F. Grupos"]]
    if difs:
        s.fail("El re-cálculo independiente de F. Grupos NO coincide con el Excel: " + "; ".join(difs[:6]))
    else:
        s.ok(f"F. Grupos: el re-cálculo independiente coincide EXACTO con el Excel para los {len(breakdown)} jugadores.")

    # (c) Estado de categorías: cuáles tienen puntos y cuáles aún se leen (no se
    #     re-calculan de forma independiente todavía).
    cat_totals: dict[str, int] = {}
    for d in breakdown.values():
        for cat, v in d["cats"].items():
            cat_totals[cat] = cat_totals.get(cat, 0) + v
    activas = {c: t for c, t in cat_totals.items() if t}
    s.note("Puntos por categoría (suma de todos): " +
           (", ".join(f"{c}={t}" for c, t in activas.items()) if activas else "solo F. Grupos"))
    # Los clasificados KO ("Equipos N/M") los re-calcula el módulo 10; aquí solo
    # quedan posiciones de grupo y honor sin re-cálculo independiente.
    cubiertas_mod10 = {cat for cat, *_ in sc.KO_EQUIPOS_BLOCKS}
    no_indep = [c for c in activas
                if not sc.CLAS_CATEGORIES[c][1] and c not in cubiertas_mod10]
    if no_indep:
        s.warn("Categorías ACTIVAS que el Excel DERIVA y aún no se re-calculan de forma "
               "independiente: " + ", ".join(no_indep) + " (posiciones de grupo y/o honor; "
               "verificadas por consistencia interna). Los clasificados KO sí se re-calculan "
               "en el módulo 10.")
    else:
        s.ok("Las categorías con puntos están re-calculadas de forma independiente "
             "(F. Grupos aquí; clasificados KO en el módulo 10) y cuadran.")
    return s


# --- Módulo 3+4: Salud del Excel + determinismo -----------------------------

def check_excel_health(ctx: Ctx) -> Section:
    s = Section(
        "3. Salud del Excel (estructura, errores, determinismo)",
        id="salud_excel",
        que="Que el ADMIN abra, recalcule de forma determinista, no tenga celdas de error, y que jugadores y predicciones estén completos.",
        como="Abrir con openpyxl; recalc() con LibreOffice sobre una COPIA y comparar ranking cacheado vs recalculado (determinismo); escanear CLAS/ADMIN/DailyClas buscando #REF!/#NAME?/#VALUE!/#DIV/0! excluyendo Stats N:T y WORLDCUP!Z100 (rotas conocidas); slots con nombre == players.json; contar predicciones de grupos por jugador (esperado 72).")
    # Carga básica.
    try:
        load(ctx.admin)
    except Exception as e:
        s.fail(f"El ADMIN.xlsx no se puede abrir: {e}")
        return s

    # Nº de jugadores: D5 == players.json == slots con nombre.
    wb = load_values(ctx.admin)
    try:
        d5 = int(wb["ADMIN"]["D5"].value or 0)
    except Exception:
        d5 = -1
    n_players_json = len(ctx.players)
    # Slots con nombre en ADMIN fila 5 (cols 19,22,25,... hasta 25 slots).
    # OJO: ADMIN!D5 es la CAPACIDAD de la plantilla matejero (25), no el nº real
    # de jugadores; el real son los slots con nombre == players.json.
    adm = wb["ADMIN"]
    filled = 0
    for slot in range(25):
        col = 19 + slot * 3
        v = adm.cell(row=5, column=col).value
        if v and not (isinstance(v, str) and v.startswith("Pegar")):
            filled += 1
    if n_players_json == filled:
        s.ok(f"Jugadores coherentes: players.json={n_players_json} == slots con datos={filled} (capacidad plantilla D5={d5}).")
    else:
        s.warn(f"Descuadre de jugadores: players.json={n_players_json} vs slots con datos={filled} (capacidad plantilla D5={d5}).")

    # Predicciones presentes: ningún slot de grupos vacío.
    try:
        ranking_names = [r["name"] for r in read_ranking(ctx.admin)]
        group_rows = list(build_admin_row_map(ctx.admin).values())[:72]
        preds = read_matches_predictions(ctx.admin, group_rows) if group_rows else {}
        counts = {}
        for arow, perslot in preds.items():
            for name in perslot:
                counts[name] = counts.get(name, 0) + 1
        vacios = [n for n in ranking_names if counts.get(n, 0) == 0]
        if vacios:
            s.fail(f"Jugador(es) SIN predicciones de grupos (¿slot borrado?): {', '.join(vacios)}")
        else:
            s.ok(f"Todos los jugadores tienen predicciones de grupos (min {min(counts.values()) if counts else 0}, max {max(counts.values()) if counts else 0}).")
    except Exception as e:
        s.warn(f"No pude verificar presencia de predicciones: {e}")

    # Recalc sobre copia + escáner de errores + determinismo.
    if ctx.args.no_recalc:
        s.note("Recalc y determinismo saltados (--no-recalc). Escaneo errores sobre los valores cacheados del fichero.")
        bad = sc.find_error_cells(ctx.admin, ["CLAS", "ADMIN", "DailyClas"], KNOWN_BROKEN)
        if bad:
            s.fail(f"{len(bad)} celda(s) con error de fórmula: {'; '.join(bad[:8])}")
        else:
            s.ok("Sin celdas de error en CLAS/ADMIN/DailyClas (cacheado; excluyendo Stats N:T y WORLDCUP!Z100).")
        return s

    try:
        with tempfile.TemporaryDirectory() as td:
            copy = Path(td) / "ADMIN_audit.xlsx"
            shutil.copy2(ctx.admin, copy)
            from lib_excel import recalc
            ranking_committed = {r["name"]: r["points"] for r in read_ranking(ctx.admin)}
            recalc(copy)
            ranking_fresh = {r["name"]: r["points"] for r in read_ranking(copy)}
            # Determinismo: el fichero commiteado debe coincidir con un recalc fresco.
            difs = [f"{n}: commiteado {ranking_committed.get(n)} vs recalc {ranking_fresh.get(n)}"
                    for n in set(ranking_committed) | set(ranking_fresh)
                    if ranking_committed.get(n) != ranking_fresh.get(n)]
            if difs:
                s.fail(f"El total cacheado NO coincide con un recalc fresco (fórmulas no deterministas o caché viejo): {'; '.join(difs[:6])}")
            else:
                s.ok(f"Determinismo OK: el ranking cacheado coincide con un recalc fresco ({len(ranking_fresh)} jugadores).")
            bad = sc.find_error_cells(copy, ["CLAS", "ADMIN", "DailyClas"], KNOWN_BROKEN)
            if bad:
                s.fail(f"{len(bad)} celda(s) con error tras recalc: {'; '.join(bad[:8])}")
            else:
                s.ok("Sin celdas de error en CLAS/ADMIN/DailyClas tras recalc (excluyendo Stats N:T y WORLDCUP!Z100).")
    except Exception as e:
        s.warn(f"No se pudo recalcular en copia (¿LibreOffice no disponible?): {e}. Usa --no-recalc para saltarlo.")
    return s


# --- Módulo 5: Consistencia del state.json vs Excel + API -------------------

def check_state_consistency(ctx: Ctx) -> Section:
    s = Section(
        "5. Datos que ve la gente (state.json) vs Excel + API",
        id="consistencia_state",
        que="Que los datos que sirve el bot (state.json) cuadren con el Excel y la API.",
        como="leaderboard == read_ranking(ADMIN); next_match == próximo SCHEDULED de la API; announced_match_ids sin duplicados y ⊆ FINISHED de la API; nombres ≤40 chars sin mojibake; match_points_by_player == suma de los puntos de all_predictions.")
    st = ctx.state
    if not st:
        s.warn("No hay state.json local (¿porra recién creada?).")
        return s

    # leaderboard == read_ranking(ADMIN)
    try:
        excel_rank = {r["name"]: r["points"] for r in read_ranking(ctx.admin)}
    except Exception as e:
        excel_rank = {}
        s.warn(f"No pude leer el ranking del Excel: {e}")
    lb = {r["name"]: r["points"] for r in st.get("leaderboard", [])}
    if excel_rank:
        difs = [f"{n}: state {lb.get(n)} vs Excel {excel_rank.get(n)}"
                for n in set(excel_rank) | set(lb) if excel_rank.get(n) != lb.get(n)]
        if difs:
            s.warn(f"El ranking del bot NO cuadra con el Excel (¿state sin re-sincronizar?): {'; '.join(difs[:6])}")
        else:
            s.ok(f"El ranking del bot coincide con el Excel ({len(lb)} jugadores).")

    # announced_match_ids sin duplicados; nº vs API.
    ann = st.get("announced_match_ids", [])
    if len(ann) != len(set(ann)):
        s.fail(f"announced_match_ids tiene DUPLICADOS ({len(ann)} vs {len(set(ann))} únicos).")
    else:
        s.ok(f"announced_match_ids sin duplicados ({len(ann)}).")
    if ctx.api_finished is not None:
        api_n = len({(ctx.es(m['home']), ctx.es(m['away'])) for m in ctx.api_finished
                     if ctx.es(m['home']) and ctx.es(m['away'])})
        if len(ann) < api_n:
            s.warn(f"Hay {api_n} partidos FINISHED en la API pero solo {len(ann)} anunciados (puede que el cron aún no haya procesado los últimos).")
        else:
            s.ok(f"announced ({len(ann)}) ≥ FINISHED de la API ({api_n}).")

    # next_match vs API.
    if ctx.api_next is not None:
        nm = st.get("next_match") or {}
        api_h, api_a = ctx.es(ctx.api_next["home"]), ctx.es(ctx.api_next["away"])
        if nm.get("home_es") != api_h or nm.get("away_es") != api_a:
            s.warn(f"next_match del bot ({nm.get('home_es')} vs {nm.get('away_es')}) ≠ próximo real de la API ({api_h} vs {api_a}). El bot lo refresca en el próximo cron.")
        else:
            s.ok(f"next_match coincide con la API ({api_h} vs {api_a}).")

    # Nombres ≤40 chars y sin mojibake.
    bad_names = [r["name"] for r in st.get("leaderboard", [])
                 if len(r["name"]) > 40 or "�" in r["name"]]
    if bad_names:
        s.fail(f"Nombres sospechosos en el ranking (largos o corruptos): {', '.join(bad_names[:5])}")
    else:
        s.ok("Nombres del ranking correctos (≤40 chars, sin caracteres corruptos).")

    # all_predictions: resultado/puntos coherentes (si el state ya los trae).
    ap = st.get("all_predictions", [])
    if ap and any("resultado" in m for m in ap):
        results = {(m["home"], m["away"]): m for m in sc.read_worldcup_results(ctx.admin) if m["played"]}
        mpbp = st.get("match_points_by_player", {})
        recompute = {}
        bad = 0
        for m in ap:
            if not m.get("puntos"):
                continue
            # localizar el resultado por el label "dd/mm · Home vs Away"
            try:
                pair = m["label"].split(" · ", 1)[1].split(" vs ")
                res = results.get((pair[0], pair[1]))
            except Exception:
                res = None
            if not res:
                continue
            for name, pts in m["puntos"].items():
                recompute[name] = recompute.get(name, 0) + pts
        difs = [f"{n}: state {mpbp.get(n)} vs recálculo {recompute.get(n)}"
                for n in set(mpbp) | set(recompute) if mpbp.get(n) != recompute.get(n)]
        if difs:
            s.warn(f"match_points_by_player no cuadra con all_predictions: {'; '.join(difs[:6])}")
        else:
            s.ok(f"match_points_by_player cuadra con all_predictions ({len(mpbp)} jugadores).")
    else:
        s.note("state.json local aún sin resultado/puntos por partido (se rellenan en el próximo cron con el código nuevo).")
    return s


# --- Módulo 6: Integridad de ficheros de datos ------------------------------

def check_files_integrity(ctx: Ctx) -> Section:
    s = Section(
        "6. Integridad de ficheros de datos",
        id="integridad_ficheros",
        que="Que los ficheros de datos sean coherentes entre sí y cubran lo necesario.",
        como="match_row_map.json tiene los 72 partidos de grupos; broadcast_2026.json casa con match_row_map; teams_en_es.json cubre todos los equipos vistos en la API.")
    mm = ctx.match_row_map.get("matches", {})
    if len(mm) == 72:
        s.ok(f"match_row_map: 72 partidos de grupos.")
    else:
        s.warn(f"match_row_map tiene {len(mm)} partidos (esperado 72).")

    # broadcast keys deben casar con match_row_map (ambos bajo ["matches"]).
    bc = ctx.broadcast.get("matches", {}) if isinstance(ctx.broadcast, dict) else {}
    bc_keys, mm_keys = set(bc.keys()), set(mm.keys())
    if bc_keys == mm_keys:
        s.ok(f"broadcast_2026.json casa exactamente con match_row_map ({len(bc_keys)} partidos).")
    else:
        falta = mm_keys - bc_keys
        sobra = bc_keys - mm_keys
        s.warn(f"broadcast_2026.json descuadra con match_row_map: faltan {len(falta)}, sobran {len(sobra)}.")

    # teams_en_es cubre los equipos que aparecen en la API.
    if ctx.api_finished is not None:
        unmapped = sorted({m["home"] for m in ctx.api_finished if not ctx.es(m["home"])} |
                          {m["away"] for m in ctx.api_finished if not ctx.es(m["away"])})
        if unmapped:
            s.fail(f"Equipos de la API sin mapear en teams_en_es.json: {', '.join(unmapped)}")
        else:
            s.ok("teams_en_es.json cubre todos los equipos vistos en la API.")
    return s


# --- Módulo 7: Mensajes enviados a la gente ---------------------------------

def check_sent_messages(ctx: Ctx) -> Section:
    s = Section(
        "7. Mensajes enviados a la gente (registro)",
        id="mensajes_enviados",
        que="Que el registro de mensajes enviados exista y no tenga texto corrupto.",
        como="Leer pools/<pool>/sent_messages.jsonl (broadcasts del cron); con --with-vps-log, traer data/<pool>/sent_replies.jsonl del VPS por SSH (respuestas a comandos, p.ej. !claudio); contar y detectar caracteres corruptos.")
    log = ctx.pool_dir / "sent_messages.jsonl"
    if not log.exists():
        s.note("Aún no hay registro de broadcasts (pools/<pool>/sent_messages.jsonl). Se empieza "
               "a generar en el próximo cron con el código nuevo.")
    else:
        n, bad = 0, 0
        recientes: list[tuple[str, str]] = []
        for line in log.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                bad += 1
                continue
            n += 1
            text = rec.get("text", "")
            if "�" in text:
                s.warn(f"Mensaje con caracteres corruptos ({rec.get('ts', '')[:16]}): {text[:60]}")
            recientes.append((rec.get("ts", ""), text))
        if bad:
            s.warn(f"{bad} línea(s) del registro no parseables.")
        ultimos = " | ".join(t[:40].replace("\n", " ") for _, t in recientes[-3:])
        s.ok(f"{n} mensajes (broadcasts) archivados. Últimos: {ultimos}")
    _check_vps_replies(ctx, s)
    s.note("La caption de la clasificación diaria no se archiva (es trivial).")
    return s


def _check_vps_replies(ctx: Ctx, s: Section) -> None:
    """Con --with-vps-log, trae por SSH las respuestas a comandos del bot (sobre
    todo !claudio) y revisa que no tengan texto corrupto. Read-only."""
    if not ctx.args.with_vps_log:
        s.note("Respuestas de comandos del bot (p.ej. !claudio): usa --with-vps-log para traerlas "
               "del VPS (requiere el bot con archiveReply desplegado).")
        return
    if not VPS_SSH:
        s.warn("--with-vps-log requiere la env var VPS_SSH (p.ej. root@<ip>); check saltado.")
        return
    try:
        out = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=15", VPS_SSH,
             f"cat /root/porra-bot/data/{ctx.pool}/sent_replies.jsonl 2>/dev/null || true"],
            capture_output=True, text=True, encoding="utf-8", timeout=40)
    except Exception as e:
        s.warn(f"No pude traer el log de respuestas del VPS: {e}")
        return
    lines = [l for l in out.stdout.splitlines() if l.strip()]
    if not lines:
        s.note("VPS: aún no hay respuestas archivadas (data/<pool>/sent_replies.jsonl vacío o sin desplegar).")
        return
    n, claudio, corruptos = 0, 0, 0
    for l in lines:
        try:
            rec = json.loads(l)
        except Exception:
            continue
        n += 1
        if rec.get("cmd") in ("!claudio", "!bot"):
            claudio += 1
        if "�" in rec.get("text", ""):
            corruptos += 1
    if corruptos:
        s.warn(f"VPS: {corruptos} respuesta(s) con texto corrupto de {n}.")
    else:
        s.ok(f"VPS: {n} respuestas del bot archivadas ({claudio} de !claudio), sin texto corrupto.")


# --- Módulo 8: Operativo ----------------------------------------------------

def check_operational(ctx: Ctx) -> Section:
    s = Section(
        "8. Operativo (bot, cron, tokens)",
        id="operativo",
        que="Que el bot y el cron estén sanos, y avisar de la caducidad de tokens.",
        como="GET /health (whatsapp ready, dispatcher activo, última clasificación); ausencia de .ingest_failures; frescura de last_run_at; fechas de caducidad de GH_DISPATCH_TOKEN y GH_PAT.")
    # .ingest_failures en la raíz del repo.
    if (PROJECT_ROOT / ".ingest_failures").exists():
        s.fail("Existe .ingest_failures: el cron dejó partidos sin procesar. Revisar el último run.")
    else:
        s.ok("Sin .ingest_failures (el cron no reporta fallos de procesado).")

    # last_run_at fresco.
    lra = ctx.state.get("last_run_at")
    if lra:
        try:
            dt = datetime.fromisoformat(lra)
            age_h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
            if age_h > 12:
                s.warn(f"El último run del cron fue hace {age_h:.0f} h (last_run_at={lra}). ¿Cron parado?")
            else:
                s.ok(f"Último run del cron hace {age_h:.1f} h.")
        except Exception:
            s.note(f"last_run_at no parseable: {lra}")

    # /health del VPS.
    if not ctx.args.no_api and not VPS_HEALTH_URL:
        s.note("VPS_HEALTH_URL no configurada (env var): check de /health saltado.")
    if not ctx.args.no_api and VPS_HEALTH_URL:
        try:
            import requests
            h = requests.get(VPS_HEALTH_URL, timeout=15).json()
            wa = h.get("whatsapp")
            disp = (h.get("dispatcher") or {})
            if wa == "ready":
                s.ok(f"Bot vivo: status={h.get('status')}, whatsapp=ready, dispatcher={disp.get('enabled')}, última clasificación={disp.get('last_leaderboard')}.")
            else:
                s.fail(f"WhatsApp NO está ready (whatsapp={wa}). ¿Sesión caducada? Abre WhatsApp en el móvil de la eSIM.")
        except Exception as e:
            s.warn(f"No pude consultar {VPS_HEALTH_URL}: {e}")

    # Caducidad de tokens.
    today = datetime.now(timezone.utc).date()
    for name, dstr in TOKEN_EXPIRIES.items():
        try:
            exp = datetime.fromisoformat(dstr).date()
            days = (exp - today).days
            if days < 0:
                s.fail(f"{name} CADUCADO el {dstr}.")
            elif days < 14:
                s.warn(f"{name} caduca en {days} días ({dstr}).")
            else:
                s.ok(f"{name} vigente ({days} días, caduca {dstr}).")
        except Exception:
            pass
    s.note("Recordatorio: abre WhatsApp en el móvil de la eSIM 1 vez/semana (la sesión caduca a ~14 días sin uso).")
    return s


# --- Módulo 9: Monotonía semanal -------------------------------------------

def check_monotonicity(ctx: Ctx) -> Section:
    s = Section(
        "9. Monotonía (los totales solo pueden subir)",
        id="monotonia",
        que="Que los totales de cada jugador solo suban de una ejecución a la siguiente.",
        como="Comparar el ranking actual (read_ranking) con el snapshot guardado en audit/snapshots/<pool>.json; cualquier bajada indica un resultado cambiado/borrado o el Excel roto.")
    snap_path = AUDIT_DIR / "snapshots" / f"{ctx.pool}.json"
    try:
        current = {r["name"]: r["points"] for r in read_ranking(ctx.admin)}
    except Exception as e:
        s.warn(f"No pude leer el ranking actual: {e}")
        return s
    prev = _load_json(snap_path, default=None)
    prev_totals = prev.get("totals", {}) if prev else None
    if prev_totals is None:
        s.note("Primera ejecución: no hay snapshot previo con el que comparar. Guardo el actual.")
    else:
        bajadas = [f"{n}: {prev_totals[n]} → {current.get(n)}"
                   for n in prev_totals if current.get(n, 0) < prev_totals[n]]
        if bajadas:
            s.fail(f"¡Totales que BAJARON desde {prev.get('ts','?')[:10]}! (resultado cambiado/borrado o Excel roto): {'; '.join(bajadas)}")
        else:
            s.ok(f"Todos los totales ≥ que la semana pasada ({prev.get('ts','?')[:10]}).")
    # Guardar el snapshot SOLO si cambia (o es el primero): así ejecutar el audit
    # varias veces no ensucia el árbol git con cambios de solo-timestamp.
    if prev_totals != current:
        snap_path.parent.mkdir(parents=True, exist_ok=True)
        snap_path.write_text(json.dumps(
            {"ts": datetime.now(timezone.utc).isoformat(), "totals": current},
            ensure_ascii=False, indent=2), encoding="utf-8")
    return s


# --- Módulo 10: Re-cálculo independiente de clasificados (eliminatorias) -----

def check_knockout_recompute(ctx: Ctx) -> Section:
    s = Section(
        "10. Re-cálculo independiente de clasificados (eliminatorias)",
        id="knockout_recompute",
        que="Re-calcula en Python los puntos de 'Equipos N/M' (selecciones que cada "
            "jugador predijo que se clasifican a cada ronda KO) y los cruza con la "
            "columna CLAS del Excel. Detecta que el motor del Excel/LibreOffice puntúe "
            "mal los clasificados, y que los picks estén presentes en el ADMIN.",
        como="Predicho = bloques S del ADMIN (read_knockout_qualifier_picks). Real = "
             "equipos de los cruces KO de la API oficial (o columna M de WORLDCUP si "
             "--no-api). Puntos = |predicho ∩ real| × baremo (set_scoring). Divergencia "
             "con CLAS = FAIL.")
    picks = sc.read_knockout_qualifier_picks(ctx.admin)
    if not picks:
        s.note("Sin jugadores cargados.")
        return s
    bd = sc.read_clas_breakdown(ctx.admin)
    baremo = sc.knockout_equipos_baremo()
    if ctx.api_all is not None:
        actual = sc.knockout_actuals_from_api(ctx.api_all, ctx.teams_en_es)
        src = "API oficial"
    else:
        actual = sc.knockout_actuals_from_worldcup(ctx.admin)
        src = "WORLDCUP (--no-api: no totalmente independiente del Excel)"
    resolved = [cat for cat, *_ in sc.KO_EQUIPOS_BLOCKS if actual.get(cat)]
    if not resolved:
        s.note("Ninguna ronda de eliminatorias resuelta todavía; nada que re-calcular.")
        return s
    recompute = sc.knockout_qualifier_recompute(picks, actual, baremo)
    mismatches = 0
    for name, cats in recompute.items():
        clas_cats = bd.get(name, {}).get("cats", {})
        for cat in resolved:
            expected = cats[cat]["puntos"]
            got = clas_cats.get(cat, 0)
            if expected != got:
                s.fail(f"{name} · {cat}: Python {expected} pts ({cats[cat]['n']} aciertos) "
                       f"vs Excel {got}")
                s.evid({"jugador": name, "categoria": cat, "python_pts": expected,
                        "excel_pts": got, "aciertos": cats[cat]["aciertos"]})
                mismatches += 1
    # Guard del bug original: una ronda resuelta donde NINGÚN jugador tiene picks
    # en el ADMIN = el re-ingest no pegó los bloques (clasificados quedarían a 0).
    for cat in resolved:
        if all(not picks.get(n, {}).get(cat) for n in recompute):
            s.fail(f"{cat}: NINGÚN jugador tiene picks de clasificados en el ADMIN pese "
                   f"a estar la ronda resuelta (¿falta el re-ingest? era el bug original).")
    n_actual = {cat: len(actual[cat]) for cat in resolved}
    if mismatches == 0 and s.status != FAIL:
        s.ok(f"Clasificados re-calculados ({src}) cuadran con el Excel para "
             f"{len(recompute)} jugadores. Rondas resueltas: "
             + ", ".join(f"{c} ({n_actual[c]} equipos)" for c in resolved) + ".")
    return s


# --- Render del informe -----------------------------------------------------

def render_report(pool: str, sections: list[Section]) -> tuple[str, str]:
    worst = OK
    for sec in sections:
        if _SEV[sec.status] > _SEV[worst]:
            worst = sec.status
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    out = [f"# Auditoría porra «{pool}» — {ts}", "",
           f"**Resultado global: {ICON[worst]} {worst}**", ""]
    for sec in sections:
        out.append(f"## {ICON[sec.status]} {sec.title}")
        for status, msg in sec.lines:
            out.append(f"- {ICON[status]} {msg}")
        out.append("")
    return "\n".join(out), worst


def build_json_report(pool: str, sections: list[Section], worst: str, ctx: Ctx) -> dict:
    """Informe estructurado y reproducible: cada check con qué/cómo/status/evidencia.

    Pensado para descargarlo y que una IA externa, con los Excel, reproduzca la
    auditoría de forma independiente.
    """
    return {
        "pool": pool,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall": worst,
        "tool": {
            "script": "scripts/audit_pool.py",
            "args": {"no_api": ctx.args.no_api, "no_recalc": ctx.args.no_recalc,
                     "with_vps_log": ctx.args.with_vps_log},
        },
        "sources": {
            "admin_xlsx": f"pools/{pool}/ADMIN.xlsx",
            "state_json": f"pools/{pool}/state.json",
            "players_json": f"pools/{pool}/players.json",
            "api": "football-data.org · competición WC · status FINISHED",
            "baremo": {
                "grupos_signo": sc.GRUPOS_SIGNO_PTS,
                "grupos_exacto": sc.GRUPOS_EXACTO_PTS,
                "fuente": "scripts/set_scoring.py SCORING (mismas celdas que ADMIN!D8:D47)",
            },
        },
        "como_reproducir": (
            "Descarga el ADMIN.xlsx y los Excel de predicciones de cada jugador. Para cada check, "
            "sigue su campo 'como' y contrasta con su 'evidencia'. Para una verificación 100% "
            "independiente: pásale a otra IA este JSON + los Excel y pídele que reproduzca cada "
            "comprobación por su cuenta y reporte si llega al mismo resultado."
        ),
        "checks": [s.to_json() for s in sections],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pool", required=True, help="familia | amigos | ...")
    ap.add_argument("--no-api", action="store_true", help="sin red (no consulta football-data ni /health)")
    ap.add_argument("--no-recalc", action="store_true", help="no recalcula con LibreOffice (más rápido)")
    ap.add_argument("--with-vps-log", action="store_true",
                    help="trae por SSH las respuestas a comandos del bot del VPS (sent_replies.jsonl)")
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    ctx = Ctx(args.pool, args)
    if not ctx.admin.exists():
        print(f"ERROR: no existe {ctx.admin}", file=sys.stderr)
        return 2

    try:
        ctx.fetch_api()
    except Exception as e:
        print(f"WARN: fallo consultando la API ({e}); sigo sin los módulos que la necesitan.", file=sys.stderr)

    sections = [
        check_results_vs_api(ctx),
        check_scoring(ctx),
        check_excel_health(ctx),
        check_state_consistency(ctx),
        check_files_integrity(ctx),
        check_sent_messages(ctx),
        check_operational(ctx),
        check_monotonicity(ctx),
        check_knockout_recompute(ctx),
    ]

    report, worst = render_report(args.pool, sections)

    reports_dir = AUDIT_DIR / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    report_path = reports_dir / f"audit_{args.pool}_{stamp}.md"
    report_path.write_text(report, encoding="utf-8")

    # Informe JSON reproducible (para descargar / pasar a otra IA con los Excel).
    json_report = build_json_report(args.pool, sections, worst, ctx)
    json_path = reports_dir / f"audit_{args.pool}_{stamp}.json"
    json_path.write_text(json.dumps(json_report, ensure_ascii=False, indent=2), encoding="utf-8")

    # Resumen por consola.
    print(report)
    print(f"\nInforme legible:    {report_path}")
    print(f"Informe JSON:       {json_path}")
    return 0 if worst != FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
