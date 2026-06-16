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
from urllib.parse import urlsplit


# Identify ourselves honestly on every outbound request (politeness + so site
# operators can see who is fetching). Shared by the full-text fetcher below.
UA = "PolicyLogic-evidence-collector/1.0 (research; +https://policylogic.io)"


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
#
# CREDIBILITY NOTE: matching is on the URL's HOSTNAME, by exact host or true
# subdomain — never a raw substring of the whole URL. Substring matching let a
# gov string in a path/query (evil.com/?x=congress.gov) or an attacker subdomain
# (congress.gov.phish.ru) masquerade as Tier 1, which would unlock D3/D4 delivery
# credit it hasn't earned. Hostname matching closes that.
TIER1_HOST_SUFFIXES = ("gov", "mil")  # official US government (.gov / .mil)
# Nonpartisan watchdogs live on .gov but play a Tier-2 (authoritative) role.
# Checked BEFORE the generic .gov rule so they aren't over-credited as primary.
TIER2_GOV_HOSTS = ("cbo.gov", "gao.gov", "crsreports.congress.gov")
TIER2_HOSTS = (
    "reuters.com", "apnews.com", "nytimes.com", "washingtonpost.com",
    "wsj.com", "bloomberg.com", "npr.org", "politico.com", "propublica.org",
)
# Social / self-published platforms: never sufficient on their own. (Bluesky
# was previously mislabeled authoritative — it's social media, same as X.)
INADMISSIBLE_HOSTS = ("twitter.com", "x.com", "facebook.com", "instagram.com",
                      "threads.net", "reddit.com", "t.me", "telegram.org",
                      "truthsocial.com", "bsky.app", "tiktok.com")

# Operator-curated extension to the Tier-2 (authoritative) news list. The built-in
# TIER2_HOSTS is national-only, which leaves local/regional reporting — exactly
# where a governor's or mayor's record lives — stuck at Tier 3. Load a vetted list
# of additional outlet hosts (one per line, '#' comments allowed) to promote them.
# This is a TRUST decision, so it is opt-in and operator-maintained, never scraped.
EXTRA_TIER2_HOSTS: set[str] = set()


def load_tier2_allowlist(path: str) -> int:
    """Merge an external newline-delimited host allowlist into EXTRA_TIER2_HOSTS.
    Returns the number of hosts now registered. Missing/empty file is a no-op."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                host = line.split("#", 1)[0].strip().lower().strip(".")
                if host:
                    EXTRA_TIER2_HOSTS.add(host)
    except FileNotFoundError:
        print(f"  [allowlist not found, skipped] {path}", file=sys.stderr)
    return len(EXTRA_TIER2_HOSTS)


def _hostname(url: str) -> str:
    """Lowercased host with no port/userinfo, trailing dot stripped, or '' if the
    URL can't be parsed to a host."""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return host.lower().strip(".")


def _host_matches(host: str, domain: str) -> bool:
    """True iff `host` is exactly `domain` or a subdomain of it. So 'gov' matches
    'www.congress.gov' but NOT 'congress.gov.evil.ru' (parent registrable domain
    is evil.ru) and NOT 'notagov.com'. This is the substring-free guarantee."""
    domain = domain.lower().strip(".")
    return host == domain or host.endswith("." + domain)


def classify_tier(url: str, *, is_official_press_release: bool = False) -> Tier:
    host = _hostname(url)
    if not host:
        # Unparseable / hostless URL: weakest admissible tier, never credited high.
        return Tier.SUPPORTING
    if any(_host_matches(host, d) for d in INADMISSIBLE_HOSTS):
        return Tier.INADMISSIBLE
    # Nonpartisan watchdogs are Tier 2 even though .gov — check before generic .gov.
    if any(_host_matches(host, d) for d in TIER2_GOV_HOSTS):
        return Tier.AUTHORITATIVE
    if any(_host_matches(host, s) for s in TIER1_HOST_SUFFIXES):
        # An official's OWN press release is not sufficient as the sole support
        # for a positive outcome (methodology). When the caller knows the item is
        # such a release, drop it to Tier 3 so the corroboration rule applies
        # downstream instead of auto-crediting it as primary.
        return Tier.SUPPORTING if is_official_press_release else Tier.PRIMARY
    if any(_host_matches(host, d) for d in TIER2_HOSTS):
        return Tier.AUTHORITATIVE
    # Operator-curated local/regional outlets, if an allowlist was loaded.
    if any(_host_matches(host, d) for d in EXTRA_TIER2_HOSTS):
        return Tier.AUTHORITATIVE
    # Unknown domain: supporting, needs corroboration — never Tier 1.
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
    def search(self, query: str, *, max_results: int = 10,
               pages: int = 1) -> list[dict[str, Any]]: ...


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
    fetched: bool = False    # True if `text` is full page body, False if search snippet


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


