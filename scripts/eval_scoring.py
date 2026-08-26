"""Measures whether the LLM scorer (graphs/discovery_graph.py's score_node)
actually agrees with human judgment, instead of just trusting that it does.

Two sources of ground truth, kept separate because they carry different
caveats:

1. Hand labels (`label` subcommand): you rate a sample of already-scored
   offers 1-5 for how relevant they genuinely are to your search. Stored in
   scripts/scoring_eval_labels.json (offer_id, llm_score, human_score,
   labeled_at) -- a plain sidecar file, not a database table, since this is
   an evaluation artifact, not something the running app reads. Re-running
   `label` skips offers already labeled and adds more, so the sample grows
   over time instead of resetting.

2. Behavioral signal, already in hobot.db for free: offers.status IN
   ('applied', 'excluded') is a real decision you already made on a real
   offer. Weaker evidence than #1 though -- `excluded` collapses several
   different reasons into one status (genuinely irrelevant, but also
   "already applied elsewhere", "expired", "changed my mind" -- see
   exclure_offre in graphs/chat_agent.py, whose `raison` argument is only
   used in the confirmation message, never persisted, so the specific reason
   behind any one exclusion isn't recoverable here). Reported as a secondary
   signal, not a substitute for #1.

Usage:
    .venv/bin/python scripts/eval_scoring.py label [--n 20]
    .venv/bin/python scripts/eval_scoring.py report
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.db import get_connection  # noqa: E402

LABELS_PATH = Path(__file__).resolve().parent / "scoring_eval_labels.json"


def _load_labels() -> list[dict]:
    if not LABELS_PATH.exists():
        return []
    return json.loads(LABELS_PATH.read_text())


def _save_labels(labels: list[dict]) -> None:
    LABELS_PATH.write_text(json.dumps(labels, indent=2, ensure_ascii=False))


def _sample_offers(n: int, already_labeled: set[int]) -> list[dict]:
    """Stratified across score terciles (low/mid/high) rather than a plain
    random sample -- a scorer that's only ever tested on offers it already
    scored high tells you nothing about whether it correctly scores things
    low, which is just as important (a bad match wasting your time is the
    failure mode this whole feature exists to prevent)."""
    with get_connection() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, title, company, location, description, score, score_reason "
            "FROM offers WHERE score IS NOT NULL ORDER BY score"
        ).fetchall()]
    rows = [r for r in rows if r["id"] not in already_labeled]
    if not rows:
        return []
    buckets = [rows[i::3] for i in range(3)]  # low/mid/high thirds by score rank
    per_bucket = max(1, n // 3)
    sample = []
    for bucket in buckets:
        sample.extend(bucket[:per_bucket])
    return sample[:n]


def label(n: int) -> None:
    labels = _load_labels()
    already = {entry["offer_id"] for entry in labels}
    sample = _sample_offers(n, already)
    if not sample:
        print("Nothing left to label (or no scored offers in the database yet).")
        return

    print(f"{len(sample)} offers to rate. For each: how relevant is this to your actual "
          "search, 1 (not at all) to 5 (excellent match)? Enter to skip, Ctrl+C to stop early.\n")
    try:
        for offer in sample:
            print(f"--- #{offer['id']} -- {offer['title']} -- {offer['company']} ({offer['location']}) ---")
            print((offer["description"] or "")[:400])
            print(f"[LLM score: {offer['score']}] {offer['score_reason'] or ''}")
            raw = input("Your rating (1-5, Enter to skip): ").strip()
            print()
            if not raw:
                continue
            try:
                human_score = int(raw)
                assert 1 <= human_score <= 5
            except (ValueError, AssertionError):
                print("Not a number 1-5, skipped.\n")
                continue
            labels.append({
                "offer_id": offer["id"], "llm_score": offer["score"],
                "human_score": human_score, "labeled_at": _now(),
            })
            _save_labels(labels)
    except KeyboardInterrupt:
        print("\nStopped early, labels saved so far.")
    print(f"\n{len(labels)} total hand labels in {LABELS_PATH}.")


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    """Rank correlation without a scipy dependency -- Pearson correlation
    computed on ranks IS Spearman's rho. Returns None when there isn't
    enough variance to compute anything meaningful from (fewer than 2 points,
    or every value identical)."""
    n = len(xs)
    if n < 2:
        return None

    def _ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        ranks = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            avg_rank = (i + j) / 2 + 1
            for k in range(i, j + 1):
                ranks[order[k]] = avg_rank
            i = j + 1
        return ranks

    rx, ry = _ranks(xs), _ranks(ys)
    mean_x, mean_y = sum(rx) / n, sum(ry) / n
    cov = sum((a - mean_x) * (b - mean_y) for a, b in zip(rx, ry))
    var_x = sum((a - mean_x) ** 2 for a in rx)
    var_y = sum((b - mean_y) ** 2 for b in ry)
    if var_x == 0 or var_y == 0:
        return None
    return cov / (var_x * var_y) ** 0.5


def _report_hand_labels() -> None:
    labels = _load_labels()
    print(f"=== Hand-labeled sample ({len(labels)} offers) ===")
    if len(labels) < 5:
        print("Fewer than 5 labels -- not enough to draw a conclusion from. "
              "Run `label` to add more.\n")
        return
    llm = [entry["llm_score"] for entry in labels]
    human = [entry["human_score"] for entry in labels]
    rho = _spearman(llm, human)
    mean_abs_diff = sum(abs(a - b * 20) for a, b in zip(llm, human)) / len(labels)  # human 1-5 -> 0-100 scale
    print(f"Spearman rank correlation (LLM score vs. your 1-5 rating): "
          f"{rho:.2f}" if rho is not None else "Spearman: undefined (no variance in one of the two series)")
    print(f"Mean absolute difference (your rating scaled to 0-100): {mean_abs_diff:.1f} points")
    print()


def _report_behavioral_signal() -> None:
    with get_connection() as conn:
        applied = [r["score"] for r in conn.execute(
            "SELECT score FROM offers WHERE status = 'applied' AND score IS NOT NULL"
        ).fetchall()]
        excluded = [r["score"] for r in conn.execute(
            "SELECT score FROM offers WHERE status = 'excluded' AND score IS NOT NULL"
        ).fetchall()]

    def _median(values):
        s = sorted(values)
        n = len(s)
        if n == 0:
            return None
        mid = n // 2
        return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2

    print("=== Behavioral signal (applied vs. excluded, real decisions already in hobot.db) ===")
    print(f"Applied ({len(applied)} offers): median score {_median(applied)}, range "
          f"{min(applied) if applied else '-'}-{max(applied) if applied else '-'}")
    print(f"Excluded ({len(excluded)} offers): median score {_median(excluded)}, range "
          f"{min(excluded) if excluded else '-'}-{max(excluded) if excluded else '-'}")
    print("Caveat: 'excluded' mixes several different reasons (genuinely irrelevant, "
          "but also expired/duplicate/already applied elsewhere/changed your mind) -- "
          "the reason isn't persisted, so a low correlation here doesn't necessarily mean "
          "the scorer is wrong. Treat this as a sanity check, not the main result.\n")


def report() -> None:
    _report_hand_labels()
    _report_behavioral_signal()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    label_parser = sub.add_parser("label", help="Rate a sample of scored offers by hand.")
    label_parser.add_argument("--n", type=int, default=20, help="How many offers to sample (default 20).")
    sub.add_parser("report", help="Print agreement metrics from labels collected so far.")

    args = parser.parse_args()
    if args.command == "label":
        label(args.n)
    elif args.command == "report":
        report()
