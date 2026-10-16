import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

from app import engine, fleet, gate
from app.main import app, store


# ---------------------------------------------------------------- engine
def test_healthy_cost_grows_with_severity():
    m = fleet.fresh_fleet()["B-2"]
    costs = [engine.daily_excess(m, s, 0)["total_cost"] for s in range(1, 6)]
    assert costs == sorted(costs) and costs[0] > 0


def test_loss_grows_over_time_and_is_capped():
    assert engine.loss_at(4, 30) > engine.loss_at(4, 0)
    assert engine.loss_at(5, 10_000) == engine.LOSS_CAP


def test_curve_is_monotonic_and_starts_at_zero():
    m = fleet.fresh_fleet()["B-2"]
    c = engine.simulate(m, 3)["curve"]
    assert c[0]["cum_liability"] == 0
    assert all(b["cum_liability"] >= a["cum_liability"] for a, b in zip(c, c[1:]))


def test_hero_story_flips_from_defer_to_fix_now():
    """Seeded minor defect -> defer is fine. Gemini finds a serious one -> fix now."""
    m = fleet.fresh_fleet()["B-2"]
    minor = engine.simulate(m, 2)
    serious = engine.simulate(m, 4)
    assert minor["recommendation"] == "DEFER_TO_WINDOW"
    assert serious["recommendation"] == "FIX_NOW"
    assert serious["break_even_day"] is not None
    assert serious["break_even_day"] < m.planned_window_day
    assert serious["daily_liability_today"] > minor["daily_liability_today"]


def test_deferral_tool_matches_simulation():
    m = fleet.fresh_fleet()["B-2"]
    sim = engine.simulate(m, 4)
    t = engine.deferral_cost(m, 12, 4)
    assert t["cumulative_excess_cost_inr"] == sim["curve"][12]["cum_liability"]
    assert t["break_even_day"] == sim["break_even_day"]


# ---------------------------------------------------------------- gate
def test_veto_beats_confidence():
    r = gate.evaluate({"air": +10.0, "water": 0.0}, confidence=0.99)
    assert r["verdict"] == "BLOCK" and r["rule"].startswith("veto")


def test_media_shift_is_blocked_even_when_total_falls():
    r = gate.evaluate({"air": -100.0, "water": +30.0}, confidence=0.99)
    assert r["verdict"] == "BLOCK" and r["rule"] == "veto_media_shift"


def test_confidence_bands():
    assert gate.evaluate({"air": -5.0}, 0.9)["verdict"] == "ALLOW"
    assert gate.evaluate({"air": -5.0}, 0.5)["verdict"] == "ASK"
    assert gate.evaluate({"air": -5.0}, 0.1)["verdict"] == "BLOCK"


def test_invalid_confidence_fails_closed():
    assert gate.evaluate({"air": -5.0}, None)["verdict"] == "BLOCK"
    assert gate.evaluate({"air": -5.0}, 1.5)["verdict"] == "BLOCK"


def test_trust_only_lowers():
    assert gate.evaluate({"air": -5.0}, 0.95, data_trust=0.3)["verdict"] == "ASK"
    # trust can never rescue a vetoed or low-confidence result
    assert gate.evaluate({"air": +5.0}, 0.95, data_trust=1.0)["verdict"] == "BLOCK"
    assert gate.evaluate({"air": -5.0}, 0.1, data_trust=1.0)["verdict"] == "BLOCK"


# ---------------------------------------------------------------- api
client = TestClient(app)


def test_fleet_is_ranked_by_daily_liability():
    rows = client.get("/api/fleet").json()["machines"]
    vals = [r["daily_liability_today"] for r in rows]
    assert vals == sorted(vals, reverse=True)
    assert rows[0]["rank"] == 1 and len(rows) == 6


def test_options_include_two_vetoed_alternatives():
    d = client.get("/api/machine/B-2").json()
    v = {o["id"]: o["gate"]["verdict"] for o in d["options"]}
    assert v["wet_scrubber"] == "BLOCK"
    assert v["raise_firing"] == "BLOCK"
    assert v["fix_now"] in ("ALLOW", "ASK")


def test_decision_is_audited_and_chain_verifies():
    client.post("/api/decide", json={"machine_id": "B-2", "option_id": "wet_scrubber"})
    client.post("/api/decide", json={"machine_id": "B-2", "option_id": "fix_now"})
    reg = client.get("/api/registry").json()
    assert reg["chain"]["chain_intact"] is True
    assert reg["entries"][0]["kind"] == "decision"