@dataclass
class CollectorConfig:
    """All scope/depth knobs in one place, so coverage is tuned here (or via CLI
    flags) instead of hunting through code. Bigger numbers = broader/deeper, but
    also more API cost and more low-tier noise — defaults aim at a deeper-than-
    before setting that is still bounded and tier-gated.

    SCOPE (breadth)
      max_results   results requested per query (Brave hard cap is 20)
      pages         result pages to pull per query (pagination beyond page 1)
    DEPTH (how much we actually read)
      max_fetches   total Tier-1/Tier-2 full-page body fetches per official,
                    split across the promise and verification passes
      tier3_fetch   ADDITIONAL bodies fetched from top-ranked Tier-3 results per
                    pass (own cap; never eats the Tier-1/2 budget). 0 = off.
      max_chars     per-page text cap
    STRUCTURED
      congress_limit records per Congress.gov / GovTrack call
    """
    max_results: int = 15
    pages: int = 2
    max_fetches: int = 24
    tier3_fetch: int = 6
    max_chars: int = 8000
    congress_limit: int = 100
    # Path to an operator-curated Tier-2 news allowlist (one host per line). Falls
    # back to the POLICYLOGIC_NEWS_ALLOWLIST env var when unset. None = built-in
    # national list only.
    news_allowlist: str | None = None


# Query banks — generated per official. More framings = more recall. Each entry
# is a template formatted with name/office/jurisdiction. Kept as data so the set
# is easy to extend without touching control flow.
PROMISE_QUERY_TEMPLATES = (
    '"{name}" {office} campaign promise',
    '"{name}" pledge OR "I will" {juris}',
    '"{name}" vowed OR "promised to" {juris}',
    '"{name}" platform OR agenda {office}',
    '"{name}" plan OR proposal {office} {juris}',
    '"{name}" debate transcript {juris}',
    '"{name}" inaugural OR "first day" OR "day one" commitment',
    '"{name}" "first 100 days" OR "if elected" {juris}',
)
VERIFICATION_QUERY_TEMPLATES = (
    '"{name}" {office} signed OR enacted OR passed {juris}',
    '"{name}" executive order OR budget OR policy record {juris}',
    '"{name}" delivered OR failed OR "broke promise" {juris}',
    '"{name}" {office} accomplishment OR outcome OR "track record"',
    '"{name}" voted OR vetoed OR blocked {juris}',
    '"{name}" progress OR "status update" {office} {juris}',
)


def _build_queries(templates, official_name: str, seat: Seat) -> list[str]:
    return [t.format(name=official_name, office=seat.office, juris=seat.jurisdiction)
            for t in templates]


def _search_pass(searcher: Searcher, queries: list[str], *, pass_type: str,
                 source_kind: str, note_suffix: str, seat: Seat,
                 budget: dict[str, int], push: Callable[[EvidenceItem], None],
                 config: CollectorConfig) -> None:
    """Run a set of search queries, fetch full page bodies within budget, and
    push the resulting items.

    Credibility-driven choices:
      1. Tier-1 bodies are fetched BEFORE Tier-2, so when the budget is tight the
         highest-authority text (the kind required for D3/D4) is what we capture.
      2. Tier-3 bodies are fetched only after Tier-1/2 and only up to a SEPARATE
         small cap (config.tier3_fetch) — so we can read local-news / statement
         bodies without ever letting them displace primary-source reads.
      3. A URL seen across multiple queries is fetched at most once — duplicate
         hits reuse the body instead of burning budget re-fetching it.
    Items are still emitted in original discovery order."""
    gathered = [(r, classify_tier(r.get("url", "")))
                for q in queries
                for r in searcher.search(q, max_results=config.max_results,
                                         pages=config.pages)]
    # Fetch primaries first, then authoritative, then (separately capped) tier-3.
    fetch_rank = {Tier.PRIMARY: 0, Tier.AUTHORITATIVE: 1, Tier.SUPPORTING: 2}
    order = sorted(range(len(gathered)),
                   key=lambda i: fetch_rank.get(gathered[i][1], 9))
    full_by_url: dict[str, str] = {}
    tier3_left = config.tier3_fetch
    for i in order:
        r, tier = gathered[i]
        url = r.get("url", "")
        if not url or url in full_by_url:
            continue
        if tier in (Tier.PRIMARY, Tier.AUTHORITATIVE):
            if budget["remaining"] <= 0:
                continue
            full = fetch_full_text(url, max_chars=config.max_chars)
            if full:
                budget["remaining"] -= 1
                full_by_url[url] = full
        elif tier is Tier.SUPPORTING and tier3_left > 0:
            full = fetch_full_text(url, max_chars=config.max_chars)
            if full:
                tier3_left -= 1
                full_by_url[url] = full
    for r, tier in gathered:
        url = r.get("url", "")
        snippet = r.get("snippet") or r.get("title") or ""
        full = full_by_url.get(url, "")
        push(EvidenceItem(
            pass_type=pass_type,
            text=(full or snippet),
            url=url, date=r.get("date", ""),
            tier=tier.value,
            term_origin=term_origin(r.get("date", ""), seat),
            source_kind=source_kind,
            fetched=bool(full),
            note=("Full page text fetched." if full else "Snippet only.") + note_suffix))


