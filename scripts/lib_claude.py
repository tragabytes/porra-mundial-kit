"""Genera comentario socarrón estilo Marañón/Camacho para un partido del Mundial.

- Modelo: Claude Opus 4.5 (decidido en PLAN.md, prioriza calidad de tono).
- Prompt caching: el system prompt (instrucciones + perfiles de los 25 jugadores)
  se cachea con `cache_control: ephemeral`. Solo el user prompt varía por partido.
- Anti-alucinación: el LLM solo redacta el comentario. TODOS los datos numéricos
  (resultado, puntos, ranking) se inyectan en el user prompt para que Claude los
  cite literalmente. Si Claude inventa una cifra, es un bug del prompt.

Modo DRY_RUN=1: devuelve un comentario sintético sin llamar a la API. Útil para
testear el pipeline completo sin gastar tokens y sin necesitar la API key.

Usage como módulo:
  from lib_claude import generate_commentary
  text = generate_commentary(match, points_per_player, ranking_before,
                             ranking_after, players_db)

Usage CLI (smoke test):
  python scripts/lib_claude.py            # llama API real (necesita ANTHROPIC_API_KEY)
  DRY_RUN=1 python scripts/lib_claude.py  # sin API, devuelve mock
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

MODEL = "claude-opus-4-5"
MAX_TOKENS = 500

# Modelo más ligero para los anuncios pre-partido: 3-4 frases, sin necesidad de
# la calidad de prosa de Opus. Sonnet 4.6 cuesta ~10x menos y va sobrado.
PREVIEW_MODEL = "claude-sonnet-4-6"
PREVIEW_MAX_TOKENS = 400

MADRID_TZ = ZoneInfo("Europe/Madrid")

SYSTEM_INSTRUCTIONS = """Eres un comentarista de fútbol español irreverente y socarrón, estilo Manolo Lama, Marañón o Camacho. Tu trabajo: comentar el resultado de un partido del Mundial 2026 para un grupo de WhatsApp donde 25 amigos compiten en una porra.

REGLAS DURAS:
- Cita el resultado y los puntos EXACTOS tal como aparecen en el contexto. NO inventes números ni resultados.
- Usa los nombres de jugador tal como aparecen.
- Tono: socarrón, juegos de palabras, alguna metáfora futbolística clásica (parar un autobús, partido feo de croqueta, etc.).
- Pulla suave a los que fallaron y halago exagerado a quien clavó el resultado. Si te paso las PREDICCIONES de este partido, úsalas para pullas concretas con nombre: destaca a quien clavó el marcador exacto y a algún fallón sonado. Cita a 2-4 personas como mucho; NO recites la lista entera.
- En ELIMINATORIAS solo puntúa quien acertó el CRUCE (los dos equipos que se enfrentan). Si te paso la clasificación por grupos (acertaron el cruce / solo un equipo / nada), respétala al pie de la letra: NO digas que clavó el resultado ni que acertó nadie que no esté en el grupo "acertaron el cruce". A quien acertó solo un equipo o nada, trátalo como fallo del cruce.
- Si te paso el RANKING DEL DÍA, suéltalo de pasada (quién manda hoy en la porra).
- Aprovecha las afinidades de equipo de cada jugador si vienen al caso (madridista comentando un Atleti, culer sufriendo, sufridor de Argentina, etc.).
- Longitud: 4-7 frases. Castellano de España. Una línea en blanco entre párrafos si te ayuda a respirar.

PROHIBIDO:
- Insultos personales reales (idiota, gilipollas, subnormal, etc.). Sí valen "pardillo", "manta", "cenizo".
- Temas sensibles (política, religión, conflictos territoriales).
- Inventarte cualquier dato que no esté en el contexto.

