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
import re
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
# ============================================================================
# Full-content fetching — turn a search result into the actual page text.
# Search gives one-line snippets; the real promises/evidence live in the page
# body. We fetch and extract readable text for credible-tier results only
# (Tier 1 and 2), since fetching weak sources wastes time and adds noise.
# ============================================================================
def fetch_full_text(url: str, *, max_chars: int = 6000) -> str:
    """Return clean readable text from a page, or '' on any failure.
    Uses trafilatura if installed (best extraction); falls back to a simple
    HTML-strip if not. Never raises — a fetch failure just means we keep the
    snippet."""
    if not url:
        return ""
    try:
        try:
            import trafilatura
            downloaded = trafilatura.fetch_url(url)
            if downloaded:
                text = trafilatura.extract(downloaded, include_comments=False,
                                           include_tables=False) or ""
                return text.strip()[:max_chars]
        except ImportError:
            pass
        # Fallback: fetch + crude tag strip.
        import re as _re
        import requests
        r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
        r.raise_for_status()
        html = r.text
        html = _re.sub(r"<script.*?</script>", " ", html, flags=_re.S | _re.I)
        html = _re.sub(r"<style.*?</style>", " ", html, flags=_re.S | _re.I)
        text = _re.sub(r"<[^>]+>", " ", html)
        text = _re.sub(r"\s+", " ", text)
        return text.strip()[:max_chars]
    except Exception:  # noqa: BLE001 — fetch failure must never break collection
        return ""


def collect(seat: Seat, official_id: str, official_name: str,
            searcher: Searcher, verifiers: list[VerificationAPI],
            *, max_fetches: int = 12) -> dict[str, Any]:
    items: list[EvidenceItem] = []
    seen_urls: set[str] = set()
    fetch_budget = {"remaining": max_fetches}
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
            snippet = r.get("snippet") or r.get("title") or ""
            # For credible sources, pull the full page text — the actual promise
            # statements live in the body, not the one-line snippet.
            full = ""
            if tier in (Tier.PRIMARY, Tier.AUTHORITATIVE) and fetch_budget["remaining"] > 0:
                full = fetch_full_text(url)
                if full:
                    fetch_budget["remaining"] -= 1
            push(EvidenceItem(
                pass_type="promise",
                text=(full or snippet),
                url=url, date=r.get("date", ""),
                tier=tier.value,
                term_origin=term_origin(r.get("date", ""), seat),
                source_kind="campaign_statement",
                note=("Full page text fetched." if full else "Snippet only.") +
                     " Promise CANDIDATE. The AI step decides if it qualifies; "
                     "the analyst verifies."))

    # ---- PASS 2: VERIFICATION ----
    # (a) Structured APIs where they exist (Congress) — high-confidence bonus.
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

    # (b) Tiered web-search verification — runs for EVERY official, so governors,
    # mayors, and the president get verification too, not just Congress. Searches
    # for evidence of action on commitments; tier ranking handles authority.
    verification_queries = [
        f'"{official_name}" {seat.office} signed OR enacted OR passed {seat.jurisdiction}',
        f'"{official_name}" executive order OR budget OR policy record {seat.jurisdiction}',
        f'"{official_name}" delivered OR failed OR broke promise {seat.jurisdiction}',
        f'"{official_name}" {seat.office} accomplishment OR outcome',
        f'"{official_name}" voted OR vetoed OR blocked {seat.jurisdiction}',
    ]
    for q in verification_queries:
        for r in searcher.search(q, max_results=10):
            url = r.get("url", "")
            tier = classify_tier(url)
            snippet = r.get("snippet") or r.get("title") or ""
            full = ""
            if tier in (Tier.PRIMARY, Tier.AUTHORITATIVE) and fetch_budget["remaining"] > 0:
                full = fetch_full_text(url)
                if full:
                    fetch_budget["remaining"] -= 1
            push(EvidenceItem(
                pass_type="verification",
                text=(full or snippet),
                url=url, date=r.get("date", ""),
                tier=tier.value,
                term_origin=term_origin(r.get("date", ""), seat),
                source_kind="reported_action",
                note=("Full page text fetched." if full else "Snippet only.") +
                     " Verification CANDIDATE from tiered search."))

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
    """Brave Search API adapter. Free tier; needs BRAVE_API_KEY.
    Provider is swappable: this class is the only thing to replace to switch to
    Tavily/SerpAPI later — the collector only depends on the .search() shape.

    Returns: list of {title, url, snippet, date}.
    """
    ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str | None = None, *, requests_module=None):
        self.api_key = api_key or os.environ.get("BRAVE_API_KEY")
        # injectable for testing; real use imports requests lazily
        self._requests = requests_module

    def _http(self):
        if self._requests:
            return self._requests
        import requests  # lazy: keeps module importable without the dep
        return requests

    def search(self, query: str, *, max_results: int = 10) -> list[dict[str, Any]]:
        if not self.api_key:
            raise RuntimeError("BRAVE_API_KEY not set. Get a free key at "
                               "https://brave.com/search/api/ or use FixtureSearcher.")
        # Brave rejects some punctuation (e.g. periods in "U.S.") with a 422.
        # Sanitize: drop periods, collapse whitespace, cap length.
        clean = re.sub(r"\.", "", query)
        clean = re.sub(r"\s+", " ", clean).strip()[:380]
        try:
            r = self._http().get(
                self.ENDPOINT,
                headers={"Accept": "application/json",
                         "X-Subscription-Token": self.api_key},
                params={"q": clean, "count": min(max_results, 20)},
                timeout=30,
            )
            r.raise_for_status()
            return self.parse(r.json(), max_results)
        except requests.exceptions.HTTPError as e:
            # One bad query must not kill the whole run. Log and skip.
            code = getattr(e.response, "status_code", "?")
            print(f"  [search skipped: HTTP {code}] {clean[:60]}", file=sys.stderr)
            return []
        except requests.exceptions.RequestException as e:
            print(f"  [search skipped: {type(e).__name__}] {clean[:60]}", file=sys.stderr)
            return []

    @staticmethod
    def parse(payload: dict[str, Any], max_results: int = 10) -> list[dict[str, Any]]:
        """Pure parser — unit-testable without network. Maps Brave's response to
        the collector's {title,url,snippet,date} shape."""
        results = (payload.get("web") or {}).get("results") or []
        out = []
        for item in results[:max_results]:
            out.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("description", ""),
                # Brave exposes age/page_age when available (e.g. "2024-09-01").
                "date": (item.get("page_age") or item.get("age") or "")[:10],
            })
        return out


