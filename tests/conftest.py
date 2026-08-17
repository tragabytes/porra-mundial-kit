"""Config común de los tests.

El orquestador (`scripts/ingest_match_results.py`) vive en scripts/ y hace
`sys.exit` si falta POOL_ID al importarse. Preparamos ambas cosas ANTES de que
los tests lo importen.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("POOL_ID", "familia")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
