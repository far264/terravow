"""TerraVow API. Runs on Cloud Run; serves the single-page UI from /static."""
from __future__ import annotations

import base64
import io
import logging
import os
from typing import Dict, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import engine, fleet, gate, gemini_client
from .store import Store

log = logging.getLogger("terravow")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="TerraVow", version="0.1.0")
store = Store()

STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")
MAX_IMAGE_BYTES = 8 * 1024 * 1024


# ------------------------------------------------------------------ helpers
def get_fleet() -> Dict[str, engine.Machine]:
    machines = fleet.fresh_fleet()
    for mid, h in store.load_health().items():
        if mid in machines:
            machines[mid].health = h
    return machines


def get_machine(machine_id: str) -> engine.Machine:
    m = get_fleet().get(machine_id)
    if not m:
        raise HTTPException(404, f"Unknown machine '{machine_id}'")
    return m


def data_trust(health: dict) -> float:
    """Seeded data is trusted. A Gemini assessment is trusted as far as its own
    confidence, and notes-only (no photo) is capped at 0.7: weaker evidence."""
    if health.get("source") != "gemini":
        return 1.0
    conf = float(health.get("confidence", 0.0))
    return conf if health.get("had_image") else min(conf, 0.7)


def machine_view(m: engine.Machine, with_curve: bool = True) -> dict:
    sev = int(m.health.get("severity", 1))
    sim = engine.simulate(m, sev)
    trust = data_trust(m.health)
    options = []
    for o in fleet.build_options(m, sev):
        g = gate.evaluate(o["deltas_kg"], o["confidence"], trust)
        options.append({**o, "gate": g})
    out = {
        "id": m.id, "name": m.name, "kind": m.kind, "fuel": m.fuel, "fuel_unit": m.fuel_unit,
        "base_fuel_per_day": m.base_fuel_per_day,
        "repair_cost": m.repair_cost,
        "downtime_cost_now": m.downtime_cost_now,
        "downtime_cost_planned": m.downtime_cost_planned,
        "planned_window_day": m.planned_window_day,
        "health": {k: v for k, v in m.health.items() if k != "ref_image_b64"},
        "has_baseline_photo": bool(m.health.get("ref_image_b64")),
        "data_trust": round(trust, 2),
        "footprint": engine.footprint(m, sev),
        "daily_liability_today": sim["daily_liability_today"],
        "excess_co2_kg_per_day": sim["today"]["co2_kg"],
        "downtime_savings_if_wait": sim["downtime_savings_if_wait"],
        "waiting_cost_at_window": sim["waiting_cost_at_window"],
        "break_even_day": sim["break_even_day"],
        "recommendation": sim["recommendation"],
        "options": options,
        "assumptions": sim["assumptions"], "formula": sim["formula"],
    }
    if with_curve:
        out["curve"] = sim["curve"]
    return out


