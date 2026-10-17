"""Deterministic cost/emissions model. NO LLM math lives here or anywhere else:
Gemini reads the machine's condition and explains results; this module computes.

All coefficients are ILLUSTRATIVE demo assumptions (see ASSUMPTIONS) and are
returned to the UI so nothing is hidden from the user.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

HORIZON_DAYS = 90

# Efficiency loss (absolute fraction of baseline efficiency) at day 0, by severity 1..5
LOSS_BY_SEVERITY = {1: 0.005, 2: 0.015, 3: 0.03, 4: 0.055, 5: 0.085}
# Daily compounding growth of that loss while the defect is left unrepaired
GROWTH_BY_SEVERITY = {1: 0.004, 2: 0.008, 3: 0.012, 4: 0.018, 5: 0.025}
LOSS_CAP = 0.25

# Illustrative cost parameters (INR)
CARBON_RATE_INR_PER_TCO2E = 1500.0   # exposure proxy, replace with a cited figure
NOX_RATE_INR_PER_KG = 40.0           # exposure proxy, replace with a cited figure

ASSUMPTIONS = {
    "efficiency_loss_at_day0_by_severity": LOSS_BY_SEVERITY,
    "daily_loss_growth_by_severity": GROWTH_BY_SEVERITY,
    "loss_cap": LOSS_CAP,
    "carbon_exposure_inr_per_tco2e": CARBON_RATE_INR_PER_TCO2E,
    "nox_exposure_inr_per_kg": NOX_RATE_INR_PER_KG,
    "note": "Illustrative demo coefficients. Replace with plant-specific and "
            "regulator-sourced values before any real use.",
}


@dataclass
class Machine:
    id: str
    name: str
    kind: str
    fuel: str
    fuel_unit: str
    base_fuel_per_day: float      # fuel units/day at healthy efficiency
    fuel_price: float             # INR per fuel unit
    ef_co2: float                 # kg CO2 per fuel unit
    ef_nox: float                 # kg NOx per fuel unit
    repair_cost: float            # INR to repair now
    downtime_cost_now: float      # INR lost if stopped now (unplanned)
    downtime_cost_planned: float  # INR lost if repaired in the planned window
    planned_window_day: int       # days until next planned shutdown
    repair_escalation_per_day: float  # repair cost growth/day if deferred
    health: dict = field(default_factory=dict)  # severity, confidence, component...
    output_per_day: float = 0.0           # units of product made per day (illustrative)
    output_label: str = "tonne of product"


def loss_at(severity: int, day: int) -> float:
    sev = max(1, min(5, int(severity)))
    return min(LOSS_CAP, LOSS_BY_SEVERITY[sev] * (1 + GROWTH_BY_SEVERITY[sev]) ** day)


def daily_excess(m: Machine, severity: int, day: int) -> dict:
    """Excess fuel/emissions/cost on a given day, versus a healthy machine.

    Efficiency falls by `loss` relative to baseline, and fuel needed scales as
    1/efficiency, so excess fuel = base * (1/(1-loss) - 1).
    """
    loss = loss_at(severity, day)
    excess_fuel = m.base_fuel_per_day * (1.0 / (1.0 - loss) - 1.0)
    co2_kg = excess_fuel * m.ef_co2
    nox_kg = excess_fuel * m.ef_nox
    energy_cost = excess_fuel * m.fuel_price
    exposure = (co2_kg / 1000.0) * CARBON_RATE_INR_PER_TCO2E + nox_kg * NOX_RATE_INR_PER_KG
    return {
        "loss": loss,
        "excess_fuel": excess_fuel,
        "co2_kg": co2_kg,
        "nox_kg": nox_kg,
        "energy_cost": energy_cost,
        "exposure": exposure,
        "total_cost": energy_cost + exposure,
    }


def simulate(m: Machine, severity: Optional[int] = None, horizon: int = HORIZON_DAYS) -> dict:
    """Cumulative liability curve plus break-even for 'wait for the planned window'.

    Fixing now costs repair + unplanned downtime. Waiting saves the difference
    between unplanned and planned downtime, but accrues excess cost and repair
    escalation. Break-even is the first day waiting costs more than it saves.
    """
    sev = int(severity if severity is not None else m.health.get("severity", 1))
    savings = max(0.0, m.downtime_cost_now - m.downtime_cost_planned)

    curve = []
    cum = cum_co2 = cum_nox = cum_fuel = 0.0
    break_even = None
    for d in range(0, horizon + 1):
        ex = daily_excess(m, sev, d)
        if d > 0:
            cum += ex["total_cost"]
            cum_co2 += ex["co2_kg"]
            cum_nox += ex["nox_kg"]
            cum_fuel += ex["excess_fuel"]
        repair_premium = m.repair_cost * m.repair_escalation_per_day * d
        waiting_cost = cum + repair_premium
        if break_even is None and d >= 1 and waiting_cost >= savings:
            break_even = d
        curve.append({
            "day": d,
            "daily_cost": round(ex["total_cost"], 2),
            "cum_liability": round(cum, 2),
            "waiting_cost": round(waiting_cost, 2),
            "cum_co2_kg": round(cum_co2, 2),
            "cum_nox_kg": round(cum_nox, 2),
        })

    w = min(m.planned_window_day, horizon)
    wait_at_window = curve[w]["waiting_cost"]
    recommendation = "DEFER_TO_WINDOW" if wait_at_window < savings else "FIX_NOW"

    today = daily_excess(m, sev, 0)
    return {
        "machine_id": m.id,
        "severity": sev,
        "today": {k: round(v, 4) for k, v in today.items()},
        "daily_liability_today": round(today["total_cost"], 2),
        "downtime_savings_if_wait": round(savings, 2),
        "planned_window_day": m.planned_window_day,
        "waiting_cost_at_window": round(wait_at_window, 2),
        "break_even_day": break_even,
        "recommendation": recommendation,
        "curve": curve,
        "assumptions": ASSUMPTIONS,
        "formula": (
            "excess_fuel = base_fuel x (1/(1-loss) - 1); "
            "cost = fuel x price + CO2(t) x carbon_rate + NOx(kg) x nox_rate; "
            "break-even = first day (cumulative excess cost + repair escalation) "
            ">= downtime saved by waiting"
        ),
    }


def deferral_cost(m: Machine, defer_days: int, severity: Optional[int] = None) -> dict:
    """Verified tool exposed to Gemini via function calling."""
    sev = int(severity if severity is not None else m.health.get("severity", 1))
    d = max(0, min(HORIZON_DAYS, int(defer_days)))
    sim = simulate(m, sev)
    pt = sim["curve"][d]
    return {
        "machine": m.name,
        "defer_days": d,
        "cumulative_excess_cost_inr": pt["cum_liability"],
        "total_waiting_cost_inr_incl_repair_escalation": pt["waiting_cost"],
        "cumulative_excess_co2_kg": pt["cum_co2_kg"],
        "downtime_saved_by_waiting_inr": sim["downtime_savings_if_wait"],
        "break_even_day": sim["break_even_day"],
        "daily_liability_today_inr": sim["daily_liability_today"],
        "recommendation": sim["recommendation"],
        "planned_window_day": m.planned_window_day,
    }


# ---------------------------------------------------------------- footprint passport
def footprint(m: Machine, severity: Optional[int] = None, day: int = 0) -> dict:
    """Direct combustion CO2 per unit of output: healthy versus current condition.

    This is the machine-condition-adjusted figure a customer could use in place of
    an industry-average factor. Illustrative until real fuel and output data are wired in.
    """
    sev = int(severity if severity is not None else m.health.get("severity", 1))
    out = m.output_per_day or 1.0
    healthy_kg = m.base_fuel_per_day * m.ef_co2
    ex = daily_excess(m, sev, day)
    current_kg = healthy_kg + ex["co2_kg"]
    return {
        "unit": m.output_label,
        "healthy_kg_per_unit": round(healthy_kg / out, 2),
        "current_kg_per_unit": round(current_kg / out, 2),
        "extra_kg_per_unit": round(ex["co2_kg"] / out, 2),
        "extra_pct": round(100.0 * ex["co2_kg"] / healthy_kg, 2) if healthy_kg else 0.0,
        "output_per_day": m.output_per_day,
    }


# ---------------------------------------------------------------- improvement
def improvement(m: Machine, before_sev: int, after_sev: int, horizon: int = 30) -> dict:
    """What a repair is worth: the model's trajectory before versus after, over `horizon` days."""
    b = simulate(m, before_sev, horizon=horizon)
    a = simulate(m, after_sev, horizon=horizon)
    cb, ca = b["curve"][horizon], a["curve"][horizon]
    return {
        "before_severity": before_sev,
        "after_severity": after_sev,
        "avoided_cost_per_day": round(b["daily_liability_today"] - a["daily_liability_today"], 2),
        "avoided_co2_kg_per_day": round(b["today"]["co2_kg"] - a["today"]["co2_kg"], 2),
        "horizon_days": horizon,
        "avoided_cost_horizon": round(cb["cum_liability"] - ca["cum_liability"], 2),
        "avoided_co2_kg_horizon": round(cb["cum_co2_kg"] - ca["cum_co2_kg"], 2),
        "footprint_before": footprint(m, before_sev),
        "footprint_after": footprint(m, after_sev),
    }


