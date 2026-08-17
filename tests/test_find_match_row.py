"""_norm_team: comparación robusta de nombres para el fallback KO de
find_match_row (sin acentos/espacios/caja). Cierra discrepancias entre
teams_en_es.json y los nombres que el matejero cachea en WORLDCUP — una tilde
o un espacio de más bastaban para un SKIP silencioso de una eliminatoria.
"""
import ingest_match_results as ing


def test_norm_quita_acentos_y_caja():
    assert ing._norm_team("Bélgica") == ing._norm_team("belgica")
    assert ing._norm_team("Corea del Sur") == ing._norm_team("COREA DEL SUR")


def test_norm_colapsa_espacios():
    assert ing._norm_team("Bosnia y  Herzegovina") == ing._norm_team("Bosnia y Herzegovina")
    assert ing._norm_team("  Estados Unidos ") == ing._norm_team("Estados Unidos")
    assert ing._norm_team("Arabia Saudita") == ing._norm_team("arabia  saudita")


def test_norm_none_y_vacio():
    assert ing._norm_team(None) == ""
    assert ing._norm_team("") == ""


def test_norm_distingue_equipos_distintos():
    assert ing._norm_team("Brasil") != ing._norm_team("Brasilia")
    assert ing._norm_team("Argentina") != ing._norm_team("Argelia")
    assert ing._norm_team("Corea del Sur") != ing._norm_team("Corea del Norte")
