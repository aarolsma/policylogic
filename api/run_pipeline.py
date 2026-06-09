#!/usr/bin/env python3
"""
run_pipeline.py — Step 2 of the PolicyLogic pipeline: the Claude API call.

Takes ONE collected senator record (e.g. S000033.json from scrape_senators.py),
sends its facts + source material to Claude using the bucket-assignment prompt,
then hands the model's JSON to the validation adapter, which scores it
deterministically. Nothing is published — output is an AI DRAFT for human review.

PIPELINE POSITION:
    scrape_senators.py  ->  [THIS SCRIPT + Claude]  ->  adapter -> engine  ->  human review

WHAT THIS SCRIPT CANNOT DO:
    The scraper emits source *seeds* (links to look at), not promise text. Claude
    can only classify evidence it is given. So the collected record must carry a
    "source_material" field containing the actual campaign statements and
    delivery evidence you have gathered, each with a citation id. Until you add
    that, use --demo to exercise the wiring with a clearly-marked sample.

USAGE:
    export ANTHROPIC_API_KEY=sk-ant-...
    python3 run_pipeline.py senators/S000033.json
    python3 run_pipeline.py senators/S000033.json --demo   # inject sample evidence
    python3 run_pipeline.py senators/S000033.json --out-dir scored/

REQUIREMENTS:
    pip install anthropic
"""

from __future__ import annotations
import argparse
import json
import os
import re
import sys
from typing import Any

import adapter  # local: validation + scoring

PROMPT_FILE = "ai_bucket_assignment_prompt.md"
MODEL = "claude-opus-4-20250514"   # set to your chosen scoring model; logged in output
MAX_TOKENS = 8000


# ---------------------------------------------------------------------------
# Prompt loading — the .md file is the single source of truth. We slice the two
# fenced code blocks (system prompt, user template) out of it at runtime so the
# script never holds a second, drifting copy of the rubric.
# ---------------------------------------------------------------------------
def load_prompt(path: str) -> tuple[str, str]:
    text = open(path, encoding="utf-8").read()
    blocks = re.findall(r"```(.*?)```", text, flags=re.S)
    # Find the system block (contains "NON-NEGOTIABLE RULES") and the user block
    # (contains the three {{slots}}).
    system = next((b.strip() for b in blocks if "NON-NEGOTIABLE RULES" in b), None)
    user = next((b.strip() for b in blocks
                 if "{{official_block}}" in b and "{{source_material}}" in b), None)
    if not system or not user:
        sys.exit(f"Could not locate system/user blocks in {path}. "
                 "Check the prompt file's fenced code blocks are intact.")
    return system, user


# ---------------------------------------------------------------------------
# Build the three prompt fill-values directly from a collector PACKET.
# The packet (from collect_evidence.py) is the single data vocabulary:
#   official: {id, name, office, jurisdiction, current_term, prior_terms_same_seat}
#   items:    [{cid, pass_type, text, url, date, tier, term_origin, source_kind}]
#   coverage: {...}
# ---------------------------------------------------------------------------
TIER_LABEL = {
    "tier1_primary": "TIER 1 (primary/official)",
    "tier2_authoritative": "TIER 2 (authoritative)",
    "tier3_supporting": "TIER 3 (supporting — needs corroboration)",
}


def official_block(packet: dict[str, Any]) -> str:
    o = packet.get("official", {})
    term = o.get("current_term") or ["", ""]
    return (f"Name: {o.get('name')}\n"
            f"Office: {o.get('office')}\n"
            f"Jurisdiction: {o.get('jurisdiction')}\n"
            f"Current term: {term[0]} to {term[1]}\n"
            f"ID: {o.get('id')}")


def context_block(packet: dict[str, Any]) -> str:
    o = packet.get("official", {})
    return json.dumps({
        "current_term": o.get("current_term"),
        "prior_terms_same_seat": o.get("prior_terms_same_seat", []),
        "coverage": packet.get("coverage", {}),
    }, indent=2, ensure_ascii=False)


