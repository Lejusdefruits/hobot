from datetime import datetime, timedelta

from core.circuit_breaker import compute_backoff_until, is_backed_off
from core.db import get_connection


def _log_run(conn, source, backoff_until=None):
    conn.execute(
        "INSERT INTO run_log (run_type, source, backoff_until) VALUES ('discovery', ?, ?)",
        (source, backoff_until),
    )


def test_not_backed_off_with_no_history(db):
    backed_off, until = is_backed_off("jobspy")
    assert backed_off is False
    assert until is None


def test_not_backed_off_after_a_success(db):
    with get_connection() as conn:
        _log_run(conn, "jobspy", backoff_until=None)
    backed_off, _ = is_backed_off("jobspy")
    assert backed_off is False


def test_backed_off_while_backoff_until_is_in_the_future(db):
    future = (datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds")
    with get_connection() as conn:
        _log_run(conn, "jobspy", backoff_until=future)
    backed_off, until = is_backed_off("jobspy")
    assert backed_off is True
    assert until == future


def test_not_backed_off_once_backoff_until_is_in_the_past(db):
    past = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    with get_connection() as conn:
        _log_run(conn, "jobspy", backoff_until=past)
    backed_off, _ = is_backed_off("jobspy")
    assert backed_off is False


def test_compute_backoff_until_returns_none_on_success():
    assert compute_backoff_until("jobspy", has_error=False) is None


def test_compute_backoff_until_starts_at_base_hours(db):
    until = compute_backoff_until("jobspy", has_error=True)
    dt = datetime.fromisoformat(until)
    delta = dt - datetime.now()
    assert timedelta(hours=1, minutes=55) < delta <= timedelta(hours=2, minutes=5)


def test_compute_backoff_until_doubles_on_consecutive_failures(db):
    future = (datetime.now() + timedelta(hours=2)).isoformat(timespec="seconds")
    with get_connection() as conn:
        _log_run(conn, "jobspy", backoff_until=future)
    until = compute_backoff_until("jobspy", has_error=True)
    dt = datetime.fromisoformat(until)
    delta = dt - datetime.now()
    # Second consecutive failure -> 4h, not 2h.
    assert timedelta(hours=3, minutes=55) < delta <= timedelta(hours=4, minutes=5)


def test_compute_backoff_until_caps_at_max_hours(db):
    with get_connection() as conn:
        # Ten prior failures in a row, each with its own backoff_until set,
        # is already well past the point 2h*2^n would exceed the 24h cap.
        for _ in range(10):
            _log_run(conn, "jobspy", backoff_until=(datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds"))
    until = compute_backoff_until("jobspy", has_error=True)
    dt = datetime.fromisoformat(until)
    delta = dt - datetime.now()
    assert timedelta(hours=23, minutes=55) < delta <= timedelta(hours=24, minutes=5)


def test_backoff_is_per_source(db):
    future = (datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds")
    with get_connection() as conn:
        _log_run(conn, "jobspy", backoff_until=future)
    backed_off, _ = is_backed_off("adzuna")
    assert backed_off is False
