#!/usr/bin/env python3
"""
make_scorecard.py — PolicyLogic end-to-end: candidate in, scorecard out.

Given ONE official (candidate + term + office/seat), this runs the whole
pipeline as a single command:

  1. SCRAPE PROMISES      web search across tiered sources, windowed to the term
  2. SCRAPE DELIVERY      structured APIs (Congress.gov, GovTrack) + tiered news
  3. RANK BY CREDIBILITY  every source tagged Tier 1 / 2 / 3 / inadmissible
  4. AI + METHODOLOGY     Claude assigns buckets; the deterministic engine
                          computes the grade; a completed scorecard is written

It is a thin ORCHESTRATOR. All real work lives in the existing modules — this
file only wires them together so a human runs ONE command instead of two:

    api/collect_evidence.py   (steps 1-3)  ->  packet.json
    api/run_pipeline.py        (step 4, Claude + script/adapter + scoring_engine)

USAGE
    python3 make_scorecard.py --seat data/seats/aoc.json --out-dir build

    # scrape only, no AI bill — works without an Anthropic key:
    python3 make_scorecard.py --seat data/seats/aoc.json --out-dir build --collect-only

    # offline wiring test (fixtures instead of live web) — still needs an
    # Anthropic key for the grading step unless combined with --collect-only:
    python3 make_scorecard.py --seat data/seats/aoc.json --out-dir build --demo --collect-only

SEAT FILE (one JSON per official; see data/seats/*.json)
    {
      "id": "O000172",                         # Congress bioguide ID (used to
                                               #   query sponsored legislation)
      "name": "Alexandria Ocasio-Cortez",
      "party": "Democratic",
      "office": "U.S. Representative",          # must contain 'senator' or
                                               #   'representative' for the
                                               #   Congress verifiers to engage
      "jurisdiction": "New York (NY-14)",
      "seat_key": "US-HOUSE-NY-14",
      "current_term": ["2025-01-03", "2027-01-03"],
      "prior_terms_same_seat": [["2023-01-03", "2025-01-03"]]
    }

API KEYS (live mode only; set in your shell before running)
    BRAVE_API_KEY        promise + news search   (free: brave.com/search/api)
    CONGRESS_API_KEY     sponsored legislation    (free: api.data.gov/signup)
    ANTHROPIC_API_KEY    the grading step         (console.anthropic.com)
"""

from __future__ import annotations
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# run_pipeline.py lives in api/ but imports `adapter` and reads the prompt file
# from script/. Put both on the path so the AI step resolves regardless of CWD.
sys.path.insert(0, os.path.join(HERE, "api"))
sys.path.insert(0, os.path.join(HERE, "script"))

import collect_evidence as ce  # noqa: E402


def _is_congress(office: str) -> bool:
    o = office.lower()
    return "senator" in o or "representative" in o or "congress" in o


def preflight(seat: dict, *, demo: bool, collect_only: bool) -> None:
    """Fail early with a precise message instead of crashing mid-scrape."""
    problems = []
    if not demo and not os.environ.get("BRAVE_API_KEY"):
        problems.append("BRAVE_API_KEY is required to scrape promises/news "
                        "(or pass --demo for an offline fixture run).")
    if not demo and _is_congress(seat["office"]) and not os.environ.get("CONGRESS_API_KEY"):
        # Not fatal — the verifier degrades to a note — but worth saying.
        print("  [warn] CONGRESS_API_KEY not set: sponsored-legislation "
              "verification will be skipped.", file=sys.stderr)
    if not collect_only and not os.environ.get("ANTHROPIC_API_KEY"):
        problems.append("ANTHROPIC_API_KEY is required for the grading step "
                        "(or pass --collect-only to stop after scraping).")
    if problems:
        sys.exit("Cannot run:\n  - " + "\n  - ".join(problems))


