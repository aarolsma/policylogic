#!/usr/bin/env python3
"""
adapter.py — Validation seam between the AI bucket-assignment stage and the
deterministic scoring engine.

ROLE: Parse the AI stage's JSON, reject anything malformed or unsupported by
evidence, and only then map valid promises into engine Promise objects and grade
them. A promise that fails validation is NOT scored — it is returned for human
review. Failing loud is the point: a silently-accepted bad bucket corrupts a
published accountability card.

This module performs NO scoring itself and assigns NO buckets. It validates and
routes.
"""

from __future__ import annotations
import json
from dataclasses import dataclass, field
from typing import Any

from scoring_engine import (
    Promise, grade_scorecard,
    DELIVERY_POINTS, DIFFICULTY_MAX, SCALE_POINTS, MAGNITUDE_POINTS,
    THEIR_ROLE_VALUES,
)

# Behavioral flags the engine acts on (Tables 6 / 20).
BEHAVIORAL_FLAGS = {
    "REVERSED", "REDEFINED", "EXTERNALLY BLOCKED",
    "CREDIT OVERCLAIMED", "DEADLINE SHIFTED", "SCOPE REDUCED",
}
PROMISE_TYPES = {"Quantitative", "Qualitative", "Negative"}


@dataclass
class ValidationResult:
    valid_promises: list[Promise] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)  # {id, errors, raw}
    card_errors: list[str] = field(default_factory=list)
    selection: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return not self.card_errors and not self.rejected


def _reject(rejected: list, pid: str, errors: list[str], raw: Any) -> None:
    rejected.append({"id": pid, "errors": errors, "raw": raw})


def validate_payload(payload: str | dict[str, Any]) -> ValidationResult:
    """Validate one official's AI-stage JSON. Returns a ValidationResult.

    Validation is per-promise: valid promises pass through even if a sibling is
    rejected, so a single bad promise doesn't sink the whole card — but the card
    is not gradeable until `rejected` is empty (the caller decides)."""
    res = ValidationResult()

    # 1. Parse.
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as e:
            res.card_errors.append(f"Invalid JSON: {e}")
            return res
    else:
        data = payload

    if isinstance(data, dict) and "error" in data and "promises" not in data:
        res.card_errors.append(f"AI stage returned error: {data['error']}")
        return res

    if not isinstance(data, dict) or "promises" not in data:
        res.card_errors.append("Payload missing 'promises' array.")
        return res

    res.selection = data.get("promise_selection")

    # 2. Per-promise validation.
    for i, p in enumerate(data["promises"]):
        pid = p.get("id") or f"index_{i}"
        errors: list[str] = []

        # Enum membership against the engine's allowed sets.
        if p.get("promise_type") not in PROMISE_TYPES:
            errors.append(f"promise_type {p.get('promise_type')!r} not in {sorted(PROMISE_TYPES)}")
        if p.get("delivery") not in DELIVERY_POINTS:
            errors.append(f"delivery {p.get('delivery')!r} not in {sorted(DELIVERY_POINTS)}")
        # Negative promises are binary at input: only D4 or D0 (REDEFINED cap to
        # D2 is applied by the engine, not asserted by the AI here).
        if p.get("promise_type") == "Negative" and p.get("delivery") not in ("D4", "D0"):
            errors.append(
                f"negative promise delivery {p.get('delivery')!r} must be D4 or D0 "
                "(intermediate buckets do not apply to negative promises)")
        if p.get("difficulty") not in DIFFICULTY_MAX:
            errors.append(f"difficulty {p.get('difficulty')!r} not in {sorted(DIFFICULTY_MAX)}")
        if p.get("scale") not in SCALE_POINTS:
            errors.append(f"scale {p.get('scale')!r} not in {sorted(SCALE_POINTS)}")
        if p.get("magnitude") not in MAGNITUDE_POINTS:
            errors.append(f"magnitude {p.get('magnitude')!r} not in {sorted(MAGNITUDE_POINTS)}")

        role = p.get("their_role")
        if role not in THEIR_ROLE_VALUES:
            errors.append(f"their_role {role!r} not in {sorted(THEIR_ROLE_VALUES)}")

        clarity = p.get("clarity")
        if not isinstance(clarity, int) or not (2 <= clarity <= 5):
            errors.append(f"clarity {clarity!r} must be int 2..5 (no clarity 1; values statements excluded at qualification)")

        # time_pressure: number or null. Null is allowed (engine treats missing
        # downstream); but if present it must be a non-negative number.
        tp = p.get("time_pressure")
        if tp is not None and (not isinstance(tp, (int, float)) or tp < 0):
            errors.append(f"time_pressure {tp!r} must be null or a non-negative number")

        # actions_taken must be explicit boolean (engine relies on it for floor).
        if not isinstance(p.get("actions_taken"), bool):
            errors.append("actions_taken must be a boolean")

        # Evidence-discipline checks (methodology §13 audit trail).
        flags = p.get("flags", [])
        if not isinstance(flags, list):
            errors.append("flags must be a list")
            flags = []
        rationales = p.get("flag_rationales", {}) or {}
        for fl in flags:
            if fl not in BEHAVIORAL_FLAGS:
                errors.append(f"unknown behavioral flag {fl!r}")
            elif not rationales.get(fl):
                errors.append(f"flag {fl!r} has no flag_rationales entry (evidence required)")

        # Their Role must cite a lookup anchor.
        if role in THEIR_ROLE_VALUES and not p.get("their_role_anchor"):
            errors.append("their_role present without their_role_anchor (§13 requires it)")

        # Delivery bucket must cite evidence.
        if not p.get("delivery_evidence"):
            errors.append("delivery has no delivery_evidence citations")

        # AI DRAFT must be present until human review (§10/§12).
        review_flags = p.get("review_flags", []) or []
        if "AI DRAFT" not in review_flags:
            errors.append("review_flags must include 'AI DRAFT' (unreviewed)")

        # time_pressure null is permitted but must be acknowledged, not silent.
        if tp is None and not p.get("time_pressure_basis"):
            errors.append("time_pressure is null without time_pressure_basis note")

        if errors:
            _reject(res.rejected, pid, errors, p)
            continue

        # Passed: build the engine Promise. Carry behavioral flags only;
        # review_flags travel separately to the published card.
        res.valid_promises.append(Promise(
            promise_type=p["promise_type"],
            delivery=p["delivery"],
            their_role=float(role),
            difficulty=p["difficulty"],
            scale=p["scale"],
            magnitude=p["magnitude"],
            clarity=int(clarity),
            # Engine's time_pressure is required; null maps to on-track 0.75
            # ONLY as an explicit, logged default decided here — never silently.
            time_pressure=float(tp) if tp is not None else 0.75,
            flags=list(flags),
            actions_taken=bool(p["actions_taken"]),
            id=pid,
        ))

    return res


