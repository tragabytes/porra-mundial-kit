"""compute_standings: tabla de grupo desde los resultados (pura, sin Excel/API)."""
import build_web_data as bw

ISO = {"A": "AAA", "B": "BBB", "C": "CCC", "D": "DDD"}


def fin(h, a, hs, as_):
    return {"home_es": h, "away_es": a, "home_score": hs, "away_score": as_, "status": "finalizado"}


def sched(h, a):
    return {"home_es": h, "away_es": a, "home_score": None, "away_score": None, "status": "programado"}


def test_standings_basico():
    ms = [fin("A", "B", 2, 0), fin("C", "D", 1, 1), sched("A", "C")]
    st = bw.compute_standings(ms, ISO)
    by = {t["team_es"]: t for t in st}
    # los 4 equipos presentes (aunque A-C aún no se juega)
    assert set(by) == {"A", "B", "C", "D"}
    assert by["A"]["pts"] == 3 and by["A"]["dg"] == 2 and by["A"]["pos"] == 1
    assert by["B"]["pts"] == 0 and by["B"]["pj"] == 1
    assert by["C"]["pts"] == 1 and by["D"]["pts"] == 1
    assert by["A"]["team_iso"] == "AAA"
    # el partido programado no suma
    assert by["A"]["pj"] == 1


def test_standings_orden_por_pts_dg_gf():
    # A y C empatan a 3 pts; A con más DG va primero.
    ms = [fin("A", "B", 3, 0), fin("C", "D", 1, 0)]
    st = bw.compute_standings(ms, ISO)
    assert [t["team_es"] for t in st][:2] == ["A", "C"]
    assert st[0]["pos"] == 1 and st[1]["pos"] == 2


def test_standings_grupo_sin_jugar():
    # 4 equipos, ningún partido jugado -> todos a 0, presentes.
    ms = [sched("A", "B"), sched("C", "D")]
    st = bw.compute_standings(ms, ISO)
    assert len(st) == 4
    assert all(t["pj"] == 0 and t["pts"] == 0 for t in st)


def test_standings_desempate_head_to_head():
    # A y B empatan en pts/DG/GF globales; B ganó el enfrentamiento directo 2-1.
    # FIFA -> B por delante de A. Sin H2H, el alfabético daría A,B (mal).
    ms = [fin("B", "A", 2, 1), fin("A", "C", 1, 0), fin("C", "B", 1, 0)]
    st = bw.compute_standings(ms, ISO)
    order = [t["team_es"] for t in st]
    assert order[:2] == ["B", "A"]
    assert st[0]["pts"] == 3 and st[1]["pts"] == 3 and st[0]["dg"] == st[1]["dg"]


def test_standings_triple_empate_cae_a_alfabetico():
    # Ciclo A>B>C>A: todos pts3/DG0/GF1 y el H2H también es simétrico (cada uno
    # 1V/1D entre ellos) -> último recurso alfabético, determinista.
    ms = [fin("A", "B", 1, 0), fin("B", "C", 1, 0), fin("C", "A", 1, 0)]
    st = bw.compute_standings(ms, ISO)
    assert [t["team_es"] for t in st] == ["A", "B", "C"]
    assert all(t["pts"] == 3 and t["dg"] == 0 for t in st)


# --- Cuadro radial: _split_cruce / build_player_brackets / reached / eliminated ---

_VALID = {"España", "Italia", "Bosnia-Herzegovina", "Croacia", "Corea del Sur", "Canadá"}


def test_split_cruce_simple():
    assert bw._split_cruce("España-Italia", _VALID) == ("España", "Italia")


def test_split_cruce_nombre_con_espacios():
    assert bw._split_cruce("Corea del Sur-Canadá", _VALID) == ("Corea del Sur", "Canadá")


def test_split_cruce_guion_en_el_nombre():
    # 'Bosnia-Herzegovina' lleva guion: parte donde ambos lados son válidos.
    assert bw._split_cruce("Bosnia-Herzegovina-Croacia", _VALID) == ("Bosnia-Herzegovina", "Croacia")


def test_split_cruce_fallback_desconocido():
    assert bw._split_cruce("X-Y", set()) == ("X", "Y")