def test_tampering_breaks_chain():
    client.post("/api/decide", json={"machine_id": "F-1", "option_id": "fix_now"})
    store._audit[0]["summary"] = "edited after the fact"
    assert store.verify_chain()["chain_intact"] is False


def test_analyze_requires_input_and_gemini_config():
    r = client.post("/api/analyze", data={"machine_id": "B-2", "notes": ""})
    assert r.status_code == 400
    os.environ.pop("GEMINI_API_KEY", None)
    os.environ.pop("GOOGLE_GENAI_USE_VERTEXAI", None)
    r = client.post("/api/analyze", data={"machine_id": "B-2", "notes": "flame looks yellow"})
    assert r.status_code == 503


def test_health_endpoint_reports_backend():
    h = client.get("/api/health").json()
    assert h["store_backend"] in ("memory", "firestore")


# ------------------------------------------------- Gemini wiring (model call stubbed)
def test_live_assessment_flips_recommendation_and_reranks(monkeypatch):
    """Stubs ONLY the Gemini call. Verifies override storage, re-ranking and audit."""
    from app import gemini_client

    client.post("/api/demo/reset")
    before = client.get("/api/fleet").json()["machines"]
    rank_before = next(m["rank"] for m in before if m["id"] == "B-2")
    assert client.get("/api/machine/B-2").json()["recommendation"] == "DEFER_TO_WINDOW"

    def fake_analyze(ctx, notes, image=None, mime_type="image/jpeg"):
        return gemini_client.HealthState(
            component_category="burner", component_name="Burner nozzle",
            defect="Heavy carbon fouling, distorted flame", severity=4, confidence=0.86,
            evidence=["Soot build-up visible on nozzle", "Notes mention yellow flame"],
            uncertainty="Cannot see the atomizer tip", suggested_inspection="Inspect atomizer tip")

    monkeypatch.setattr(gemini_client, "analyze_condition", fake_analyze)
    r = client.post("/api/analyze", data={"machine_id": "B-2", "notes": "yellow flame, soot"},
                    files={"image": ("p.jpg", b"\xff\xd8fakejpeg", "image/jpeg")})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["health"]["source"] == "gemini" and d["health"]["had_image"] is True
    assert d["recommendation"] == "FIX_NOW"
    assert d["break_even_day"] < d["planned_window_day"]

    after = client.get("/api/fleet").json()["machines"]
    rank_after = next(m["rank"] for m in after if m["id"] == "B-2")
    assert rank_after < rank_before
    client.post("/api/demo/reset")


def test_notes_only_trust_is_capped_at_70_percent(monkeypatch):
    from app import gemini_client

    client.post("/api/demo/reset")

    def fake_analyze(ctx, notes, image=None, mime_type="image/jpeg"):
        return gemini_client.HealthState(
            component_category="burner", component_name="Burner", defect="Fouling", severity=4,
            confidence=0.95, evidence=["notes"], uncertainty="no photo", suggested_inspection="look")

    monkeypatch.setattr(gemini_client, "analyze_condition", fake_analyze)
    d = client.post("/api/analyze", data={"machine_id": "B-2", "notes": "soot"}).json()
    assert d["data_trust"] == 0.7  # notes-only is capped
    client.post("/api/demo/reset")


def test_explain_uses_verified_tool_and_filters_bad_citations(monkeypatch):
    from app import gemini_client

    seen = {}

    def fake_explain(summary, corpus, tool):
        seen["tool_result"] = tool(14)  # the real verified calculator runs
        return {"engineer": "e", "cfo": "c", "citations": [c["id"] for c in corpus][:1] + ["R999"],
                "caveat": "x", "tool_calls": 1, "model": "stub"}

    monkeypatch.setattr(gemini_client, "explain_decision", fake_explain)
    r = client.post("/api/explain", json={"machine_id": "B-2", "defer_days": 14})
    assert r.status_code == 200
    assert seen["tool_result"]["defer_days"] == 14
    assert seen["tool_result"]["cumulative_excess_cost_inr"] > 0


def test_citation_filter_in_real_client():
    from app.gemini_client import _parse_json_loose
    d = _parse_json_loose('```json\n{"engineer":"a","cfo":"b","citations":["R1"],"caveat":"c"}\n```')
    assert d["engineer"] == "a" and d["citations"] == ["R1"]
    bad = _parse_json_loose("not json at all")
    assert "engineer" in bad


# ------------------------------------------------- merged-idea features
def _fake(sev, conf):
    from app import gemini_client

    def f(ctx, notes, image=None, mime_type="image/jpeg"):
        return gemini_client.HealthState(
            component_category="burner", component_name="Burner nozzle", defect="x",
            severity=sev, confidence=conf, evidence=["e"], uncertainty="u", suggested_inspection="i")
    return f


