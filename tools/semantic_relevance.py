"""Optional semantic fallback for the relevance pre-filter
(graphs/discovery_graph.py::_is_relevant) -- catches a genuinely relevant
posting that the keyword+description check still misses (different
wording for the same domain, e.g. "AI Engineer" against a profile that only
ever says "IA") using a French sentence-embedding model instead of literal
word matching.

Model choice is NOT the obvious "keep it light" pick, on purpose: a small
generic multilingual model (paraphrase-multilingual-MiniLM-L12-v2, ~120MB)
was tried first and empirically does NOT separate relevant from irrelevant
postings for this profile -- tested live against real title+description
pairs, "Machine Learning Engineer" scored LOWER (0.03) than "Assistant
comptable" (0.25) despite being the relevant one; intfloat/multilingual-e5-small
(a retrieval-oriented model) was tried next and was worse still, compressing
every score into a narrow 0.82-0.88 band regardless of topic.
Lajavaness/sentence-camembert-large (~1.3GB, fine-tuned specifically on
French STS) is the one that actually works: the same test set separated
cleanly, every relevant posting above ~0.38, every irrelevant one below
~0.28. A pre-filter fallback that doesn't reliably discriminate is worse
than no fallback at all (it would either never fire, or let noise through
depending on the threshold) -- so this ships the model that was actually
verified to work, not the smallest one that runs.

Off by default and never a hard dependency regardless: RELEVANCE_SEMANTIC_FALLBACK=1
in .env, plus `pip install sentence-transformers` (deliberately NOT in
requirements.txt -- see README's Job sources section), are both required
for this to actually run. Either missing, and is_relevant_semantic()
returns None ("not applicable"), which _is_relevant() reads as "fall
through to whatever it already decided from keywords" -- this module is
never even imported for someone who didn't opt in, let alone loading a
model.
"""
import os

RELEVANCE_SEMANTIC_FALLBACK = os.environ.get("RELEVANCE_SEMANTIC_FALLBACK", "0") == "1"
SIMILARITY_THRESHOLD = float(os.environ.get("RELEVANCE_SIMILARITY_THRESHOLD", "0.3"))
MODEL_NAME = os.environ.get("RELEVANCE_SEMANTIC_MODEL", "Lajavaness/sentence-camembert-large")

_model = None
_warned = False


def _get_model():
    """Lazy singleton, loaded on first real use -- same pattern as this
    project's other lazy-loaded clients (core/llm.py's shared Ollama
    client). Never loaded at all unless the flag is on AND a call actually
    reaches here (the keyword+description check already passed most
    postings without ever calling this)."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def is_relevant_semantic(text: str, target_roles: list[str]) -> bool | None:
    """Cosine similarity between `text` (an offer's title + description)
    and the profile's target roles joined together. Returns None -- "not
    applicable", not "not relevant" -- when the feature is off, there's
    nothing to compare against, or the dependency isn't installed, so
    _is_relevant() (graphs/discovery_graph.py) can fall through cleanly to
    its own keyword-based decision either way. Never raises past this
    boundary, same convention as every other optional integration in this
    project (web_search.py, sources_pappers.py, ...)."""
    global _warned
    if not RELEVANCE_SEMANTIC_FALLBACK or not target_roles or not text:
        return None
    try:
        from sentence_transformers.util import cos_sim
        model = _get_model()
    except ImportError:
        if not _warned:
            print("[semantic_relevance] RELEVANCE_SEMANTIC_FALLBACK=1 but sentence-transformers "
                  "isn't installed (pip install sentence-transformers) -- falling back to "
                  "keyword-only relevance.", flush=True)
            _warned = True
        return None
    except Exception as e:
        print(f"[semantic_relevance] model load failed ({e}) -- falling back to keyword-only relevance.", flush=True)
        return None

    try:
        profile_text = ", ".join(target_roles)
        embeddings = model.encode([profile_text, text[:2000]], convert_to_tensor=True)
        similarity = float(cos_sim(embeddings[0], embeddings[1]))
        return similarity >= SIMILARITY_THRESHOLD
    except Exception as e:
        print(f"[semantic_relevance] embedding/comparison failed ({e}) -- falling back to keyword-only relevance.", flush=True)
        return None


if __name__ == "__main__":
    import sys

    RELEVANCE_SEMANTIC_FALLBACK = True  # force-enabled for this standalone smoke test
    roles = ["Ingenieur IA", "Ingenieur IA (alternance)"]
    tests = sys.argv[1:] or [
        "Data Scientist Junior en alternance", "AI Engineer - Contrat de professionnalisation",
        "Serveur / Serveuse de restaurant", "Vendeur / Vendeuse en patisserie",
    ]
    for t in tests:
        print(f"{is_relevant_semantic(t, roles)!s:5}  {t}")
