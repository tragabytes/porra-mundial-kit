"""Fase 2 de la web: genera el web_data.json que consume web/panel.html.

Reúne, por porra, TODO lo que la web necesita:
- leaderboard con desglose por categoría (CLAS) + stats de aciertos (exactos/signos),
- perfiles de jugador + cuadro de honor (players.json),
- los ~104 partidos: calendario/estado/fase de la API + predicciones/puntos/clavaron
  del Excel (unidos por par de equipos en español),
- tablas de cada grupo (calculadas desde los resultados),
- cuadro de eliminatorias (de los partidos de fase final de la API; se rellena solo).

Solo lectura sobre el ADMIN; la API se consulta UNA vez (todos los partidos).
Reutiliza lib_scoring / lib_excel / lib_football_api (sin las deps pesadas del cron).

Uso:
  python scripts/build_web_data.py --pool familia
  python scripts/build_web_data.py --pool familia --out web/data.json   # previsualizar
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_excel import build_admin_row_map, read_all_match_predictions
import lib_scoring as sc
from lib_scoring import read_ranking, read_clas_breakdown, grupos_match_points, result_sign
from lib_football_api import fetch_all_matches

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA = PROJECT_ROOT / "data"
MADRID = ZoneInfo("Europe/Madrid")

# Etapa de la API -> clave de ronda que usa web/panel.html.
STAGE_ROUND = {"LAST_32": "LAST_32", "LAST_16": "LAST_16", "QUARTER_FINALS": "QUARTER",
               "SEMI_FINALS": "SEMI", "FINAL": "FINAL", "THIRD_PLACE": "THIRD"}
KO_ORDER = ["LAST_32", "LAST_16", "QUARTER_FINALS", "SEMI_FINALS", "FINAL", "THIRD_PLACE"]
# Ronda del bracket -> categoría CLAS de clasificados (para el sello ×N del cuadro).
ROUND_TO_CAT = {"LAST_32": "Equipos 1/16", "LAST_16": "Equipos 1/8", "QUARTER": "Equipos 1/4",
                "SEMI": "Equipos 1/2", "FINAL": "Equipos Final", "THIRD": "Equipos 3-4"}
STATUS_ES = {"FINISHED": "finalizado", "IN_PLAY": "en juego", "PAUSED": "en juego",
             "POSTPONED": "aplazado", "SUSPENDED": "suspendido", "CANCELLED": "cancelado"}
GROUPS = list("ABCDEFGHIJKL")

# Cuadro RADIAL por jugador (web/panel.html). Se omite el 3º/4º puesto (bracket
# estándar R32→CHAMP). Por ronda: nombre de ronda en el ADMIN -> clave web + nº de
# equipos del anillo (32→16→8→4→2; CHAMP es 1 aparte).
RADIAL_ROUNDS = [
    ("Dieciseisavos", "R32", 32),
    ("Octavos", "R16", 16),
    ("Cuartos", "QF", 8),
    ("Semifinales", "SF", 4),
    ("Final", "F", 2),
]
# Ronda exterior -> ronda interior (el ganador de un cruce de la exterior juega en la interior).
_RADIAL_INNER = {"R32": "R16", "R16": "QF", "QF": "SF", "SF": "F"}
# Etapa de la API -> clave de ronda radial (para el cuadro REAL/oficial).
STAGE_TO_RADIAL = {"LAST_32": "R32", "LAST_16": "R16", "QUARTER_FINALS": "QF",
                   "SEMI_FINALS": "SF", "FINAL": "F"}
# Etapas KO de la API y a qué ronda radial avanzan sus GANADORES (apagado en vivo).
KO_STAGES = {"LAST_32", "LAST_16", "QUARTER_FINALS", "SEMI_FINALS", "FINAL", "THIRD_PLACE"}
_KO_WIN_FLOW = [("LAST_32", "R16"), ("LAST_16", "QF"), ("QUARTER_FINALS", "SF"),
                ("SEMI_FINALS", "F"), ("FINAL", "CHAMP")]


def _split_cruce(cruce: str, valid: set) -> tuple[str, str]:
    """Parte 'Local-Visitante' en (local, visitante).

    Robusto a guiones dentro del nombre (p.ej. 'Bosnia-Herzegovina'): elige la
    posición de '-' en la que AMBOS lados son equipos válidos. Fallback: primer '-'."""
    s = (cruce or "").strip()
    idx = -1
    while True:
        idx = s.find("-", idx + 1)
        if idx == -1:
            break
        left, right = s[:idx], s[idx + 1:]
        if left in valid and right in valid:
            return left, right
    left, _, right = s.partition("-")
    return left, right


def _team_node(es: str, iso: dict):
    """{iso, es} para el cuadro, o None si no hay equipo."""
    es = (es or "").strip()
    return {"iso": iso.get(es), "es": es} if es else None


def _champ_node(rounds: dict, valid: set, iso: dict, campeon: str | None):
    """Campeón del jugador: ganador de SU cruce Final (por marcador) o, si empate /
    sin Final, su 'campeon' de players.json. {iso, es} o None."""
    final = rounds.get("Final") or []
    if final:
        h, a = _split_cruce(final[0].get("cruce"), valid)
        mar = (final[0].get("marcador") or "").split("-")
        if len(mar) == 2:
            try:
                gl, gv = int(mar[0]), int(mar[1])
                if gl > gv:
                    return _team_node(h, iso)
                if gv > gl:
                    return _team_node(a, iso)
            except ValueError:
                pass
    return _team_node(campeon, iso) if campeon else None


def _cross_pairs(rounds: dict, valid: set) -> dict:
    """{R32:[(home_es,away_es),...16], R16:[...8], QF:[...4], SF:[...2], F:[...1]} en
    orden de fila del ADMIN (partiendo cada cruce con _split_cruce)."""
    out = {}
    for ronda, key, _size in RADIAL_ROUNDS:
        out[key] = [_split_cruce(it.get("cruce"), valid) for it in rounds.get(ronda, [])]
    return out


def _ko_skeleton(pairs: dict):
    """Orden de filas EN ORDEN DE ÁRBOL por anillo, derivado por identidad desde la
    Final: {F:[1 fila], SF:[2], QF:[4], R16:[8], R32:[16]}. El ganador de un cruce es
    el equipo que reaparece en la ronda interior. None si el bracket no está completo
    o no es consistente (algún cruce sin ganador identificable)."""
    need = {"F": 1, "SF": 2, "QF": 4, "R16": 8, "R32": 16}
    if any(len(pairs.get(k, [])) < n for k, n in need.items()):
        return None
    teamset = {k: {t for cr in v for t in cr} for k, v in pairs.items()}
    w2row = {}  # por ronda exterior: equipo ganador -> índice de fila de su cruce
    for k in ("R32", "R16", "QF", "SF"):
        ns, m = teamset.get(_RADIAL_INNER[k], set()), {}
        for idx, (h, a) in enumerate(pairs[k]):
            w = h if (h in ns and a not in ns) else a if (a in ns and h not in ns) else None
            if w is not None:
                m[w] = idx
        w2row[k] = m
    ring_teams = list(pairs["F"][0])           # los dos finalistas
    skel = {"F": [0]}
    for outer in ("SF", "QF", "R16", "R32"):
        rows, nxt = [], []
        for t in ring_teams:
            idx = w2row[outer].get(t)
            if idx is None:
                return None                     # inconsistente -> no derivable
            rows.append(idx)
            nxt.extend(pairs[outer][idx])
        skel[outer] = rows
        ring_teams = nxt
    return skel


def _lay_on_skeleton(pairs: dict, skel: dict, iso: dict) -> dict:
    """Coloca los cruces de cada ronda en el orden del esqueleto y los aplana en sus
    dos equipos -> anillos {R32,R16,QF,SF,F} de {iso,es}/None (sin CHAMP)."""
    br = {}
    for ronda, key, size in RADIAL_ROUNDS:
        prs = pairs.get(key, [])
        teams = []
        for r in skel.get(key, []):
            h, a = prs[r] if (r is not None and r < len(prs)) else (None, None)
            teams += [_team_node(h, iso), _team_node(a, iso)]
        br[key] = (teams + [None] * size)[:size]
    return br


def _flatten_rows(pairs: dict, iso: dict) -> dict:
    """Fallback (sin esqueleto): aplana cada ronda en orden de fila. La adyacencia del
    embudo NO es fiable, pero al menos pinta los equipos."""
    br = {}
    for ronda, key, size in RADIAL_ROUNDS:
        teams = []
        for h, a in pairs.get(key, []):
            teams += [_team_node(h, iso), _team_node(a, iso)]
        br[key] = (teams + [None] * size)[:size]
    return br


def _derive_skeleton(ko_matchups: dict, valid: set):
    """Esqueleto del torneo (estructura compartida), del primer jugador con bracket
    completo y consistente. None si ninguno lo está."""
    for rounds in ko_matchups.values():
        if rounds:
            s = _ko_skeleton(_cross_pairs(rounds, valid))
            if s:
                return s
    return None


def build_player_brackets(ko_matchups: dict, players: list, iso: dict, skel: dict | None) -> dict:
    """{nombre: {R32:[...32], R16:[...16], QF:[...8], SF:[...4], F:[...2], CHAMP:[1]}}.

    Reconstruye el árbol por identidad usando el esqueleto compartido `skel` (el ganador
    de un cruce alimenta el hueco correcto del anillo interior), de modo que el embudo
    sea correcto y todos los jugadores queden alineados. Sin esqueleto: orden de fila."""
    valid = set(iso)
    champ_by_name = {p.get("name"): p.get("campeon") for p in players}
    out: dict[str, dict] = {}
    for name, rounds in ko_matchups.items():
        if not rounds:
            continue
        pairs = _cross_pairs(rounds, valid)
        br = _lay_on_skeleton(pairs, skel, iso) if skel else _flatten_rows(pairs, iso)
        br["CHAMP"] = [_champ_node(rounds, valid, iso, champ_by_name.get(name))]
        out[name] = br
    return out


def _winner_es(m: dict):
    """Equipo ganador (es) de un partido KO FINALIZADO; None si sin resolver.
    Empate a 120' -> decide por penaltis (None si no hay info de penaltis).
    Solo cuenta si el partido está finalizado: un partido EN JUEGO no decide nada
    (si no, un 0-1 en directo marcaría al que va ganando como clasificado)."""
    if m.get("status") != "finalizado":
        return None
    hs, as_ = m.get("home_score"), m.get("away_score")
    if hs is None or as_ is None:
        return None
    if hs == as_:
        hp, ap = m.get("home_penalties"), m.get("away_penalties")
        if hp is None:
            return None
        return m["home_es"] if (hp or 0) > (ap or 0) else m["away_es"]
    return m["home_es"] if hs > as_ else m["away_es"]


def _loser_es(m: dict):
    """Equipo perdedor (es) de un partido KO finalizado; None si sin resolver."""
    w = _winner_es(m)
    if not w:
        return None
    return m["away_es"] if w == m["home_es"] else m["home_es"]


def build_reached_by_round(matches: list, iso: dict) -> dict:
    """{R32,R16,QF,SF,F,CHAMP} -> [iso...] de equipos que han LLEGADO a cada ronda KO,
    EN VIVO. R32 = los 32 equipos de 16avos; R16/QF/SF/F/CHAMP = GANADORES de los
    partidos finalizados de la ronda anterior (a tiempo: un equipo 'llega' a octavos
    en cuanto gana su 16avo, sin esperar a que la API cree el partido de octavos)."""
    out: dict[str, list] = {}
    r32 = {iso.get(t) for m in matches if m.get("stage") == "LAST_32"
           for t in (m.get("home_es"), m.get("away_es")) if iso.get(t)}
    if r32:
        out["R32"] = sorted(r32)
    for stage, key in _KO_WIN_FLOW:
        ws = sorted({x for m in matches if m.get("stage") == stage
                     for x in (iso.get(_winner_es(m)),) if x})
        if ws:
            out[key] = ws
    return out


def build_ko_eliminated(matches: list, iso: dict) -> list:
    """[iso...] de equipos eliminados (perdedores de partidos KO finalizados)."""
    out = {x for m in matches if m.get("stage") in KO_STAGES
           for x in (iso.get(_loser_es(m)),) if x}
    return sorted(out)


def _real_champ_node(matches: list, iso: dict):
    """{iso, es} del campeón real (ganador de la Final) o None si no resuelta."""
    for m in matches:
        if m.get("stage") == "FINAL":
            w = _winner_es(m)
            if w:
                return _team_node(w, iso)
    return None


def build_actual_bracket(matches: list, skel: dict | None, iso: dict) -> dict:
    """Cuadro REAL/oficial sobre el MISMO esqueleto que los jugadores (para que quede
    alineado y con la adyacencia correcta). Partidos reales por ronda (ordenados por
    id) colocados en el orden del esqueleto; huecos/rondas sin resolver a None; CHAMP
    real desde la Final. {} si no hay esqueleto (la capa oficial cae a placeholders)."""
    if not skel:
        return {}
    real_pairs = {}
    for api_stage, key in STAGE_TO_RADIAL.items():
        sm = sorted([m for m in matches if m.get("stage") == api_stage], key=lambda m: m["id"])
        real_pairs[key] = [(m.get("home_es"), m.get("away_es")) for m in sm]
    br = _lay_on_skeleton(real_pairs, skel, iso)
    br["CHAMP"] = [_real_champ_node(matches, iso)]
    return br


def _load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def _madrid(utc, fmt):
    try:
        return datetime.fromisoformat(utc.replace("Z", "+00:00")).astimezone(MADRID).strftime(fmt)
    except Exception:
        return ""


def _h2h_stats(team: str, tied: set, group_matches: list[dict]) -> tuple:
    """(pts, DG, GF) de `team` SOLO en los partidos jugados contra los equipos de
    `tied`. Es el enfrentamiento directo (criterios FIFA 4-6 de desempate)."""
    pts = gf = gc = 0
    for m in group_matches:
        if m.get("status") != "finalizado" or m.get("home_score") is None:
            continue
        h, a = m.get("home_es"), m.get("away_es")
        if h not in tied or a not in tied:
            continue
        hs, as_ = m["home_score"], m["away_score"]
        if h == team:
            gf += hs; gc += as_
            pts += 3 if hs > as_ else 1 if hs == as_ else 0
        elif a == team:
            gf += as_; gc += hs
            pts += 3 if as_ > hs else 1 if hs == as_ else 0
    return (pts, gf - gc, gf)


def compute_standings(group_matches: list[dict], iso: dict) -> list[dict]:
    """Tabla de un grupo desde los partidos (estado web). Puro y testeable.

    Cada partido: {home_es, away_es, home_score, away_score, status}. Suma PJ/GF/GC/
    Pts de los finalizados; ordena con los desempates FIFA (ver más abajo).
    Los 4 equipos salen de los enfrentamientos (aunque no se hayan jugado aún).
    """
    teams: dict[str, dict] = {}
    for m in group_matches:
        for es in (m.get("home_es"), m.get("away_es")):
            if es and es not in teams:
                teams[es] = {"team_es": es, "team_iso": iso.get(es), "pj": 0, "gf": 0, "gc": 0, "pts": 0}
    for m in group_matches:
        if m.get("status") != "finalizado" or m.get("home_score") is None:
            continue
        h, a, hs, as_ = m["home_es"], m["away_es"], m["home_score"], m["away_score"]
        if h in teams:
            teams[h]["pj"] += 1; teams[h]["gf"] += hs; teams[h]["gc"] += as_
            teams[h]["pts"] += 3 if hs > as_ else 1 if hs == as_ else 0
        if a in teams:
            teams[a]["pj"] += 1; teams[a]["gf"] += as_; teams[a]["gc"] += hs
            teams[a]["pts"] += 3 if as_ > hs else 1 if hs == as_ else 0
    st = list(teams.values())
    for t in st:
        t["dg"] = t["gf"] - t["gc"]
    # Orden FIFA: 1) pts, 2) DG global, 3) GF global; y para los que sigan
    # empatados en esos tres, 4-6) enfrentamiento directo (pts/DG/GF SOLO entre
    # ellos). Último recurso: alfabético (el fair-play y el sorteo no se pueden
    # computar sin datos de tarjetas). Solo afecta al display de la web; los
    # puntos de la porra los calcula el Excel con su propia lógica de desempates.
    st.sort(key=lambda t: (-t["pts"], -t["dg"], -t["gf"]))
    ordered: list[dict] = []
    i = 0
    while i < len(st):
        j = i + 1
        while (j < len(st)
               and (st[j]["pts"], st[j]["dg"], st[j]["gf"])
               == (st[i]["pts"], st[i]["dg"], st[i]["gf"])):
            j += 1
        tied = st[i:j]
        if len(tied) > 1:
            names = {t["team_es"] for t in tied}
            h2h = {t["team_es"]: _h2h_stats(t["team_es"], names, group_matches) for t in tied}
            tied.sort(key=lambda t: (-h2h[t["team_es"]][0], -h2h[t["team_es"]][1],
                                     -h2h[t["team_es"]][2], t["team_es"]))
        ordered.extend(tied)
        i = j
    for i, t in enumerate(ordered, 1):
        t["pos"] = i
    return ordered


def build_web_data(pool: str) -> dict:
    pdir = PROJECT_ROOT / "pools" / pool
    admin = pdir / "ADMIN.xlsx"
    players = _load(pdir / "players.json", [])
    teams_es = _load(DATA / "teams_en_es.json", {})
    iso = _load(DATA / "teams_iso.json", {})
    bc = (_load(DATA / "broadcast_2026.json", {}) or {}).get("matches", {})

    def tv_for(h, a):
        r = bc.get(f"{h}|{a}") or bc.get(f"{a}|{h}")
        return (r or {}).get("tv") if r else None

    def ico(es):
        return iso.get(es) if es else None

    # --- Excel: predicciones + resultado por par de equipos (ES) ---
    admin_map = build_admin_row_map(admin)
    rows = read_all_match_predictions(admin, admin_map, sc.COL_HOME_TEAM, sc.COL_AWAY_TEAM,
                                      sc.COL_HOME_SCORE, sc.COL_AWAY_SCORE)
    valid = set(teams_es.values())
    ex_by_pair = {(r["home"], r["away"]): r for r in rows
                  if r["home"] in valid and r["away"] in valid}

    # --- API: todos los partidos ---
    api = fetch_all_matches()

    # Cruces predichos por jugador: en eliminatorias las predicciones por partido
    # se cruzan por equipos (no por fila), y también alimentan los picks del H2H.
    ko_matchups = sc.read_knockout_matchup_picks(admin)

    matches = []
    for m in api:
        he, ae = teams_es.get(m["home"]), teams_es.get(m["away"])
        status = STATUS_ES.get(m.get("status"), "programado")
        played = status == "finalizado" and m.get("home_score") is not None
        e = {
            "id": m["id"], "stage": m.get("stage"),
            "group": (m.get("group") or "").replace("GROUP_", "") or None,  # API da "GROUP_A"
            "matchday": m.get("matchday"),
            "date": _madrid(m["utc_kickoff"], "%Y-%m-%d"),
            "kickoff_madrid": _madrid(m["utc_kickoff"], "%d/%m %H:%M"),
            "home_es": he, "away_es": ae, "home_iso": ico(he), "away_iso": ico(ae),
            "home_score": m.get("home_score"), "away_score": m.get("away_score"),
            "duration": m.get("duration"), "home_penalties": m.get("home_penalties"),
            "away_penalties": m.get("away_penalties"),
            "status": status, "tv": tv_for(he, ae) if (he and ae) else None,
            "predicciones": {}, "puntos": {}, "clavaron": [],
            "consenso": {"home": 0, "draw": 0, "away": 0},
        }
        ronda = sc.KO_STAGE_TO_RONDA.get(m.get("stage"))
        if ronda and he and ae:
            # eliminatoria: predicciones fieles al cruce que predijo cada jugador
            # (no por la fila). Puntos/clavaron por partido con el baremo KO, solo
            # para quien predijo ESTE cruce (preds ya viene filtrado).
            preds = sc.knockout_predicciones(ko_matchups, ronda, he, ae)
            e["predicciones"] = {n: f"{p[1]}-{p[2]}" for n, p in preds.items()}
            for p in preds.values():
                s = result_sign(p[1], p[2])
                e["consenso"]["home" if s == "1" else "away" if s == "2" else "draw"] += 1
            if played:
                rh, ra = m["home_score"], m["away_score"]
                for n, p in preds.items():
                    e["puntos"][n] = sc.knockout_match_points(p, rh, ra, ronda)
                    if p[1] == rh and p[2] == ra:
                        e["clavaron"].append(n)
        else:
            ex = ex_by_pair.get((he, ae)) if (he and ae) else None
            if ex:
                preds = ex["predicciones"]  # {name: (sgn, h, a)}
                e["predicciones"] = {n: f"{p[1]}-{p[2]}" for n, p in preds.items()}
                for p in preds.values():
                    s = result_sign(p[1], p[2])
                    e["consenso"]["home" if s == "1" else "away" if s == "2" else "draw"] += 1
                if played and e["group"]:   # puntos itemizados solo en fase de grupos
                    rh, ra = m["home_score"], m["away_score"]
                    for n, p in preds.items():
                        e["puntos"][n] = grupos_match_points(p, rh, ra)
                        if p[1] == rh and p[2] == ra:
                            e["clavaron"].append(n)
        matches.append(e)

    # --- eliminatorias: clasificados predichos vs reales (×N, aciertos, picks propios) ---
    ko_picks = sc.read_knockout_qualifier_picks(admin)
    ko_actual = sc.knockout_actuals_from_api(api, teams_es)
    ko_recompute = sc.knockout_qualifier_recompute(ko_picks, ko_actual, sc.knockout_equipos_baremo())
    # equipo -> [jugadores que lo predijeron como clasificado], por categoría
    pick_by_team: dict[str, dict[str, list[str]]] = {}
    for cat, *_ in sc.KO_EQUIPOS_BLOCKS:
        d: dict[str, list[str]] = {}
        for name, pcats in ko_picks.items():
            for team in pcats.get(cat, []):
                d.setdefault(team, []).append(name)
        pick_by_team[cat] = d
    ko_aciertos = {
        name: {"total_n": sum(c["n"] for c in rc.values()),
               "por_ronda": {cat: rc[cat]["n"] for cat in rc if rc[cat]["n"]}}
        for name, rc in ko_recompute.items()
    }

    # --- próximo / live ---
    sched = sorted([m for m in api if STATUS_ES.get(m.get("status"), "programado") == "programado"
                    and teams_es.get(m["home"]) and teams_es.get(m["away"])],
                   key=lambda m: m.get("utc_kickoff") or "")
    nm = sched[0] if sched else None
    next_match = None
    if nm:
        he, ae = teams_es.get(nm["home"]), teams_es.get(nm["away"])
        next_match = {"id": nm["id"], "home_es": he, "away_es": ae, "home_iso": ico(he), "away_iso": ico(ae),
                      "kickoff_madrid": _madrid(nm["utc_kickoff"], "%d/%m %H:%M"), "tv": tv_for(he, ae)}
    live_id = next((m["id"] for m in matches if m["status"] == "en juego"), None)

    # --- leaderboard: ranking + cats (activas) + exactos/signos ---
    ranking = read_ranking(admin)
    bd = read_clas_breakdown(admin)
    cat_sum: dict[str, int] = {}
    for d in bd.values():
        for c, v in d["cats"].items():
            cat_sum[c] = cat_sum.get(c, 0) + v
    active_cats = [c for c in sc.CLAS_CATEGORIES if cat_sum.get(c, 0) > 0] or ["F. Grupos"]
    ex_cnt, sg_cnt = {}, {}
    for mt in matches:
        if mt["status"] != "finalizado" or mt["home_score"] is None:
            continue
        rh, ra = mt["home_score"], mt["away_score"]
        for n, pick in mt["predicciones"].items():
            h, a = (int(x) for x in pick.split("-"))
            if h == rh and a == ra:
                ex_cnt[n] = ex_cnt.get(n, 0) + 1
            elif result_sign(h, a) == result_sign(rh, ra):
                sg_cnt[n] = sg_cnt.get(n, 0) + 1
    leaderboard = [{
        "position": r["position"], "name": r["name"], "points": r["points"],
        "cats": {c: bd.get(r["name"], {}).get("cats", {}).get(c, 0) for c in active_cats},
        "exactos": ex_cnt.get(r["name"], 0), "signos": sg_cnt.get(r["name"], 0),
        # eliminatorias: total de clasificados acertados (H2H) y picks propios (feature 4)
        "ko_aciertos": ko_aciertos.get(r["name"], {"total_n": 0, "por_ronda": {}}),
        "ko_picks": ko_matchups.get(r["name"], {}),
    } for r in ranking]

    # --- players ---
    players_out = [{
        "name": p.get("name"), "club": p.get("club"), "national": p.get("national"),
        "campeon": p.get("campeon"), "bota_oro": p.get("bota_oro"), "balon_oro": p.get("balon_oro"),
        "honor_aciertos": {"campeon": None, "bota_oro": None, "balon_oro": None},
    } for p in players]

    # --- grupos ---
    groups = [{"group": g, "standings": compute_standings(
        [m for m in matches if m["group"] == g], iso)} for g in GROUPS]

    # --- bracket (de los partidos de fase final de la API) ---
    bracket = []
    for stage in KO_ORDER:
        sm = sorted([m for m in matches if m["stage"] == stage], key=lambda m: m["id"])
        if not sm:
            continue
        cat = ROUND_TO_CAT.get(STAGE_ROUND[stage])
        pbt = pick_by_team.get(cat, {}) if cat else {}
        bms = [{
            "id": i, "home_seed": None, "away_seed": None,
            "home_es": m["home_es"], "away_es": m["away_es"],
            "home_iso": m["home_iso"], "away_iso": m["away_iso"],
            "match_id": m["id"] if (m["home_es"] and m["away_es"]) else None,
            # cuántos predijeron cada equipo como clasificado a esta ronda (sello ×N)
            "picks_count": {"home": len(pbt.get(m["home_es"], [])),
                            "away": len(pbt.get(m["away_es"], []))},
            "quien_home": sorted(pbt.get(m["home_es"], []))[:25],
            "quien_away": sorted(pbt.get(m["away_es"], []))[:25],
        } for i, m in enumerate(sm)]
        bracket.append({"round": STAGE_ROUND[stage], "matches": bms})

    # --- cuadro radial por jugador + bracket real, ambos sobre el MISMO esqueleto ---
    # (árbol por identidad: el ganador de un cruce alimenta el hueco correcto; así el
    #  embudo es correcto y el cuadro real queda alineado con el del jugador).
    skel = _derive_skeleton(ko_matchups, set(iso))
    player_brackets = build_player_brackets(ko_matchups, players, iso, skel)
    actual_bracket = build_actual_bracket(matches, skel, iso)
    # apagado EN VIVO por equipo: a qué ronda ha llegado cada uno (por ganadores) y
    # quién está eliminado (perdedor de un KO). Sustituye al gate por-ronda-completa.
    reached_by_round = build_reached_by_round(matches, iso)
    ko_eliminated = build_ko_eliminated(matches, iso)

    return {
        "pool": pool,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "live_match_id": live_id,
        "next_match": next_match,
        "leaderboard": leaderboard,
        "players": players_out,
        "matches": matches,
        "groups": groups,
        "bracket": bracket,
        "player_brackets": player_brackets,
        "actual_bracket": actual_bracket,
        "reached_by_round": reached_by_round,
        "ko_eliminated": ko_eliminated,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pool", required=True, help="familia | amigos | ...")
    ap.add_argument("--out", default=None, help="ruta de salida (def: pools/<pool>/web_data.json)")
    ap.add_argument("--sync", action="store_true",
                    help="después de generar, sube el resultado al VPS vía POST /web-data")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    data = build_web_data(args.pool)
    out = Path(args.out) if args.out else (PROJECT_ROOT / "pools" / args.pool / "web_data.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    n_played = sum(1 for m in data["matches"] if m["status"] == "finalizado")
    print(f"[{args.pool}] web_data.json -> {out}")
    print(f"  partidos={len(data['matches'])} (jugados={n_played}) · jugadores={len(data['leaderboard'])} "
          f"· grupos={len(data['groups'])} · rondas KO={len(data['bracket'])} · live={data['live_match_id']}")

    if args.sync:
        from lib_whatsapp_client import sync_web_data, upload_web_file
        try:
            r = sync_web_data(args.pool, data)
            print(f"[{args.pool}] sync web_data al VPS OK: {r}")
        except Exception as e:
            print(f"[{args.pool}] WARN: sync web_data al VPS falló: {e}", file=sys.stderr)
        # ADMIN.xlsx descargable desde el panel (Fase 3b): se refresca cada partido.
        admin = PROJECT_ROOT / "pools" / args.pool / "ADMIN.xlsx"
        try:
            if admin.exists():
                r = upload_web_file(args.pool, "admin.xlsx", admin)
                print(f"[{args.pool}] sync ADMIN.xlsx al VPS OK: {r}")
        except Exception as e:
            print(f"[{args.pool}] WARN: sync ADMIN.xlsx al VPS falló: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
