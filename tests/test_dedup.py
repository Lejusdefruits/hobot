from core.db import get_connection
from graphs.discovery_graph import _is_relevant, _relevance_keywords, persist_adhoc_offers
from tools.common import make_offer, normalize_text


def test_normalize_text_strips_accents_case_and_punctuation():
    assert normalize_text("Développeur/euse Full-Stack (H/F)") == "developpeur euse full stack h f"


def test_normalize_text_handles_non_string_input():
    """A pandas NaN for a missing JobSpy field is a float, not None --
    must not crash."""
    assert normalize_text(float("nan")) == ""
    assert normalize_text(None) == ""


def test_dedup_key_is_case_and_accent_insensitive():
    a = make_offer(source="jobspy_indeed", external_id="1", title="Développeur Backend",
                    company="Acme SAS", location="Paris", description="", url="https://a")
    b = make_offer(source="adzuna", external_id="2", title="DEVELOPPEUR BACKEND",
                    company="acme sas", location="Paris", description="", url="https://b")
    assert a["dedup_key"] == b["dedup_key"]


def test_url_hash_differs_per_source_for_the_same_listing():
    """Two sources scraping the same posting must not collide on url_hash --
    only dedup_key is meant to catch that, at the dedup step, not storage."""
    a = make_offer(source="jobspy_indeed", external_id="123", title="Backend Engineer",
                    company="Acme", location="Paris", description="", url="https://a")
    b = make_offer(source="adzuna", external_id="123", title="Backend Engineer",
                    company="Acme", location="Paris", description="", url="https://a")
    assert a["url_hash"] != b["url_hash"]


def test_relevance_keywords_keeps_short_all_uppercase_acronyms():
    """A role like 'Ingenieur IA' must not silently lose 'IA' to the
    >=4-letters rule -- that's the one word that actually matters."""
    keywords = _relevance_keywords(["Ingenieur IA"])
    assert "ia" in keywords


def test_relevance_keywords_drops_short_lowercase_function_words():
    keywords = _relevance_keywords(["Ingenieur de la Data"])
    assert "de" not in keywords
    assert "la" not in keywords


def test_is_relevant_true_with_no_profile_keywords():
    assert _is_relevant("Anything", "Anything", [], []) is True


def test_is_relevant_matches_on_title():
    assert _is_relevant("Ingenieur Backend Python", "", ["python"], []) is True


def test_is_relevant_falls_back_to_description():
    assert _is_relevant("Poste technique", "Stack: Python, Django", ["python"], []) is True


def test_is_relevant_false_when_neither_title_nor_description_match(monkeypatch):
    import tools.semantic_relevance as semantic_relevance
    monkeypatch.setattr(semantic_relevance, "is_relevant_semantic", lambda *a, **k: None)
    assert _is_relevant("Poste commercial", "Vente terrain B2B", ["python"], []) is False


def test_persist_adhoc_offers_dedupes_within_the_same_batch(db):
    offer = make_offer(source="jobspy_indeed", external_id="1", title="Backend Engineer",
                        company="Acme", location="Paris", description="x", url="https://a")
    result = persist_adhoc_offers([offer, dict(offer)])
    assert len(result) == 1


def test_persist_adhoc_offers_dedupes_cross_source_by_company_and_title(db):
    """Same company + same title (normalized) from two different sources
    must resolve to a single database row."""
    first = make_offer(source="jobspy_indeed", external_id="1", title="Backend Engineer",
                        company="Acme", location="Paris", description="x", url="https://a")
    second = make_offer(source="adzuna", external_id="2", title="backend engineer",
                         company="ACME", location="Paris", description="y", url="https://b")

    persist_adhoc_offers([first])
    result = persist_adhoc_offers([second])

    assert len(result) == 1
    with get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) c FROM offers").fetchone()["c"]
    assert count == 1


def test_persist_adhoc_offers_keeps_offers_from_different_companies_separate(db):
    first = make_offer(source="jobspy_indeed", external_id="1", title="Backend Engineer",
                        company="Acme", location="Paris", description="x", url="https://a")
    second = make_offer(source="jobspy_indeed", external_id="2", title="Backend Engineer",
                         company="Globex", location="Paris", description="y", url="https://b")

    result = persist_adhoc_offers([first, second])

    assert len(result) == 2


def test_persist_adhoc_offers_filters_formation_intermediary_listings(db):
    offer = make_offer(
        source="lba", external_id="1", title="Alternance Developpeur", company=None,
        location="Paris",
        description="Rejoignez notre formation diplomante chez notre entreprise partenaire.",
        url="https://a",
    )
    result = persist_adhoc_offers([offer])
    assert result == []
    with get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) c FROM offers").fetchone()["c"]
    assert count == 0
