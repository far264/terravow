"""Synthetic demo fleet (clearly labelled as seeded) and remediation options."""
from __future__ import annotations

import copy
import json
import os
from typing import Dict, List

from .engine import Machine, simulate

SEED_LABEL = "Synthetic demo fleet: illustrative values, not real plant data."

# severity/confidence below are the *seeded* health state. Running a live Gemini
# analysis on a machine replaces it (and re-ranks the fleet).
_SEED: List[dict] = [
    dict(id="B-2", name="Boiler B-2", kind="Steam boiler", fuel="Furnace oil", fuel_unit="L",
         base_fuel_per_day=2400, fuel_price=55, ef_co2=3.15, ef_nox=0.007,
         repair_cost=240000, downtime_cost_now=450000, downtime_cost_planned=150000,
         planned_window_day=35, repair_escalation_per_day=0.01,
         health=dict(component_category="burner", component_name="Burner nozzle",
                     defect="Slight carbon build-up", severity=2, confidence=0.82, source="seeded")),
    dict(id="F-1", name="Reheating Furnace F-1", kind="Reheat furnace", fuel="Natural gas", fuel_unit="SCM",
         base_fuel_per_day=6800, fuel_price=48, ef_co2=2.02, ef_nox=0.0035,
         repair_cost=180000, downtime_cost_now=320000, downtime_cost_planned=90000,
         planned_window_day=14, repair_escalation_per_day=0.008,
         health=dict(component_category="seal", component_name="Door seal",
                     defect="Air ingress at door seal", severity=3, confidence=0.78, source="seeded")),
    dict(id="K-4", name="Kiln K-4", kind="Rotary kiln", fuel="Coal", fuel_unit="kg",
         base_fuel_per_day=18000, fuel_price=9, ef_co2=2.4, ef_nox=0.006,
         repair_cost=520000, downtime_cost_now=900000, downtime_cost_planned=260000,
         planned_window_day=44, repair_escalation_per_day=0.006,
         health=dict(component_category="heat_exchanger", component_name="Preheater duct",
                     defect="Moderate fouling", severity=3, confidence=0.7, source="seeded")),
    dict(id="C-7", name="Thermal Oil Heater C-7", kind="Thermal oil heater", fuel="Furnace oil", fuel_unit="L",
         base_fuel_per_day=1100, fuel_price=55, ef_co2=3.15, ef_nox=0.007,
         repair_cost=95000, downtime_cost_now=140000, downtime_cost_planned=60000,
         planned_window_day=28, repair_escalation_per_day=0.012,
         health=dict(component_category="filter", component_name="Air filter",
                     defect="Clogged intake filter", severity=4, confidence=0.88, source="seeded")),
    dict(id="D-3", name="Dryer D-3", kind="Spray dryer", fuel="Natural gas", fuel_unit="SCM",
         base_fuel_per_day=3100, fuel_price=48, ef_co2=2.02, ef_nox=0.0035,
         repair_cost=70000, downtime_cost_now=110000, downtime_cost_planned=50000,
         planned_window_day=21, repair_escalation_per_day=0.005,
         health=dict(component_category="other", component_name="Duct insulation",
                     defect="Minor insulation wear", severity=1, confidence=0.9, source="seeded")),
    dict(id="G-5", name="Gas Genset G-5", kind="Gas engine", fuel="Natural gas", fuel_unit="SCM",
         base_fuel_per_day=2200, fuel_price=48, ef_co2=2.02, ef_nox=0.0035,
         repair_cost=130000, downtime_cost_now=260000, downtime_cost_planned=120000,
         planned_window_day=42, repair_escalation_per_day=0.007,
         health=dict(component_category="burner", component_name="Fuel injector",
                     defect="Early injector drift", severity=2, confidence=0.74, source="seeded")),
]


# Illustrative daily output per machine, used for the footprint-per-unit card.
_OUTPUT = {
    "B-2": dict(output_per_day=28, output_label="tonne of steam"),
    "F-1": dict(output_per_day=200, output_label="tonne of reheated steel"),
    "K-4": dict(output_per_day=160, output_label="tonne of clinker"),
    "C-7": dict(output_per_day=36, output_label="GJ of process heat"),
    "D-3": dict(output_per_day=25, output_label="tonne of powder"),
    "G-5": dict(output_per_day=8.8, output_label="MWh of electricity"),
}


def fresh_fleet() -> Dict[str, Machine]:
    out = {}
    for d in _SEED:
        d = copy.deepcopy(d)
        d.update(_OUTPUT[d["id"]])
        out[d["id"]] = Machine(**d)
    return out


# Remediation options. Deltas are computed from the engine (kg over 30 days vs the
# do-nothing path), so the Decision Gate sees real numbers.
def build_options(m: Machine, severity: int) -> List[dict]:
    sim = simulate(m, severity, horizon=30)
    c30 = sim["curve"][30]
    avoidable_kg = c30["cum_co2_kg"] + c30["cum_nox_kg"]  # emitted if we do nothing
    conf = float(m.health.get("confidence", 0.0))
    return [
        {
            "id": "fix_now",
            "label": "Repair now",
            "detail": f"Repair {m.health.get('component_name', 'component')} immediately.",
            "deltas_kg": {"air": round(-avoidable_kg, 1), "water": 0.0},
            "confidence": conf,
        },
        {
            "id": "defer_window",
            "label": f"Wait for planned shutdown (day {m.planned_window_day})",
            "detail": "Keep running; repair in the planned window.",
            "deltas_kg": {"air": 0.0, "water": 0.0},
            "confidence": conf,
        },
        {
            "id": "wet_scrubber",
            "label": "Add a wet scrubber",
            "detail": "Captures part of the extra flue emissions, but sends the load to wastewater.",
            "deltas_kg": {"air": round(-0.6 * avoidable_kg, 1), "water": round(0.35 * avoidable_kg + 40, 1)},
            "confidence": min(conf, 0.85),
        },
        {
            "id": "raise_firing",
            "label": "Raise firing rate to make up lost heat",
            "detail": "Burns more fuel to hide the efficiency loss instead of fixing it.",
            "deltas_kg": {"air": round(0.4 * avoidable_kg + 25, 1), "water": 0.0},
            "confidence": min(conf, 0.9),
        },
    ]


def load_corpus() -> List[dict]:
    path = os.path.join(os.path.dirname(__file__), "..", "data", "regulatory_excerpts.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
