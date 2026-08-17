"""Cliente mínimo para resultados oficiales del Mundial 2026.

Estrategia:
- Fuente principal: football-data.org (free tier, 600 req/día, sin tarjeta).
  Sesión con 3 reintentos + backoff para 429/5xx puntuales.
- Modo DRY_RUN=1 (env var): devuelve un partido falso para desarrollo/tests.
- Sin fallback de otra fuente: aceptamos el riesgo (probabilidad de outage
  >2h durante el Mundial es baja; con cron cada 15 min se recupera solo en
  cuanto vuelva la API).

API key se pasa por argumento o env FOOTBALL_API_KEY.

Modelo de partido devuelto:
  {
    "id": int,                 # ID estable de football-data.org
    "home": str,               # nombre en inglés (mapear con teams_en_es.json)
    "away": str,
    "home_score": int,         # score.fullTime.home (incluye goles de extra time, NO penaltis)
    "away_score": int,         # score.fullTime.away (incluye goles de extra time, NO penaltis)
    "duration": str,           # "REGULAR" (90'), "EXTRA_TIME" (120'), "PENALTY_SHOOTOUT"
    "home_penalties": int|None,  # solo si fue a penaltis; sino None
    "away_penalties": int|None,
    "status": "FINISHED",      # solo FINISHED nos interesa para el cron
    "utc_kickoff": str,        # ISO 8601 UTC
    "stage": str,              # "GROUP_STAGE" | "LAST_16" | "LAST_32" | "QUARTER_FINALS" | "SEMI_FINALS" | "THIRD_PLACE" | "FINAL"
    "group": str|None,         # "A"-"L" en GROUP_STAGE, None en eliminatorias
    "matchday": int|None,      # 1-3 en GROUP_STAGE, None en eliminatorias
  }

Nota matejero: para knockouts que van a penaltis, además del score 120' en
WORLDCUP!AC:AD hay que escribir penalties en WORLDCUP!AB:AE para que la
fórmula del Excel pueda inferir el ganador y resolver el bracket.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

FD_BASE = "https://api.football-data.org/v4"
WC_COMPETITION_CODE = "WC"  # FIFA World Cup (id 2000), confirmado vs docs football-data.org


def _build_session() -> requests.Session:
    """Sesión con reintentos para sobrevivir a 429/5xx puntuales de football-data.

    3 reintentos con backoff 0.5s/1s/2s (urllib3 añade jitter). Aplicado a GET.
    Si el servidor manda Retry-After (típico en 429), se respeta.
    """
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    s = requests.Session()
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


_SESSION = _build_session()


def _dry_run_matches() -> list[dict[str, Any]]:
    """Partido sintético del Mundial 2026 (España 2-1 Cabo Verde, Grupo H Jornada 1)
    para que el flujo del orquestador encuentre la fila en match_row_map.json."""
    return [
        {
            "id": 999014,
            "home": "Spain",
            "away": "Cape Verde",
            "home_score": 2,
            "away_score": 1,
            "duration": "REGULAR",
            "home_penalties": None,
            "away_penalties": None,
            "status": "FINISHED",
            "utc_kickoff": "2026-06-15T19:00:00Z",
            "stage": "GROUP_STAGE",
            "group": "H",
            "matchday": 1,
        }
    ]


def fetch_finished_matches(
    api_key: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    timeout: int = 15,
) -> list[dict[str, Any]]:
    """Devuelve partidos del Mundial con status=FINISHED en el rango de fechas.

    Si DRY_RUN=1 está en env, devuelve datos falsos y NO llama a la API.
    """
    if os.environ.get("DRY_RUN") == "1":
        return _dry_run_matches()

    api_key = api_key or os.environ.get("FOOTBALL_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Falta FOOTBALL_API_KEY (env var) o argumento api_key. "
            "Regístrate gratis en https://www.football-data.org/client/register"
        )

    today = datetime.now(timezone.utc).date()
    # Ventana ayer+hoy: si un partido falla de forma transitoria y el reintento
    # cae pasada la medianoche UTC, el siguiente cron aún lo ve (lo ya anunciado
    # se filtra por announced_match_ids, así que no hay duplicados).
    df = (date_from or (today - timedelta(days=1))).isoformat()
    dt = (date_to or today).isoformat()

    url = f"{FD_BASE}/competitions/{WC_COMPETITION_CODE}/matches"
    params = {"dateFrom": df, "dateTo": dt, "status": "FINISHED"}
    headers = {"X-Auth-Token": api_key}

    resp = _SESSION.get(url, params=params, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    matches = []
    for m in data.get("matches", []):
        score = m.get("score") or {}
        ft = score.get("fullTime") or {}  # ya incluye goles de ET (no penaltis)
        pens = score.get("penalties") or {}
        duration = score.get("duration", "REGULAR")
        if ft.get("home") is None or ft.get("away") is None:
            # Glitch de la API (visto el 11/06 con el inaugural): FINISHED con
            # marcador null. Se descarta sin anunciar para que el próximo cron
            # lo reintente ya con datos completos.
            print(f"WARN: partido {m.get('id')} FINISHED sin marcador, lo dejo "
                  f"para el próximo cron", file=sys.stderr)
            continue
        # Eliminatoria empatada en el campo sin la tanda de penaltis poblada:
        # mismo riesgo que el marcador null pero un nivel más profundo. La API
        # llega a marcar FINISHED con el empate de 120' ANTES de exponer
        # score.penalties; si lo procesáramos, escribiríamos un empate sin
        # desempate, el Excel no podría inferir quién pasa y el cuadro quedaría
        # congelado (y el partido marcado como anunciado no se reintentaría).
        # Se descarta para que el próximo cron lo reintente con la tanda completa.
        if (duration in ("EXTRA_TIME", "PENALTY_SHOOTOUT")
                and ft.get("home") == ft.get("away")
                and (pens.get("home") is None or pens.get("away") is None)):
            print(f"WARN: partido {m.get('id')} {duration} empatado sin penaltis, "
                  f"lo dejo para el próximo cron", file=sys.stderr)
            continue
        matches.append({
            "id": m["id"],
            "home": m["homeTeam"]["name"],
            "away": m["awayTeam"]["name"],
            "home_score": ft.get("home"),
            "away_score": ft.get("away"),
            "duration": duration,
            "home_penalties": pens.get("home"),
            "away_penalties": pens.get("away"),
            "status": m["status"],
            "utc_kickoff": m["utcDate"],
            "stage": m.get("stage", "GROUP_STAGE"),
            "group": m.get("group"),
            "matchday": m.get("matchday"),
        })
    return matches


def fetch_upcoming_matches(
    within_minutes: int = 20,
    api_key: str | None = None,
    timeout: int = 15,
) -> list[dict[str, Any]]:
    """Partidos cuyo kickoff cae en [now, now+within_minutes] (UTC).

    Usado para anunciar el inicio de cada partido en el grupo WhatsApp.
    Filtramos en local porque la API solo permite filtrar por fecha, no por hora.

    DRY_RUN=1: devuelve un partido sintético con kickoff a +5 min.
    """
    if os.environ.get("DRY_RUN") == "1":
        kickoff = datetime.now(timezone.utc) + timedelta(minutes=5)
        return [
            {
                "id": 999016,
                "home": "Mexico",
                "away": "Saudi Arabia",
                "utc_kickoff": kickoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "stage": "GROUP_STAGE",
                "group": "A",
                "matchday": 1,
            }
        ]

    api_key = api_key or os.environ.get("FOOTBALL_API_KEY")
    if not api_key:
        raise RuntimeError("Falta FOOTBALL_API_KEY (env var) o argumento api_key.")

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(minutes=within_minutes)
    # dateTo cubre hoy y, si el horizonte cruza medianoche UTC, mañana también.
    params = {
        "dateFrom": now.date().isoformat(),
        "dateTo": horizon.date().isoformat(),
        "status": "SCHEDULED",
    }
    url = f"{FD_BASE}/competitions/{WC_COMPETITION_CODE}/matches"
    headers = {"X-Auth-Token": api_key}
    resp = _SESSION.get(url, params=params, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    out: list[dict[str, Any]] = []
    for m in data.get("matches", []):
        # utcDate viene como "2026-06-11T20:00:00Z"; admit ambos sufijos por si la API cambia.
        raw = m["utcDate"].replace("Z", "+00:00")
        kickoff = datetime.fromisoformat(raw)
        if now <= kickoff <= horizon:
            out.append({
                "id": m["id"],
                "home": m["homeTeam"]["name"],
                "away": m["awayTeam"]["name"],
                "utc_kickoff": m["utcDate"],
                "stage": m.get("stage", "GROUP_STAGE"),
                "group": m.get("group"),
                "matchday": m.get("matchday"),
            })
    return out


def fetch_scheduled_matches(
    api_key: str | None = None,
    days_ahead: int = 2,
    timeout: int = 15,
) -> list[dict[str, Any]]:
    """Partidos SCHEDULED del Mundial en [hoy, hoy+days_ahead], por kickoff asc.

    Lo usa el bot vía state.json: `!proximo` (primero de la lista a 14 días) y
    el contexto de predicciones de !claudio (próximos ~2 días).

    DRY_RUN=1: devuelve un partido sintético.
    """
    if os.environ.get("DRY_RUN") == "1":
        return [{
            "id": 999015,
            "home": "Mexico",
            "away": "Saudi Arabia",
            "utc_kickoff": "2026-06-11T20:00:00Z",
            "stage": "GROUP_STAGE",
            "group": "A",
            "matchday": 1,
        }]

    api_key = api_key or os.environ.get("FOOTBALL_API_KEY")
    if not api_key:
        raise RuntimeError("Falta FOOTBALL_API_KEY (env var) o argumento api_key.")

    today = datetime.now(timezone.utc).date()
    params = {
        "dateFrom": today.isoformat(),
        "dateTo": (today + timedelta(days=days_ahead)).isoformat(),
        "status": "SCHEDULED",
    }
    url = f"{FD_BASE}/competitions/{WC_COMPETITION_CODE}/matches"
    headers = {"X-Auth-Token": api_key}
    resp = _SESSION.get(url, params=params, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    matches = sorted(data.get("matches", []), key=lambda m: m["utcDate"])
    return [{
        "id": m["id"],
        "home": m["homeTeam"]["name"],
        "away": m["awayTeam"]["name"],
        "utc_kickoff": m["utcDate"],
        "stage": m.get("stage", "GROUP_STAGE"),
        "group": m.get("group"),
        "matchday": m.get("matchday"),
    } for m in matches]


def fetch_next_scheduled_match(
    api_key: str | None = None,
    days_ahead: int = 14,
    timeout: int = 15,
) -> dict[str, Any] | None:
    """Próximo partido SCHEDULED (kickoff más cercano) o None si no hay."""
    matches = fetch_scheduled_matches(api_key=api_key, days_ahead=days_ahead,
                                      timeout=timeout)
    return matches[0] if matches else None


def fetch_today_matches(
    api_key: str | None = None,
    timeout: int = 15,
) -> list[dict[str, Any]]:
    """Partidos del Mundial cuya fecha en Europe/Madrid es HOY, en cualquier estado
    (SCHEDULED/IN_PLAY/PAUSED/FINISHED...), con marcador si lo hay.

    Lo usa el bot ('partidos de hoy', incluidos los ya jugados con resultado).
    Un día de Madrid cruza dos fechas UTC, así que se pide [hoy-1, hoy] en UTC y
    se filtra a los que caen en el día de Madrid de hoy.

    DRY_RUN=1: devuelve un partido sintético de hoy (en juego).
    """
    madrid = ZoneInfo("Europe/Madrid")
    today_madrid = datetime.now(madrid).date()

    if os.environ.get("DRY_RUN") == "1":
        return [{
            "id": 999017,
            "home": "Spain",
            "away": "Cape Verde",
            "home_score": 1,
            "away_score": 0,
            "status": "IN_PLAY",
            "utc_kickoff": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "stage": "GROUP_STAGE",
            "group": "H",
            "matchday": 1,
        }]

    api_key = api_key or os.environ.get("FOOTBALL_API_KEY")
    if not api_key:
        raise RuntimeError("Falta FOOTBALL_API_KEY (env var) o argumento api_key.")

    params = {
        "dateFrom": (today_madrid - timedelta(days=1)).isoformat(),
        "dateTo": today_madrid.isoformat(),
    }
    url = f"{FD_BASE}/competitions/{WC_COMPETITION_CODE}/matches"
    headers = {"X-Auth-Token": api_key}
    resp = _SESSION.get(url, params=params, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    out: list[dict[str, Any]] = []
    for m in sorted(data.get("matches", []), key=lambda m: m["utcDate"]):
        raw = m["utcDate"].replace("Z", "+00:00")
        if datetime.fromisoformat(raw).astimezone(madrid).date() != today_madrid:
            continue
        score = m.get("score") or {}
        ft = score.get("fullTime") or {}
        out.append({
            "id": m["id"],
            "home": m["homeTeam"]["name"],
            "away": m["awayTeam"]["name"],
            "home_score": ft.get("home"),
            "away_score": ft.get("away"),
            "status": m["status"],
            "utc_kickoff": m["utcDate"],
            "stage": m.get("stage", "GROUP_STAGE"),
            "group": m.get("group"),
            "matchday": m.get("matchday"),
        })
    return out


def fetch_all_matches(
    api_key: str | None = None,
    timeout: int = 15,
) -> list[dict[str, Any]]:
    """TODOS los partidos del Mundial (cualquier estado), para la web.

    Una sola llamada al endpoint de la competición (sin filtro de estado/fecha):
    devuelve el calendario completo con marcador (si lo hay), estado, fase, grupo
    y jornada. Los partidos de eliminatorias sin equipos aún resueltos llegan con
    `home`/`away` a None (la web los pinta como "por definir").

    DRY_RUN=1: devuelve el partido sintético de fetch_finished_matches.
    """
    if os.environ.get("DRY_RUN") == "1":
        return _dry_run_matches()

    api_key = api_key or os.environ.get("FOOTBALL_API_KEY")
    if not api_key:
        raise RuntimeError("Falta FOOTBALL_API_KEY (env var) o argumento api_key.")

    url = f"{FD_BASE}/competitions/{WC_COMPETITION_CODE}/matches"
    resp = _SESSION.get(url, headers={"X-Auth-Token": api_key}, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    out: list[dict[str, Any]] = []
    for m in data.get("matches", []):
        score = m.get("score") or {}
        ft = score.get("fullTime") or {}
        pens = score.get("penalties") or {}
        out.append({
            "id": m["id"],
            "home": (m.get("homeTeam") or {}).get("name"),   # None si aún por definir
            "away": (m.get("awayTeam") or {}).get("name"),
            "home_score": ft.get("home"),
            "away_score": ft.get("away"),
            "duration": score.get("duration", "REGULAR"),
            "home_penalties": pens.get("home"),
            "away_penalties": pens.get("away"),
            "status": m.get("status"),
            "utc_kickoff": m.get("utcDate"),
            "stage": m.get("stage", "GROUP_STAGE"),
            "group": m.get("group"),
            "matchday": m.get("matchday"),
        })
    return out


if __name__ == "__main__":
    # Ejecución directa para smoke test: imprime lo que devuelva (DRY_RUN o API).
    import json
    matches = fetch_finished_matches()
    print(json.dumps(matches, indent=2, ensure_ascii=False))
