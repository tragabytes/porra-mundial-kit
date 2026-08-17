"""Orquestador del cron: procesa partidos FINISHED nuevos y publica al grupo.

Flujo (cada partido se procesa secuencialmente, sin batch):
1. Lee state.json para saber qué partidos ya se anunciaron.
2. Llama football-data.org (o openfootball fallback) para partidos FINISHED hoy.
3. Filtra los nuevos.
4. Para cada partido nuevo:
   a. Snapshot del ranking ANTES.
   b. Escribe goles en WORLDCUP!AC/AD.
   c. Recalcula con LibreOffice headless (~43s).
   d. Snapshot del ranking DESPUÉS y calcula delta de puntos por jugador.
   e. Renderiza PNG del leaderboard (lib_screenshot).
   f. Genera comentario socarrón con Claude Opus 4.5 (lib_claude).
   g. POST al VPS para publicar en el grupo (lib_whatsapp_client).
   h. Marca como anunciado en state.json (atómico tras éxito del POST).

El workflow de GitHub Actions que invoca a este script es responsable de
commitear y pushear ADMIN.xlsx + state.json al final.

DRY_RUN=1 se hereda por cada lib (football_api devuelve fake match,
claude devuelve mock, whatsapp_client solo imprime).

Usage:
  python scripts/ingest_match_results.py
  DRY_RUN=1 python scripts/ingest_match_results.py
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
from lib_excel import (load, save, unprotect_all_sheets, reprotect_all_sheets,
                       recalc, load_values, build_admin_row_map,
                       read_match_predictions, read_matches_predictions,
                       read_all_match_predictions, read_match_date,
                       read_daily_ranking)
from lib_football_api import (fetch_finished_matches, fetch_next_scheduled_match,
                              fetch_scheduled_matches, fetch_upcoming_matches,
                              fetch_today_matches)
from lib_screenshot import render_leaderboard, WC_TOTAL_MATCHES
from lib_claude import generate_commentary, generate_preview
from lib_whatsapp_client import publish, sync_pool, web_url_for_pool
from set_scoring import SCORING
import lib_scoring as sc

# Baremo de un partido de FASE DE GRUPOS (fuente única: set_scoring.SCORING, las
# mismas celdas ADMIN!D8/D10 que escribe el Excel). Acertar 1X2 = 1 pt; resultado
# exacto = +3 (total 4). Los puntos de eliminatorias, posición de grupo y cuadro
# de honor NO se itemizan por partido: se reconcilian como "otros" (total del
# ranking − suma de puntos por partido) para que los números siempre cuadren.
GRUPOS_SIGNO_PTS = SCORING[8]    # 1
GRUPOS_EXACTO_PTS = SCORING[10]  # +3

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# POOL_ID identifica la porra que se procesa en este run (env var obligatoria).
# Cada porra tiene su propio directorio bajo pools/ con ADMIN, state y players.
POOL_ID = os.environ.get("POOL_ID")
if not POOL_ID:
    print("ERROR: falta env var POOL_ID (familia|amigos|...).", file=sys.stderr)
    sys.exit(2)

POOL_DIR = PROJECT_ROOT / "pools" / POOL_ID
ADMIN_PATH = POOL_DIR / "ADMIN.xlsx"
STATE_PATH = POOL_DIR / "state.json"
PLAYERS_PATH = POOL_DIR / "players.json"
TEAMS_PATH = PROJECT_ROOT / "data" / "teams_en_es.json"
MATCH_MAP_PATH = PROJECT_ROOT / "data" / "match_row_map.json"
BROADCAST_PATH = PROJECT_ROOT / "data" / "broadcast_2026.json"

# Interruptor del comentario post-partido (imagen de clasificación + texto
# socarrón al grupo). Apagado el 12/06 por decisión del organizador: el partido
# se sigue procesando entero (resultado, recalc, puntos, !hoy), solo se omite
# la publicación. Poner a True para reactivarlo.
POST_MATCH_COMMENT = False

# Aviso corto y conservador de adelantamientos tras cada partido (separado del
# comentario completo de arriba). Solo cambios gordos: nuevo líder o reordenación
# del top 5. Pedido por el organizador el 13/06.
POST_MATCH_OVERTAKES = True

# Mensaje ⚽💥 tras cada partido celebrando a quien clavó el resultado EXACTO.
# Solo se publica si hay al menos un acierto (si nadie clava, silencio). Pedido
# por el organizador el 17/06.
POST_MATCH_EXACT = True

# Celdas clave en el ADMIN
COL_HOME_SCORE = 29  # AC en WORLDCUP (goles local en 90' + ET, sin penaltis)
COL_AWAY_SCORE = 30  # AD en WORLDCUP (goles visitante en 90' + ET, sin penaltis)
COL_HOME_PENS = 28   # AB en WORLDCUP (penaltis local, solo en knockouts a penaltis)
COL_AWAY_PENS = 31   # AE en WORLDCUP (penaltis visitante)
COL_HOME_TEAM = 27   # AA
COL_AWAY_TEAM = 32   # AF
CLAS_FIRST_DATA_ROW = 5
CLAS_NAME_COL = 3   # C
CLAS_TOTAL_COL = 4  # D


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _madrid_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/Madrid"))


def _in_quiet_hours() -> bool:
    """True si la hora de Madrid está en la franja de silencio nocturno [00:00, 07:00).
    En esa franja no se publica nada al grupo: los partidos se acumulan en
    state['night_digest'] y se resumen a las 07:00 (ver _flush_night_digest)."""
    return 0 <= _madrid_now().hour < 7


def load_state() -> dict:
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    else:
        state = {"announced_match_ids": [], "last_run_at": None}
    # Migración suave: campos añadidos en PR-D para el anuncio de kickoff.
    state.setdefault("announced_kickoff_ids", [])
    # Silencio nocturno: resumen acumulado de madrugada + fecha del último envío.
    state.setdefault("night_digest", [])
    state.setdefault("night_digest_sent_date", None)
    return state


def _atomic_write_text(path: Path, content: str) -> None:
    """Escribe `content` a `path` de forma atómica: archivo temporal + rename.

    Evita estado corrupto si el proceso muere a mitad de write_text. os.replace
    es atómico en POSIX y en Windows (cuando el destino está en el mismo FS).
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def save_state(state: dict) -> None:
    state["last_run_at"] = _now_iso()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(
        STATE_PATH, json.dumps(state, indent=2, ensure_ascii=False)
    )


