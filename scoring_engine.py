#!/usr/bin/env python3
"""
scoring_engine.py — PolicyLogic Methodology v2 deterministic scoring engine.

DESIGN BOUNDARY (Methodology §12.3):
    This module performs ARITHMETIC ONLY. It never identifies promises, assigns
    buckets, or fires flags — those are the AI bucket-assignment stage's job. It
    consumes bucket values (already chosen by that stage + human review) and
    computes adjusted delivery, difficulty earned, impact, promise score, and the
    final grade. Because it is a pure function of the buckets, the grade formula
    cannot be altered by prompt drift or model behavior changes.

    Every value in the tables below is transcribed directly from the methodology
    document's tables. No value is invented here.

LOCKED DECISIONS (confirmed with methodology owner):
    #1  Time-pressure order: applied as a delivery-points reduction. Commutative
        with the Their Role multiply, so order is immaterial to the result.
    #2  Difficulty uses RAW bucket delivery points (clean bucket value, before
        time-pressure and before Their Role). Late-but-delivered ambition still
        earns difficulty credit; lateness is penalized only on the delivery axis.
    #3  REVERSED is an override: Delivery = D0 regardless of other flags, beating
        any floor. A promise carrying BOTH reversed and externally_blocked is
        routed to human review (CONTESTED) because the flags tell contradictory
        stories.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

# ============================================================================
# CONSTANTS — transcribed verbatim from Methodology v2 tables
# ============================================================================

# Table 5 — Delivery buckets -> points
DELIVERY_POINTS = {"D4": 12, "D3": 9, "D2": 6, "D1": 3, "D0": 0}
DELIVERY_ORDER = ["D0", "D1", "D2", "D3", "D4"]  # ascending, for cap/floor logic

# Table 7 — Difficulty (Their Mountain) -> max points
DIFFICULTY_MAX = {"H3": 5, "H2": 3, "H1": 1}

# Table 8 — Scale -> points
SCALE_POINTS = {"S3": 4, "S2": 2, "S1": 1}

# Table 9 — Magnitude -> points
MAGNITUDE_POINTS = {"M3": 4, "M2": 2, "M1": 1}
MAGNITUDE_ORDER = ["M1", "M2", "M3"]

# Table 16 — Their Role lookup (valid discrete values)
THEIR_ROLE_VALUES = {1.0, 0.8, 0.6, 0.4, 0.2, 0.0}

# Per-promise maximum promise score (Table 11 normalization):
#   12 (delivery) + 5 (max difficulty) + 8 (max impact: S3 4 + M3 4) = 25
PROMISE_SCORE_MAX = 25
DELIVERY_MAX = 12

# Grade bands — live methodology (methodology.html), 6-band scale.
# Boundary convention: inclusive of the lower bound.
#   A+ 90+ · A 85-89 · B 70-84 · C 50-69 · D 30-49 · F <30
GRADE_BANDS = [
    (90, "A+"), (85, "A"), (70, "B"), (50, "C"), (30, "D"), (0, "F"),
]


# ============================================================================
# Helpers for cap/floor logic on the ordered delivery ladder
# ============================================================================
def _cap_delivery(code: str, max_code: str) -> str:
    """Return the lower (more constrained) of code and max_code on the ladder."""
    return code if DELIVERY_ORDER.index(code) <= DELIVERY_ORDER.index(max_code) else max_code


def _floor_delivery(code: str, min_code: str) -> str:
    """Return the higher of code and min_code on the ladder."""
    return code if DELIVERY_ORDER.index(code) >= DELIVERY_ORDER.index(min_code) else min_code


# ============================================================================
# Time Pressure (Table 15)
# ============================================================================
def time_pressure_factor(tp: float, deadline_shifted: bool = False) -> dict[str, Any]:
    """Return the delivery-weight treatment for a Time Pressure value.

    Returns a dict with:
        mode: 'early' | 'on_track' | 'overdue'
        multiplier: factor applied to delivery points (early/overdue) — for
                    'early' this is the provisional weight (TP*2); for overdue it
                    is (1 - reduction); for on_track it is 1.0.
        provisional: True when grade should be marked provisional (early mode).
        reduction: the overdue reduction fraction (0 unless overdue).
    """
    if tp < 0.5:
        # Early: delivery weight = TP * 2, grade provisional.
        return {"mode": "early", "multiplier": tp * 2, "provisional": True, "reduction": 0.0}
    if tp < 1.0:
        # On track: 0.5 to <1.0 (lower bound inclusive; 1.0 begins overdue).
        return {"mode": "on_track", "multiplier": 1.0, "provisional": False, "reduction": 0.0}

    # Overdue (tp >= 1.0). Banded reduction, lower bound inclusive.
    if tp < 1.25:
        reduction = 0.10
    elif tp < 1.5:
        reduction = 0.20
    else:
        # +10% per 0.25 over 1.0, capped at 40%.
        steps = (tp - 1.0) / 0.25
        reduction = min(0.40, 0.10 * steps)
        # Round to avoid float noise; methodology bands are in 10% steps.
        reduction = min(0.40, round(reduction, 4))

    # DEADLINE SHIFTED: reduction applies but no overdue-cap relief.
    # (Table 6/20: the 40% cap still holds; there is simply no relief from it.)
    return {"mode": "overdue", "multiplier": 1.0 - reduction,
            "provisional": False, "reduction": reduction}


# ============================================================================
# Per-promise scoring
# ============================================================================
@dataclass
class Promise:
    """Bucket values assigned UPSTREAM (AI + human). The engine only reads them."""
    promise_type: str           # "Quantitative" | "Qualitative" | "Negative"
    delivery: str               # D0..D4 (base, before flags)
    their_role: float           # one of THEIR_ROLE_VALUES
    difficulty: str             # H1..H3
    scale: str                  # S1..S3
    magnitude: str              # M1..M3 (base, before caps)
    clarity: int                # 2..5 (no clarity 1; values statements screened out at qualification)
    time_pressure: float        # ratio
    flags: list[str] = field(default_factory=list)  # behavioral flag codes
    actions_taken: bool = False  # for EXTERNALLY BLOCKED floor condition
    id: str = ""


def score_promise(p: Promise) -> dict[str, Any]:
    notes: list[str] = []
    review_flags: list[str] = []
    f = set(p.flags)

    # --- Clarity validity (live methodology: scale starts at 2) ---
    # Pure values statements carry no verifiable condition and are screened out
    # at qualification ("What Counts as a Promise") — they never reach scoring,
    # so there is no clarity-1 score. A clarity < 2 here is malformed input.
    if p.clarity < 2:
        return {
            "id": p.id, "scored": False,
            "reason": "Clarity < 2 is invalid: values statements are excluded at "
                      "qualification, not scored. Route to review.",
            "review_flags": ["UNDER REVIEW"],
        }

    # --- #3 collision routing: both REVERSED and EXTERNALLY BLOCKED ---
    if "REVERSED" in f and "EXTERNALLY BLOCKED" in f:
        review_flags.append("CONTESTED")
        notes.append("REVERSED and EXTERNALLY BLOCKED both fired — contradictory; "
                     "routed to human review. REVERSED override applied pending review.")

    # --- Delivery bucket with flags (live methodology Behavioral Flags) ---
    delivery_code = p.delivery

    # Negative promises are binary: D4 (avoided) or D0 (occurred). Intermediate
    # buckets D1-D3 do not apply. The single exception is REDEFINED, which caps
    # at D2 (handled below). Any intermediate input for a negative promise is
    # snapped to the nearer pole and flagged, rather than silently scored.
    if p.promise_type == "Negative" and delivery_code not in ("D4", "D0"):
        if "REDEFINED" not in f:
            snapped = "D4" if delivery_code in ("D3",) else "D0"
            notes.append(
                f"Negative promise had intermediate delivery {delivery_code}; "
                f"binary rule applies (D4/D0) so snapped to {snapped}. Verify in review.")
            review_flags.append("CONTESTED")
            delivery_code = snapped

    # REDEFINED cap at D2.
    if "REDEFINED" in f:
        delivery_code = _cap_delivery(delivery_code, "D2")
        notes.append("REDEFINED: delivery capped at D2.")

    # EXTERNALLY BLOCKED floor at D2 — only if meaningful actions taken (Table 6).
    if "EXTERNALLY BLOCKED" in f:
        if p.actions_taken:
            delivery_code = _floor_delivery(delivery_code, "D2")
            notes.append("EXTERNALLY BLOCKED: D2 floor applied (actions taken).")
        else:
            notes.append("EXTERNALLY BLOCKED noted but no actions taken — floor not applied.")

    # REVERSED override — beats everything, applied last (#3).
    if "REVERSED" in f:
        delivery_code = "D0"
        notes.append("REVERSED: delivery forced to D0 regardless of other factors.")

    base_delivery_pts = DELIVERY_POINTS[delivery_code]  # raw bucket pts (for #2)

    # --- Their Role, with CREDIT OVERCLAIMED cap (Table 17) ---
    role = p.their_role
    if "CREDIT OVERCLAIMED" in f:
        role = min(role, 0.4)
        notes.append("CREDIT OVERCLAIMED: Their Role capped at 0.4.")

    # --- Time Pressure on delivery points (#1) ---
    tp = time_pressure_factor(p.time_pressure, deadline_shifted="DEADLINE SHIFTED" in f)
    tp_adjusted_pts = base_delivery_pts * tp["multiplier"]

    # --- Adjusted Delivery = (tp-adjusted delivery pts) × Their Role (§3.1) ---
    adjusted_delivery = tp_adjusted_pts * role

    # --- Difficulty Earned = Bucket × (RAW delivery pts / 12)  (#2, §3.2) ---
    difficulty_earned = DIFFICULTY_MAX[p.difficulty] * (base_delivery_pts / 12)

    # --- Impact = Scale + Magnitude, with caps (§3.3) ---
    magnitude_code = p.magnitude
    # Specificity Cap: Clarity 2 (directional, no specifics) caps Magnitude at M1.
    if p.clarity == 2:
        magnitude_code = _cap_magnitude(magnitude_code, "M1")
        notes.append("Specificity Cap (Clarity 2): Magnitude capped at M1.")
    # SCOPE REDUCED caps Magnitude at M1 (Table 6).
    if "SCOPE REDUCED" in f:
        magnitude_code = _cap_magnitude(magnitude_code, "M1")
        notes.append("SCOPE REDUCED: Magnitude capped at M1.")
    impact = SCALE_POINTS[p.scale] + MAGNITUDE_POINTS[magnitude_code]

    promise_score = adjusted_delivery + difficulty_earned + impact

    return {
        "id": p.id, "scored": True,
        "delivery_code_final": delivery_code,
        "base_delivery_pts": base_delivery_pts,
        "time_pressure": tp,
        "their_role_used": role,
        "adjusted_delivery": round(adjusted_delivery, 4),
        "difficulty_earned": round(difficulty_earned, 4),
        "impact": impact,
        "promise_score": round(promise_score, 4),
        "provisional": tp["provisional"],
        "notes": notes,
        "review_flags": review_flags,
    }


def _cap_magnitude(code: str, max_code: str) -> str:
    return code if MAGNITUDE_ORDER.index(code) <= MAGNITUDE_ORDER.index(max_code) else max_code


# ============================================================================
# Scorecard aggregation (Table 11)
# ============================================================================
def grade_scorecard(promises: list[Promise]) -> dict[str, Any]:
    scored = [score_promise(p) for p in promises]
    counted = [s for s in scored if s.get("scored")]
    n = len(counted)
    result: dict[str, Any] = {"per_promise": scored, "n_scored": n}

    if n == 0:
        result.update({"grade": None, "grade_input_pct": None,
                       "card_flags": ["LOW PROMISE COUNT"],
                       "note": "No scorable promises."})
        return result

    sum_adj = sum(s["adjusted_delivery"] for s in counted)
    sum_score = sum(s["promise_score"] for s in counted)

    delivery_ratio = sum_adj / (n * DELIVERY_MAX)
    promise_ratio = sum_score / (n * PROMISE_SCORE_MAX)
    grade_input = (delivery_ratio * 0.60) + (promise_ratio * 0.40)
    pct = grade_input * 100

    letter = next(l for low, l in GRADE_BANDS if pct >= low)

    card_flags: list[str] = []
    if n < 5:
        card_flags.append("LOW PROMISE COUNT")  # Table 21
    if any(s.get("provisional") for s in counted):
        card_flags.append("PROVISIONAL (early-term)")
    if any("CONTESTED" in s.get("review_flags", []) for s in counted):
        card_flags.append("CONTESTED — human review required")

    result.update({
        "delivery_ratio": round(delivery_ratio, 4),
        "promise_ratio": round(promise_ratio, 4),
        "grade_input_pct": round(pct, 2),
        "grade": letter,
        "card_flags": card_flags,
    })
    return result


# ============================================================================
# TESTS — doc worked examples + the two collision cases we analyzed
# ============================================================================
def _approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


def run_tests() -> None:
    print("Running scoring engine tests...\n")

    # --- Doc example: difficulty H3 with D2 = 5 × (6/12) = 2.5 (§3.2) ---
    p = Promise(promise_type="Qualitative", delivery="D2", their_role=1.0,
                difficulty="H3", scale="S1", magnitude="M1", clarity=3,
                time_pressure=0.75, id="doc-difficulty")
    r = score_promise(p)
    assert _approx(r["difficulty_earned"], 2.5), r["difficulty_earned"]
    print(f"PASS  doc example: H3×(D2 6/12) difficulty = {r['difficulty_earned']}")

    # --- Time pressure: TP 1.25-1.5 = 20% reduction (Table 15) ---
    tpf = time_pressure_factor(1.4)
    assert _approx(tpf["reduction"], 0.20), tpf
    print(f"PASS  TP 1.4 -> {int(tpf['reduction']*100)}% reduction")
    # cap at 40%
    assert _approx(time_pressure_factor(3.0)["reduction"], 0.40)
    print("PASS  TP 3.0 -> reduction capped at 40%")
    # early mode
    em = time_pressure_factor(0.3)
    assert em["mode"] == "early" and _approx(em["multiplier"], 0.6) and em["provisional"]
    print(f"PASS  TP 0.3 -> early mode, weight ×{em['multiplier']}, provisional")

    # --- #3 collision: REVERSED + EXTERNALLY BLOCKED -> D0 + CONTESTED ---
    p = Promise(promise_type="Qualitative", delivery="D3", their_role=0.8,
                difficulty="H3", scale="S3", magnitude="M3", clarity=4,
                time_pressure=0.75, flags=["REVERSED", "EXTERNALLY BLOCKED"],
                actions_taken=True, id="collision")
    r = score_promise(p)
    assert r["delivery_code_final"] == "D0", r["delivery_code_final"]
    assert _approx(r["adjusted_delivery"], 0.0)
    assert _approx(r["difficulty_earned"], 0.0)
    assert r["impact"] == 8  # impact survives (§3.3)
    assert "CONTESTED" in r["review_flags"]
    print(f"PASS  REVERSED+BLOCKED -> D0, impact={r['impact']} survives, CONTESTED routed")

    # --- REDEFINED cap meets EXTERNALLY BLOCKED floor at D2 ---
    p = Promise(promise_type="Negative", delivery="D4", their_role=0.6,
                difficulty="H2", scale="S2", magnitude="M2", clarity=3,
                time_pressure=0.75, flags=["REDEFINED", "EXTERNALLY BLOCKED"],
                actions_taken=True, id="cap-floor")
    r = score_promise(p)
    assert r["delivery_code_final"] == "D2", r["delivery_code_final"]
    print(f"PASS  REDEFINED cap + BLOCKED floor -> {r['delivery_code_final']}")

    # --- CREDIT OVERCLAIMED caps role at 0.4 ---
    p = Promise(promise_type="Quantitative", delivery="D4", their_role=1.0,
                difficulty="H1", scale="S1", magnitude="M1", clarity=5,
                time_pressure=0.75, flags=["CREDIT OVERCLAIMED"], id="overclaim")
    r = score_promise(p)
    assert _approx(r["their_role_used"], 0.4)
    print(f"PASS  CREDIT OVERCLAIMED -> role {r['their_role_used']}")

    # --- Specificity cap: clarity 2 caps magnitude at M1 ---
    p = Promise(promise_type="Qualitative", delivery="D3", their_role=0.6,
                difficulty="H2", scale="S3", magnitude="M3", clarity=2,
                time_pressure=0.75, id="clarity-cap")
    r = score_promise(p)
    assert r["impact"] == SCALE_POINTS["S3"] + MAGNITUDE_POINTS["M1"], r["impact"]
    print(f"PASS  Clarity 2 -> magnitude capped, impact={r['impact']}")

    # --- Full scorecard grade (our 3-promise worked example, raw difficulty) ---
    A = Promise("Qualitative", "D4", 0.8, "H3", "S3", "M3", 4, 1.4, id="A")  # overdue 20%
    B = Promise("Qualitative", "D3", 0.6, "H2", "S2", "M2", 3, 0.75, id="B")
    C = Promise("Qualitative", "D2", 1.0, "H1", "S1", "M1", 5, 0.75, id="C")
    g = grade_scorecard([A, B, C])
    print(f"\nScorecard: grade input {g['grade_input_pct']}%  -> {g['grade']}  "
          f"(flags: {g['card_flags']})")
    assert g["grade"] == "C", g["grade"]  # 50-69% band on the 6-band scale
    print("PASS  3-promise scorecard grades C (6-band scale)")

    # --- Live methodology worked example: housing promise = 9.1 / 25 ---
    h = score_promise(Promise("Qualitative", "D2", 0.6, "H2", "S2", "M2", 4,
                              0.75, id="housing"))
    assert _approx(h["adjusted_delivery"], 3.6), h["adjusted_delivery"]
    assert _approx(h["difficulty_earned"], 1.5), h["difficulty_earned"]
    assert h["impact"] == 4 and _approx(h["promise_score"], 9.1), h["promise_score"]
    print(f"PASS  live housing example: promise score {h['promise_score']} / 25")

    # --- Negative promise is binary: intermediate input snaps + flags ---
    neg = score_promise(Promise("Negative", "D2", 1.0, "H1", "S1", "M1", 3,
                                0.75, id="neg"))
    assert neg["delivery_code_final"] in ("D0", "D4")
    assert "CONTESTED" in neg["review_flags"]
    print(f"PASS  negative promise binary rule: D2 -> {neg['delivery_code_final']}, CONTESTED\n")

    print("All tests passed.")


if __name__ == "__main__":
    run_tests()
