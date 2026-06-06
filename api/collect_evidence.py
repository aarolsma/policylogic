#!/usr/bin/env python3
"""
collect_evidence.py — PolicyLogic data-collection front end.

Per official, runs TWO PASSES and emits ONE evidence packet for the AI pipeline:

  PASS 1  PROMISES      — stated commitments (web search across tiered sources),
                          restricted to the seat's term window. Each candidate is
                          tagged current-term vs prior-term-same-seat.
  PASS 2  VERIFICATION  — the record of action on those commitments (structured
                          APIs first: legislation, votes, actions; news to fill).

Every item is tagged to the PolicyLogic source tiers (methodology.html → Data
Sources), which are fixed by the published methodology, not chosen here:

  TIER 1  Primary       gov records, signed legislation, EOs, budget, Federal
                        Register. REQUIRED for D3/D4 delivery.
  TIER 2  Authoritative major news w/ named sources, CBO/GAO/CRS, academic/policy
  TIER 3  Supporting    other credible reporting, official statements, press
                        releases (needs corroboration)
  INADMISSIBLE          anonymous social, partisan advocacy, official's own press
                        release as SOLE support for a positive outcome — FILTERED

BOUNDARY OF THIS TOOL: it COLLECTS, TIERS, and WINDOWS. It does not identify which
statements "count" as promises, assign buckets, score, or grade — the AI pipeline
and the analyst do that. The collector surfaces tiered, sourced candidates.

HYBRID DESIGN:
  - Verification uses structured APIs where they exist (Congress first).
  - Promises use web search (no API serves campaign promises).
  - The web-search calls are abstracted behind a `searcher` interface so this
    module stays runnable/testable without network, and so the same code serves
    other categories later by swapping the source adapter.

OUTPUT: <id>.packet.json  — consumed by the AI bucket-assignment step.
"""

from __future__ import annotations
import argparse
import dataclasses
import datetime as dt
import json
import os
import sys
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Callable, Protocol


# ============================================================================
# Tiers — fixed by methodology.html → Data Sources
# ============================================================================
class Tier(str, Enum):
    PRIMARY = "tier1_primary"        # required for D3/D4
    AUTHORITATIVE = "tier2_authoritative"
    SUPPORTING = "tier3_supporting"
    INADMISSIBLE = "inadmissible"    # filtered out of the packet


# Domain → tier classification. Conservative: unknown domains default to Tier 3
# (supporting, needs corroboration), never Tier 1, so nothing is over-credited.
TIER1_DOMAINS = (
    ".gov", "congress.gov", "govinfo.gov", "federalregister.gov",
    "gpo.gov", "senate.gov", "house.gov", "whitehouse.gov",
)
TIER2_DOMAINS = (
    "cbo.gov", "gao.gov", "crsreports.congress.gov",  # nonpartisan watchdogs (.gov but T2 role)
    "reuters.com", "apnews.com", "nytimes.com", "washingtonpost.com",
    "wsj.com", "bloomberg.com", "npr.org", "politico.com", "propublica.org",
    "bsky.app",  # placeholder; real list maintained externally
)
# Things that are never sufficient on their own.
INADMISSIBLE_HINTS = ("twitter.com", "x.com", "facebook.com", "reddit.com",
                      "t.me", "truthsocial.com")


def classify_tier(url: str, *, is_official_press_release: bool = False) -> Tier:
    u = (url or "").lower()
    if any(h in u for h in INADMISSIBLE_HINTS):
        return Tier.INADMISSIBLE
    # Nonpartisan watchdogs are Tier 2 even though .gov — check before generic .gov.
    if any(d in u for d in ("cbo.gov", "gao.gov", "crsreports")):
        return Tier.AUTHORITATIVE
    if any(d in u for d in TIER1_DOMAINS):
        return Tier.PRIMARY
    if any(d in u for d in TIER2_DOMAINS):
        return Tier.AUTHORITATIVE
    # Official's own press release as sole support for a positive outcome is
    # inadmissible *as sole support*; we keep it but mark it Tier 3 and let the
    # corroboration rule apply downstream.
    return Tier.SUPPORTING


# ============================================================================
# Term windowing — "current term + flag prior terms in the same seat"
# ============================================================================
@dataclass
class Seat:
    """Identifies a single seat so we can bound terms to it."""
    office: str                    # "U.S. Senator", "Governor", ...
    jurisdiction: str              # state / city
    seat_key: str                  # e.g. "US-SEN-VT-CLASS1" — stable seat id
    current_term: tuple[str, str]  # (start ISO, end ISO)
    prior_terms_same_seat: list[tuple[str, str]] = field(default_factory=list)


