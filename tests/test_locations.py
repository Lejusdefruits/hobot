from core.locations import resolve, resolve_many


def test_resolve_bare_city_name():
    assert resolve("Lyon")["label"] == "Lyon"


def test_resolve_city_named_inside_descriptive_text():
    assert resolve("Paris / Île-de-France (priority)")["label"] == "Paris"


def test_resolve_region_alias_with_no_city_name_in_it():
    """"Île-de-France" alone has no city name for the phrase-matching to
    find -- confirmed live, this exact target_locations value meant
    Adzuna/France Travail/LBA never searched Paris at all."""
    assert resolve("Île-de-France")["label"] == "Paris"
    assert resolve("IDF")["label"] == "Paris"


def test_resolve_unknown_location_returns_none():
    assert resolve("Antarctica") is None


def test_resolve_many_keeps_a_region_alias_alongside_real_cities():
    resolved = resolve_many(["Le Havre", "Île-de-France", "Rennes"])
    assert [loc["label"] for loc in resolved] == ["Le Havre", "Paris", "Rennes"]