def make_thumb(img_bytes: bytes) -> Optional[str]:
    """Small JPEG kept with an assessment so a later after-repair photo can be compared
    against it. Returns base64, or None if the image cannot be decoded."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        im.thumbnail((640, 640))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=72)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


def gemini_guard(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except gemini_client.GeminiNotConfigured as e:
        raise HTTPException(503, str(e))
    except Exception as e:  # never leak secrets; keep the message short
        log.exception("Gemini call failed")
        raise HTTPException(502, f"Gemini call failed: {type(e).__name__}: {str(e)[:300]}")


async def read_image(image: Optional[UploadFile]):
    if image is None or not image.filename:
        return None, "image/jpeg"
    mime = image.content_type or "image/jpeg"
    if not mime.startswith("image/"):
        raise HTTPException(400, "Upload an image file (jpg, png or webp).")
    data = await image.read()
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "Image is larger than 8 MB.")
    return data, mime


# ------------------------------------------------------------------ routes
@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "gemini_mode": gemini_client.mode(),
        "gemini_model": gemini_client.MODEL,
        "store_backend": store.backend,
        "store_error": store.backend_error,
        "runtime": "cloud-run" if os.getenv("K_SERVICE") else "local",
        "service": os.getenv("K_SERVICE"),
    }


@app.get("/api/fleet")
def get_fleet_ranked():
    rows = [machine_view(m, with_curve=False) for m in get_fleet().values()]
    rows.sort(key=lambda r: r["daily_liability_today"], reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return {"label": fleet.SEED_LABEL, "machines": rows}


@app.get("/api/machine/{machine_id}")
def machine_detail(machine_id: str):
    return machine_view(get_machine(machine_id))


@app.get("/api/machine/{machine_id}/baseline")
def baseline_photo(machine_id: str):
    """The original photo kept for after-repair comparison (small JPEG thumbnail)."""
    b64 = get_machine(machine_id).health.get("ref_image_b64")
    if not b64:
        raise HTTPException(404, "No baseline photo on record")
    return Response(content=base64.b64decode(b64), media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.post("/api/analyze")
async def analyze(machine_id: str = Form(...), notes: str = Form(""),
                  image: Optional[UploadFile] = File(None)):
    m = get_machine(machine_id)
    img_bytes, mime = await read_image(image)
    if not img_bytes and not notes.strip():
        raise HTTPException(400, "Add a photo, notes, or both.")

    ctx = f"{m.name} ({m.kind}), fuel: {m.fuel}"
    hs = gemini_guard(gemini_client.analyze_condition, ctx, notes, img_bytes, mime)
    health_doc = hs.model_dump()
    health_doc.update(source="gemini", had_image=bool(img_bytes), model=gemini_client.MODEL)
    if img_bytes:
        health_doc["ref_image_b64"] = make_thumb(img_bytes)
    store.save_health(machine_id, health_doc)

    view = machine_view(get_machine(machine_id))
    store.add_audit({
        "kind": "condition_assessment", "machine_id": machine_id, "machine": m.name,
        "summary": f"{hs.component_name}: {hs.defect} (severity {hs.severity}, "
                   f"confidence {hs.confidence:.2f}, {'photo+notes' if img_bytes else 'notes only'})",
        "verdict": None, "model": gemini_client.MODEL,
    })
    return view


class ExplainReq(BaseModel):
    machine_id: str
    defer_days: int = 0


@app.post("/api/explain")
def explain(req: ExplainReq):
    m = get_machine(req.machine_id)
    sim = engine.simulate(m)
    summary = (
        f"Machine: {m.name} ({m.kind}). Defect: {m.health.get('component_name')} - "
        f"{m.health.get('defect')} (severity {m.health.get('severity')}/5, "
        f"assessment confidence {m.health.get('confidence')}).\n"
        f"Repair cost now: INR {m.repair_cost:,.0f}. Planned shutdown in {m.planned_window_day} days. "
        f"Currently exploring deferral of {req.defer_days} days. "
        f"Deterministic result: break-even day = {sim['break_even_day']}, "
        f"recommendation = {sim['recommendation']}. "
        "Use the tool to fetch exact figures before writing."
    )

    def compute_deferral_cost(defer_days: int) -> dict:
        """Verified calculator for this machine. Returns the cumulative excess cost
        in INR, total waiting cost including repair escalation, excess CO2 in kg,
        downtime saved by waiting, the break-even day and recommendation, if the
        repair is deferred by `defer_days` days (integer 0 to 90)."""
        return engine.deferral_cost(m, defer_days)

    out = gemini_guard(gemini_client.explain_decision, summary, fleet.load_corpus(),
                       compute_deferral_cost)
    store.add_audit({
        "kind": "explanation", "machine_id": m.id, "machine": m.name,
        "summary": f"Gemini explanation generated ({out.get('tool_calls', 0)} verified tool calls, "
                   f"citations: {', '.join(out.get('citations', [])) or 'none'})",
        "verdict": None, "model": out.get("model"),
    })
    return out


class DecideReq(BaseModel):
    machine_id: str
    option_id: str


@app.post("/api/decide")
def decide(req: DecideReq):
    m = get_machine(req.machine_id)
    view = machine_view(m, with_curve=False)
    opt = next((o for o in view["options"] if o["id"] == req.option_id), None)
    if not opt:
        raise HTTPException(404, f"Unknown option '{req.option_id}'")
    rec = store.add_audit({
        "kind": "decision", "machine_id": m.id, "machine": m.name,
        "summary": f"{opt['label']}: {opt['gate']['reason']}",
        "verdict": opt["gate"]["verdict"], "rule": opt["gate"]["rule"],
        "deltas_kg": opt["deltas_kg"], "confidence": opt["confidence"],
        "data_trust": view["data_trust"], "model": None,
    })
    return {"option": opt, "audit": rec}



@app.get("/api/impact")
def impact():
    return engine.fleet_impact(get_fleet())


@app.post("/api/verify_improvement")
async def verify_improvement(machine_id: str = Form(...), notes: str = Form(""),
                             image: Optional[UploadFile] = File(None)):
    """After a repair: compare an after photo with the original assessment photo, and record a
    verified improvement only if it is the same component AND the evidence supports it.

    This is an evidence record, NOT an official certification."""
    m = get_machine(machine_id)
    img_bytes, mime = await read_image(image)
    if not img_bytes and not notes.strip():
        raise HTTPException(400, "Add an after-repair photo, notes, or both.")

    before = dict(m.health)
    before_sev = int(before.get("severity", 1))
    thumb_b64 = before.get("ref_image_b64")
    ctx = f"{m.name} ({m.kind}), fuel: {m.fuel}. This is a re-inspection AFTER a repair."

    comp = {"same": None, "confidence": None, "reason": None}
    if img_bytes and thumb_b64:
        prior = f"{before.get('component_name')} ({before.get('component_category')}): {before.get('defect')}"
        ra = gemini_guard(gemini_client.reassess_after_repair, ctx, notes, img_bytes, mime,
                          base64.b64decode(thumb_b64), prior)
        hs = ra
        comp = {"same": ra.same_component, "confidence": ra.same_component_confidence,
                "reason": ra.same_component_reason}
    else:
        hs = gemini_guard(gemini_client.analyze_condition, ctx, notes, img_bytes, mime)

    after = {k: v for k, v in hs.model_dump().items() if k in gemini_client.HealthState.model_fields}
    after.update(source="gemini", had_image=bool(img_bytes), model=gemini_client.MODEL)

    imp = engine.improvement(m, before_sev, hs.severity)
    conf = min(float(before.get("confidence", 0.0)), hs.confidence,
               comp["confidence"] if comp["confidence"] is not None else 1.0)
    trust = data_trust(after)
    drop = before_sev - hs.severity

    def res(verdict, rule, reason):
        return {"verdict": verdict, "rule": rule, "reason": reason, "confidence": conf, "data_trust": trust}

    # Order matters: identity first, then improvement, then evidence quality, then the gate.
    if comp["same"] is False:
        gate_res = res("BLOCK", "different_component",
                       "The after-repair photo does not show the same component as the original "
                       f"assessment. {comp['reason'] or ''}".strip())
        status = "NOT_VERIFIED"
    elif drop <= 0:
        gate_res = res("BLOCK", "no_improvement",
                       "The new assessment does not show a lower severity, so nothing is verified.")
        status = "NOT_VERIFIED"
    elif not img_bytes:
        gate_res = res("ASK", "photo_required",
                       "Notes alone cannot verify an improvement. Add an after-repair photo, "
                       "or have a person confirm.")
        status = "NEEDS_REVIEW"
    elif comp["same"] is None:
        gate_res = res("ASK", "no_baseline_photo",
                       "There is no original photo on record to compare against, so a person must "
                       "confirm this is the same component. Run an assessment with a photo first.")
        status = "NEEDS_REVIEW"
    elif comp["confidence"] < 0.7:
        gate_res = res("ASK", "component_unclear",
                       "It is not clear that this is the same component as before, so a person "
                       f"must confirm. {comp['reason'] or ''}".strip())
        status = "NEEDS_REVIEW"
    else:
        gate_res = gate.evaluate({"air": -imp["avoided_co2_kg_horizon"], "water": 0.0}, conf, trust)
        status = {"ALLOW": "VERIFIED", "ASK": "NEEDS_REVIEW", "BLOCK": "NOT_VERIFIED"}[gate_res["verdict"]]

    if status == "VERIFIED":
        store.save_health(machine_id, {**after, "verified": True, "ref_image_b64": make_thumb(img_bytes)})

    mode_txt = "photo+notes" if img_bytes else "notes only"
    t_co2 = imp["avoided_co2_kg_horizon"] / 1000
    if status == "VERIFIED":
        summary = (f"Verified: severity {before_sev} to {hs.severity} ({mode_txt}); avoids about "
                   f"{t_co2:.1f} t CO2 over {imp['horizon_days']} days. {gate_res['reason']}")
    elif status == "NEEDS_REVIEW":
        summary = (f"Needs review: severity {before_sev} to {hs.severity} ({mode_txt}); would avoid about "
                   f"{t_co2:.1f} t CO2 if confirmed. {gate_res['reason']}")
    else:
        summary = f"Not verified: {gate_res['reason']}"

    audit = store.add_audit({
        "kind": "verified_improvement", "machine_id": machine_id, "machine": m.name,
        "status": status, "summary": summary,
        "verdict": gate_res["verdict"], "rule": gate_res["rule"],
        "before_severity": before_sev, "after_severity": hs.severity,
        "baseline_source": before.get("source", "seeded"),
        "same_component": comp["same"],
        "avoided_co2_kg_horizon": imp["avoided_co2_kg_horizon"] if status == "VERIFIED" else 0.0,
        "avoided_cost_horizon": imp["avoided_cost_horizon"] if status == "VERIFIED" else 0.0,
        "confidence": conf, "data_trust": round(trust, 2), "model": gemini_client.MODEL,
    })
    return {"status": status, "gate": gate_res, "improvement": imp, "component_check": comp,
            "before": {k: v for k, v in before.items() if k != "ref_image_b64"},
            "after": after, "audit": audit, "machine": machine_view(get_machine(machine_id))}


@app.get("/api/improvements")
def improvements(limit: int = 20):
    rows = [e for e in store.list_audit(100)
            if e.get("kind") == "verified_improvement" and e.get("status") == "VERIFIED"]
    return {"entries": rows[:limit],
            "disclaimer": "Evidence records produced by this prototype. Not an official certification."}


@app.get("/api/registry")
def registry(limit: int = 25):
    return {
        "backend": store.backend,
        "chain": store.verify_chain(),
        "entries": store.list_audit(min(max(limit, 1), 100)),
    }


@app.post("/api/demo/reset")
def reset_demo():
    """Clears live Gemini overrides so the demo can be re-run. Audit log is kept."""
    store.clear_health()
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