def load_teams_en_es() -> dict[str, str]:
    raw = json.loads(TEAMS_PATH.read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def load_match_row_map() -> dict[str, int]:
    raw = json.loads(MATCH_MAP_PATH.read_text(encoding="utf-8"))
    return raw["matches"]


_BROADCASTS_CACHE: dict | None = None


def load_broadcasts() -> dict:
    """Lee data/broadcast_2026.json (dónde se ve cada partido en España).

    Cacheado en memoria. Tolerante a fallo: si el fichero no existe o no parsea,
    devuelve {} y el campo `tv` simplemente se omite (no rompe el ingest).
    """
    global _BROADCASTS_CACHE
    if _BROADCASTS_CACHE is None:
        try:
            raw = json.loads(BROADCAST_PATH.read_text(encoding="utf-8"))
            _BROADCASTS_CACHE = raw.get("matches", {})
        except Exception as e:
            print(f"[{POOL_ID}] WARN: no se pudo leer {BROADCAST_PATH}: {e}",
                  file=sys.stderr)
            _BROADCASTS_CACHE = {}
    return _BROADCASTS_CACHE


def _tv_for(home_es: str, away_es: str) -> str | None:
    """Cadena de TV en España para un partido (nombres ES). Prueba ambos órdenes
    local|visitante. None si no está en el mapa (p.ej. eliminatorias)."""
    bc = load_broadcasts()
    rec = bc.get(f"{home_es}|{away_es}") or bc.get(f"{away_es}|{home_es}")
    return rec.get("tv") if rec else None


def load_players_db() -> list[dict]:
    if not PLAYERS_PATH.exists():
        print(f"[{POOL_ID}] WARN: {PLAYERS_PATH} no existe, comentario sin perfiles de jugador",
              file=sys.stderr)
        return []
    return json.loads(PLAYERS_PATH.read_text(encoding="utf-8"))


def read_ranking(admin_path: Path) -> list[dict]:
    """Top jugadores ordenados por puntos desc. Asume recálculo previo."""
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


def compute_points_delta(before: list[dict], after: list[dict]) -> dict[str, int]:
    """{name: delta} para todos los jugadores en `after`."""
    pts_before = {r["name"]: r["points"] for r in before}
    return {r["name"]: r["points"] - pts_before.get(r["name"], 0) for r in after}


def _result_sign(home: int, away: int) -> str:
    """'1' (gana local), '2' (gana visitante) o 'X' (empate)."""
    return "1" if home > away else ("2" if away > home else "X")


def grupos_match_points(pred: tuple[str, int, int], real_home: int, real_away: int) -> int:
    """Puntos de un jugador en un partido de FASE DE GRUPOS según su predicción.

    `pred` es (signo, goles_local, goles_visitante) de parse_prediction. El signo
    se deriva de los goles (no se confía en el carácter guardado). Resultado
    exacto = signo + exacto; solo el signo correcto = signo; fallo = 0.
    """
    _sgn, ph, pa = pred
    if ph == real_home and pa == real_away:
        return GRUPOS_SIGNO_PTS + GRUPOS_EXACTO_PTS
    if _result_sign(ph, pa) == _result_sign(real_home, real_away):
        return GRUPOS_SIGNO_PTS
    return 0


def compute_overtakes(before: list[dict], after: list[dict]) -> str | None:
    """Texto corto con los cambios GORDOS de clasificación tras un partido: nuevo
    líder o adelantamientos dentro del top 5. Devuelve None si no hay nada
    destacable. Conservador a propósito (el organizador apagó el comentario
    completo por ser demasiado). `before`/`after` son listas de read_ranking
    ([{name, points, position}])."""
    if not before or not after:
        return None
    pb = {r["name"]: r["position"] for r in before}
    pa = {r["name"]: r["position"] for r in after}
    lines: list[str] = []

    if before[0]["name"] != after[0]["name"]:
        lines.append(
            f"👑 Nuevo líder: {after[0]['name']} ({after[0]['points']} pts), "
            f"adelanta a {before[0]['name']}"
        )

    for r in after[:5]:
        name = r["name"]
        if name not in pb or pa[name] >= pb[name]:
            continue  # no subió de posición
        if name == after[0]["name"] and before[0]["name"] != name:
            continue  # ya cubierto por la línea de "nuevo líder"
        passed = sorted(
            (o["name"] for o in after
             if pb.get(o["name"], 0) < pb[name] and pa[o["name"]] > pa[name]),
            key=lambda n: pa[n],
        )
        if not passed:
            continue
        quien = passed[0] if len(passed) == 1 else f"{passed[0]} (+{len(passed) - 1})"
        lines.append(f"📈 {name} adelanta a {quien} ({pb[name]}º→{pa[name]}º)")

    if not lines:
        return None
    return "📊 Cambios en la porra:\n" + "\n".join(lines[:4])


def _norm_team(name: str | None) -> str:
    """Normaliza un nombre de equipo para comparar de forma robusta: sin acentos,
    sin espacios sobrantes y en minúsculas. Cierra las discrepancias entre los
    nombres de teams_en_es.json y los que el matejero cachea en WORLDCUP (una
    tilde o un espacio de más bastaban para un SKIP silencioso de un KO)."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def find_match_row(admin_path: Path, home_es: str, away_es: str,
                   row_map: dict[str, int]) -> int | None:
    """Busca fila WORLDCUP. Map estático para fase grupos; fallback fresh para knockouts."""
    key = f"{home_es}|{away_es}"
    if key in row_map:
        return row_map[key]
    # Fallback: el partido es eliminatoria YA resuelta, los nombres están en
    # WORLDCUP. Comparación NORMALIZADA para no fallar por acentos/espacios/caja
    # entre teams_en_es.json y lo cacheado por el matejero (un SKIP aquí dejaría
    # el KO sin procesar y el cron en rojo hasta corregirlo a mano).
    nh, na = _norm_team(home_es), _norm_team(away_es)
    wb = load_values(admin_path)
    ws = wb["WORLDCUP"]
    for r in range(4, ws.max_row + 1):
        h = ws.cell(row=r, column=COL_HOME_TEAM).value
        a = ws.cell(row=r, column=COL_AWAY_TEAM).value
        if _norm_team(h) == nh and _norm_team(a) == na:
            return r
    return None


def write_score(admin_path: Path, row: int, home_score: int, away_score: int,
                home_pens: int | None = None, away_pens: int | None = None,
                daily_date: datetime | None = None) -> None:
    """Escribe el resultado en WORLDCUP. En knockouts que fueron a penaltis,
    además del 120' en AC/AD escribe los penaltis en AB/AE para que la fórmula
    del Excel pueda inferir el ganador.

    Si se pasa daily_date, fija además el selector DailyPrediction!H1 a esa
    fecha para que el recálculo posterior deje las hojas DailyClas/DailyPrediction
    en el día de este partido (V7)."""
    wb = load(admin_path)
    unprotect_all_sheets(wb)
    ws = wb["WORLDCUP"]
    ws.cell(row=row, column=COL_HOME_SCORE).value = home_score
    ws.cell(row=row, column=COL_AWAY_SCORE).value = away_score
    if home_pens is not None and away_pens is not None:
        ws.cell(row=row, column=COL_HOME_PENS).value = home_pens
        ws.cell(row=row, column=COL_AWAY_PENS).value = away_pens
    if daily_date is not None:
        wb["DailyPrediction"]["H1"].value = daily_date
    reprotect_all_sheets(wb)
    save(wb, admin_path)


def process_match(match: dict, teams_es: dict, row_map: dict, players_db: list,
                  state: dict) -> bool:
    """Procesa un partido. True si se anunció con éxito, False si se omitió."""
    home_es = teams_es.get(match["home"])
    away_es = teams_es.get(match["away"])
    if not home_es or not away_es:
        print(f"[{POOL_ID}]   SKIP: equipos no mapeados EN→ES: '{match['home']}' vs '{match['away']}'",
              file=sys.stderr)
        return False

    row = find_match_row(ADMIN_PATH, home_es, away_es, row_map)
    if row is None:
        print(f"[{POOL_ID}]   SKIP: no encuentro fila WORLDCUP para {home_es} vs {away_es}",
              file=sys.stderr)
        return False

    score = f"{home_es} {match['home_score']}-{match['away_score']} {away_es}"
    if match.get("duration") == "PENALTY_SHOOTOUT":
        score += f" (pens {match['home_penalties']}-{match['away_penalties']})"
    print(f"[{POOL_ID}]   -> {score} (fila {row})")

    # Snapshot del ADMIN: si cualquier paso falla (write/recalc/render/claude/
    # publish), restauramos el .xlsx al estado previo. Próximo cron reintenta
    # el partido desde cero sin recalc redundante ni Excel a medio escribir.
    backup_path = ADMIN_PATH.with_suffix(ADMIN_PATH.suffix + ".bak")
    shutil.copy2(ADMIN_PATH, backup_path)
    publish_ok = False
    daily_date = None
    daily_ranking: list[dict] = []
    try:
        ranking_before = read_ranking(ADMIN_PATH)

        # V7: localizar la fila ADMIN del partido para leer predicciones y fecha.
        admin_row = build_admin_row_map(ADMIN_PATH).get(row)
        predictions = read_match_predictions(ADMIN_PATH, admin_row) if admin_row else {}
        daily_date = read_match_date(ADMIN_PATH, admin_row) if admin_row else None

        write_score(
            ADMIN_PATH, row,
            match["home_score"], match["away_score"],
            home_pens=match.get("home_penalties"),
            away_pens=match.get("away_penalties"),
            daily_date=daily_date,
        )
        recalc(ADMIN_PATH)
        ranking_after = read_ranking(ADMIN_PATH)
        points_delta = compute_points_delta(ranking_before, ranking_after)
        # Ranking de la jornada (solo si el partido tiene fecha = fase de grupos).
        daily_ranking = read_daily_ranking(ADMIN_PATH) if daily_date else []

        # Quién clavó el RESULTADO EXACTO (lo usan el resumen nocturno y el ⚽💥).
        # En grupos el partido es fijo: basta comparar el marcador. En eliminatorias
        # hay que ser FIEL AL BAREMO (acertar el cruce Y el marcador): la fila del
        # partido real no se corresponde con el cruce que predijo cada jugador, así
        # que se cruza por equipos con read_knockout_matchup_picks.
        rh, ra = match["home_score"], match["away_score"]
        ronda_ko = sc.ronda_for_admin_row(admin_row) if admin_row else None
        ko_clasif = None
        if ronda_ko:
            matchups = sc.read_knockout_matchup_picks(ADMIN_PATH)
            ko_clasif = sc.knockout_clasificacion(matchups, ronda_ko, home_es, away_es, rh, ra)
            clavaron = [name for name, v in ko_clasif["cruce"].items() if v["exacto"]]
            exact_pts = sc.KO_RONDA_EXACTO_PTS.get(ronda_ko)
        else:
            clavaron = [name for name, (_s, ph, pa) in (predictions or {}).items()
                        if ph == rh and pa == ra]
            exact_pts = GRUPOS_SIGNO_PTS + GRUPOS_EXACTO_PTS

        if POST_MATCH_COMMENT:
            remaining = WC_TOTAL_MATCHES - len(state["announced_match_ids"]) - 1
            png = render_leaderboard(
                ADMIN_PATH,
                match=match,
                home_es=home_es,
                away_es=away_es,
                ranking_before=ranking_before,
                matches_remaining=max(remaining, 0),
            )
            match_data = {
                "home_es": home_es,
                "away_es": away_es,
                "home_score": match["home_score"],
                "away_score": match["away_score"],
                "label": match.get("utc_kickoff", "")[:10],
            }
            text = generate_commentary(
                match_data, points_delta, ranking_before, ranking_after, players_db,
                predictions=predictions, daily_ranking=daily_ranking,
                ko_clasificacion=ko_clasif,
            )
            publish(text, image_base64=base64.b64encode(png).decode("ascii"))
        else:
            print(f"[{POOL_ID}]   comentario post-partido apagado; partido procesado sin publicar")

        # Silencio nocturno (00:00-07:00 Madrid): no se publica nada al grupo; se
        # acumula una entrada en state["night_digest"] y se resume a las 07:00.
        quiet = _in_quiet_hours()
        if quiet:
            state.setdefault("night_digest", []).append({
                "home_es": home_es, "away_es": away_es,
                "home_score": match["home_score"], "away_score": match["away_score"],
                "duration": match.get("duration"),
                "home_penalties": match.get("home_penalties"),
                "away_penalties": match.get("away_penalties"),
                "clavaron": clavaron,
                "ts": _madrid_now().isoformat(),
            })
            print(f"[{POOL_ID}]   -> silencio nocturno: "
                  f"{home_es} {match['home_score']}-{match['away_score']} {away_es} "
                  f"al resumen de las 07:00")

        # Mensaje ⚽💥: quién clavó el resultado exacto. Solo si hay acierto.
        # Como overtakes: si el publish falla NO se revierte el partido (ya está
        # procesado). El partido se procesa igual aunque nadie acierte.
        if not quiet and POST_MATCH_EXACT and clavaron:
            nombres = ", ".join(clavaron)
            uno = len(clavaron) == 1
            verbo = "Lo clavó" if uno else "Lo clavaron"
            # En grupos el +N son 4 (1X2 + exacto); en eliminatorias es el total de
            # la ronda (signo + exacto), ya fiel al cruce que predijo cada uno.
            cola = ""
            if exact_pts:
                cola = f" (+{exact_pts} pts)" if uno else f" (+{exact_pts} pts cada uno)"
            txt = (f"⚽💥 ¡RESULTADO EXACTO! {home_es} {rh}-{ra} {away_es}\n"
                   f"{verbo}: {nombres}{cola}")
            # En un KO resuelto en los penaltis el "exacto" se mide contra el
            # empate de 120' (correcto), pero conviene aclarar la tanda y quién
            # pasó para que no chirríe ("clavé el 1-1 pero pasó el otro").
            if (match.get("duration") == "PENALTY_SHOOTOUT"
                    and match.get("home_penalties") is not None
                    and match.get("away_penalties") is not None):
                hp, ap = match["home_penalties"], match["away_penalties"]
                quien = home_es if hp > ap else away_es
                txt += f"\n(se decidió en los penaltis {hp}-{ap}; pasó {quien})"
            try:
                publish(txt)
                print(f"[{POOL_ID}]   -> ⚽💥 exacto enviado ({len(clavaron)} acierto/s)")
            except Exception as e:
                print(f"[{POOL_ID}]   WARN publicando ⚽💥 exacto: {e}",
                      file=sys.stderr)

        # Aviso conservador de adelantamientos (independiente del comentario
        # completo de arriba). Solo cambios gordos; si el publish del aviso falla
        # NO se revierte el partido (el resultado ya está procesado y guardado).
        # En silencio nocturno se omite (el resumen de las 07:00 va sin adelantamientos).
        if not quiet and POST_MATCH_OVERTAKES:
            overtakes = compute_overtakes(ranking_before, ranking_after)
            if overtakes:
                web_url = web_url_for_pool(POOL_ID)
                if web_url:
                    overtakes = f"{overtakes}\n\n🌐 {web_url}"
                try:
                    publish(overtakes)
                    print(f"[{POOL_ID}]   -> aviso de adelantamientos enviado")
                except Exception as e:
                    print(f"[{POOL_ID}]   WARN publicando adelantamientos: {e}",
                          file=sys.stderr)

        publish_ok = True
    except Exception as e:
        print(f"[{POOL_ID}]   ERROR procesando partido: {e}; revirtiendo ADMIN y dejándolo para el próximo cron",
              file=sys.stderr)
        return False
    finally:
        if not publish_ok and backup_path.exists():
            shutil.copy2(backup_path, ADMIN_PATH)
        backup_path.unlink(missing_ok=True)

    state["announced_match_ids"].append(match["id"])
    if daily_date:
        _refresh_today(state, daily_date, daily_ranking)
    save_state(state)  # atómico tras éxito
    return True


_BOT_NAME_FORBIDDEN = str.maketrans({c: " " for c in "\r\n\t[]<>{}`"})


def _sanitize_for_bot(value: str) -> str:
    """Limpia un string que va a entrar como dato en el system prompt del bot.

    Quita saltos de línea y caracteres usables para prompt-injection y trunca a
    40 chars. Los nombres legítimos no superan eso; los que sí, son sospechosos.
    """
    if not isinstance(value, str):
        return ""
    return value.translate(_BOT_NAME_FORBIDDEN).strip()[:40]


def _refresh_today(state: dict, daily_date: datetime, daily_ranking: list[dict]) -> None:
    """Mete state['today'] = {date, ranking} con el ranking de la jornada.

    Lo consume el bot para responder !hoy. Se actualiza solo al procesar un
    partido con fecha (fase de grupos); en knockouts y runs sin partidos
    persiste el último valor. Sanitiza los nombres antes de exponerlos al bot.
    """
    state["today"] = {
        "date": daily_date.strftime("%Y-%m-%d"),
        "ranking": [
            {"position": r["position"],
             "name": _sanitize_for_bot(r["name"]),
             "points": r["points"]}
            for r in daily_ranking
        ],
    }


def _refresh_leaderboard(state: dict) -> None:
    """Mete state['leaderboard'] con el ranking actual del ADMIN.

    Lo consume el bot para responder !ranking. Si el ADMIN aún no tiene
    nombres (porra recién creada), la lista queda vacía y el bot responde
    'aún no hay ranking'. Sanitiza los nombres antes de exponerlos al bot
    (defensa frente a prompt-injection vía nombre en el Excel del jugador).
    """
    try:
        ranking = read_ranking(ADMIN_PATH)
        for r in ranking:
            r["name"] = _sanitize_for_bot(r["name"])
        state["leaderboard"] = ranking
    except Exception as e:
        print(f"[{POOL_ID}] WARN: fallo al leer leaderboard: {e}", file=sys.stderr)
        state["leaderboard"] = []


def _refresh_next_match(state: dict, teams_es: dict) -> None:
    """Mete state['next_match'] con el próximo partido programado (nombres en ES).

    No bloqueante: si falla, log y deja el campo a None (el bot responderá
    'no disponible' a !proximo y la siguiente ejecución reintenta).
    """
    try:
        nm = fetch_next_scheduled_match()
    except Exception as e:
        print(f"[{POOL_ID}] WARN: fallo al obtener next_match: {e}", file=sys.stderr)
        state["next_match"] = None
        return
    if nm is None:
        state["next_match"] = None
        return
    home_es = _sanitize_for_bot(teams_es.get(nm["home"], nm["home"]))
    away_es = _sanitize_for_bot(teams_es.get(nm["away"], nm["away"]))
    state["next_match"] = {
        "home_es": home_es,
        "away_es": away_es,
        "utc_kickoff": nm["utc_kickoff"],
        "stage": nm["stage"],
        "group": nm.get("group"),
        "matchday": nm.get("matchday"),
        "tv": _tv_for(home_es, away_es),
    }


def _refresh_upcoming_kickoffs(state: dict, teams_es: dict) -> None:
    """Mete state['upcoming_kickoffs']: calendario a 7 días para el dispatcher
    del VPS (dispara el workflow ~18 min antes de cada kickoff y tras el final).

    A diferencia de sus hermanos, en caso de error CONSERVA el valor anterior:
    este campo alimenta el trigger del workflow y vaciarlo lo dejaría ciego.
    7 días cubre los descansos más largos del torneo (semis→final).
    """
    state.setdefault("upcoming_kickoffs", [])
    try:
        matches = fetch_scheduled_matches(days_ahead=7)
    except Exception as e:
        print(f"[{POOL_ID}] WARN: fallo al refrescar upcoming_kickoffs: {e}",
              file=sys.stderr)
        return
    out = []
    for m in matches:
        home_es = _sanitize_for_bot(teams_es.get(m["home"], m["home"]))
        away_es = _sanitize_for_bot(teams_es.get(m["away"], m["away"]))
        out.append({
            "id": m["id"],
            "utc_kickoff": m["utc_kickoff"],
            "home_es": home_es,
            "away_es": away_es,
            "tv": _tv_for(home_es, away_es),
        })
    state["upcoming_kickoffs"] = out


def _refresh_today_matches(state: dict, teams_es: dict) -> None:
    """state['today_matches']: partidos de HOY (Madrid) con estado, marcador y
    canal de TV, para el bloque 'partidos de hoy' del bot (incluye los ya
    jugados, que desaparecen de upcoming_kickoffs). No bloqueante: si falla,
    conserva el valor anterior."""
    try:
        matches = fetch_today_matches()
    except Exception as e:
        print(f"[{POOL_ID}] WARN: fallo al refrescar today_matches: {e}",
              file=sys.stderr)
        return
    madrid = ZoneInfo("Europe/Madrid")
    status_es = {
        "SCHEDULED": "programado", "TIMED": "programado", "IN_PLAY": "en juego",
        "PAUSED": "descanso", "FINISHED": "finalizado", "SUSPENDED": "suspendido",
        "POSTPONED": "aplazado", "CANCELLED": "cancelado",
    }
    out = []
    for m in matches:
        home_es = _sanitize_for_bot(teams_es.get(m["home"], m["home"]))
        away_es = _sanitize_for_bot(teams_es.get(m["away"], m["away"]))
        utc = datetime.fromisoformat(
            m["utc_kickoff"].replace("Z", "+00:00")).astimezone(madrid)
        out.append({
            "home_es": home_es,
            "away_es": away_es,
            "kickoff_madrid": utc.strftime("%H:%M"),
            "date": utc.strftime("%Y-%m-%d"),
            "status": status_es.get(m.get("status"), (m.get("status") or "").lower()),
            "home_score": m.get("home_score"),
            "away_score": m.get("away_score"),
            "tv": _tv_for(home_es, away_es),
        })
    state["today_matches"] = out


def _refresh_upcoming_predictions(state: dict, teams_es: dict, row_map: dict) -> None:
    """Mete state['upcoming_predictions']: picks de cada jugador para los
    partidos de los próximos ~2 días. Lo consume el !claudio del bot para
    opinar sobre predicciones ("¿qué opinas de mi pick para esta noche?").

    No bloqueante: si algo falla, deja la lista vacía y el bot responderá
    que aún no tiene datos. Tope de 8 partidos para no inflar su prompt.
    """
    try:
        matches = fetch_scheduled_matches(days_ahead=2)[:8]
        admin_map = build_admin_row_map(ADMIN_PATH) if matches else {}
        rows: dict[int, tuple[dict, str, str]] = {}
        for m in matches:
            home_es = teams_es.get(m["home"])
            away_es = teams_es.get(m["away"])
            if not home_es or not away_es:
                continue
            prow = find_match_row(ADMIN_PATH, home_es, away_es, row_map)
            arow = admin_map.get(prow) if prow else None
            if arow:
                rows[arow] = (m, home_es, away_es)
        preds_by_row = read_matches_predictions(ADMIN_PATH, list(rows)) if rows else {}
        matchups = None
        upcoming = []
        for arow, (m, home_es, away_es) in rows.items():
            # En eliminatorias la predicción se cruza por equipos (fiel al cruce),
            # no por la fila del partido.
            ronda = sc.ronda_for_admin_row(arow)
            if ronda:
                if matchups is None:
                    matchups = sc.read_knockout_matchup_picks(ADMIN_PATH)
                preds = sc.knockout_predicciones(matchups, ronda, home_es, away_es)
            else:
                preds = preds_by_row.get(arow, {})
            try:
                utc = datetime.fromisoformat(m["utc_kickoff"].replace("Z", "+00:00"))
                when = utc.astimezone(ZoneInfo("Europe/Madrid")).strftime("%d/%m %H:%M")
            except Exception:
                when = ""
            # Sanitizar por partes: el truncado a 40 chars del sanitizador es
            # para nombres; un label entero ("X vs Y · dd/mm hh:mm") no cabe.
            label = f"{_sanitize_for_bot(home_es)} vs {_sanitize_for_bot(away_es)}"
            if when:
                label += f" · {when}"
            upcoming.append({
                "label": label,
                "predicciones": {
                    _sanitize_for_bot(name): f"{h}-{a}"
                    for name, (_sgn, h, a) in preds.items()
                },
            })
        state["upcoming_predictions"] = upcoming
    except Exception as e:
        print(f"[{POOL_ID}] WARN: fallo al refrescar upcoming_predictions: {e}",
              file=sys.stderr)
        state["upcoming_predictions"] = []


def _refresh_all_predictions(state: dict, teams_es: dict) -> None:
    """state['all_predictions']: picks de todos para TODO el torneo (solo filas
    con ambos equipos resueltos). Lo consumen el !claudio y el !mispuntos del
    bot (NO !miprediccion). Las predicciones están congeladas; se re-exporta en
    cada run porque los nombres de las eliminatorias se van resolviendo y porque
    los resultados/puntos de los partidos jugados se van rellenando. ~25 KB por
    pool: cabe de sobra en el /sync (límite 1 MB) y en el state del repo.

    Cada partido jugado lleva además `resultado` ("local-visitante") y, en fase
    de grupos, `puntos` {jugador: pts del partido}. También deja
    state['match_points_by_player'] = {jugador: suma de puntos por partido}, que
    el bot usa para la línea de resumen (otros = total del ranking − esta suma).

    No bloqueante: si falla, deja la lista vacía y el bot dirá que no lo tiene.
    """
    try:
        # Solo filas cuyos equipos son selecciones reales: las eliminatorias sin
        # resolver llevan placeholders tipo "W101"/"2A" que ensucian el prompt;
        # cuando el Excel las resuelva con nombres de verdad, entran solas.
        valid = set(teams_es.values())
        admin_map = build_admin_row_map(ADMIN_PATH)
        rows = read_all_match_predictions(ADMIN_PATH, admin_map,
                                          COL_HOME_TEAM, COL_AWAY_TEAM,
                                          COL_HOME_SCORE, COL_AWAY_SCORE)
        matchups = None
        out = []
        match_points: dict[str, int] = {}
        for r in rows:
            if r["home"] not in valid or r["away"] not in valid:
                continue
            # En eliminatorias la predicción de cada jugador se cruza por equipos
            # (fiel al cruce que predijo), no por la fila del partido.
            arow = admin_map.get(r["row"])
            ronda = sc.ronda_for_admin_row(arow) if arow else None
            if ronda:
                if matchups is None:
                    matchups = sc.read_knockout_matchup_picks(ADMIN_PATH)
                preds = sc.knockout_predicciones(matchups, ronda, r["home"], r["away"])
            else:
                preds = r["predicciones"]
            if not preds:
                continue
            label = f"{_sanitize_for_bot(r['home'])} vs {_sanitize_for_bot(r['away'])}"
            if r["fecha"]:
                label = f"{r['fecha'].strftime('%d/%m')} · {label}"
            entry = {
                "label": label,
                "predicciones": {
                    _sanitize_for_bot(name): f"{h}-{a}"
                    for name, (_sgn, h, a) in preds.items()
                },
            }
            rh, ra = r.get("home_score"), r.get("away_score")
            jugado = isinstance(rh, int) and isinstance(ra, int)
            if jugado:
                entry["resultado"] = f"{rh}-{ra}"
                # Puntos por partido solo en fase de grupos (fecha = datetime).
                # En eliminatorias los puntos (signo/exacto + quién pasa) caen en
                # la reconciliación "otros" hasta que se itemicen.
                if r["fecha"] is not None:
                    pts = {}
                    for name, pred in preds.items():
                        sname = _sanitize_for_bot(name)
                        p = grupos_match_points(pred, rh, ra)
                        pts[sname] = p
                        match_points[sname] = match_points.get(sname, 0) + p
                    entry["puntos"] = pts
            else:
                entry["resultado"] = None
            out.append(entry)
        state["all_predictions"] = out
        state["match_points_by_player"] = match_points
    except Exception as e:
        print(f"[{POOL_ID}] WARN: fallo al refrescar all_predictions: {e}",
              file=sys.stderr)
        state["all_predictions"] = []
        state["match_points_by_player"] = {}


def _group_by_kickoff(matches: list[dict]) -> list[list[dict]]:
    """Agrupa partidos por hora de kickoff exacta, preservando el orden de aparición.

    Los partidos simultáneos del Mundial comparten el mismo `utc_kickoff`, así que
    se anuncian juntos en un único mensaje. Los de horas distintas quedan en grupos
    separados (mensajes separados)."""
    groups: dict[str, list[dict]] = {}
    for m in matches:
        groups.setdefault(m["utc_kickoff"], []).append(m)
    return list(groups.values())


def _announce_upcoming_kickoffs(state: dict, teams_es: dict, row_map: dict,
                                players_db: list[dict]) -> None:
    """Anuncia en el grupo los partidos cuyo kickoff está a 0-20 min vista.

    Los partidos que arrancan a la misma hora se unifican en un solo mensaje
    (ver `_group_by_kickoff`).

    Idempotente: cada partido se anuncia una sola vez por porra. La idempotencia
    la garantiza `state['announced_kickoff_ids']`, que se persiste tras cada
    publish exitoso.

    No bloqueante: si falla la API, la generación o el publish, log y seguimos
    (la siguiente ejecución del cron lo reintentará si la ventana sigue abierta).
    """
    try:
        upcoming = fetch_upcoming_matches(within_minutes=20)
    except Exception as e:
        print(f"[{POOL_ID}] WARN: fallo al obtener upcoming matches: {e}", file=sys.stderr)
        return

    if not upcoming:
        return

    pending = [m for m in upcoming if m["id"] not in state["announced_kickoff_ids"]]
    if not pending:
        return

    # V7-bis: cuántos jugadores predicen a cada selección como campeona (de
    # players.json, no de la hoja Stats: sus fórmulas no sobreviven al recalc
    # de LibreOffice). El system prompt decide si lo menciona o lo omite.
    champion_counter = Counter(
        _sanitize_for_bot(p["campeon"]) for p in players_db if p.get("campeon")
    )
    champion_counts = champion_counter.most_common()

    print(f"[{POOL_ID}] Anunciando inicio de {len(pending)} partido(s)...")
    for group in _group_by_kickoff(pending):
        if _in_quiet_hours():
            # Silencio nocturno: no se manda preview de madrugada (a las 07:00 el
            # partido ya estará jugado; el resultado va en el resumen). Se marcan
            # como anunciados para que no se disparen más tarde.
            for m in group:
                state["announced_kickoff_ids"].append(m["id"])
            save_state(state)
            pares = ", ".join(f"{m['home']} vs {m['away']}" for m in group)
            print(f"[{POOL_ID}]   -> silencio nocturno: preview de {pares} descartado")
            continue

        # Resolver equipos, predicciones y TV de cada partido mapeado del grupo.
        items = []          # partidos para generate_preview (cada uno con 'predictions')
        tv_lines = []       # (etiqueta, canal) deterministas, fuera del prompt del LLM
        announced_ids = []  # ids a marcar tras un publish exitoso
        for m in group:
            home_es = teams_es.get(m["home"])
            away_es = teams_es.get(m["away"])
            if not home_es or not away_es:
                print(f"[{POOL_ID}]   SKIP preview: equipos no mapeados '{m['home']}' vs '{m['away']}'",
                      file=sys.stderr)
                continue

            # V7: predicciones de la porra para este partido (lectura cruda, sin recalc).
            # En eliminatorias se clasifica por cruce (fiel al cruce que predijo cada
            # uno, no por la fila del partido): quién acertó el cruce, quién solo un
            # equipo, quién los dos sin emparejar y cuántos nada.
            predictions = {}
            ko_clasificacion = None
            try:
                prow = find_match_row(ADMIN_PATH, home_es, away_es, row_map)
                arow = build_admin_row_map(ADMIN_PATH).get(prow) if prow else None
                if arow:
                    ronda = sc.ronda_for_admin_row(arow)
                    if ronda:
                        ko_clasificacion = sc.knockout_clasificacion(
                            sc.read_knockout_matchup_picks(ADMIN_PATH),
                            ronda, home_es, away_es)
                    else:
                        predictions = read_match_predictions(ADMIN_PATH, arow)
            except Exception as e:
                print(f"[{POOL_ID}]   WARN leyendo predicciones de {home_es}-{away_es}: {e}",
                      file=sys.stderr)

            items.append({
                "home_es": home_es,
                "away_es": away_es,
                "utc_kickoff": m["utc_kickoff"],
                "stage": m.get("stage", "GROUP_STAGE"),
                "group": m.get("group"),
                "matchday": m.get("matchday"),
                "predictions": predictions,
                "ko_clasificacion": ko_clasificacion,
            })
            announced_ids.append(m["id"])

            tv = _tv_for(home_es, away_es)
            if tv:
                tv_lines.append((f"{home_es}–{away_es}", tv))

        if not items:
            # Ningún partido del grupo está mapeado: se reintenta en el próximo run.
            continue

        try:
            text = generate_preview(items, champion_counts=champion_counts)
        except Exception as e:
            pares = ", ".join(f"{it['home_es']}-{it['away_es']}" for it in items)
            print(f"[{POOL_ID}]   ERROR generando preview {pares}: {e}", file=sys.stderr)
            continue

        # Dónde se ve en España (RTVE en abierto / DAZN). Determinista, fuera del
        # prompt. Con 1 partido va sin etiqueta; con 2+ se etiqueta cada uno.
        if len(items) == 1:
            if tv_lines:
                text = f"{text}\n\n📺 {tv_lines[0][1]}"
        elif tv_lines:
            joined = "\n".join(f"📺 {label}: {tv}" for label, tv in tv_lines)
            text = f"{text}\n\n{joined}"

        # Enlace al panel web de la porra (determinista, fuera del prompt del LLM).
        web_url = web_url_for_pool(POOL_ID)
        if web_url:
            text = f"{text}\n\n🌐 {web_url}"

        try:
            publish(text)
        except Exception as e:
            pares = ", ".join(f"{it['home_es']}-{it['away_es']}" for it in items)
            print(f"[{POOL_ID}]   ERROR publicando preview {pares}: {e}", file=sys.stderr)
            continue

        state["announced_kickoff_ids"].extend(announced_ids)
        save_state(state)  # atómico tras cada éxito
        pares = ", ".join(f"{it['home_es']} vs {it['away_es']}" for it in items)
        print(f"[{POOL_ID}]   -> preview enviado: {pares}")


def _refresh_knockout(state: dict) -> None:
    """state['knockout'] = cuadro de eliminatorias para el bot: por jugador, qué
    selecciones clasificadas acertó/falló (países concretos) y sus cruces predichos
    por ronda. Clasificados reales desde WORLDCUP (sin coste de API). Sanitizado."""
    try:
        picks = sc.read_knockout_qualifier_picks(ADMIN_PATH)
        actual = sc.knockout_actuals_from_worldcup(ADMIN_PATH)
        recompute = sc.knockout_qualifier_recompute(picks, actual, sc.knockout_equipos_baremo())
        matchups = sc.read_knockout_matchup_picks(ADMIN_PATH)
    except Exception as e:
        print(f"[{POOL_ID}] WARN _refresh_knockout: {e}", file=sys.stderr)
        return
    resolved = {cat: sorted(actual[cat]) for cat, *_ in sc.KO_EQUIPOS_BLOCKS if actual.get(cat)}
    por_jugador: dict[str, dict] = {}
    for name, rc in recompute.items():
        cats = {}
        for cat in resolved:
            r = rc.get(cat, {})
            if r.get("aciertos") or r.get("fallos"):
                cats[cat] = {"n": r["n"],
                             "aciertos": [_sanitize_for_bot(t) for t in r["aciertos"]],
                             "fallos": [_sanitize_for_bot(t) for t in r["fallos"]]}
        if cats:
            por_jugador[_sanitize_for_bot(name)] = cats
    picks_out: dict[str, dict] = {}
    for name, rounds in matchups.items():
        ro = {rnd: [{"cruce": _sanitize_for_bot(it["cruce"]), "marcador": it["marcador"]}
                    for it in items]
              for rnd, items in rounds.items() if items}
        if ro:
            picks_out[_sanitize_for_bot(name)] = ro
    state["knockout"] = {
        "rondas": {cat: [_sanitize_for_bot(t) for t in teams] for cat, teams in resolved.items()},
        "por_jugador": por_jugador,
        "picks": picks_out,
    }


def _sync_to_vps(state: dict, players_db: list[dict]) -> None:
    """Empuja state.json y players.json al VPS. No bloqueante."""
    try:
        sync_pool(POOL_ID, state, players_db)
        print(f"[{POOL_ID}] sync al VPS OK")
    except Exception as e:
        print(f"[{POOL_ID}] WARN: sync al VPS falló: {e}", file=sys.stderr)


def _format_night_digest(entries: list[dict]) -> str:
    """Resumen escueto de los partidos jugados durante el silencio nocturno: por
    cada partido, marcador y quién clavó el resultado. Una sola publicación."""
    lines = [f"🌙 Resumen de noche · {_madrid_now().strftime('%d/%m')}", ""]
    for e in entries:
        linea = f"⚽ {e['home_es']} {e['home_score']}-{e['away_score']} {e['away_es']}"
        if (e.get("duration") == "PENALTY_SHOOTOUT"
                and e.get("home_penalties") is not None
                and e.get("away_penalties") is not None):
            hp, ap = e["home_penalties"], e["away_penalties"]
            quien = e["home_es"] if hp > ap else e["away_es"]
            linea += f" (pens {hp}-{ap}; pasó {quien})"
        lines.append(linea)
        clavaron = e.get("clavaron") or []
        lines.append(f"   🎯 Lo clavó: {', '.join(clavaron)}" if clavaron
                     else "   🎯 Nadie clavó")
    web_url = web_url_for_pool(POOL_ID)
    if web_url:
        lines += ["", f"🌐 {web_url}"]
    return "\n".join(lines)


def _flush_night_digest(state: dict) -> None:
    """A las 07:00 (Madrid) publica el resumen de la noche y vacía el digest.

    Idempotente por fecha de Madrid (night_digest_sent_date), igual que el disparo
    diario del dispatcher. Banda de envío [07:00, 12:00): fuera de ella se descarta
    sin enviar para no soltar un resumen rancio por la tarde."""
    digest = state.get("night_digest") or []
    if not digest:
        return
    today = _madrid_now().strftime("%Y-%m-%d")
    if state.get("night_digest_sent_date") == today:
        state["night_digest"] = []   # restos de un envío ya hecho hoy
        return
    hour = _madrid_now().hour
    if hour < 7:
        return  # sigue el silencio
    entries = [e for e in digest if str(e.get("ts", "")).startswith(today)]
    if 7 <= hour < 12 and entries:
        try:
            publish(_format_night_digest(entries), force=True)
            print(f"[{POOL_ID}] resumen nocturno enviado ({len(entries)} partido/s)")
        except Exception as e:
            print(f"[{POOL_ID}] WARN publicando resumen nocturno: {e}", file=sys.stderr)
            return  # no marcar como enviado: que reintente el próximo run
    else:
        print(f"[{POOL_ID}] resumen nocturno descartado (hora {hour}h / sin entradas de hoy)")
    state["night_digest_sent_date"] = today
    state["night_digest"] = []


def main() -> int:
    state = load_state()
    teams_es = load_teams_en_es()
    row_map = load_match_row_map()
    players_db = load_players_db()

    matches = fetch_finished_matches()
    new_matches = [m for m in matches if m["id"] not in state["announced_match_ids"]]

    if not new_matches:
        print(f"[{POOL_ID}] No hay partidos nuevos. last_run={state.get('last_run_at')} "
              f"ya_anunciados={len(state['announced_match_ids'])}")
        _refresh_leaderboard(state)
        _refresh_next_match(state, teams_es)
        _refresh_upcoming_kickoffs(state, teams_es)
        _refresh_today_matches(state, teams_es)
        _refresh_upcoming_predictions(state, teams_es, row_map)
        _refresh_all_predictions(state, teams_es)
        _refresh_knockout(state)
        _announce_upcoming_kickoffs(state, teams_es, row_map, players_db)
        _flush_night_digest(state)
        save_state(state)
        _sync_to_vps(state, players_db)
        return 0

    print(f"[{POOL_ID}] Procesando {len(new_matches)} partido(s) nuevo(s)...")
    success = 0
    for m in new_matches:
        if process_match(m, teams_es, row_map, players_db, state):
            success += 1

    failed = len(new_matches) - success
    if failed:
        # Bandera para que el workflow ponga la run en rojo DESPUÉS de commitear
        # (sin romper el revert/reintento): sin esto el fallo queda enterrado en
        # el log con la run verde y nadie se entera (así se perdió el inaugural).
        Path(".ingest_failures").write_text(
            f"[{POOL_ID}] {failed} partido(s) sin procesar\n", encoding="utf-8")

    _refresh_leaderboard(state)
    _refresh_next_match(state, teams_es)
    _refresh_upcoming_kickoffs(state, teams_es)
    _refresh_upcoming_predictions(state, teams_es, row_map)
    _refresh_all_predictions(state, teams_es)
    _refresh_knockout(state)
    _announce_upcoming_kickoffs(state, teams_es, row_map, players_db)
    _flush_night_digest(state)
    save_state(state)
    _sync_to_vps(state, players_db)
    print(f"[{POOL_ID}] OK. {success}/{len(new_matches)} anunciados.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