FORMATO DE SALIDA: solo el texto del comentario, sin prefijos como "Comentario:" ni explicaciones. Listo para enviarse tal cual al grupo de WhatsApp."""


def _build_system_prompt(players_db: list[dict]) -> str:
    """System prompt: instrucciones + perfil de los 25 jugadores (cacheable)."""
    lines = [SYSTEM_INSTRUCTIONS, "", "PERFILES DE LOS JUGADORES:"]
    for p in sorted(players_db, key=lambda x: x.get("slot", 0)):
        bits = [p["name"]]
        if p.get("club"):
            bits.append(f"del {p['club']}")
        if p.get("national") and p["national"] != "España":
            bits.append(f"afín a {p['national']}")
        picks = []
        if p.get("bota_oro"):
            picks.append(f"{p['bota_oro']} como bota de oro")
        if p.get("balon_oro"):
            picks.append(f"{p['balon_oro']} como balón de oro")
        if p.get("campeon"):
            picks.append(f"{p['campeon']} como campeón del Mundial")
        if picks:
            bits.append("predijo " + " y ".join(picks))
        lines.append(f"- " + ", ".join(bits))
    return "\n".join(lines)


def _sign(home: int, away: int) -> str:
    """Signo 1X2 de un marcador."""
    return "1" if home > away else ("2" if home < away else "X")


def _format_ko_partial_lines(clasif: dict) -> list[str]:
    """Líneas comunes (ambos / un equipo / nada) de la clasificación por cruce KO.

    Quien acertó los dos equipos sin emparejar y quien acertó solo uno se nombran;
    los que no acertaron nada van solo como recuento (suelen ser mayoría)."""
    lines = []
    ambos = clasif.get("ambos") or []
    if ambos:
        lines.append(f"- Acertaron los dos equipos pero no el cruce: {', '.join(ambos)}")
    un_equipo = clasif.get("un_equipo") or {}
    if un_equipo:
        partes = ", ".join(f"{n} ({eq})" for n, eq in un_equipo.items())
        lines.append(f"- Acertaron solo un equipo: {partes}")
    nada = clasif.get("nada") or []
    if nada:
        n = len(nada)
        lines.append(f"- No acertaron el cruce: {n} jugador{'es' if n != 1 else ''}")
    return lines


def _build_user_prompt(
    match: dict,
    points_per_player: dict[str, int],
    ranking_before: list[dict],
    ranking_after: list[dict],
    predictions: dict[str, tuple] | None = None,
    daily_ranking: list[dict] | None = None,
    ko_clasificacion: dict | None = None,
) -> str:
    """User prompt: datos del partido + tabla antes/después + predicciones.

    `ko_clasificacion` (eliminatorias): clasificación por cruce de
    lib_scoring.knockout_clasificacion. Si viene, manda sobre `predictions` (que en
    KO se lee por fila y no es fiel al cruce). En grupos va None y se usa
    `predictions`."""
    lines = [
        f"PARTIDO: {match['home_es']} {match['home_score']}-{match['away_score']} {match['away_es']}",
        f"({match.get('label', 'Mundial 2026')})",
        "",
        "Puntos repartidos en este partido:",
    ]
    sorted_points = sorted(points_per_player.items(), key=lambda kv: -kv[1])
    for name, pts in sorted_points:
        lines.append(f"- {name}: {pts:+d} pts" if pts != 0 else f"- {name}: 0 pts")

    # Predicciones de este partido, clasificadas contra el resultado real.
    if ko_clasificacion is not None:
        # Eliminatoria: solo puntúa quien acertó el cruce. Se subdivide ese grupo por
        # acierto del marcador y se añaden los parciales (un equipo / nada).
        real_h, real_a = match["home_score"], match["away_score"]
        real_sign = _sign(real_h, real_a)
        cruce = ko_clasificacion.get("cruce") or {}
        clavaron, signo_ok, fallaron = [], [], []
        for name, v in cruce.items():
            etiqueta = f"{name} ({v['marcador']})"
            if v.get("exacto"):
                clavaron.append(etiqueta)
            elif v.get("signo") == real_sign:
                signo_ok.append(etiqueta)
            else:
                fallaron.append(etiqueta)
        lines += ["", f"Predicciones de este partido (resultado real {real_h}-{real_a}; "
                  "ELIMINATORIA: solo puntúa quien acertó el cruce):"]
        lines.append(f"- Acertaron el cruce y clavaron el resultado exacto: {', '.join(clavaron) or 'nadie'}")
        lines.append(f"- Acertaron el cruce y el ganador (no el marcador): {', '.join(signo_ok) or 'nadie'}")
        lines.append(f"- Acertaron el cruce pero fallaron el marcador: {', '.join(fallaron) or 'nadie'}")
        lines += _format_ko_partial_lines(ko_clasificacion)
    elif predictions:
        real_h, real_a = match["home_score"], match["away_score"]
        real_sign = _sign(real_h, real_a)
        clavaron, solo_signo, fallaron = [], [], []
        for name, (_sgn, ph, pa) in predictions.items():
            if ph == real_h and pa == real_a:
                clavaron.append(f"{name} ({ph}-{pa})")
            elif _sign(ph, pa) == real_sign:
                solo_signo.append(f"{name} ({ph}-{pa})")
            else:
                fallaron.append(f"{name} ({ph}-{pa})")
        lines += ["", f"Predicciones de este partido (resultado real {real_h}-{real_a}):"]
        lines.append(f"- Clavaron el resultado exacto: {', '.join(clavaron) or 'nadie'}")
        lines.append(f"- Acertaron solo el ganador: {', '.join(solo_signo) or 'nadie'}")
        lines.append(f"- Fallaron del todo: {', '.join(fallaron) or 'nadie'}")

    lines += ["", "Top 5 ANTES del partido:"]
    for entry in ranking_before[:5]:
        lines.append(f"  {entry['position']}. {entry['name']} — {entry['points']} pts")

    lines += ["", "Top 5 DESPUÉS del partido:"]
    for entry in ranking_after[:5]:
        lines.append(f"  {entry['position']}. {entry['name']} — {entry['points']} pts")

    # Cambios destacables: movimientos de >2 posiciones
    movers = _detect_movers(ranking_before, ranking_after)
    if movers:
        lines += ["", "Movimientos destacables:"]
        lines += [f"- {m}" for m in movers]

    if daily_ranking:
        lines += ["", "Ranking del día (puntos sumados hoy):"]
        for entry in daily_ranking[:8]:
            lines.append(f"  {entry['position']}. {entry['name']} — {entry['points']} pts")

    lines += ["", "Genera tu comentario."]
    return "\n".join(lines)


def _detect_movers(before: list[dict], after: list[dict]) -> list[str]:
    """Detecta jugadores que han subido/bajado >=3 posiciones."""
    pos_before = {p["name"]: p["position"] for p in before}
    movers = []
    for entry in after:
        old = pos_before.get(entry["name"])
        if old is None:
            continue
        delta = old - entry["position"]
        if delta >= 3:
            movers.append(f"{entry['name']} sube {delta} puestos hasta el {entry['position']}º")
        elif delta <= -3:
            movers.append(f"{entry['name']} cae {-delta} puestos hasta el {entry['position']}º")
    return movers


def _dry_run_commentary(match: dict, points_per_player: dict[str, int]) -> str:
    """Comentario sintético para tests sin API."""
    top_scorer = max(points_per_player.items(), key=lambda kv: kv[1], default=("nadie", 0))
    return (
        f"[DRY_RUN] {match['home_es']} {match['home_score']}-{match['away_score']} {match['away_es']}.\n"
        f"En esta porra el listo de hoy es {top_scorer[0]} con {top_scorer[1]} pts. "
        f"El resto a llorar al maestro armero."
    )


def generate_commentary(
    match: dict,
    points_per_player: dict[str, int],
    ranking_before: list[dict],
    ranking_after: list[dict],
    players_db: list[dict],
    predictions: dict[str, tuple] | None = None,
    daily_ranking: list[dict] | None = None,
    ko_clasificacion: dict | None = None,
) -> str:
    """Llama a Claude (o DRY_RUN mock) y devuelve el texto del comentario."""
    if os.environ.get("DRY_RUN") == "1":
        return _dry_run_commentary(match, points_per_player)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Falta ANTHROPIC_API_KEY (env var). "
            "Si solo quieres testear, ejecuta con DRY_RUN=1."
        )

    import anthropic  # import lazy para que DRY_RUN no exija la dependencia
    client = anthropic.Anthropic(api_key=api_key)

    system_prompt = _build_system_prompt(players_db)
    user_prompt = _build_user_prompt(match, points_per_player, ranking_before,
                                     ranking_after, predictions, daily_ranking,
                                     ko_clasificacion=ko_clasificacion)

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_prompt}],
    )
    return response.content[0].text.strip()


# ---------------------------------------------------------------------------
# Preview pre-partido (PR-D)
# ---------------------------------------------------------------------------

PREVIEW_SYSTEM_INSTRUCTIONS = """Eres el bot de una porra del Mundial 2026 en un grupo de WhatsApp donde 25 amigos compiten. Tu trabajo: avisar de forma breve y neutra de que está a punto de empezar uno o varios partidos, y comentar las predicciones de la porra para ese/esos partido(s). Tono informativo y tranquilo, sin hacer de comentarista.