_ISO_KO = {"España": "ESP", "Italia": "ITA", "Brasil": "BRA", "Japón": "JPN", "Francia": "FRA"}


def test_player_brackets_orden_tamanos_y_champ():
    mu = {"Ana": {
        "Dieciseisavos": [{"cruce": "España-Italia", "marcador": "2-1"},
                          {"cruce": "Brasil-Japón", "marcador": "0-0"}],
        "Final": [{"cruce": "España-Brasil", "marcador": "3-1"}],
    }}
    out = bw.build_player_brackets(mu, [{"name": "Ana", "campeon": "Italia"}], _ISO_KO, None)
    br = out["Ana"]
    assert [len(br[k]) for k in ("R32", "R16", "QF", "SF", "F")] == [32, 16, 8, 4, 2]
    assert br["R32"][0] == {"iso": "ESP", "es": "España"}
    assert br["R32"][1] == {"iso": "ITA", "es": "Italia"}
    assert br["R32"][2] == {"iso": "BRA", "es": "Brasil"}
    assert br["R32"][4] is None                       # hueco rellenado con None
    # CHAMP = ganador del cruce Final (España 3-1), no el 'campeon' de players.json
    assert br["CHAMP"] == [{"iso": "ESP", "es": "España"}]


def test_player_brackets_champ_fallback_a_campeon_si_empate():
    mu = {"Leo": {"Final": [{"cruce": "España-Brasil", "marcador": "1-1"}]}}
    out = bw.build_player_brackets(mu, [{"name": "Leo", "campeon": "Francia"}], _ISO_KO, None)
    assert out["Leo"]["CHAMP"] == [{"iso": "FRA", "es": "Francia"}]


def test_player_brackets_omite_jugador_sin_picks():
    assert bw.build_player_brackets({"Vacio": {}}, [], _ISO_KO, None) == {}


# --- Reconstrucción del árbol por identidad (esqueleto) ---

def _full_bracket(oct_row_order):
    """Bracket completo y consistente de 32 equipos T0..T31. 16avos cruce i =
    (T{2i} vs T{2i+1}), gana T{2i}. Octavos/cuartos/… emparejan ganadores en orden de
    ÁRBOL, pero las FILAS de octavos se escriben según `oct_row_order` (permutación de
    0..7) para simular el orden NO adyacente del Excel."""
    def cr(h, a): return {"cruce": f"{h}-{a}", "marcador": "1-0"}
    t = lambda i: f"T{i}"
    d16 = [cr(t(2 * i), t(2 * i + 1)) for i in range(16)]
    w16 = [t(2 * i) for i in range(16)]
    oct_tree = [(w16[2 * r], w16[2 * r + 1]) for r in range(8)]
    woct = [p[0] for p in oct_tree]
    d8 = [cr(*oct_tree[r]) for r in oct_row_order]      # filas de octavos (posible) desordenadas
    qf_tree = [(woct[2 * r], woct[2 * r + 1]) for r in range(4)]
    wqf = [p[0] for p in qf_tree]
    d4 = [cr(*qf_tree[r]) for r in range(4)]
    sf_tree = [(wqf[2 * r], wqf[2 * r + 1]) for r in range(2)]
    wsf = [p[0] for p in sf_tree]
    d2 = [cr(*sf_tree[r]) for r in range(2)]
    return {"Dieciseisavos": d16, "Octavos": d8, "Cuartos": d4,
            "Semifinales": d2, "Final": [cr(wsf[0], wsf[1])]}


_ISO_T = {f"T{i}": f"T{i:02d}" for i in range(32)}


def test_skeleton_corrige_adyacencia_no_adyacente():
    pairs = bw._cross_pairs(_full_bracket([7, 6, 5, 4, 3, 2, 1, 0]), set(_ISO_T))
    # Aplanar por fila ROMPE la adyacencia del embudo...
    flat = bw._flatten_rows(pairs, _ISO_T)
    assert not all(flat["R16"][k] and flat["R16"][k]["iso"] in
                   {flat["R32"][2 * k]["iso"], flat["R32"][2 * k + 1]["iso"]} for k in range(16))
    # ...y el esqueleto la ARREGLA: cada Octavos[k] es uno de su par de 16avos.
    skel = bw._ko_skeleton(pairs)
    assert skel is not None
    laid = bw._lay_on_skeleton(pairs, skel, _ISO_T)
    assert [len(laid[x]) for x in ("R32", "R16", "QF", "SF", "F")] == [32, 16, 8, 4, 2]
    for k in range(16):
        par = {laid["R32"][2 * k]["iso"], laid["R32"][2 * k + 1]["iso"]}
        assert laid["R16"][k]["iso"] in par, f"R16[{k}] no es ganador de su par de 16avos"