def collect(seat: Seat, official_id: str, official_name: str,
            searcher: Searcher, verifiers: list[VerificationAPI],
            *, config: CollectorConfig | None = None) -> dict[str, Any]:
    config = config or CollectorConfig()
    max_fetches = config.max_fetches
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

    # Split the fetch budget so the verification pass — which carries the Tier-1
    # evidence that anchors delivery scores — can't be starved by the promise
    # pass running first. Verification reclaims whatever promises leave unspent.
    promise_budget = {"remaining": max_fetches // 2}

    # ---- PASS 1: PROMISES via tiered web search ----
    # Queries fan out across source intent; the term window is applied per-result
    # by date, since search can't be perfectly time-scoped.
    promise_queries = _build_queries(PROMISE_QUERY_TEMPLATES, official_name, seat)
    _search_pass(searcher, promise_queries, pass_type="promise",
                 source_kind="campaign_statement",
                 note_suffix=" Promise CANDIDATE. The AI step decides if it "
                             "qualifies; the analyst verifies.",
                 seat=seat, budget=promise_budget, push=push, config=config)

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
    verif_budget = {"remaining": max_fetches - max_fetches // 2
                    + promise_budget["remaining"]}
    verification_queries = _build_queries(VERIFICATION_QUERY_TEMPLATES,
                                          official_name, seat)
    _search_pass(searcher, verification_queries, pass_type="verification",
                 source_kind="reported_action",
                 note_suffix=" Verification CANDIDATE from tiered search.",
                 seat=seat, budget=verif_budget, push=push, config=config)

    # Coverage honesty: surface what we have and don't have.
    primaries = sum(1 for i in items if i.tier == Tier.PRIMARY.value)
    promises = [i for i in items if i.pass_type == "promise"]
    verifs = [i for i in items if i.pass_type == "verification"]
    prior = sum(1 for i in items if i.term_origin == "prior_term_same_seat")
    full_text = sum(1 for i in items if i.fetched)

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
            "full_text_fetched": full_text,  # items backed by page body, not just a snippet
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

    def search(self, query: str, *, max_results: int = 10,
               pages: int = 1) -> list[dict[str, Any]]:
        if not self.api_key:
            raise RuntimeError("BRAVE_API_KEY not set. Get a free key at "
                               "https://brave.com/search/api/ or use FixtureSearcher.")
        # Brave rejects some punctuation (e.g. periods in "U.S.") with a 422.
        # Sanitize: drop periods, collapse whitespace, cap length.
        clean = re.sub(r"\.", "", query)
        clean = re.sub(r"\s+", " ", clean).strip()[:380]
        # Pull `pages` result pages and merge, de-duping by URL across pages so
        # pagination adds reach without re-counting the same hit.
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(max(1, pages)):
            for item in self._one_page(clean, max_results, offset=page):
                u = item.get("url", "")
                if u and u in seen:
                    continue
                if u:
                    seen.add(u)
                out.append(item)
        return out

    def _one_page(self, clean_query: str, max_results: int,
                  *, offset: int) -> list[dict[str, Any]]:
        # Reference the requests module via the (possibly injected) handle so the
        # exception classes resolve — `requests` is imported lazily, not at module
        # scope, so `requests.exceptions.*` here would otherwise be a NameError
        # that masked every real HTTP failure.
        req = self._http()
        try:
            r = req.get(
                self.ENDPOINT,
                headers={"Accept": "application/json",
                         "X-Subscription-Token": self.api_key},
                params={"q": clean_query, "count": min(max_results, 20),
                        "offset": offset},
                timeout=30,
            )
            r.raise_for_status()
            return self.parse(r.json(), max_results)
        except req.exceptions.HTTPError as e:
            # One bad query/page must not kill the whole run. Log and skip.
            code = getattr(e.response, "status_code", "?")
            print(f"  [search skipped: HTTP {code}] {clean_query[:60]}", file=sys.stderr)
            return []
        except req.exceptions.RequestException as e:
            print(f"  [search skipped: {type(e).__name__}] {clean_query[:60]}", file=sys.stderr)
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

    def __init__(self, api_key: str | None = None, *, limit: int = 50,
                 requests_module=None):
        self.api_key = api_key or os.environ.get("CONGRESS_API_KEY")
        self.limit = limit
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
        # Sponsored = bills the member authored; cosponsored = bills they signed
        # onto. Both are evidence of action; pull both for fuller coverage.
        out: list[dict[str, Any]] = []
        for endpoint, key, role in (
            ("sponsored-legislation", "sponsoredLegislation", "Sponsored"),
            ("cosponsored-legislation", "cosponsoredLegislation", "Cosponsored"),
        ):
            try:
                r = self._http().get(
                    f"{self.BASE}/member/{official_id}/{endpoint}",
                    params={"api_key": self.api_key, "limit": self.limit},
                    headers={"Accept": "application/json"}, timeout=30,
                )
                r.raise_for_status()
                out.extend(self.parse(r.json(), key=key, role=role))
            except Exception as e:  # noqa: BLE001 — one endpoint failing must not kill the run
                out.append({"summary": f"Congress.gov {role.lower()} fetch failed: {e}",
                            "url": "", "date": "", "kind": "note", "note": ""})
        return out

    @staticmethod
    def parse(payload: dict[str, Any], *, key: str = "sponsoredLegislation",
              role: str = "Sponsored") -> list[dict[str, Any]]:
        out = []
        for b in payload.get(key, []) or []:
            num = f"{b.get('type','')}{b.get('number','')}".strip()
            out.append({
                "summary": f"{role} {num}: {b.get('title','(untitled)')}",
                "url": b.get("url", ""),
                "date": (b.get("introducedDate") or "")[:10],
                "kind": f"{role.lower()}_bill",
                "note": f"Congress.gov {role.lower()} legislation — evidence of "
                        "action, not delivery itself; confirm outcome.",
            })
        return out


class GovTrackVerifier:
    """Verification via GovTrack (no key). Returns recent votes by the member as
    action records. Complements Congress.gov sponsored bills."""
    BASE = "https://www.govtrack.us/api/v2"

    def __init__(self, *, limit: int = 50, requests_module=None):
        self.limit = limit
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
                                  params={"person": pid, "limit": self.limit,
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
    def search(self, query: str, *, max_results: int = 10,
               pages: int = 1) -> list[dict[str, Any]]:
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
    # Scope/depth knobs (default to CollectorConfig's values). Raising these
    # broadens coverage and reads deeper, at higher API cost.
    _cfg = CollectorConfig()
    ap.add_argument("--max-results", type=int, default=_cfg.max_results,
                    help="Search results requested per query (Brave cap 20).")
    ap.add_argument("--pages", type=int, default=_cfg.pages,
                    help="Result pages to pull per query.")
    ap.add_argument("--max-fetches", type=int, default=_cfg.max_fetches,
                    help="Total Tier-1/Tier-2 full-page fetches per official.")
    ap.add_argument("--tier3-fetch", type=int, default=_cfg.tier3_fetch,
                    help="Extra top-ranked Tier-3 bodies fetched per pass (0=off).")
    ap.add_argument("--max-chars", type=int, default=_cfg.max_chars,
                    help="Per-page extracted-text cap.")
    ap.add_argument("--congress-limit", type=int, default=_cfg.congress_limit,
                    help="Records per Congress.gov / GovTrack call.")
    ap.add_argument("--news-allowlist", default=None,
                    help="Path to a curated Tier-2 news host allowlist (one host "
                         "per line). Defaults to $POLICYLOGIC_NEWS_ALLOWLIST.")
    args = ap.parse_args()

    config = CollectorConfig(
        max_results=args.max_results, pages=args.pages,
        max_fetches=args.max_fetches, tier3_fetch=args.tier3_fetch,
        max_chars=args.max_chars, congress_limit=args.congress_limit,
        news_allowlist=args.news_allowlist)

    allowlist = config.news_allowlist or os.environ.get("POLICYLOGIC_NEWS_ALLOWLIST")
    if allowlist:
        n = load_tier2_allowlist(allowlist)
        print(f"  [tier-2 allowlist] {n} operator-curated outlet(s) registered")

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
            verifiers = [CongressAPIVerifier(limit=config.congress_limit),
                         GovTrackVerifier(limit=config.congress_limit)]
        else:
            verifiers = []  # tiered search verification still runs in collect()

    packet = collect(seat, args.id, args.name, searcher, verifiers, config=config)
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
