"""Envía la imagen diaria de la clasificación al grupo WhatsApp de la porra.

Pensado para el cron de las 09:00 UTC (11:00 Madrid en horario de verano):
renderiza el leaderboard en modo "edición diaria" (sin partido) desde el
ADMIN.xlsx del repo —ya recalculado por el cron de resultados— y lo publica
con una caption mínima, sin comentario de IA. No escribe nada (ni ADMIN ni
state), así que no necesita LibreOffice ni step de commit.

Env vars: POOL_ID (obligatoria), WHATSAPP_GROUP_ID, VPS_URL, VPS_WEBHOOK_TOKEN.
DRY_RUN=1 imprime en lugar de publicar (lo gestiona lib_whatsapp_client).
"""

from __future__ import annotations

import base64
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
from lib_screenshot import render_leaderboard, WC_TOTAL_MATCHES
from lib_whatsapp_client import publish, web_url_for_pool

PROJECT_ROOT = Path(__file__).resolve().parent.parent

POOL_ID = os.environ.get("POOL_ID")
if not POOL_ID:
    print("ERROR: falta env var POOL_ID (familia|amigos|...).", file=sys.stderr)
    sys.exit(2)

POOL_DIR = PROJECT_ROOT / "pools" / POOL_ID
ADMIN_PATH = POOL_DIR / "ADMIN.xlsx"
STATE_PATH = POOL_DIR / "state.json"

MADRID = ZoneInfo("Europe/Madrid")


def _format_next_match(nm: dict | None) -> str | None:
    """'Canadá vs Bosnia y Herzegovina · 12/06 21:00' (hora Madrid) o None."""
    if not nm or not nm.get("home_es"):
        return None
    try:
        utc = datetime.fromisoformat(nm["utc_kickoff"].replace("Z", "+00:00"))
        when = utc.astimezone(MADRID).strftime("%d/%m %H:%M")
    except Exception:
        when = ""
    label = f"{nm['home_es']} vs {nm['away_es']}"
    return f"{label} · {when}" if when else label


def main() -> int:
    if not STATE_PATH.exists():
        print(f"[{POOL_ID}] ERROR: no existe {STATE_PATH} (porra sin inicializar)",
              file=sys.stderr)
        return 1
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    today = datetime.now(MADRID)
    png = render_leaderboard(
        ADMIN_PATH,
        titular="ASÍ VA LA PORRA",
        fase_label=f"Clasificación general · {today.strftime('%d/%m/%Y')}",
        next_match=_format_next_match(state.get("next_match")),
        matches_remaining=max(WC_TOTAL_MATCHES - len(state.get("announced_match_ids", [])), 0),
    )
    caption = f"Clasificación · {today.strftime('%d/%m')}"
    web_url = web_url_for_pool(POOL_ID)
    if web_url:
        caption += f"\n\n🌐 {web_url}"
    publish(caption, image_base64=base64.b64encode(png).decode("ascii"))
    print(f"[{POOL_ID}] clasificación diaria enviada")
    return 0


if __name__ == "__main__":
    sys.exit(main())