def grade_if_clean(payload: str | dict[str, Any]) -> dict[str, Any]:
    """Validate, and grade only if nothing was rejected. Otherwise return the
    rejections for human review without producing a grade."""
    res = validate_payload(payload)
    out: dict[str, Any] = {
        "selection": res.selection,
        "rejected": res.rejected,
        "card_errors": res.card_errors,
    }
    if not res.ok:
        out["graded"] = False
        out["reason"] = "Validation failures — routed to human review, not scored."
        return out
    out["graded"] = True
    out["scorecard"] = grade_scorecard(res.valid_promises)
    return out


# ============================================================================
# TESTS
# ============================================================================
def _good_promise(**over):
    base = {
        "id": "p1", "promise_text": "x", "promise_source": "c1",
        "promise_type": "Qualitative", "delivery": "D3",
        "delivery_rationale": "passed one chamber", "delivery_evidence": ["c2"],
        "their_role": 0.8, "their_role_anchor": "0.8 championed and signed",
        "difficulty": "H2", "scale": "S2", "magnitude": "M2",
        "magnitude_rationale": "county population", "clarity": 4,
        "time_pressure": 0.75, "time_pressure_basis": "1y promise, 9mo elapsed",
        "actions_taken": True, "actions_evidence": ["c2"],
        "flags": [], "flag_rationales": {}, "review_flags": ["AI DRAFT"],
    }
    base.update(over)
    return base


def run_tests():
    print("Running adapter tests...\n")

    # Clean payload grades.
    clean = {"official_id": "S1", "promise_selection": {"tracked": 1},
             "promises": [_good_promise()]}
    r = grade_if_clean(clean)
    assert r["graded"] and r["scorecard"]["grade"], r
    print(f"PASS  clean payload graded -> {r['scorecard']['grade']}")

    # Bad enum rejected.
    bad = {"promises": [_good_promise(delivery="D9")]}
    r = validate_payload(bad)
    assert r.rejected and any("delivery" in e for e in r.rejected[0]["errors"])
    print("PASS  invalid delivery bucket rejected")

    # Flag without rationale rejected.
    bad = {"promises": [_good_promise(flags=["REVERSED"], flag_rationales={})]}
    r = validate_payload(bad)
    assert any("flag_rationales" in e for e in r.rejected[0]["errors"])
    print("PASS  flag without rationale rejected")

    # Role without anchor rejected.
    bad = {"promises": [_good_promise(their_role_anchor="")]}
    r = validate_payload(bad)
    assert any("anchor" in e for e in r.rejected[0]["errors"])
    print("PASS  Their Role without anchor rejected")

    # Missing AI DRAFT rejected.
    bad = {"promises": [_good_promise(review_flags=[])]}
    r = validate_payload(bad)
    assert any("AI DRAFT" in e for e in r.rejected[0]["errors"])
    print("PASS  missing AI DRAFT rejected")

    # Malformed JSON string.
    r = validate_payload("{not json")
    assert r.card_errors and not r.ok
    print("PASS  malformed JSON string rejected at card level")

    # null time_pressure without basis rejected; with basis accepted.
    bad = {"promises": [_good_promise(time_pressure=None, time_pressure_basis="")]}
    assert validate_payload(bad).rejected
    ok = {"promises": [_good_promise(time_pressure=None,
                                     time_pressure_basis="no timeframe stated")]}
    assert not validate_payload(ok).rejected
    print("PASS  null time_pressure handled (basis required)")

    # One bad promise doesn't sink a good sibling, but card isn't graded.
    mixed = {"promises": [_good_promise(id="ok"),
                          _good_promise(id="bad", scale="S9")]}
    r = grade_if_clean(mixed)
    assert r["graded"] is False and len(r["rejected"]) == 1
    print("PASS  mixed payload: good promise kept, card held for review\n")

    print("All adapter tests passed.")


if __name__ == "__main__":
    run_tests()