ESTRUCTURA (3-5 frases en total):
1. Anuncio escueto de qué partido(s) empieza(n) y a qué hora. Si te paso VARIOS partidos, es porque arrancan a la vez: preséntalos juntos en una sola frase ("Empiezan a la vez España-Brasil y Francia-Argentina a las 20:00").
2. Comenta las predicciones de la porra. Por cada partido, di quién se moja con qué marcador. Si son pocos, cítalos uno a uno; si son muchos, agrupa ("la mayoría ve un 2-0, y Fulano se sale con un 3-1"). De forma llana, sin piques ni hipérbole. En ELIMINATORIAS te paso una clasificación por cruce: di quién acertó el cruce (con su marcador), quién acertó solo un equipo (y cuál), quién acertó los dos equipos pero sin emparejarlos, y cuántos no acertaron el cruce. Respeta esos grupos tal cual: no atribuyas el cruce ni el marcador a quien no esté en el grupo "acertaron el cruce".
3. Si te paso la lista de campeones predichos en la porra Y alguna de las selecciones de los partidos listados aparece en ella, remata con cuántos le tienen fe ("4 de vosotros fían el Mundial a España"). Si ninguna aparece, omite este punto: no recites la lista ni menciones a otras selecciones.

REGLAS DURAS:
- PROHIBIDO inventarte datos. No añadas anécdotas, estadísticas, historiales ni curiosidades: limítate a anunciar el/los partido(s) y comentar las predicciones que te paso.
- No inventes predicciones de jugadores: usa SOLO las que aparezcan en la lista que te paso, con sus nombres tal cual.
- Tu conocimiento llega hasta enero 2026. NO hables del Mundial 2026 en sí, ni de la "forma actual" de los equipos, lesiones recientes ni resultados recientes.
- Castellano de España. Sin emojis. Sin disclaimers ("según mis datos…", "creo que…").
- Sin insultos personales (idiota, gilipollas…). Sin temas sensibles (política, religión, conflictos territoriales).

