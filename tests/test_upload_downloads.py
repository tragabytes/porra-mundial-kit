"""Tests del kebab de upload_downloads.py.

Invariante crítica: kebab() DEBE producir exactamente el mismo slug que el
slug() de web/panel.html (línea 777), o las predicciones se subirían a un nombre
que el panel no pide → enlace 404. Aquí fijamos casos representativos (acentos,
espacios, inicial con punto, ñ).
"""
from upload_downloads import kebab


def test_kebab_casos_basicos():
    assert kebab("Rober") == "rober"
    assert kebab("Nico") == "nico"
    assert kebab("Eva M.") == "eva-m"        # espacio + punto → un solo guion, trim final
    assert kebab("José María") == "jose-maria"  # acentos fuera
    assert kebab("Iñaki") == "inaki"          # ñ → n
    assert kebab("  Pedro  ") == "pedro"      # espacios extremos → trim
    assert kebab("J. R.") == "j-r"
    assert kebab("") == ""