def test_skeleton_none_si_incompleto():
    assert bw._ko_skeleton({"R32": [("A", "B")], "R16": [], "QF": [], "SF": [], "F": []}) is None


def test_actual_bracket_sobre_esqueleto_y_champ():
    skel = bw._ko_skeleton(bw._cross_pairs(_full_bracket(list(range(8))), set(_ISO_T)))
    matches = [{"id": i, "stage": "LAST_32", "home_es": f"T{2 * i}", "away_es": f"T{2 * i + 1}",
                "home_score": 1, "away_score": 0, "home_penalties": None, "away_penalties": None,
                "status": "finalizado"} for i in range(16)]
    matches.append({"id": 99, "stage": "FINAL", "home_es": "T0", "away_es": "T2", "status": "finalizado",
                    "home_score": 2, "away_score": 1, "home_penalties": None, "away_penalties": None})
    ab = bw.build_actual_bracket(matches, skel, _ISO_T)
    assert len(ab["R32"]) == 32 and all(ab["R32"])          # 32 equipos reales colocados
    assert ab["CHAMP"] == [{"iso": "T00", "es": "T0"}]       # ganador de la final
    assert all(t is None for t in ab["R16"])                # octavos reales sin datos -> None
    assert bw.build_actual_bracket(matches, None, _ISO_T) == {}


def _ko_match(stage, h, a, hs, as_, hp=None, ap=None, status=None):
    if status is None:
        status = "finalizado" if hs is not None else "programado"
    return {"stage": stage, "home_es": h, "away_es": a, "home_score": hs,
            "away_score": as_, "home_penalties": hp, "away_penalties": ap, "status": status}


_ISO_RX = {"A": "AAA", "B": "BBB", "C": "CCC", "D": "DDD", "E": "EEE"}


def test_reached_by_round_en_vivo_por_ganadores():
    # 2 dieciseisavos: uno finalizado (A gana), otro sin jugar (C-D). E no clasificó.
    matches = [_ko_match("LAST_32", "A", "B", 2, 0),
               _ko_match("LAST_32", "C", "D", None, None)]
    r = bw.build_reached_by_round(matches, _ISO_RX)
    assert set(r["R32"]) == {"AAA", "BBB", "CCC", "DDD"}   # los 4 juegan 16avos
    assert r["R16"] == ["AAA"]                              # solo el ganador resuelto
    assert "QF" not in r and "E" not in r.get("R32", [])


def test_reached_en_curso_no_decide():
    # Partido EN JUEGO con marcador: NO debe marcar ganador ni eliminado (Alemania-Paraguay).
    matches = [_ko_match("LAST_32", "A", "B", 0, 1, status="en juego")]
    r = bw.build_reached_by_round(matches, _ISO_RX)
    assert set(r["R32"]) == {"AAA", "BBB"}     # ambos juegan 16avos (llegaron a R32)
    assert "R16" not in r                       # nadie ha pasado todavía
    assert bw.build_ko_eliminated(matches, _ISO_RX) == []  # nadie eliminado


def test_reached_by_round_champ_y_penaltis():
    matches = [_ko_match("FINAL", "A", "B", 1, 1, hp=4, ap=2)]   # empate -> penaltis A
    assert bw.build_reached_by_round(matches, _ISO_RX)["CHAMP"] == ["AAA"]


def test_ko_eliminated_perdedores():
    matches = [_ko_match("LAST_32", "A", "B", 2, 0),            # B fuera
               _ko_match("LAST_16", "A", "C", 1, 1, hp=2, ap=4),  # A fuera por penaltis
               _ko_match("LAST_32", "D", "E", None, None)]       # sin jugar -> nadie fuera
    assert bw.build_ko_eliminated(matches, _ISO_RX) == ["AAA", "BBB"]
