"""grupos_match_points: puntos por partido de fase de grupos (signo/exacto).

Baremo fijo (set_scoring.SCORING): acertar 1X2 = 1 pt, resultado exacto = +3
(total 4). El signo se deriva de los goles, no del carácter guardado.
"""
import ingest_match_results as ing


def gp(sign, ph, pa, rh, ra):
    return ing.grupos_match_points((sign, ph, pa), rh, ra)


def test_exacto_da_cuatro():
    assert gp("1", 2, 1, 2, 1) == 4


def test_solo_signo_da_uno():
    # Predijo victoria local 3-0; salió 2-1 (también local) -> solo signo.
    assert gp("1", 3, 0, 2, 1) == 1


def test_fallo_da_cero():
    # Predijo victoria local; salió victoria visitante.
    assert gp("1", 2, 1, 0, 2) == 0


def test_empate_exacto():
    assert gp("X", 1, 1, 1, 1) == 4


def test_empate_solo_signo():
    # Predijo 0-0; salió 1-1 -> mismo signo (empate), no exacto.
    assert gp("X", 0, 0, 1, 1) == 1


def test_visitante_solo_signo():
    assert gp("2", 0, 3, 1, 2) == 1


def test_signo_derivado_de_goles_no_del_caracter():
    # Carácter de signo incoherente con los goles: manda el marcador.
    # 'X' pero 2-0 es victoria local; salió 1-0 (local) -> signo correcto.
    assert gp("X", 2, 0, 1, 0) == 1


def test_constantes_del_baremo():
    assert ing.GRUPOS_SIGNO_PTS == 1
    assert ing.GRUPOS_EXACTO_PTS == 3
