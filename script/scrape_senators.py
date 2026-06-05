#!/usr/bin/env python3
"""
scrape_senators.py — PolicyLogic Methodology v2 data collector (Senators)

DESIGN BOUNDARY (Methodology §12.2 / §12.3, and PolicyLogic core constraint):
    This script COLLECTS and STRUCTURES raw material only. It does NOT identify
    promises, assign Delivery/Difficulty/Impact/Clarity buckets, fire flags, or
    compute grades. Every scored field is emitted empty for the AI + deterministic
    scoring stage and human review. The scraper surfaces; it does not conclude.

Two-stage pipeline:
    Stage 1  Roster + structured facts   -> reliable structured sources
    Stage 2  Promise-source seeding      -> candidate documents only (no extraction)

OUTPUT: one JSON file per senator at /mnt/user-data/outputs/senators/<bioguide>.json
        shaped to the Methodology v2 scorecard INPUT (pre-scoring).
"""

from __future__ import annotations
import json
import os
import sys
import time
import datetime as dt
from dataclasses import dataclass, field, asdict
from typing import Any
from urllib.parse import quote

import requests
import yaml

OUT_DIR = "/mnt/user-data/outputs/senators"
USER_AGENT = "PolicyLogic-Methodology-v2-collector/1.0 (research; contact: policylogic.io)"
REQUEST_PAUSE = 0.6  # be polite to public endpoints

# ---- Stable structured sources ---------------------------------------------
# @unitedstates project: public-domain bulk legislator data (no key required).
# The project distributes current legislators as YAML on the main branch.
# Served via github.com (which redirects to the raw host) for allow-list reasons.
LEGISLATORS_CURRENT = (
    "https://github.com/unitedstates/"
    "congress-legislators/raw/main/legislators-current.yaml"
)
# Wayback availability API: lets us seed archived campaign-site captures
# without guessing live URLs that are usually dead post-election.
WAYBACK_AVAILABLE = "https://archive.org/wayback/available?url={url}&timestamp={ts}"


# ============================================================================
# Schema — mirrors Methodology v2. Scored fields intentionally left empty.
# ============================================================================
def empty_promise(source_url: str = "", note: str = "") -> dict[str, Any]:
    """A promise skeleton. Every judgment field is null/empty for the
    scoring stage (§12.2). The scraper only attaches candidate sources."""
    return {
        "promise_text": None,          # filled by AI promise-identification, not here
        "promise_type": None,          # Quantitative | Qualitative | Negative (§2)
        "domain": None,                # self-declared priority domain (§1.4)
        "clarity": None,               # 1-5 (§7) — assigned downstream
        "timeline_bucket": None,       # Immediate/Short/Mid/Long (§5.1)
        "delivery": None,              # D0-D4 (§3.1)
        "their_role": None,            # 0.0-1.0 (§6)
        "difficulty": None,            # H1-H3 (§3.2)
        "scale": None,                 # S1-S3 (§3.3)
        "magnitude": None,             # M1-M3 (§3.3)
        "behavioral_flags": [],        # (§3.1 / §9)
        "transparency_flags": [],      # (§10)
        "evidence_summary": None,      # one-sentence summary written downstream
        "sources": ([{"url": source_url, "kind": "seed", "note": note}]
                    if source_url else []),
    }


@dataclass
class ContextPanel:
    """Methodology §8. Factual, sourced fields. No editorial characterization."""
    term_stage: dict[str, Any] = field(default_factory=dict)        # pct + date range
    legislature_control: dict[str, Any] = field(default_factory=dict)
    inherited_conditions: list[str] = field(default_factory=list)
    office_power_constraints: list[str] = field(default_factory=list)
    major_external_events: list[str] = field(default_factory=list)
    promise_disclosure: str = ""  # §1.3 mandatory disclosure, finalized downstream


def http_get(url: str, *, as_json: bool = True, as_yaml: bool = False, tries: int = 3) -> Any:
    headers = {"User-Agent": USER_AGENT}
    last = None
    for attempt in range(tries):
        try:
            r = requests.get(url, headers=headers, timeout=60, allow_redirects=True)
            r.raise_for_status()
            time.sleep(REQUEST_PAUSE)
            if as_yaml:
                return yaml.safe_load(r.text)
            return r.json() if as_json else r.text
        except Exception as e:  # noqa: BLE001 — collector should be resilient
            last = e
            time.sleep(1.5 * (attempt + 1))
    print(f"  ! failed: {url}  ({last})", file=sys.stderr)
    return None


# ============================================================================
# STAGE 1 — roster + structured facts
# ============================================================================
def fetch_current_senators() -> list[dict[str, Any]]:
    data = http_get(LEGISLATORS_CURRENT, as_yaml=True)
    if not data:
        sys.exit("Could not load legislator roster. Check network allow-list "
                 "for raw.githubusercontent.com.")
    sens = []
    for person in data:
        term = person.get("terms", [])[-1] if person.get("terms") else {}
        if term.get("type") != "sen":
            continue
        sens.append((person, term))
    return sens


