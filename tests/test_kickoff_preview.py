"""Unificación de partidos simultáneos en el aviso de inicio + prompt multi-partido.

- _group_by_kickoff: agrupa por hora exacta, separa horas distintas, preserva orden.
- _build_preview_user_prompt: 1 partido (cabecera "EN BREVE" + Kickoff) vs 2+ a la
  misma hora (cabecera "MISMA HORA", ambos emparejamientos, ambos bloques de
  predicciones y la línea de campeón fiado común).
"""
import ingest_match_results as ing
import lib_claude as lc


# --- _group_by_kickoff ---

def _m(mid, ko, home="A", away="B"):
    return {"id": mid, "utc_kickoff": ko, "home": home, "away": away}


def test_group_misma_hora_unifica():
    ko = "2026-06-26T18:00:00Z"
    groups = ing._group_by_kickoff([_m(1, ko, "España", "Brasil"),
                                    _m(2, ko, "Francia", "Argentina")])
    assert len(groups) == 1
    assert [g["id"] for g in groups[0]] == [1, 2]


def test_group_horas_distintas_separa():
    groups = ing._group_by_kickoff([_m(1, "2026-06-26T18:00:00Z"),
                                    _m(2, "2026-06-26T20:00:00Z")])
    assert len(groups) == 2
    assert [g[0]["id"] for g in groups] == [1, 2]  # preserva el orden de aparición


# --- _build_preview_user_prompt ---

def _match(home, away, *, md=3, preds=None):
    return {"home_es": home, "away_es": away,
            "utc_kickoff": "2026-06-26T18:00:00Z",
            "stage": "GROUP_STAGE", "group": "H", "matchday": md,
            "predictions": preds or {}}


def test_prompt_un_partido():
    p = lc._build_preview_user_prompt(
        [_match("España", "Argentina", md=1, preds={"Juan": ("1", 2, 1)})])
    assert "PARTIDO QUE EMPIEZA EN BREVE:" in p
    assert "Kickoff:" in p
    assert "España vs Argentina" in p
    assert "- Juan: 2-1" in p
    assert "MISMA HORA" not in p


def test_prompt_dos_partidos_misma_hora():
    p = lc._build_preview_user_prompt(
        [_match("España", "Brasil", preds={"Juan": ("1", 2, 0)}),
         _match("Francia", "Argentina", preds={"Pedro": ("2", 0, 1)})],
        champion_counts=[("España", 4)])
    assert "2 PARTIDOS QUE EMPIEZAN A LA MISMA HORA" in p
    # ambos emparejamientos y ambos bloques de predicciones
    assert "España vs Brasil" in p and "Francia vs Argentina" in p
    assert "- Juan: 2-0" in p and "- Pedro: 0-1" in p
    # campeón fiado, común a todo el grupo
    assert "España: 4" in p


# --- Eliminatorias: clasificación por cruce en previa y post-partido ---

def _match_ko(home, away, clasif):
    return {"home_es": home, "away_es": away,
            "utc_kickoff": "2026-06-26T18:00:00Z",
            "stage": "LAST_32", "group": None, "matchday": None,
            "predictions": {}, "ko_clasificacion": clasif}


def test_preview_ko_clasificacion_por_cruce():
    clasif = {
        "cruce": {"ROBER": {"marcador": "2-1", "signo": "1", "exacto": None}},
        "ambos": ["Bea"],
        "un_equipo": {"Ana": "Canadá"},
        "nada": ["Tom", "Leo", "Eva"],
    }
    p = lc._build_preview_user_prompt([_match_ko("Sudáfrica", "Canadá", clasif)])
    assert "Acertaron el cruce (local-visitante):" in p
    assert "- ROBER: 2-1" in p
    assert "Acertaron los dos equipos pero no el cruce: Bea" in p
    assert "Acertaron solo un equipo: Ana (Canadá)" in p
    assert "No acertaron el cruce: 3 jugadores" in p


def test_post_match_ko_no_atribuye_clavada_sin_cruce():
    # Caso Hugo: acertó solo un equipo (Canadá); su lectura por fila era 0-1 (=real),
    # pero al no acertar el cruce NO debe figurar como que clavó.
    match = {"home_es": "Sudáfrica", "away_es": "Canadá",
             "home_score": 0, "away_score": 1, "label": "2026-07-04"}
    clasif = {"cruce": {}, "ambos": [],
              "un_equipo": {"Hugo": "Canadá"}, "nada": ["Ana", "Leo"]}
    prompt = lc._build_user_prompt(
        match, {"Hugo": 0}, [], [],
        predictions={"Hugo": ("2", 0, 1)},      # lectura por fila (no fiel al cruce)
        ko_clasificacion=clasif)
    assert "ELIMINATORIA: solo puntúa quien acertó el cruce" in prompt
    assert "Acertaron el cruce y clavaron el resultado exacto: nadie" in prompt
    assert "Acertaron solo un equipo: Hugo (Canadá)" in prompt
    assert "No acertaron el cruce: 2 jugadores" in prompt
    assert "Hugo (0-1)" not in prompt           # no se le atribuye haber clavado
