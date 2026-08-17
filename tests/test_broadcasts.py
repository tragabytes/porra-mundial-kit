"""La guía de TV (broadcast_2026.json) debe casar exactamente con el calendario
de fase de grupos y el cruce nombre→cadena debe ser robusto."""
import json
from pathlib import Path

import ingest_match_results as ing

ROOT = Path(__file__).resolve().parent.parent
BROADCASTS = json.loads((ROOT / "data" / "broadcast_2026.json").read_text(encoding="utf-8"))
ROWMAP = json.loads((ROOT / "data" / "match_row_map.json").read_text(encoding="utf-8"))


def test_broadcast_keys_match_rowmap():
    # Mismas 72 claves (local|visitante) que el mapa de filas del Excel.
    assert set(BROADCASTS["matches"]) == set(ROWMAP["matches"])


def test_broadcast_rtve_count_is_17():
    rtve = [k for k, v in BROADCASTS["matches"].items() if v.get("rtve")]
    assert len(rtve) == 17


def test_every_match_has_rtve_and_tv():
    for k, v in BROADCASTS["matches"].items():
        assert "rtve" in v and "tv" in v, k


def test_tv_for_both_orders_and_miss():
    rtve = ing._tv_for("Inglaterra", "Croacia")
    assert rtve and "La 1" in rtve
    # mismo resultado en orden invertido
    assert ing._tv_for("Croacia", "Inglaterra") == rtve
    assert ing._tv_for("Estados Unidos", "Paraguay") == "Solo en DAZN"
    # partido inexistente (p.ej. eliminatoria sin equipos aún) -> None
    assert ing._tv_for("Narnia", "Mordor") is None
