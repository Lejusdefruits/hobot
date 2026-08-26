import graphs.discovery_graph as discovery_graph
from core.db import get_connection
from tests.conftest import insert_offer


def _score_one(monkeypatch, db, response, *, machine_busy=False, respect_load_gate=True):
    monkeypatch.setattr(discovery_graph.hardware, "is_machine_busy", lambda threshold=None: machine_busy)
    monkeypatch.setattr(discovery_graph, "chat_json", lambda prompt: response)
    with get_connection() as conn:
        offer_id = insert_offer(conn, url_hash="score-1", score=None, status="new")
    result = discovery_graph.score_node({}, respect_load_gate=respect_load_gate)
    with get_connection() as conn:
        row = conn.execute("SELECT score, score_reason, status FROM offers WHERE id = ?", (offer_id,)).fetchone()
    return result, row


def test_score_node_persists_score_and_reason(monkeypatch, db):
    _, row = _score_one(monkeypatch, db, {"score": 85, "reason": "Strong match on stack and location."})
    assert row["score"] == 85
    assert row["status"] == "scored"
    assert "match" in row["score_reason"]


def test_score_node_keeps_offer_new_when_llm_reply_has_no_score_field(monkeypatch, db):
    """A syntactically valid JSON reply missing the 'score' key (seen with
    weaker/local models) must not be marked 'scored' -- that status means
    score IS NOT NULL everywhere else it's read."""
    _, row = _score_one(monkeypatch, db, {"reason": "malformed reply, no score"})
    assert row["score"] is None
    assert row["status"] == "new"


def test_score_node_keeps_offer_new_on_llm_call_failure(monkeypatch, db):
    monkeypatch.setattr(discovery_graph.hardware, "is_machine_busy", lambda threshold=None: False)

    def _raise(prompt):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(discovery_graph, "chat_json", _raise)

    with get_connection() as conn:
        offer_id = insert_offer(conn, url_hash="score-err", score=None, status="new")
    discovery_graph.score_node({}, respect_load_gate=True)

    with get_connection() as conn:
        row = conn.execute("SELECT score, status, score_reason FROM offers WHERE id = ?", (offer_id,)).fetchone()
    assert row["score"] is None
    assert row["status"] == "new"
    assert "scoring failed" in row["score_reason"]


def test_score_node_defers_on_high_load_for_scheduled_runs(monkeypatch, db):
    """The scheduled path (respect_load_gate=True, the default) must skip
    scoring entirely under high local CPU load rather than compete for it --
    but only for a local Ollama provider, see _should_defer_scoring."""
    monkeypatch.setattr(discovery_graph, "DEFER_ON_HIGH_LOAD", True)
    monkeypatch.setattr(discovery_graph, "LLM_PROVIDER", "ollama")
    called = {"chat_json": False}
    monkeypatch.setattr(discovery_graph.hardware, "is_machine_busy", lambda threshold=None: True)
    monkeypatch.setattr(discovery_graph, "chat_json", lambda prompt: called.__setitem__("chat_json", True) or {})

    with get_connection() as conn:
        offer_id = insert_offer(conn, url_hash="score-deferred", score=None, status="new")
    result = discovery_graph.score_node({}, respect_load_gate=True)

    assert result == {"scored_offers": []}
    assert called["chat_json"] is False
    with get_connection() as conn:
        row = conn.execute("SELECT score, status FROM offers WHERE id = ?", (offer_id,)).fetchone()
    assert row["status"] == "new"
    assert row["score"] is None


def test_score_node_on_demand_ignores_the_load_gate(monkeypatch, db):
    """run_scoring_now() (a direct chat/TUI request) calls with
    respect_load_gate=False -- must score immediately regardless of load,
    refusing it would be surprising rather than helpful."""
    result, row = _score_one(
        monkeypatch, db, {"score": 70, "reason": "ok"},
        machine_busy=True, respect_load_gate=False,
    )
    assert row["status"] == "scored"
    assert row["score"] == 70