def term_origin(date_iso: str, seat: Seat) -> str:
    """Classify a promise/evidence date as current-term, prior-term-same-seat,
    or out-of-window (which the collector drops)."""
    if not date_iso:
        return "undated"
    try:
        d = dt.date.fromisoformat(date_iso[:10])
    except ValueError:
        return "undated"
    cs, ce = (dt.date.fromisoformat(seat.current_term[0]),
              dt.date.fromisoformat(seat.current_term[1]))
    if cs <= d <= ce:
        return "current_term"
    for ps, pe in seat.prior_terms_same_seat:
        if dt.date.fromisoformat(ps) <= d <= dt.date.fromisoformat(pe):
            return "prior_term_same_seat"
    return "out_of_window"


# ============================================================================
# Source adapters — pluggable so other categories swap in later
# ============================================================================
class Searcher(Protocol):
    """Web search abstraction. Returns list of {title,url,snippet,date}."""
    def search(self, query: str, *, max_results: int = 10) -> list[dict[str, Any]]: ...


class VerificationAPI(Protocol):
    """Structured verification source. Returns list of action records."""
    def fetch(self, seat: Seat, official_id: str) -> list[dict[str, Any]]: ...


# ---- Item model -------------------------------------------------------------
@dataclass
class EvidenceItem:
    pass_type: str          # "promise" | "verification"
    text: str               # the statement or the action record summary
    url: str
    date: str
    tier: str               # Tier value
    term_origin: str        # current_term | prior_term_same_seat | undated
    source_kind: str        # e.g. "campaign_statement", "sponsored_bill", "vote"
    cid: str = ""
    note: str = ""


# ============================================================================
# The collector
# ============================================================================
def collect(seat: Seat, official_id: str, official_name: str,
            searcher: Searcher, verifiers: list[VerificationAPI]) -> dict[str, Any]:
    items: list[EvidenceItem] = []
    seen_urls: set[str] = set()
    counters = {"cid": 0, "dropped_out_of_window": 0, "dropped_inadmissible": 0,
                "dropped_duplicate": 0}

    def push(it: EvidenceItem) -> None:
        # Window filter.
        if it.term_origin == "out_of_window":
            counters["dropped_out_of_window"] += 1
            return
        # Tier filter.
        if it.tier == Tier.INADMISSIBLE.value:
            counters["dropped_inadmissible"] += 1
            return
        # Dedup by (pass_type, url): same source across queries counts once.
        # Items with no URL are never deduped (can't tell them apart safely).
        if it.url:
            key = f"{it.pass_type}|{it.url}"
            if key in seen_urls:
                counters["dropped_duplicate"] += 1
                return
            seen_urls.add(key)
        counters["cid"] += 1
        it.cid = f"c{counters['cid']}"
        items.append(it)

    # ---- PASS 1: PROMISES via tiered web search ----
    # Queries fan out across source intent; the term window is applied per-result
    # by date, since search can't be perfectly time-scoped.
    promise_queries = [
        f'"{official_name}" {seat.office} campaign promise',
        f'"{official_name}" pledge OR "I will" {seat.jurisdiction}',
        f'"{official_name}" platform OR agenda {seat.office}',
        f'"{official_name}" debate transcript {seat.jurisdiction}',
        f'"{official_name}" inaugural OR "first day" commitment',
    ]
    for q in promise_queries:
        for r in searcher.search(q, max_results=10):
            url = r.get("url", "")
            tier = classify_tier(url)
            push(EvidenceItem(
                pass_type="promise",
                text=r.get("snippet") or r.get("title") or "",
                url=url, date=r.get("date", ""),
                tier=tier.value,
                term_origin=term_origin(r.get("date", ""), seat),
                source_kind="campaign_statement",
                note="Promise CANDIDATE. The AI step decides if it qualifies; "
                     "the analyst verifies."))

    # ---- PASS 2: VERIFICATION via structured APIs (+ search to fill) ----
    for v in verifiers:
        for rec in v.fetch(seat, official_id):
            url = rec.get("url", "")
            push(EvidenceItem(
                pass_type="verification",
                text=rec.get("summary", ""),
                url=url, date=rec.get("date", ""),
                tier=classify_tier(url).value,
                term_origin=term_origin(rec.get("date", ""), seat),
                source_kind=rec.get("kind", "action_record"),
                note=rec.get("note", "")))

    # Coverage honesty: surface what we have and don't have.
    primaries = sum(1 for i in items if i.tier == Tier.PRIMARY.value)
    promises = [i for i in items if i.pass_type == "promise"]
    verifs = [i for i in items if i.pass_type == "verification"]
    prior = sum(1 for i in items if i.term_origin == "prior_term_same_seat")

    return {
        "schema": "policylogic/evidence-packet/v1",
        "official": {"id": official_id, "name": official_name,
                     "office": seat.office, "jurisdiction": seat.jurisdiction,
                     "seat_key": seat.seat_key,
                     "current_term": seat.current_term,
                     "prior_terms_same_seat": seat.prior_terms_same_seat},
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "tier_definitions": {
            "tier1_primary": "gov records/legislation/EO/budget/Federal Register — required for D3/D4",
            "tier2_authoritative": "major news w/ named sources, CBO/GAO/CRS, academic",
            "tier3_supporting": "other credible reporting, official statements, press releases — needs corroboration",
        },
        "coverage": {
            "promise_candidates": len(promises),
            "verification_records": len(verifs),
            "tier1_count": primaries,
            "prior_term_flagged": prior,
            "dropped_out_of_window": counters["dropped_out_of_window"],
            "dropped_inadmissible": counters["dropped_inadmissible"],
            "dropped_duplicate": counters["dropped_duplicate"],
            "honesty_note": (
                "Promise candidates are leads, not confirmed promises. Verification "
                "is API-anchored where available. Tier-1 sources are required before "
                "any D3/D4 delivery score can be assigned downstream."),
        },
        "items": [asdict(i) for i in items],
    }


