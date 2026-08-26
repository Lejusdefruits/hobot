from datetime import datetime, timedelta

import requests

import tools.link_check as link_check
from core.db import get_connection
from tests.conftest import insert_offer


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


def _mock_head(status_code):
    def _head(url, **kwargs):
        return _FakeResponse(status_code)
    return _head


def test_404_marks_dead(monkeypatch):
    monkeypatch.setattr(requests, "head", _mock_head(404))
    assert link_check._is_dead("https://example.com/gone") is True


def test_2xx_is_alive(monkeypatch):
    monkeypatch.setattr(requests, "head", _mock_head(200))
    assert link_check._is_dead("https://example.com/alive") is False


def test_3xx_redirect_is_alive(monkeypatch):
    monkeypatch.setattr(requests, "head", _mock_head(301))
    assert link_check._is_dead("https://example.com/moved") is False


def test_403_is_ambiguous_not_dead(monkeypatch):
    """A 403 is often an anti-bot block on a perfectly live listing --
    must never be treated as proof the offer is gone."""
    monkeypatch.setattr(requests, "head", _mock_head(403))
    assert link_check._is_dead("https://example.com/blocked") is None


def test_timeout_is_ambiguous_not_dead(monkeypatch):
    def _raise(url, **kwargs):
        raise requests.Timeout("timed out")
    monkeypatch.setattr(requests, "head", _raise)
    assert link_check._is_dead("https://example.com/slow") is None


def test_405_retries_with_get(monkeypatch):
    def _head(url, **kwargs):
        return _FakeResponse(405)
    def _get(url, **kwargs):
        return _FakeResponse(404)
    monkeypatch.setattr(requests, "head", _head)
    monkeypatch.setattr(requests, "get", _get)
    assert link_check._is_dead("https://example.com/head-not-allowed") is True


def test_sweep_marks_confirmed_dead_offer_as_expired(db, monkeypatch):
    monkeypatch.setattr(requests, "head", _mock_head(404))
    stale = (datetime.now() - timedelta(days=30)).isoformat(sep=" ", timespec="seconds")
    with get_connection() as conn:
        offer_id = insert_offer(conn, status="scored", score=80, url_hash="h1",
                                 url="https://example.com/dead", last_seen_at=stale)

    expired = link_check.sweep_dead_links()

    assert [o["id"] for o in expired] == [offer_id]
    with get_connection() as conn:
        row = conn.execute("SELECT status FROM offers WHERE id = ?", (offer_id,)).fetchone()
    assert row["status"] == "expired"


def test_sweep_leaves_alive_offer_status_untouched_and_refreshes_last_seen(db, monkeypatch):
    monkeypatch.setattr(requests, "head", _mock_head(200))
    stale = (datetime.now() - timedelta(days=30)).isoformat(sep=" ", timespec="seconds")
    with get_connection() as conn:
        offer_id = insert_offer(conn, status="scored", score=80, url_hash="h2",
                                 url="https://example.com/alive", last_seen_at=stale)

    expired = link_check.sweep_dead_links()

    assert expired == []
    with get_connection() as conn:
        row = conn.execute("SELECT status, last_seen_at FROM offers WHERE id = ?", (offer_id,)).fetchone()
    assert row["status"] == "scored"
    assert row["last_seen_at"] > stale


def test_sweep_skips_offers_not_yet_stale(db, monkeypatch):
    monkeypatch.setattr(requests, "head", _mock_head(404))
    with get_connection() as conn:
        insert_offer(conn, status="scored", score=80, url_hash="h3",
                      url="https://example.com/fresh")  # last_seen_at defaults to now

    expired = link_check.sweep_dead_links()

    assert expired == []


def test_sweep_ignores_applied_and_excluded_offers(db, monkeypatch):
    monkeypatch.setattr(requests, "head", _mock_head(404))
    stale = (datetime.now() - timedelta(days=30)).isoformat(sep=" ", timespec="seconds")
    with get_connection() as conn:
        insert_offer(conn, status="applied", score=90, url_hash="h4",
                      url="https://example.com/applied", last_seen_at=stale)
        insert_offer(conn, status="excluded", score=10, url_hash="h5",
                      url="https://example.com/excluded", last_seen_at=stale)

    expired = link_check.sweep_dead_links()

    assert expired == []