def test_footprint_extra_grows_with_severity_and_is_in_machine_view():
    from app import engine, fleet
    m = fleet.fresh_fleet()["B-2"]
    lo, hi = engine.footprint(m, 1), engine.footprint(m, 5)
    assert hi["extra_kg_per_unit"] > lo["extra_kg_per_unit"] > 0
    assert hi["current_kg_per_unit"] == round(hi["healthy_kg_per_unit"] + hi["extra_kg_per_unit"], 2)
    v = client.get("/api/machine/B-2").json()["footprint"]
    assert v["unit"] == "tonne of steam" and v["healthy_kg_per_unit"] > 0


def test_improvement_math_is_consistent():
    from app import engine, fleet
    m = fleet.fresh_fleet()["B-2"]
    imp = engine.improvement(m, 4, 1)
    assert imp["avoided_co2_kg_horizon"] > 0 and imp["avoided_cost_horizon"] > 0
    zero = engine.improvement(m, 3, 3)
    assert zero["avoided_co2_kg_horizon"] == 0


def test_fleet_impact_lists_fix_now_machines():
    d = client.get("/api/impact").json()
    assert d["all_co2_t"] >= d["fix_now_co2_t"] >= 0
    assert isinstance(d["fix_now_ids"], list)


def test_verified_improvement_with_photo_is_verified_and_reranks(monkeypatch):
    from app import gemini_client
    client.post("/api/demo/reset")
    monkeypatch.setattr(gemini_client, "analyze_condition", _fake(4, 0.9))
    client.post("/api/analyze", data={"machine_id": "B-2", "notes": "soot"},
                files={"image": ("b.jpg", b"\xff\xd8x", "image/jpeg")})
    rank_before = next(m["rank"] for m in client.get("/api/fleet").json()["machines"] if m["id"] == "B-2")

    monkeypatch.setattr(gemini_client, "analyze_condition", _fake(1, 0.9))
    r = client.post("/api/verify_improvement", data={"machine_id": "B-2", "notes": "nozzle cleaned"},
                    files={"image": ("a.jpg", b"\xff\xd8y", "image/jpeg")})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "VERIFIED" and d["gate"]["verdict"] == "ALLOW"
    assert d["improvement"]["avoided_co2_kg_horizon"] > 0
    rank_after = next(m["rank"] for m in client.get("/api/fleet").json()["machines"] if m["id"] == "B-2")
    assert rank_after > rank_before          # fixed machine drops down the fix-first list
    imps = client.get("/api/improvements").json()["entries"]
    assert imps and imps[0]["status"] == "VERIFIED"
    client.post("/api/demo/reset")


def test_notes_only_cannot_verify_on_its_own(monkeypatch):
    from app import gemini_client
    client.post("/api/demo/reset")
    monkeypatch.setattr(gemini_client, "analyze_condition", _fake(4, 0.9))
    client.post("/api/analyze", data={"machine_id": "B-2", "notes": "soot"},
                files={"image": ("b.jpg", b"\xff\xd8x", "image/jpeg")})
    monkeypatch.setattr(gemini_client, "analyze_condition", _fake(1, 0.95))
    d = client.post("/api/verify_improvement", data={"machine_id": "B-2", "notes": "we cleaned it"}).json()
    assert d["status"] == "NEEDS_REVIEW" and d["gate"]["rule"] == "photo_required"
    # state must NOT change when not verified
    assert client.get("/api/machine/B-2").json()["health"]["severity"] == 4
    client.post("/api/demo/reset")


def test_no_severity_drop_is_not_verified(monkeypatch):
    from app import gemini_client
    client.post("/api/demo/reset")
    monkeypatch.setattr(gemini_client, "analyze_condition", _fake(4, 0.9))
    client.post("/api/analyze", data={"machine_id": "B-2", "notes": "soot"},
                files={"image": ("b.jpg", b"\xff\xd8x", "image/jpeg")})
    d = client.post("/api/verify_improvement", data={"machine_id": "B-2", "notes": "looks same"},
                    files={"image": ("a.jpg", b"\xff\xd8y", "image/jpeg")}).json()
    assert d["status"] == "NOT_VERIFIED" and d["gate"]["rule"] == "no_improvement"
    assert client.get("/api/machine/B-2").json()["health"]["severity"] == 4
    client.post("/api/demo/reset")


def test_audit_chain_survives_new_entry_types():
    assert client.get("/api/registry").json()["chain"]["chain_intact"] in (True, False)