def source_block(packet: dict[str, Any], demo: bool) -> str:
    items = packet.get("items", [])
    if not items and demo:
        return _DEMO_SOURCE_MATERIAL
    if not items:
        sys.exit("Packet has no items. Run collect_evidence.py first to gather "
                 "evidence, or pass --demo to test the wiring.")
    promises = [i for i in items if i.get("pass_type") == "promise"]
    verifs = [i for i in items if i.get("pass_type") == "verification"]
    lines = ["Tier-1 sources are REQUIRED before any D3/D4 delivery score.", "",
             "PROMISE CANDIDATES (campaign-era commitments — you decide which qualify):"]
    for i in promises:
        lines.append(f"[{i['cid']}] {TIER_LABEL.get(i['tier'], i['tier'])} | "
                     f"{i.get('term_origin')} | {i.get('date','')}")
        lines.append(f"      {i.get('text','')}")
        lines.append(f"      source: {i.get('url','')}")
    lines += ["", "VERIFICATION EVIDENCE (record of action on commitments):"]
    for i in verifs:
        lines.append(f"[{i['cid']}] {TIER_LABEL.get(i['tier'], i['tier'])} | "
                     f"{i.get('source_kind','')} | {i.get('date','')}")
        lines.append(f"      {i.get('text','')}")
        lines.append(f"      source: {i.get('url','')}")
    return "\n".join(lines)


# A clearly-fictional sample for --demo wiring tests only. NOT real data.
_DEMO_SOURCE_MATERIAL = """\
[DEMO / FICTIONAL EVIDENCE — for wiring tests only, not a real record]

[c1] TIER 3 | current_term | 2018: "I will pass a federal $15 minimum wage."
[c2] TIER 1 | sponsored_bill | 2021: introduced Raise the Wage Act; passed committee.
[c4] TIER 1 | current_term | 2018: "I will not vote for any bill that cuts Medicare."
[c5] TIER 1 | vote | 2023: no votes for Medicare-cutting legislation this term.
"""


# ---------------------------------------------------------------------------
# Claude call
# ---------------------------------------------------------------------------
def call_claude(system: str, user: str) -> str:
    try:
        from anthropic import Anthropic
    except ImportError:
        sys.exit("The 'anthropic' package is required: pip install anthropic")

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("Set ANTHROPIC_API_KEY in your environment before running.")

    client = Anthropic(api_key=key)
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    # Concatenate text blocks.
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")


def extract_json(raw: str) -> str:
    """The prompt demands JSON-only, but strip stray fences defensively."""
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.S).strip()
    return s


# ---------------------------------------------------------------------------
# Assemble the CANONICAL scorecard (contract shape) by merging the AI's
# tangible record (status, what_happened, sources) with the engine's computed
# math. The engine output is authoritative for all numbers; the AI is
# authoritative for the human-readable record. Neither overwrites the other.
# ---------------------------------------------------------------------------
def _slugify(name: str, role: str, juris: str) -> str:
    base = f"{name}-{juris}-{role}".lower()
    return re.sub(r"[^a-z0-9]+", "-", base).strip("-")


