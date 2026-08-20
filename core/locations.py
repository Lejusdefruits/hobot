"""Single registry mapping city -> (latitude, longitude, INSEE commune code).

Common entry point for any source that needs coordinates or a commune code
rather than a free-text city name (La Bonne Alternance, France Travail) --
`user_profile.target_locations` (set through chat) directly drives where
these sources search, by resolving each city against this registry.

INSEE codes pulled from France Travail's own official commune reference
rather than guessed -- France Travail silently ignores latitude/longitude
when `commune` is absent, so a correct commune code is a requirement, not a
nice-to-have. Coordinates are each commune's usual city-center point.

Deliberate limitation: about thirty major French cities, not an exhaustive
geographic database -- enough for a candidate targeting cities, not villages.
resolve() returns None for anything else rather than guessing or silently
falling back to a different city -- deciding the fallback is left to the
caller (see DEFAULT_LOCATION)."""
from tools.common import normalize_text

LOCATIONS: dict[str, dict] = {
    "paris": {"label": "Paris", "lat": 48.8566, "lon": 2.3522, "commune_insee": "75056"},
    "rouen": {"label": "Rouen", "lat": 49.4431, "lon": 1.0993, "commune_insee": "76540"},
    "lyon": {"label": "Lyon", "lat": 45.7640, "lon": 4.8357, "commune_insee": "69123"},
    "toulouse": {"label": "Toulouse", "lat": 43.6047, "lon": 1.4442, "commune_insee": "31555"},
    "lille": {"label": "Lille", "lat": 50.6292, "lon": 3.0573, "commune_insee": "59350"},
    "nantes": {"label": "Nantes", "lat": 47.2184, "lon": -1.5536, "commune_insee": "44109"},
    "bordeaux": {"label": "Bordeaux", "lat": 44.8378, "lon": -0.5792, "commune_insee": "33063"},
    "rennes": {"label": "Rennes", "lat": 48.1173, "lon": -1.6778, "commune_insee": "35238"},
    "strasbourg": {"label": "Strasbourg", "lat": 48.5734, "lon": 7.7521, "commune_insee": "67482"},
    "montpellier": {"label": "Montpellier", "lat": 43.6108, "lon": 3.8767, "commune_insee": "34172"},
    "grenoble": {"label": "Grenoble", "lat": 45.1885, "lon": 5.7245, "commune_insee": "38185"},
    "nice": {"label": "Nice", "lat": 43.7102, "lon": 7.2620, "commune_insee": "06088"},
    "marseille": {"label": "Marseille", "lat": 43.2965, "lon": 5.3698, "commune_insee": "13055"},
    "le havre": {"label": "Le Havre", "lat": 49.4944, "lon": 0.1079, "commune_insee": "76351"},
    "reims": {"label": "Reims", "lat": 49.2583, "lon": 4.0317, "commune_insee": "51454"},
    "dijon": {"label": "Dijon", "lat": 47.3220, "lon": 5.0415, "commune_insee": "21231"},
    "angers": {"label": "Angers", "lat": 47.4784, "lon": -0.5632, "commune_insee": "49007"},
    "nimes": {"label": "Nîmes", "lat": 43.8367, "lon": 4.3601, "commune_insee": "30189"},
    "toulon": {"label": "Toulon", "lat": 43.1242, "lon": 5.9280, "commune_insee": "83137"},
    "saint etienne": {"label": "Saint-Étienne", "lat": 45.4397, "lon": 4.3872, "commune_insee": "42218"},
    "clermont ferrand": {"label": "Clermont-Ferrand", "lat": 45.7772, "lon": 3.0870, "commune_insee": "63113"},
    "amiens": {"label": "Amiens", "lat": 49.8941, "lon": 2.2958, "commune_insee": "80021"},
    "metz": {"label": "Metz", "lat": 49.1193, "lon": 6.1757, "commune_insee": "57463"},
    "tours": {"label": "Tours", "lat": 47.3941, "lon": 0.6848, "commune_insee": "37261"},
    "caen": {"label": "Caen", "lat": 49.1829, "lon": -0.3707, "commune_insee": "14118"},
    "orleans": {"label": "Orléans", "lat": 47.9029, "lon": 1.9093, "commune_insee": "45234"},
    "nancy": {"label": "Nancy", "lat": 48.6921, "lon": 6.1844, "commune_insee": "54395"},
    "limoges": {"label": "Limoges", "lat": 45.8336, "lon": 1.2611, "commune_insee": "87085"},
    "perpignan": {"label": "Perpignan", "lat": 42.6886, "lon": 2.8948, "commune_insee": "66136"},
    "besancon": {"label": "Besançon", "lat": 47.2378, "lon": 6.0241, "commune_insee": "25056"},
    "mulhouse": {"label": "Mulhouse", "lat": 47.7508, "lon": 7.3359, "commune_insee": "68224"},
}

DEFAULT_LOCATION = LOCATIONS["paris"]


def _contains_phrase(words: list[str], phrase_words: list[str]) -> bool:
    n = len(phrase_words)
    return any(words[i:i + n] == phrase_words for i in range(len(words) - n + 1))


def resolve(ville: str | None) -> dict | None:
    """{"label","lat","lon","commune_insee"} for a known city (case/accent/
    punctuation-insensitive lookup via normalize_text), or None if unknown --
    never a silent fallback to some other city.

    Beyond a bare city name ("Lyon"), also recognizes a known city named
    inside a more descriptive piece of text (e.g. "Paris / Île-de-France
    (priority)", seen for real in user_profile.target_locations) -- a strict
    exact match would have silently dropped Paris the moment the profile
    describes the city with a bit of context instead of a bare name."""
    if not ville:
        return None
    norm = normalize_text(ville)
    if norm in LOCATIONS:
        return LOCATIONS[norm]
    words = norm.split()
    for key, loc in LOCATIONS.items():
        if _contains_phrase(words, key.split()):
            return loc
    return None


def resolve_many(villes: list[str] | None) -> list[dict]:
    """Resolves a list of cities (typically user_profile.target_locations),
    silently skipping any that aren't in the registry -- the caller decides
    what to do if the result is empty (see DEFAULT_LOCATION)."""
    out = []
    for v in villes or []:
        loc = resolve(v)
        if loc:
            out.append(loc)
    return out
