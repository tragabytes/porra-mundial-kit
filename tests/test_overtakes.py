"""compute_overtakes: conservador (solo nuevo líder o reordenación del top 5)."""
import ingest_match_results as ing


def _rank(rows):
    """rows = [(nombre, puntos)] en orden de clasificación -> formato read_ranking."""
    return [{"name": n, "points": p, "position": i}
            for i, (n, p) in enumerate(rows, start=1)]


def test_empty_returns_none():
    assert ing.compute_overtakes([], []) is None


def test_no_position_change_returns_none():
    before = _rank([("A", 5), ("B", 3), ("C", 1)])
    after = _rank([("A", 6), ("B", 3), ("C", 1)])  # A suma, mismo orden
    assert ing.compute_overtakes(before, after) is None


def test_new_leader():
    before = _rank([("A", 5), ("B", 3)])
    after = _rank([("B", 8), ("A", 5)])
    out = ing.compute_overtakes(before, after)
    assert out is not None
    assert "Nuevo líder: B" in out
    assert "adelanta a A" in out


def test_top_overtake_without_leader_change():
    before = _rank([("A", 10), ("B", 3), ("C", 2)])
    after = _rank([("A", 10), ("C", 6), ("B", 3)])  # C adelanta a B (3º->2º)
    out = ing.compute_overtakes(before, after)
    assert out is not None
    assert "C adelanta a B" in out
    assert "Nuevo líder" not in out
