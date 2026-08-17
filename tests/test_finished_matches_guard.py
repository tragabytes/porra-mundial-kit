"""Guard de fetch_finished_matches.

Descarta (para que el próximo cron lo reintente con datos completos):
  - FINISHED sin marcador (glitch del 11/06).
  - Eliminatoria empatada en el campo sin la tanda de penaltis poblada
    (mismo glitch un nivel más profundo; sin penaltis el Excel no sabe quién pasa).
Acepta lo legítimo: marcador normal, prórroga decisiva (2-1 AET), penaltis
completos, y los empates REGULAR de fase de grupos.
"""
import lib_football_api as fa


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _match(mid, hs, as_, duration="REGULAR", ph=None, pa=None, status="FINISHED"):
    return {
        "id": mid,
        "homeTeam": {"name": "A"},
        "awayTeam": {"name": "B"},
        "score": {
            "duration": duration,
            "fullTime": {"home": hs, "away": as_},
            "penalties": {"home": ph, "away": pa},
        },
        "status": status,
        "utcDate": "2026-06-28T18:00:00Z",
        "stage": "LAST_32",
        "group": None,
        "matchday": None,
    }


def _run(monkeypatch, matches):
    monkeypatch.setenv("FOOTBALL_API_KEY", "x")
    monkeypatch.setattr(fa._SESSION, "get",
                        lambda *a, **k: _FakeResp({"matches": matches}))
    return fa.fetch_finished_matches()


def test_marcador_normal_se_acepta(monkeypatch):
    out = _run(monkeypatch, [_match(1, 2, 1)])
    assert [m["id"] for m in out] == [1]


def test_fulltime_null_se_descarta(monkeypatch):
    out = _run(monkeypatch, [_match(1, None, None)])
    assert out == []


def test_penaltis_completos_se_aceptan(monkeypatch):
    out = _run(monkeypatch, [_match(1, 1, 1, "PENALTY_SHOOTOUT", 4, 2)])
    assert [m["id"] for m in out] == [1]
    assert out[0]["home_penalties"] == 4 and out[0]["away_penalties"] == 2
    assert out[0]["duration"] == "PENALTY_SHOOTOUT"


def test_penaltis_null_se_descarta(monkeypatch):
    # FINISHED, empate de 120', tanda aún sin poblar -> se deja para el reintento.
    out = _run(monkeypatch, [_match(1, 1, 1, "PENALTY_SHOOTOUT", None, None)])
    assert out == []


def test_prorroga_decisiva_se_acepta(monkeypatch):
    # KO resuelto en la prórroga (2-1 AET), sin penaltis: debe procesarse.
    out = _run(monkeypatch, [_match(1, 2, 1, "EXTRA_TIME")])
    assert [m["id"] for m in out] == [1]


def test_empate_regular_de_grupos_se_acepta(monkeypatch):
    # Empate de fase de grupos (REGULAR) NO debe descartarse.
    out = _run(monkeypatch, [_match(1, 1, 1, "REGULAR")])
    assert [m["id"] for m in out] == [1]
