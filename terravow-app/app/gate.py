"""Decision Gate: one safety chokepoint for every recommendation.

Order matters (and is covered by tests):
 1. HARD VETO  - any increase in total emission mass, or any shift of load between
                 media (air/water/solid), is BLOCK regardless of confidence.
 2. VALIDITY   - missing or out-of-range confidence is BLOCK (fail closed).
 3. CONFIDENCE - >= ALLOW_T -> ALLOW, >= ASK_T -> ASK, else BLOCK.
 4. TRUST CAP  - low data trust can only LOWER ALLOW to ASK, never raise anything.
"""
from __future__ import annotations

from typing import Optional

ALLOW_T = 0.75
ASK_T = 0.40
TRUST_T = 0.60
EPS = 1e-9


def evaluate(deltas_kg: dict, confidence: Optional[float], data_trust: float = 1.0) -> dict:
    """deltas_kg: change in emitted mass per medium versus the do-nothing path,
    e.g. {"air": -120.0, "water": +35.0}. Negative means a reduction."""
    total = sum(deltas_kg.values())
    increased = [k for k, v in deltas_kg.items() if v > EPS]
    reduced = [k for k, v in deltas_kg.items() if v < -EPS]

    # 1. Hard veto, applied before confidence is even looked at.
    if total > EPS:
        return _result("BLOCK", "veto_total_increase",
                       f"Would increase total emissions by {total:.1f} kg.",
                       confidence, data_trust)
    if increased and reduced:
        return _result("BLOCK", "veto_media_shift",
                       f"Cuts {', '.join(reduced)} but raises {', '.join(increased)}: "
                       "this moves pollution between media instead of reducing it.",
                       confidence, data_trust)
    if increased:
        return _result("BLOCK", "veto_media_increase",
                       f"Would increase {', '.join(increased)} emissions.",
                       confidence, data_trust)

    # 2. Validity (fail closed).
    if confidence is None or not (0.0 <= confidence <= 1.0):
        return _result("BLOCK", "invalid_confidence",
                       "Missing or invalid confidence: routed to human review.",
                       confidence, data_trust)

    # 3. Confidence classification.
    if confidence >= ALLOW_T:
        verdict, rule = "ALLOW", "confidence_allow"
        why = "Reduces or holds emissions with high confidence."
    elif confidence >= ASK_T:
        verdict, rule = "ASK", "confidence_ask"
        why = "Plausible, but confidence is moderate: needs human confirmation."
    else:
        verdict, rule = "BLOCK", "confidence_low"
        why = "Confidence is too low to act on."

    # 4. Trust cap: can only lower, never raise.
    if verdict == "ALLOW" and data_trust < TRUST_T:
        verdict, rule = "ASK", "trust_cap"
        why = "Input data trust is low, so the result is capped at ASK."
    return _result(verdict, rule, why, confidence, data_trust)


def _result(verdict, rule, reason, confidence, trust):
    return {"verdict": verdict, "rule": rule, "reason": reason,
            "confidence": confidence, "data_trust": trust}
