"""Configura el baremo de puntos en la hoja ADMIN de un Excel matejero.

Las celdas de puntos (ADMIN!D8:D47) están desbloqueadas (locked=False) en la
plantilla matejero, así que se escriben directamente SIN desproteger la hoja
(no se toca ws.protection: la hoja queda protegida igual que estaba). Tras
escribir, recalcula con LibreOffice para que CLAS/Stats reflejen los puntos.

Uso:
  python scripts/set_scoring.py --admin pools/familia/ADMIN.xlsx
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib_excel import load, save, recalc

# Baremo aprobado (06/06): fila de la columna D del ADMIN -> puntos.
# Las etiquetas de cada regla viven en la columna C de la propia hoja.
SCORING: dict[int, int] = {
    # --- Fase de grupos (por partido) ---
    8: 1,    # Signo 1X2
    9: 0,    # Diferencia/distancia de goles (desactivada)
    10: 3,   # Resultado exacto
    11: 2,   # Posición exacta 1º
    12: 2,   # Posición exacta 2º
    13: 1,   # Posición exacta 3º
    14: 1,   # Posición exacta 4º
    15: 1,   # Equipo clasificado a dieciseisavos
    # --- Dieciseisavos ---
    16: 2,   # Signo
    17: 0,   # Diferencia
    18: 4,   # Exacto
    19: 2,   # Clasificado a octavos
    # --- Octavos ---
    20: 2,
    21: 0,
    22: 4,
    23: 3,   # Clasificado a cuartos
    # --- Cuartos ---
    24: 3,
    25: 0,
    26: 5,
    27: 4,   # Clasificado a semifinales
    # --- Semifinales ---
    28: 4,
    29: 0,
    30: 6,
    31: 4,   # Clasificado a 3º/4º puesto
    32: 6,   # Clasificado a la final
    # --- 3º y 4º puesto ---
    33: 3,
    34: 0,
    35: 5,
    # --- Final ---
    36: 5,
    37: 0,
    38: 8,
    # --- Cuadro de honor ---
    39: 15,  # Campeón
    40: 8,   # Subcampeón
    41: 5,   # 3º puesto
    42: 5,   # Bota de Oro
    43: 3,   # Bota de Plata
    44: 2,   # Bota de Bronce
    45: 5,   # Balón de Oro
    46: 3,   # Balón de Plata
    47: 2,   # Balón de Bronce
}

POINTS_COL = 4  # columna D


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin", type=Path, required=True,
                        help="Ruta al ADMIN.xlsx del pool")
    args = parser.parse_args()

    if not args.admin.exists():
        print(f"ERROR: no encuentro {args.admin}", file=sys.stderr)
        return 1

    print(f"Cargando ADMIN: {args.admin}")
    wb = load(args.admin)
    ws = wb["ADMIN"]

    # Escritura directa en las celdas de puntos (locked=False). NO se toca
    # ws.protection: la hoja permanece protegida exactamente como estaba.
    for row, pts in sorted(SCORING.items()):
        label = ws.cell(row=row, column=3).value  # col C: etiqueta de la regla
        ws.cell(row=row, column=POINTS_COL).value = pts
        print(f"  D{row} = {pts:>2}  ({str(label)[:55]})")

    print(f"Guardando ADMIN: {args.admin}")
    save(wb, args.admin)

    try:
        print("Recalculando con LibreOffice headless...")
        recalc(args.admin)
        print("OK. Baremo aplicado y recalculado.")
    except Exception as e:
        print(f"AVISO: baremo guardado, pero no se pudo recalcular ({e}). "
              f"Ábrelo en Excel y pulsa Ctrl+Alt+F9.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