def assemble_canonical_card(packet: dict, payload: dict, graded: dict) -> dict:
    o = packet.get("official", {})
    sc = graded.get("scorecard", {})
    ai_promises = {p.get("id"): p for p in payload.get("promises", [])}
    # engine returns per-promise computed math under "per_promise", keyed by id
    computed = [c for c in sc.get("per_promise", []) if c.get("scored")]

    promises = []
    for i, comp in enumerate(computed):
        pid = comp.get("id") or f"p{i+1}"
        ai = ai_promises.get(pid, {})
        promises.append({
            "id": pid,
            "text": ai.get("promise_text", ""),
            "promise_type": ai.get("promise_type"),
            "domain_label": ai.get("domain_label", ""),
            # Layer 1 — verdict
            "status": ai.get("status"),
            "flags": ai.get("flags", []),
            "flag_rationales": ai.get("flag_rationales", {}),
            # Layer 2 — tangible record
            "what_happened": ai.get("what_happened", []),
            "sources": ai.get("sources", []),
            "confidence": ai.get("confidence", "Medium"),
            # Layer 3 — buckets (AI) + computed math (engine, authoritative)
            "delivery": comp.get("delivery_code_final", ai.get("delivery")),
            "their_role": ai.get("their_role"),
            "difficulty": ai.get("difficulty"),
            "scale": ai.get("scale"),
            "magnitude": ai.get("magnitude"),
            "clarity": ai.get("clarity"),
            "adjusted_delivery": comp.get("adjusted_delivery"),
            "difficulty_earned": comp.get("difficulty_earned"),
            "impact": comp.get("impact"),
            "promise_score": comp.get("promise_score"),
        })

    return {
        "schema": "policylogic/scorecard/v2",
        "slug": _slugify(o.get("name", ""), o.get("office", ""), o.get("jurisdiction", "")),
        "official": {
            "name": o.get("name"),
            "role": o.get("office"),
            "jurisdiction": o.get("jurisdiction"),
            "party": o.get("party", ""),
            "term_start": (o.get("current_term") or ["", ""])[0],
            "term_end": (o.get("current_term") or ["", ""])[1],
        },
        "summary": {
            "overall_grade": sc.get("grade"),
            "grade_input_pct": sc.get("grade_input_pct"),
            "delivery_ratio": sc.get("delivery_ratio"),
            "promise_ratio": sc.get("promise_ratio"),
            "total_promises": len(promises),
            "narrative": payload.get("card_level_notes", ""),
            "card_flags": sc.get("card_flags", []),
        },
        "promises": promises,
        "data_gaps": payload.get("data_gaps", []),
        "_meta": {
            "generated_at": __import__("datetime").date.today().isoformat(),
            "model": MODEL,
            "review_state": "AI DRAFT — pending human review",
            "archived": False,
            "sources": payload.get("card_sources", []),
        },
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def run(packet_path: str, demo: bool, out_dir: str | None) -> dict[str, Any]:
    packet = json.load(open(packet_path, encoding="utf-8"))
    system, user_tmpl = load_prompt(PROMPT_FILE)

    user = (user_tmpl
            .replace("{{official_block}}", official_block(packet))
            .replace("{{context_panel}}", context_block(packet))
            .replace("{{source_material}}", source_block(packet, demo)))

    print(f"Calling {MODEL} for {packet.get('official', {}).get('name')} ...",
          file=sys.stderr)
    raw = call_claude(system, user)
    payload = extract_json(raw)

    # Validate + score. The adapter refuses to grade if anything is malformed.
    result = adapter.grade_if_clean(payload)

    # Attach provenance the methodology's AI Pipeline page requires.
    result["_meta"] = {
        "official_id": packet.get("official", {}).get("id"),
        "model": MODEL,
        "demo_evidence": demo,
        "raw_model_output": payload,   # keep for audit / re-validation
    }

    official_id = packet.get("official", {}).get("id", "record")
    out_dir = out_dir or os.path.dirname(packet_path) or "."
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{official_id}.scored.json")
    json.dump(result, open(out_path, "w", encoding="utf-8"), indent=2, ensure_ascii=False)

    # When the card grades cleanly, also emit the CANONICAL card the site reads.
    if result.get("graded"):
        card = assemble_canonical_card(packet, payload, result)
        card_path = os.path.join(out_dir, f"{card['slug']}.json")
        json.dump(card, open(card_path, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
        print(f"Canonical card: {card_path}", file=sys.stderr)

    # Human-readable summary.
    if result.get("graded"):
        sc = result["scorecard"]
        print(f"\nGraded: {sc['grade']}  ({sc['grade_input_pct']}%)  "
              f"[{sc['n_scored']} promises]  flags: {sc.get('card_flags')}")
        print("  -> AI DRAFT. Requires human review before publication.")
    else:
        print(f"\nNOT graded: {result.get('reason')}")
        if result.get("card_errors"):
            print("  card errors:", result["card_errors"])
        for r in result.get("rejected", []):
            print(f"  rejected {r['id']}: {r['errors']}")
    print(f"\nWritten: {out_path}")
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="Run one collected record through Claude + scoring.")
    ap.add_argument("packet", help="Path to a collector packet JSON (e.g. S000033.packet.json)")
    ap.add_argument("--demo", action="store_true",
                    help="Inject fictional sample evidence to test the wiring.")
    ap.add_argument("--out-dir", default=None, help="Where to write the scored output.")
    args = ap.parse_args()
    run(args.packet, args.demo, args.out_dir)


if __name__ == "__main__":
    main()