FORMATO DE SALIDA: solo el texto del anuncio, sin prefijos como "Anuncio:" ni explicaciones. Listo para enviarse tal cual al grupo de WhatsApp."""


def _format_kickoff_madrid(utc_kickoff: str) -> str:
    """Devuelve 'HH:MM (hora Madrid)' parseando un ISO 8601 UTC."""
    raw = utc_kickoff.replace("Z", "+00:00")
    utc = datetime.fromisoformat(raw).astimezone(timezone.utc)
    madrid = utc.astimezone(MADRID_TZ)
    return madrid.strftime("%H:%M")


def _format_stage_line(match: dict) -> str | None:
    """Línea de fase del partido (grupo/jornada o ronda KO), o None si no hay datos."""
    stage = match.get("stage", "GROUP_STAGE")
    if stage == "GROUP_STAGE":
        grp = match.get("group")
        md = match.get("matchday")
        if grp and md:
            return f"Fase de grupos, Grupo {grp}, Jornada {md}"
        if grp:
            return f"Fase de grupos, Grupo {grp}"
        return None
    readable = {
        "LAST_32": "Dieciseisavos de final",
        "LAST_16": "Octavos de final",
        "QUARTER_FINALS": "Cuartos de final",
        "SEMI_FINALS": "Semifinales",
        "THIRD_PLACE": "Partido por el tercer puesto",
        "FINAL": "Final",
    }.get(stage, stage)
    return f"{readable} (ELIMINATORIA: a vida o muerte; puede ir a prórroga y penaltis)"


def _build_preview_user_prompt(
    matches: list[dict],
    champion_counts: list[tuple[str, int]] | None = None,
) -> str:
    """User prompt con el/los partido(s) + predicciones de la porra.

    `matches`: lista de dicts (1 = caso normal; 2+ = arrancan a la misma hora). Cada
    item lleva home_es/away_es/utc_kickoff/stage/group/matchday y su propio campo
    `predictions` (dict nombre -> (signo, goles_local, goles_visitante)).
    """
    multi = len(matches) > 1

    # Hora común (los simultáneos comparten kickoff); si no parsea, seguimos sin hora.
    try:
        hora = _format_kickoff_madrid(matches[0]["utc_kickoff"])
    except Exception:
        hora = None

    if multi:
        cab = f"{len(matches)} PARTIDOS QUE EMPIEZAN A LA MISMA HORA"
        if hora:
            cab += f" ({hora} hora de Madrid)"
        lines = [cab + ":"]
    else:
        lines = ["PARTIDO QUE EMPIEZA EN BREVE:"]

    for i, match in enumerate(matches, 1):
        prefix = f"PARTIDO {i}: " if multi else ""
        lines += ["", f"{prefix}{match['home_es']} vs {match['away_es']}"]
        if not multi and hora:
            lines.append(f"Kickoff: {hora} hora de Madrid")
        stage_line = _format_stage_line(match)
        if stage_line:
            lines.append(stage_line)

        clasif = match.get("ko_clasificacion")
        if clasif is not None:
            # Eliminatoria: clasificación por cruce (no hay resultado aún).
            cruce = clasif.get("cruce") or {}
            if cruce:
                lines.append("Acertaron el cruce (local-visitante):")
                for name, v in cruce.items():
                    lines.append(f"- {name}: {v['marcador']}")
            else:
                lines.append("De momento nadie ha acertado el cruce.")
            lines += _format_ko_partial_lines(clasif)
        else:
            predictions = match.get("predictions") or {}
            if predictions:
                lines.append("Predicciones de la porra (local-visitante):")
                for name, (_sgn, ph, pa) in predictions.items():
                    lines.append(f"- {name}: {ph}-{pa}")

    if champion_counts:
        lines += ["", "Campeón del Mundial predicho en la porra (nº de jugadores por selección):"]
        for team, count in champion_counts:
            lines.append(f"- {team}: {count}")

    lines += ["", "Genera el anuncio según la estructura."]
    return "\n".join(lines)


def _dry_run_preview(matches: list[dict]) -> str:
    try:
        hora = _format_kickoff_madrid(matches[0]["utc_kickoff"])
    except Exception:
        hora = "??:??"
    pairs = " y ".join(f"{m['home_es']}-{m['away_es']}" for m in matches)
    return (
        f"[DRY_RUN] Atentos: empieza(n) {pairs} a las {hora} hora de Madrid. "
        f"Mock del preview, que aquí no se inventa ni una coma."
    )


def generate_preview(
    matches: list[dict],
    champion_counts: list[tuple[str, int]] | None = None,
) -> str:
    """Genera el anuncio pre-partido (3-5 frases). DRY_RUN devuelve mock.

    `matches`: lista de partidos (1 = normal; 2+ = arrancan a la misma hora y se
    unifican en un solo mensaje). Cada item lleva su campo `predictions`.
    """
    if os.environ.get("DRY_RUN") == "1":
        return _dry_run_preview(matches)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Falta ANTHROPIC_API_KEY (env var). "
            "Si solo quieres testear, ejecuta con DRY_RUN=1."
        )

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    response = client.messages.create(
        model=PREVIEW_MODEL,
        max_tokens=PREVIEW_MAX_TOKENS,
        system=PREVIEW_SYSTEM_INSTRUCTIONS,
        messages=[{"role": "user", "content": _build_preview_user_prompt(matches, champion_counts)}],
    )
    return response.content[0].text.strip()


# Smoke test ---
def _sample_inputs() -> tuple[dict, dict, list, list, list]:
    """Inputs sintéticos para CLI smoke test."""
    match = {
        "home_es": "España",
        "away_es": "Costa Rica",
        "home_score": 7,
        "away_score": 0,
        "label": "Mundial 2022, Grupo E, Jornada 1",
    }
    points = {"Juan": 5, "María": 2, "Pedro": 0, "Laura": 5, "Ana": 1}
    ranking_before = [
        {"position": 1, "name": "Juan", "points": 18},
        {"position": 2, "name": "Pedro", "points": 17},
        {"position": 3, "name": "Laura", "points": 15},
        {"position": 4, "name": "María", "points": 14},
        {"position": 5, "name": "Ana", "points": 12},
    ]
    ranking_after = [
        {"position": 1, "name": "Juan", "points": 23},
        {"position": 2, "name": "Laura", "points": 20},
        {"position": 3, "name": "Pedro", "points": 17},
        {"position": 4, "name": "María", "points": 16},
        {"position": 5, "name": "Ana", "points": 13},
    ]
    players_db = [
        {"slot": 1, "name": "Juan", "club": "Atlético Madrid", "national": "España"},
        {"slot": 2, "name": "María", "club": "FC Barcelona", "national": "España"},
        {"slot": 3, "name": "Pedro", "club": "Real Madrid", "national": "España"},
        {"slot": 4, "name": "Laura", "club": None, "national": "Argentina"},
        {"slot": 5, "name": "Ana", "club": "Athletic Club", "national": "España"},
    ]
    return match, points, ranking_before, ranking_after, players_db


if __name__ == "__main__":
    args = _sample_inputs()
    print("=== COMENTARIO FT ===")
    print(generate_commentary(*args))
    print()
    print("=== PREVIEW KICKOFF (1 partido) ===")
    sample_preview_match = {
        "home_es": "España",
        "away_es": "Argentina",
        "utc_kickoff": "2026-06-15T19:00:00Z",
        "stage": "GROUP_STAGE",
        "group": "H",
        "matchday": 1,
        "predictions": {"Juan": ("1", 2, 1), "María": ("X", 1, 1)},
    }
    print(generate_preview([sample_preview_match]))
    print()
    print("=== PREVIEW KICKOFF (2 partidos a la misma hora) ===")
    sample_preview_match2 = {
        "home_es": "Francia",
        "away_es": "Brasil",
        "utc_kickoff": "2026-06-15T19:00:00Z",
        "stage": "GROUP_STAGE",
        "group": "H",
        "matchday": 3,
        "predictions": {"Juan": ("2", 0, 2), "Pedro": ("1", 1, 0)},
    }
    print(generate_preview([sample_preview_match, sample_preview_match2],
                           champion_counts=[("España", 4), ("Brasil", 3)]))