def build_searcher_and_verifiers(seat: ce.Seat, *, demo: bool, congress_limit: int):
    """Mirror collect_evidence.main()'s source selection."""
    if demo:
        searcher = ce.FixtureSearcher([
            {"title": "Candidate pledges $15 wage", "url": "https://apnews.com/x",
             "snippet": "Pledged to pass a $15 federal minimum wage.", "date": "2024-09-01"},
            {"title": "Random tweet", "url": "https://x.com/post/123",
             "snippet": "inadmissible social post", "date": "2024-10-01"},
        ])
        verifiers = [ce.FixtureVerifier([
            {"summary": "Sponsored S.150 Raise the Wage Act",
             "url": "https://congress.gov/bill", "date": "2025-02-01",
             "kind": "sponsored_bill"},
        ])]
        return searcher, verifiers

    searcher = ce.WebSearchSearcher()
    if _is_congress(seat.office):
        verifiers = [ce.CongressAPIVerifier(limit=congress_limit),
                     ce.GovTrackVerifier(limit=congress_limit)]
    else:
        verifiers = []  # tiered web verification still runs inside collect()
    return searcher, verifiers


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Candidate in, completed PolicyLogic scorecard out.")
    ap.add_argument("--seat", required=True,
                    help="Path to a combined seat JSON (see data/seats/*.json).")
    ap.add_argument("--out-dir", default="build",
                    help="Where packet + scorecard are written (default: build/).")
    ap.add_argument("--collect-only", action="store_true",
                    help="Stop after scraping+tiering; skip the AI grading step "
                         "(no Anthropic key needed).")
    ap.add_argument("--demo", action="store_true",
                    help="Use offline fixtures instead of live web/APIs.")
    ap.add_argument("--news-allowlist", default=None,
                    help="Optional committed Tier-2 local-news allowlist file.")
    ap.add_argument("--congress-limit", type=int, default=100)
    args = ap.parse_args()

    sd = json.load(open(args.seat, encoding="utf-8"))
    for required in ("id", "name", "office", "jurisdiction", "seat_key", "current_term"):
        if required not in sd:
            sys.exit(f"Seat file {args.seat} is missing required field '{required}'.")

    preflight(sd, demo=args.demo, collect_only=args.collect_only)

    seat = ce.Seat(
        office=sd["office"], jurisdiction=sd["jurisdiction"],
        seat_key=sd["seat_key"], current_term=tuple(sd["current_term"]),
        prior_terms_same_seat=[tuple(t) for t in sd.get("prior_terms_same_seat", [])])

    allowlist = args.news_allowlist or os.environ.get("POLICYLOGIC_NEWS_ALLOWLIST")
    if allowlist:
        n = ce.load_tier2_allowlist(allowlist)
        print(f"  [tier-2 allowlist] {n} curated outlet(s) registered")

    config = ce.CollectorConfig(congress_limit=args.congress_limit)
    searcher, verifiers = build_searcher_and_verifiers(
        seat, demo=args.demo, congress_limit=args.congress_limit)

    # ---- Steps 1-3: scrape promises + delivery, ranked by credibility tier ----
    print(f"Scraping evidence for {sd['name']} ({sd['office']}, {sd['jurisdiction']}) ...",
          file=sys.stderr)
    packet = ce.collect(seat, sd["id"], sd["name"], searcher, verifiers, config=config)
    packet["official"]["party"] = sd.get("party", "")  # carry party onto the card

    os.makedirs(args.out_dir, exist_ok=True)
    packet_path = os.path.join(args.out_dir, f"{sd['id']}.packet.json")
    json.dump(packet, open(packet_path, "w", encoding="utf-8"), indent=2, ensure_ascii=False)

    c = packet["coverage"]
    print(f"  packet: {packet_path}")
    print(f"  promises: {c['promise_candidates']}  verification: {c['verification_records']}  "
          f"tier1: {c['tier1_count']}  (dropped window/inadmissible: "
          f"{c['dropped_out_of_window']}/{c['dropped_inadmissible']})")

    if args.collect_only:
        print("\n--collect-only: stopping before the AI grading step.")
        return

    # ---- Step 4: AI applies the methodology -> deterministic grade -> card ----
    import run_pipeline as rp  # imported here so --collect-only never needs anthropic
    rp.PROMPT_FILE = os.path.join(HERE, "script", "ai_bucket_assignment_prompt.md")
    rp.run(packet_path, demo=args.demo, out_dir=args.out_dir)
    print("\nDone. Scorecard is an AI DRAFT — requires human review before publishing.")


if __name__ == "__main__":
    main()
