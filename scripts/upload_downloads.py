"""Sube al VPS los ficheros descargables del panel (Fase 3b): el Excel de
predicciones de cada jugador y el informe de auditoría más reciente.

Las predicciones (predictions/<pool>/) y los informes (audit/reports/) están
gitignored/locales, así que NO los sincroniza el cron: este script los empuja a
mano desde la máquina del organizador. El ADMIN.xlsx lo sube el cron en cada
partido (build_web_data --sync); aquí --admin sirve para el sembrado inicial.

Requiere VPS_URL y VPS_WEBHOOK_TOKEN en el entorno (igual que el cron).

Uso:
  python scripts/upload_downloads.py --pool familia
  python scripts/upload_downloads.py --pool familia --admin   # + ADMIN inicial
  DRY_RUN=1 python scripts/upload_downloads.py --pool familia  # sin red
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_whatsapp_client import upload_web_file

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def kebab(name: str) -> str:
    """Replica el slug() de web/panel.html: minúsculas, sin acentos, no-alfanum→'-'."""
    s = unicodedata.normalize("NFD", (name or "").lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def upload_predictions(pool: str) -> None:
    players_path = PROJECT_ROOT / "pools" / pool / "players.json"
    pred_dir = PROJECT_ROOT / "predictions" / pool
    if not players_path.exists():
        print(f"[{pool}] WARN: no existe {players_path}", file=sys.stderr)
        return
    players = json.loads(players_path.read_text(encoding="utf-8"))
    for p in players:
        slot, name = p.get("slot"), p.get("name")
        if not slot or not name:
            continue
        matches = sorted(pred_dir.glob(f"{int(slot):02d}_*.xlsx"))
        if not matches:
            print(f"[{pool}]   sin Excel para slot {slot} ({name}) — se omite")
            continue
        target = f"predictions/{kebab(name)}.xlsx"
        try:
            upload_web_file(pool, target, matches[0])
            print(f"[{pool}]   subido {matches[0].name} -> {target}")
        except Exception as e:
            print(f"[{pool}]   WARN subiendo {name}: {e}", file=sys.stderr)


def upload_audit(pool: str) -> None:
    reports = PROJECT_ROOT / "audit" / "reports"
    jsons = sorted(reports.glob(f"audit_{pool}_*.json"))
    if not jsons:
        print(f"[{pool}]   sin informe de auditoría (.json) — se omite")
        return
    latest = jsons[-1]
    for f, name in [(latest, "audit.json"), (latest.with_suffix(".md"), "audit.md")]:
        if not f.exists():
            print(f"[{pool}]   falta {f.name} — se omite {name}")
            continue
        try:
            upload_web_file(pool, name, f)
            print(f"[{pool}]   subido {f.name} -> {name}")
        except Exception as e:
            print(f"[{pool}]   WARN subiendo {name}: {e}", file=sys.stderr)


def upload_admin(pool: str) -> None:
    admin = PROJECT_ROOT / "pools" / pool / "ADMIN.xlsx"
    if not admin.exists():
        print(f"[{pool}]   no existe {admin} — se omite ADMIN", file=sys.stderr)
        return
    try:
        upload_web_file(pool, "admin.xlsx", admin)
        print(f"[{pool}]   subido ADMIN.xlsx")
    except Exception as e:
        print(f"[{pool}]   WARN subiendo ADMIN: {e}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pool", required=True, help="familia | amigos | ...")
    ap.add_argument("--admin", action="store_true",
                    help="subir también el ADMIN (sembrado inicial; el cron lo refresca luego)")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    print(f"[{args.pool}] subiendo descargas al VPS...")
    upload_predictions(args.pool)
    upload_audit(args.pool)
    if args.admin:
        upload_admin(args.pool)
    print(f"[{args.pool}] hecho.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