class CongressAPIVerifier:
    """Verification via Congress.gov v3 (needs CONGRESS_API_KEY, free at
    api.data.gov). Returns sponsored legislation as action records."""
    BASE = "https://api.congress.gov/v3"

    def __init__(self, api_key: str | None = None, *, requests_module=None):
        self.api_key = api_key or os.environ.get("CONGRESS_API_KEY")
        self._requests = requests_module

    def _http(self):
        if self._requests:
            return self._requests
        import requests
        return requests

    def fetch(self, seat: Seat, official_id: str) -> list[dict[str, Any]]:
        if not self.api_key:
            return [{"summary": "CONGRESS_API_KEY not set — Congress.gov skipped.",
                     "url": "", "date": "", "kind": "note",
                     "note": "Free key at https://api.data.gov/signup/"}]
        r = self._http().get(
            f"{self.BASE}/member/{official_id}/sponsored-legislation",
            params={"api_key": self.api_key, "limit": 50},
            headers={"Accept": "application/json"}, timeout=30,
        )
        r.raise_for_status()
        return self.parse(r.json())

    @staticmethod
    def parse(payload: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for b in payload.get("sponsoredLegislation", []) or []:
            num = f"{b.get('type','')}{b.get('number','')}".strip()
            out.append({
                "summary": f"Sponsored {num}: {b.get('title','(untitled)')}",
                "url": b.get("url", ""),
                "date": (b.get("introducedDate") or "")[:10],
                "kind": "sponsored_bill",
                "note": "Congress.gov sponsored legislation — evidence of action, "
                        "not delivery itself; confirm outcome.",
            })
        return out


class GovTrackVerifier:
    """Verification via GovTrack (no key). Returns recent votes by the member as
    action records. Complements Congress.gov sponsored bills."""
    BASE = "https://www.govtrack.us/api/v2"

    def __init__(self, *, requests_module=None):
        self._requests = requests_module

    def _http(self):
        if self._requests:
            return self._requests
        import requests
        return requests

    def fetch(self, seat: Seat, official_id: str) -> list[dict[str, Any]]:
        # GovTrack keys people by its own id; resolve from bioguide first.
        try:
            pr = self._http().get(f"{self.BASE}/person",
                                  params={"bioguideid": official_id}, timeout=30)
            pr.raise_for_status()
            people = pr.json().get("objects", [])
            if not people:
                return [{"summary": "GovTrack: member not found by bioguide.",
                         "url": "", "date": "", "kind": "note", "note": ""}]
            pid = people[0]["id"]
            vr = self._http().get(f"{self.BASE}/vote_voter",
                                  params={"person": pid, "limit": 50,
                                          "sort": "-created"}, timeout=30)
            vr.raise_for_status()
            return self.parse(vr.json())
        except Exception as e:  # noqa: BLE001
            return [{"summary": f"GovTrack fetch failed: {e}", "url": "",
                     "date": "", "kind": "note", "note": ""}]

    @staticmethod
    def parse(payload: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for v in payload.get("objects", []) or []:
            vote = v.get("vote", {})
            out.append({
                "summary": f"Voted {v.get('option', {}).get('value','')} on "
                           f"{vote.get('question','(vote)')}",
                "url": vote.get("link", "") or
                       f"https://www.govtrack.us{vote.get('url','')}",
                "date": (vote.get("created") or "")[:10],
                "kind": "vote",
                "note": "GovTrack roll-call vote.",
            })
        return out


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
        # Structured APIs apply only to Congress. Other offices (Governor, Mayor,
        # President) have no universal structured source, so they rely on the
        # tiered web-search verification pass — which runs for everyone.
        office_l = seat.office.lower()
        if "senator" in office_l or "representative" in office_l or "congress" in office_l:
            verifiers = [CongressAPIVerifier(), GovTrackVerifier()]
        else:
            verifiers = []  # tiered search verification still runs in collect()

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
