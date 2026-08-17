"""Cliente HTTP para enviar mensajes al bot WhatsApp que corre en el VPS.

El bot expone `POST /publish` con `Authorization: Bearer <TOKEN>` y reenvía
el contenido al grupo de WhatsApp configurado.

Env vars esperadas:
- VPS_URL              # https://<ip-o-dominio>:8443/publish
- VPS_WEBHOOK_TOKEN    # token Bearer
- WHATSAPP_GROUP_ID    # ID del grupo (opcional, override por param)

Modo DRY_RUN=1: solo imprime lo que se habría enviado, no llama al VPS.

Usage:
  from lib_whatsapp_client import publish
  publish("¡Gol de España!", image_base64=b64png)

  python scripts/lib_whatsapp_client.py            # smoke test (requiere env)
  DRY_RUN=1 python scripts/lib_whatsapp_client.py  # smoke test sin red
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

DEFAULT_TIMEOUT = 30

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _madrid_hour() -> int:
    """Hora actual (0-23) en Europe/Madrid."""
    return datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/Madrid")).hour


def _archive_sent_message(text: str, group_id: str, has_image: bool) -> None:
    """Archiva un broadcast enviado en pools/<POOL_ID>/sent_messages.jsonl.

    Para que la auditoría semanal pueda revisar 'qué se le dijo a la gente'. No
    bloqueante: si algo falla, log y seguimos (nunca rompe un envío real). Solo
    se llama tras un publish con éxito (no en DRY_RUN). El workflow del cron
    commitea este fichero junto a state.json/ADMIN.
    """
    pool_id = os.environ.get("POOL_ID")
    if not pool_id:
        return
    try:
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "pool": pool_id,
            "group_id": group_id,
            "chars": len(text),
            "image": bool(has_image),
            "text": text,
        }
        path = PROJECT_ROOT / "pools" / pool_id / "sent_messages.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"WARN: no pude archivar el mensaje enviado: {e}", file=sys.stderr)


def publish(
    text: str,
    image_base64: str | None = None,
    group_id: str | None = None,
    vps_url: str | None = None,
    token: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    force: bool = False,
) -> dict:
    """Envía un mensaje (con imagen opcional) al bot WhatsApp.

    Lanza requests.HTTPError si el VPS responde 4xx/5xx.
    Lanza RuntimeError si faltan credenciales.

    Silencio nocturno: de 00:00 a 07:00 hora de Madrid NO se envían mensajes
    automáticos (los partidos de madrugada se resumen a las 07:00; ver
    _flush_night_digest en ingest_match_results.py). force=True salta el silencio
    (lo usa el propio resumen). Las respuestas a comandos las manda el bot del VPS
    y no pasan por aquí, así que no se ven afectadas.
    """
    if os.environ.get("DRY_RUN") == "1":
        print("[DRY_RUN] WhatsApp publish:")
        print(f"  text: {text[:200]}{'...' if len(text) > 200 else ''}")
        if image_base64:
            print(f"  image_base64: <{len(image_base64)} chars>")
        if group_id:
            print(f"  group_id: {group_id}")
        return {"status": "dry_run"}

    h = _madrid_hour()
    if not force and 0 <= h < 7:
        print(f"[silencio nocturno] mensaje no enviado (hora Madrid {h}h): "
              f"{text[:80]}", file=sys.stderr)
        return {"status": "quiet_hours"}

    vps_url = vps_url or os.environ.get("VPS_URL")
    token = token or os.environ.get("VPS_WEBHOOK_TOKEN")
    group_id = group_id or os.environ.get("WHATSAPP_GROUP_ID")

    missing = [n for n, v in [("VPS_URL", vps_url),
                              ("VPS_WEBHOOK_TOKEN", token),
                              ("WHATSAPP_GROUP_ID", group_id)] if not v]
    if missing:
        raise RuntimeError(
            f"Faltan env vars: {', '.join(missing)}. "
            "Configúralas o ejecuta con DRY_RUN=1."
        )

    payload = {"text": text, "group_id": group_id}
    if image_base64:
        payload["image_base64"] = image_base64

    resp = requests.post(
        vps_url,
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    resp.raise_for_status()
    _archive_sent_message(text, group_id, image_base64 is not None)
    return resp.json()


def sync_pool(
    pool_id: str,
    state: dict,
    players: list[dict] | None = None,
    vps_url: str | None = None,
    token: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """Empuja state.json y players.json de un pool al bot del VPS.

    El bot persiste en `vps/data/<pool_id>/` y los usa para responder a
    comandos del grupo (!ranking, !proximo, !miprediccion).

    Deriva la URL del endpoint /sync sustituyendo el path final de VPS_URL
    (que apunta a /publish). Idempotente: la siguiente llamada sobrescribe.

    DRY_RUN=1: solo imprime resumen.
    """
    if os.environ.get("DRY_RUN") == "1":
        n_announced = len(state.get("announced_match_ids", []))
        n_players = len(players) if players else 0
        print(f"[DRY_RUN] sync pool={pool_id} announced={n_announced} players={n_players}")
        return {"status": "dry_run"}

    vps_url = vps_url or os.environ.get("VPS_URL")
    token = token or os.environ.get("VPS_WEBHOOK_TOKEN")
    missing = [n for n, v in [("VPS_URL", vps_url),
                              ("VPS_WEBHOOK_TOKEN", token)] if not v]
    if missing:
        raise RuntimeError(f"Faltan env vars: {', '.join(missing)}.")

    sync_url = vps_url.rsplit("/", 1)[0] + "/sync"
    payload: dict = {"pool_id": pool_id, "state": state}
    if players is not None:
        payload["players"] = players

    resp = requests.post(
        sync_url,
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def web_url_for_pool(pool_id: str) -> str | None:
    """URL pública del panel web de una porra (lee data/web_urls.json: pool->URL).

    Devuelve None si no hay entrada o el fichero falta/está corrupto (los mensajes
    que la usan la omiten sin romperse). El slug que va en la URL debe coincidir
    con WEB_SLUGS del .env del VPS (que mapea slug->pool para servir el panel).
    """
    try:
        urls = json.loads((PROJECT_ROOT / "data" / "web_urls.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    url = urls.get(pool_id)
    return url if isinstance(url, str) and url else None


def sync_web_data(
    pool_id: str,
    web_data: dict,
    vps_url: str | None = None,
    token: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """Sube el web_data.json al VPS para que GET /web/<slug>/data.json lo sirva.

    DRY_RUN=1: solo imprime resumen.
    """
    if os.environ.get("DRY_RUN") == "1":
        n = len(web_data.get("matches", []))
        print(f"[DRY_RUN] sync_web_data pool={pool_id} matches={n}")
        return {"status": "dry_run"}

    vps_url = vps_url or os.environ.get("VPS_URL")
    token = token or os.environ.get("VPS_WEBHOOK_TOKEN")
    missing = [n for n, v in [("VPS_URL", vps_url), ("VPS_WEBHOOK_TOKEN", token)] if not v]
    if missing:
        raise RuntimeError(f"Faltan env vars: {', '.join(missing)}.")

    web_data_url = vps_url.rsplit("/", 1)[0] + "/web-data"
    resp = requests.post(
        web_data_url,
        json={"pool_id": pool_id, "data": web_data},
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def upload_web_file(
    pool_id: str,
    name: str,
    file_path,
    vps_url: str | None = None,
    token: str | None = None,
    timeout: int = 60,
) -> dict:
    """Sube un fichero descargable al VPS (POST /web-file, cuerpo raw octet-stream).

    `name` debe ser uno de: admin.xlsx, audit.json, audit.md,
    predictions/<kebab>.xlsx. El VPS lo guarda en data/<pool>/download/<name> y lo
    sirve en GET /web/<slug>/download/<name>. Cuerpo raw (no base64) para no chocar
    con el límite de 1 MB de express.json del bot.

    DRY_RUN=1: solo imprime resumen.
    """
    p = Path(file_path)
    if os.environ.get("DRY_RUN") == "1":
        size = p.stat().st_size if p.exists() else 0
        print(f"[DRY_RUN] upload_web_file pool={pool_id} name={name} bytes={size}")
        return {"status": "dry_run"}

    vps_url = vps_url or os.environ.get("VPS_URL")
    token = token or os.environ.get("VPS_WEBHOOK_TOKEN")
    missing = [n for n, v in [("VPS_URL", vps_url), ("VPS_WEBHOOK_TOKEN", token)] if not v]
    if missing:
        raise RuntimeError(f"Faltan env vars: {', '.join(missing)}.")

    url = vps_url.rsplit("/", 1)[0] + "/web-file"
    resp = requests.post(
        url,
        params={"pool_id": pool_id, "name": name},
        data=p.read_bytes(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/octet-stream"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


if __name__ == "__main__":
    # Forzar UTF-8 en stdout para que los emojis del payload no rompan la consola Windows
    sys.stdout.reconfigure(encoding="utf-8")
    result = publish(
        text="Smoke test desde lib_whatsapp_client.py — si lees esto, va bien.",
        image_base64=None,
    )
    print("OK:", result)
