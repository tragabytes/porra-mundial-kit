"""Envía un cartel PNG a los grupos de WhatsApp de las porras (envío puntual).

Reutiliza `publish()` de `lib_whatsapp_client` — el mismo `POST /publish` que
usa el cron para mandar el leaderboard tras cada partido. No depende de ningún
partido ni del Excel: solo coge un PNG y lo manda.

El `group_id` de cada porra se resuelve desde `WHATSAPP_GROUP_ID_<POOL>` (los
mismos secrets que el cron). Se manda el mismo PNG a cada grupo con una pausa
entre envíos para no encadenar dos imágenes al mismo número en <5s (riesgo de
baneo de la SIM del bot, igual que el `sleep 30` del cron).

Respeta `DRY_RUN=1` (no llama al VPS, solo imprime) vía `publish()`.

Uso:
  DRY_RUN=1 python scripts/send_poster.py            # prueba en seco (todas)
  python scripts/send_poster.py                      # envío real (todas)
  ONLY_POOL=familia python scripts/send_poster.py    # solo una porra
  python scripts/send_poster.py ruta/al/cartel.png   # otro PNG
  POSTER_CAPTION="..." python scripts/send_poster.py # otro pie de foto

Env vars (las lee `publish()` salvo los group ids, que resuelve este script):
  VPS_URL, VPS_WEBHOOK_TOKEN, WHATSAPP_GROUP_ID_FAMILIA, WHATSAPP_GROUP_ID_AMIGOS
"""

from __future__ import annotations

import base64
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib_whatsapp_client import publish

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PNG = PROJECT_ROOT / "design" / "cartel_recluta.png"

# Porras activas -> env var con su group_id (mismos secrets que el cron).
POOLS = [
    ("familia", "WHATSAPP_GROUP_ID_FAMILIA"),
    ("amigos", "WHATSAPP_GROUP_ID_AMIGOS"),
]

# Pausa entre envíos (anti-spam SIM). El cron usa 30s entre jobs.
SLEEP_BETWEEN_S = 25

DEFAULT_CAPTION = (
    "📣 ¡Se abre la PORRA DEL MUNDIAL 2026!\n\n"
    "El Tío Sam te quiere a ti — no hace falta saber de fútbol, y es GRATIS.\n\n"
    "👉 Apúntate antes del 10 de junio: pídeme el Excel y rellena tu quiniela.\n\n"
    "Ranking automático al grupo tras cada partido. ¡Que no te quedes fuera!"
)


def main() -> int:
    # PNG opcional como primer argumento posicional (ignora flags tipo -x).
    positional = [a for a in sys.argv[1:] if not a.startswith("-")]
    png_path = Path(positional[0]) if positional else DEFAULT_PNG
    if not png_path.exists():
        print(f"ERROR: no existe el PNG {png_path}")
        return 1

    caption = os.environ.get("POSTER_CAPTION", DEFAULT_CAPTION)
    only = os.environ.get("ONLY_POOL", "all").strip().lower() or "all"
    dry = os.environ.get("DRY_RUN") == "1"

    targets = [(p, e) for (p, e) in POOLS if only in ("all", p)]
    if not targets:
        pools = [p for p, _ in POOLS]
        print(f"ERROR: ONLY_POOL='{only}' no coincide con ninguna porra {pools}")
        return 1

    image_b64 = base64.b64encode(png_path.read_bytes()).decode("ascii")
    print(f"Cartel: {png_path} ({len(image_b64)} chars base64)")
    print(f"Porras objetivo: {[p for p, _ in targets]} | DRY_RUN={'1' if dry else ''}")

    sent = 0
    for i, (pool, env_name) in enumerate(targets):
        group_id = os.environ.get(env_name)
        if not group_id:
            print(f"[skip] {pool}: falta env var {env_name}")
            continue
        print(f"[send] {pool} -> {group_id}")
        publish(caption, image_base64=image_b64, group_id=group_id)
        sent += 1
        # Pausa solo entre envíos reales (en dry-run no hay nada que espaciar).
        if not dry and i < len(targets) - 1:
            time.sleep(SLEEP_BETWEEN_S)

    print(f"Hecho. Envíos procesados: {sent}/{len(targets)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
