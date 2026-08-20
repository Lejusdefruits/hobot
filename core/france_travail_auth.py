"""Shared OAuth2 client_credentials client for the France Travail Connect APIs
(Offres d'emploi v2, La Bonne Boite v2) -- one identifier/secret pair, a
different scope per API.

One token per scope, cached in memory until shortly before it expires (France
Travail tokens last ~1499s) -- same idea as the single shared Ollama client in
core/llm.py, so a token never gets requested fresh on every call.

Deliberate rate limiting, not just "space things out a bit":
- Each caller (tools/sources_francetravail.py, sources_labonneboite.py)
  declares ITS OWN documented limit (calls/second, with a safety margin) and
  passes it to get(), instead of an internal table keyed by exact scope
  string -- fragile the moment an unknown scope shows up, since the limit
  would then silently never apply.
- A lock PER SCOPE (_lock_for), not a global one: waiting your turn on La
  Bonne Boite (2/s, the slowest) must never block a concurrent call to Offres
  d'emploi (10/s) -- that was a real bug in the first version.
- `time.sleep()` happens AFTER releasing the lock (the slot is reserved under
  the lock, the sleep happens outside it): two concurrent calls on the same
  scope land on distinct slots instead of risking the same one.
- A 429 (rate limit hit anyway) is NOT treated as an API outage: one short
  pause (Retry-After if given, otherwise 2s, capped at 10s so a discovery run
  never stalls for long) then a single retry -- the circuit breaker
  (core/circuit_breaker.py, 2h-24h backoff) stays reserved for real, persistent
  failures, not a transient rate-limit hiccup."""
import os
import threading
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

CLIENT_ID = os.environ.get("FRANCE_TRAVAIL_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("FRANCE_TRAVAIL_CLIENT_SECRET", "")
TOKEN_URL = "https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=/partenaire"

# Safety margin before real expiry (~1499s) so a nearly-expired token never
# gets handed to the API.
_EXPIRY_MARGIN_SECONDS = 60
# Default limit if a caller doesn't specify its own -- deliberately
# conservative, shouldn't ever actually be used in practice (every connector
# declares its own documented limit).
_DEFAULT_MAX_CALLS_PER_SECOND = 2

_locks_guard = threading.Lock()
_scope_locks: dict[str, threading.Lock] = {}
_tokens: dict[str, tuple[str, float]] = {}  # scope -> (access_token, expires_at)
_last_call_at: dict[str, float] = {}  # scope -> next allowed slot (monotonic)


def _lock_for(key: str) -> threading.Lock:
    """One lock per key (a scope, or "throttle:<scope>" for rate limiting --
    two separate lock namespaces on purpose, see the module docstring),
    created on demand. _locks_guard only protects the creation itself, it's
    never held during the actual work behind each lock."""
    with _locks_guard:
        lock = _scope_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _scope_locks[key] = lock
        return lock


def get_token(scope: str) -> str:
    """Valid token for this scope, re-fetched only if missing or close to
    expiring. Per-scope lock: refreshing one scope never has to wait on
    another."""
    cached = _tokens.get(scope)
    if cached and cached[1] > time.monotonic():
        return cached[0]

    with _lock_for(scope):
        cached = _tokens.get(scope)  # another thread may have already refreshed it
        if cached and cached[1] > time.monotonic():
            return cached[0]

        resp = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "scope": scope,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        token = data["access_token"]
        expires_at = time.monotonic() + data.get("expires_in", 1499) - _EXPIRY_MARGIN_SECONDS
        _tokens[scope] = (token, expires_at)
        return token


def _throttle(scope: str, max_calls_per_second: float) -> None:
    min_interval = 1 / max_calls_per_second
    with _lock_for(f"throttle:{scope}"):
        now = time.monotonic()
        next_slot = max(now, _last_call_at.get(scope, 0.0) + min_interval)
        _last_call_at[scope] = next_slot
        wait = next_slot - now
    if wait > 0:
        time.sleep(wait)


def _retry_after_seconds(value: str | None) -> float:
    if not value:
        return 2.0
    try:
        return min(max(float(value), 0.0), 10.0)
    except ValueError:
        return 2.0


def get(url: str, scope: str, params: dict | None = None, timeout: int = 15,
        max_calls_per_second: float = _DEFAULT_MAX_CALLS_PER_SECOND, _retries_left: int = 1) -> requests.Response:
    """Authenticated GET against a France Travail Connect API.
    `max_calls_per_second` must come from the caller (that specific API's
    documented limit, with margin) -- see the module docstring for why it's
    not inferred from the scope. Leaves the caller to decide what to do with
    the final status code (e.g. 403 insufficient_scope = access not yet
    approved by France Travail, the normal state for La Bonne Boite until
    it's granted)."""
    _throttle(scope, max_calls_per_second)
    token = get_token(scope)
    resp = requests.get(url, params=params, headers={"Authorization": f"Bearer {token}"}, timeout=timeout)

    if resp.status_code == 429 and _retries_left > 0:
        wait = _retry_after_seconds(resp.headers.get("Retry-After"))
        print(f"[france_travail_auth] 429 on {scope} despite throttling -> waiting {wait:.1f}s then retrying once", flush=True)
        time.sleep(wait)
        return get(url, scope, params=params, timeout=timeout,
                    max_calls_per_second=max_calls_per_second, _retries_left=_retries_left - 1)
    return resp
