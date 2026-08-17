"""Silencio nocturno (00:00-07:00 Madrid) + resumen de las 07:00.

- Guardia en publish(): de noche no envía (salvo force=True); DRY_RUN intacto.
- _flush_night_digest: envía una vez en [07:00,12:00), idempotente por fecha,
  descarta entradas que no sean de hoy, no envía de madrugada ni por la tarde.
- _format_night_digest: formato escueto, con penaltis y quién clavó.
"""
import pytest

import lib_whatsapp_client as wa
import ingest_match_results as ing
from datetime import datetime


# --- Guardia en publish() ---

def _clear_env(mp):
    for k in ("DRY_RUN", "VPS_URL", "VPS_WEBHOOK_TOKEN", "WHATSAPP_GROUP_ID"):
        mp.delenv(k, raising=False)


def test_publish_silencio_nocturno(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setattr(wa, "_madrid_hour", lambda: 2)
    assert wa.publish("hola")["status"] == "quiet_hours"


def test_publish_force_salta_silencio(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setattr(wa, "_madrid_hour", lambda: 2)
    # force salta el guardia -> llega a la comprobación de credenciales y falla
    # (RuntimeError), lo que prueba que NO devolvió quiet_hours.
    with pytest.raises(RuntimeError):
        wa.publish("hola", force=True)


def test_publish_de_dia_no_silencia(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setattr(wa, "_madrid_hour", lambda: 10)
    with pytest.raises(RuntimeError):
        wa.publish("hola")


def test_publish_dry_run_ignora_silencio(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setattr(wa, "_madrid_hour", lambda: 3)
    assert wa.publish("hola")["status"] == "dry_run"


# --- _flush_night_digest ---

def _now(y, m, d, h):
    return datetime(y, m, d, h, 0, 0)


def _entry(ts_date, clavaron=None, dur="REGULAR", hp=None, ap=None,
           home="México", away="Sudáfrica", hs=2, as_=0):
    return {"home_es": home, "away_es": away, "home_score": hs, "away_score": as_,
            "duration": dur, "home_penalties": hp, "away_penalties": ap,
            "clavaron": clavaron or [], "ts": f"{ts_date}T03:00:00+02:00"}


@pytest.fixture(autouse=True)
def _no_web_url(monkeypatch):
    monkeypatch.setattr(ing, "web_url_for_pool", lambda p: None)


def test_flush_madrugada_no_envia(monkeypatch):
    sent = []
    monkeypatch.setattr(ing, "publish", lambda *a, **k: sent.append(a) or {})
    monkeypatch.setattr(ing, "_madrid_now", lambda: _now(2026, 6, 29, 2))
    st = {"night_digest": [_entry("2026-06-29")], "night_digest_sent_date": None}
    ing._flush_night_digest(st)
    assert sent == [] and len(st["night_digest"]) == 1  # sigue acumulado


def test_flush_manana_envia_y_limpia(monkeypatch):
    sent = []
    monkeypatch.setattr(ing, "publish", lambda text, **k: sent.append((text, k)) or {})
    monkeypatch.setattr(ing, "_madrid_now", lambda: _now(2026, 6, 29, 7))
    st = {"night_digest": [_entry("2026-06-29", ["Teo", "Lolo"])],
          "night_digest_sent_date": None}
    ing._flush_night_digest(st)
    assert len(sent) == 1
    assert sent[0][1].get("force") is True
    assert "🌙 Resumen de noche" in sent[0][0] and "Teo" in sent[0][0]
    assert st["night_digest"] == [] and st["night_digest_sent_date"] == "2026-06-29"


def test_flush_idempotente_mismo_dia(monkeypatch):
    sent = []
    monkeypatch.setattr(ing, "publish", lambda *a, **k: sent.append(a) or {})
    monkeypatch.setattr(ing, "_madrid_now", lambda: _now(2026, 6, 29, 8))
    st = {"night_digest": [_entry("2026-06-29")], "night_digest_sent_date": "2026-06-29"}
    ing._flush_night_digest(st)
    assert sent == [] and st["night_digest"] == []  # ya enviado hoy; limpia restos


def test_flush_tarde_descarta_sin_enviar(monkeypatch):
    sent = []
    monkeypatch.setattr(ing, "publish", lambda *a, **k: sent.append(a) or {})
    monkeypatch.setattr(ing, "_madrid_now", lambda: _now(2026, 6, 29, 13))
    st = {"night_digest": [_entry("2026-06-29")], "night_digest_sent_date": None}
    ing._flush_night_digest(st)
    assert sent == [] and st["night_digest"] == [] and st["night_digest_sent_date"] == "2026-06-29"


def test_flush_descarta_entradas_de_ayer(monkeypatch):
    sent = []
    monkeypatch.setattr(ing, "publish", lambda *a, **k: sent.append(a) or {})
    monkeypatch.setattr(ing, "_madrid_now", lambda: _now(2026, 6, 29, 8))
    st = {"night_digest": [_entry("2026-06-28")], "night_digest_sent_date": None}
    ing._flush_night_digest(st)
    assert sent == []  # no hay entradas de hoy
    assert st["night_digest"] == [] and st["night_digest_sent_date"] == "2026-06-29"


def test_flush_vacio_no_hace_nada(monkeypatch):
    sent = []
    monkeypatch.setattr(ing, "publish", lambda *a, **k: sent.append(a) or {})
    monkeypatch.setattr(ing, "_madrid_now", lambda: _now(2026, 6, 29, 8))
    st = {"night_digest": [], "night_digest_sent_date": None}
    ing._flush_night_digest(st)
    assert sent == [] and st["night_digest_sent_date"] is None


# --- _format_night_digest ---

def test_format_basico(monkeypatch):
    monkeypatch.setattr(ing, "_madrid_now", lambda: _now(2026, 6, 29, 7))
    txt = ing._format_night_digest([
        _entry("2026-06-29", ["Teo"], home="México", away="Sudáfrica", hs=2, as_=0),
        _entry("2026-06-29", [], home="Corea del Sur", away="R. Checa", hs=2, as_=1),
    ])
    assert "México 2-0 Sudáfrica" in txt
    assert "🎯 Lo clavó: Teo" in txt
    assert "Corea del Sur 2-1 R. Checa" in txt
    assert "🎯 Nadie clavó" in txt


def test_format_penaltis(monkeypatch):
    monkeypatch.setattr(ing, "_madrid_now", lambda: _now(2026, 6, 29, 7))
    txt = ing._format_night_digest([
        _entry("2026-06-29", ["Iván"], dur="PENALTY_SHOOTOUT", hp=4, ap=2,
               home="Brasil", away="Francia", hs=1, as_=1),
    ])
    assert "Brasil 1-1 Francia" in txt
    assert "pens 4-2" in txt and "pasó Brasil" in txt
    assert "🎯 Lo clavó: Iván" in txt