def build_context_panel(term: dict[str, Any]) -> ContextPanel:
    """Populate only the fields derivable from structured roster data.
    Legislature control, inherited conditions, external events, and power
    constraints are seeded as TODO markers for sourced human/AI completion —
    we do not fabricate them."""
    start = term.get("start")
    end = term.get("end")
    cp = ContextPanel()
    cp.term_stage = _term_stage(start, end)
    cp.legislature_control = {
        "value": None,
        "_todo": "Senate party control + alignment for this term — needs sourced fill",
    }
    cp.office_power_constraints = [
        "_todo: enumerate Senate-specific constraints relevant to tracked promises",
    ]
    cp.inherited_conditions = []
    cp.major_external_events = []
    cp.promise_disclosure = ""  # §1.3 — completed after promise set is finalized
    return cp


def _term_stage(start: str | None, end: str | None) -> dict[str, Any]:
    if not (start and end):
        return {"pct_elapsed": None, "date_range": [start, end]}
    try:
        s = dt.date.fromisoformat(start)
        e = dt.date.fromisoformat(end)
        today = dt.date.today()
        total = (e - s).days
        elapsed = (min(today, e) - s).days
        pct = round(100 * max(0, elapsed) / total, 1) if total else None
    except ValueError:
        pct = None
    return {"pct_elapsed": pct, "date_range": [start, end]}


# ============================================================================
# STAGE 2 — promise-source SEEDING (candidate documents only, no extraction)
# ============================================================================
def seed_promise_sources(person: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect candidate source documents where campaign-era promises are
    likely to live. We do NOT read or extract promises here — we hand the
    scoring stage a list of places to look, each with provenance."""
    seeds: list[dict[str, Any]] = []
    name = person.get("name", {})
    full = name.get("official_full") or f"{name.get('first','')} {name.get('last','')}".strip()

    # 2a. Archived campaign site (Wayback), if any campaign URL is on record.
    campaign_url = None
    for url in person.get("other_names", []):  # placeholder; campaign URLs rarely in feed
        pass
    # The roster doesn't carry campaign URLs; we record the search vector instead.
    seeds.append({
        "kind": "campaign_site_archive",
        "lookup": WAYBACK_AVAILABLE.format(
            url=quote(f"{name.get('last','').lower()}forsenate.com"),
            ts="2024",
        ),
        "note": "Candidate Wayback capture of a likely campaign domain. Verify the "
                "domain actually belonged to this official before use.",
        "resolved_snapshot": None,
    })

    # 2b. GovTrack profile (votes, sponsored bills) — delivery-evidence vector.
    bioguide = person.get("id", {}).get("bioguide")
    if bioguide:
        seeds.append({
            "kind": "voting_and_bills",
            "lookup": f"https://www.govtrack.us/congress/members/{bioguide}",
            "note": "Sponsored/cosponsored legislation and votes — delivery evidence "
                    "for qualitative/quantitative promises. Not promise text.",
        })

    # 2c. News-archive search vector for campaign statements.
    seeds.append({
        "kind": "news_archive_query",
        "lookup": f'"{full}" campaign promise OR "I will" OR pledge',
        "note": "Search vector for campaign/inaugural statements (§1.1 Attributable). "
                "Press responses and in-office announcements do not qualify (§1.1).",
    })

    # Resolve the Wayback availability call where we have a concrete lookup URL.
    wb = http_get(seeds[0]["lookup"])
    if wb:
        snap = (wb.get("archived_snapshots") or {}).get("closest")
        seeds[0]["resolved_snapshot"] = snap  # may be None if no capture exists
    return seeds


# ============================================================================
# Assemble one scorecard-INPUT record per senator
# ============================================================================
def build_record(person: dict[str, Any], term: dict[str, Any]) -> dict[str, Any]:
    ids = person.get("id", {})
    name = person.get("name", {})
    cp = build_context_panel(term)
    return {
        "schema": "policylogic.methodology.v2/scorecard-input",
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "official": {
            "bioguide": ids.get("bioguide"),
            "full_name": name.get("official_full"),
            "office": "U.S. Senator",
            "state": term.get("state"),
            "party": term.get("party"),
            "term": {"start": term.get("start"), "end": term.get("end")},
        },
        "context_panel": asdict(cp),
        # §1.3 no cap; promises are added by the identification stage.
        "promises": [],
        # Candidate sources for that stage to work from.
        "promise_source_seeds": seed_promise_sources(person),
        "scoring_status": {
            "stage": "collected",          # collected -> identified -> scored -> reviewed
            "ai_model": None,              # logged at generation (§12.1)
            "human_reviewed": False,       # §AI-draft until reviewed
            "transparency_flags": ["AI DRAFT"],  # §10 default until review
        },
        "_collector_notes": [
            "Scored fields are intentionally empty (§12.2/§12.3).",
            "promise_source_seeds are leads, not verified promises.",
            "context_panel fields marked _todo require sourced completion.",
        ],
    }


def main(limit: int | None = None) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    senators = fetch_current_senators()
    if limit:
        senators = senators[:limit]
    print(f"Collecting {len(senators)} senators -> {OUT_DIR}")
    for i, (person, term) in enumerate(senators, 1):
        rec = build_record(person, term)
        bioguide = rec["official"]["bioguide"] or f"unknown_{i}"
        path = os.path.join(OUT_DIR, f"{bioguide}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=2, ensure_ascii=False)
        print(f"  [{i:>3}] {rec['official']['full_name']:<28} -> {bioguide}.json")
    print("Done. All scored fields left empty for the scoring pipeline + human review.")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else None
    main(limit=n)