def fleet_impact(machines: dict, horizon: int = 30) -> dict:
    """If the 'fix now' machines are repaired, how much CO2 and excess cost is avoided."""
    fix_ids, fix_co2, fix_cost = [], 0.0, 0.0
    all_co2, all_cost, daily_total = 0.0, 0.0, 0.0
    for m in machines.values():
        sev = int(m.health.get("severity", 1))
        sim = simulate(m, sev, horizon=horizon)
        c = sim["curve"][horizon]
        all_co2 += c["cum_co2_kg"]
        all_cost += c["cum_liability"]
        daily_total += sim["daily_liability_today"]
        if sim["recommendation"] == "FIX_NOW":
            fix_ids.append(m.id)
            fix_co2 += c["cum_co2_kg"]
            fix_cost += c["cum_liability"]
    return {
        "horizon_days": horizon,
        "fix_now_ids": fix_ids,
        "fix_now_co2_t": round(fix_co2 / 1000.0, 2),
        "fix_now_cost": round(fix_cost, 0),
        "all_co2_t": round(all_co2 / 1000.0, 2),
        "all_cost": round(all_cost, 0),
        "fleet_daily_liability": round(daily_total, 0),
        "note": "Excess cost avoided is before repair and downtime costs. "
                "Assumes a repair removes the defect. Illustrative demo values.",
    }