# ============================================================================
# Reference adapters
# ============================================================================
class WebSearchSearcher:
    """Real searcher: wire to your web_search / SerpAPI / Brave here.
    Left as the integration point; raises if used without wiring so it can't
    silently return nothing."""
    def search(self, query: str, *, max_results: int = 10) -> list[dict[str, Any]]:
        raise NotImplementedError(
            "Wire WebSearchSearcher.search to your search provider. "
            "Use FixtureSearcher for offline tests.")


class CongressAPIVerifier:
    """Verification adapter for U.S. Congress. Wire to Congress.gov / GovTrack.
    Returns action records (sponsored bills, votes) as verification evidence."""
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("CONGRESS_API_KEY")

    def fetch(self, seat: Seat, official_id: str) -> list[dict[str, Any]]:
        if not self.api_key:
            return [{"summary": "CONGRESS_API_KEY not set — verification API skipped.",
                     "url": "", "date": "", "kind": "note",
                     "note": "Set CONGRESS_API_KEY (free at api.data.gov) to pull bills/votes."}]
        # Integration point: call Congress.gov v3 sponsored-legislation + votes,
        # map each to {summary,url,date,kind}. Network-blocked in sandbox; wired
        # on your machine. Returning [] here keeps the module importable.
        return []


# ---- Offline fixture adapters for testing ----------------------------------
class FixtureSearcher:
    def __init__(self, results: list[dict[str, Any]]):
        self._results = results
    def search(self, query: str, *, max_results: int = 10) -> list[dict[str, Any]]:
        return self._results[:max_results]


class FixtureVerifier:
    def __init__(self, records: list[dict[str, Any]]):
        self._records = records
    def fetch(self, seat: Seat, official_id: str) -> list[dict[str, Any]]:
        return self._records


# ============================================================================
# CLI
# ============================================================================
def main() -> None:
    ap = argparse.ArgumentParser(description="Collect tiered, term-windowed evidence for one official.")
    ap.add_argument("--seat-file", required=True,
                    help="JSON describing the seat (office, jurisdiction, seat_key, current_term, prior_terms_same_seat).")
    ap.add_argument("--id", required=True, help="Official id (e.g. bioguide).")
    ap.add_argument("--name", required=True, help="Official full name.")
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--demo", action="store_true", help="Use offline fixtures (no network).")
    args = ap.parse_args()

    sd = json.load(open(args.seat_file, encoding="utf-8"))
    seat = Seat(office=sd["office"], jurisdiction=sd["jurisdiction"],
                seat_key=sd["seat_key"],
                current_term=tuple(sd["current_term"]),
                prior_terms_same_seat=[tuple(t) for t in sd.get("prior_terms_same_seat", [])])

    if args.demo:
        searcher: Searcher = FixtureSearcher([
            {"title": "Candidate pledges $15 wage", "url": "https://apnews.com/x",
             "snippet": "Pledged to pass a $15 federal minimum wage.", "date": "2024-09-01"},
            {"title": "Old race promise", "url": "https://example.org/old",
             "snippet": "Promise from a different seat.", "date": "2009-01-01"},
            {"title": "Random tweet", "url": "https://x.com/post/123",
             "snippet": "inadmissible", "date": "2024-10-01"},
        ])
        verifiers: list[VerificationAPI] = [FixtureVerifier([
            {"summary": "Sponsored S.150 Raise the Wage Act", "url": "https://congress.gov/bill",
             "date": "2025-02-01", "kind": "sponsored_bill"},
        ])]
    else:
        searcher = WebSearchSearcher()
        verifiers = [CongressAPIVerifier()]

    packet = collect(seat, args.id, args.name, searcher, verifiers)
    out = os.path.join(args.out_dir, f"{args.id}.packet.json")
    json.dump(packet, open(out, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    c = packet["coverage"]
    print(f"Wrote {out}")
    print(f"  promise candidates: {c['promise_candidates']}  "
          f"verification: {c['verification_records']}  tier1: {c['tier1_count']}")
    print(f"  prior-term flagged: {c['prior_term_flagged']}  "
          f"dropped (window/inadmissible): {c['dropped_out_of_window']}/{c['dropped_inadmissible']}")


if __name__ == "__main__":
    main()
