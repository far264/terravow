# TerraVow prototype

**Fix it now, or wait?** TerraVow reads a machine's condition with Gemini, prices what waiting costs in
rupees and emissions with verified code, shows the break-even day, and checks every option against a
safety gate before it is recorded.

## What actually runs in this prototype

| Piece | What it does | Google service |
|---|---|---|
| Condition intake | Photo and/or maintenance notes in, structured health state out (component, defect, severity 1-5, confidence, evidence, uncertainty) | **Gemini** (multimodal, JSON schema) on Vertex AI |
| Cost engine | Excess fuel, CO2, NOx and rupee liability; break-even day for "wait for the planned shutdown" | Plain Python, no LLM math |
| Explanation | Gemini calls the verified `compute_deferral_cost` tool, then explains the result for an engineer and a CFO, citing regulatory excerpts | **Gemini function calling** |
| Decision Gate | Hard veto on any rise in total emissions or any shift between air and water, then confidence bands, then a data-trust cap | Plain Python, unit-tested |
| Footprint card | CO2 per unit of output (for example per tonne of steam), healthy versus now, so a customer can use a machine-condition figure instead of an industry average | Plain Python |
| Verified improvement | After a repair, Gemini compares an after photo with the stored original photo. An improvement is recorded only if it is the same component, severity drops, and the evidence is strong; a photo of a different part, or notes alone, never verify. This is an evidence record, **not an official certification** | **Gemini** + **Firestore** |
| Fleet impact | CO2 and excess cost avoided over 30 days if the "Fix now" machines are repaired | Plain Python |
| Decision record | Every assessment, explanation, decision and verification stored with a hash of the previous entry | **Firestore** |
| Hosting | API and UI in one container | **Cloud Run** (optionally fronted by **Firebase Hosting**) |

Fleet data is a **synthetic demo fleet**. Cost coefficients and regulatory excerpts are **illustrative placeholders**
(`app/engine.py`, `data/regulatory_excerpts.json`) and are shown in the UI under "How this is calculated".

## What is NOT built (roadmap)
Live sensor ingestion (Pub/Sub), BigQuery analytics, Vertex AI Search over a real regulatory corpus,
Agent Engine, supply-chain propagation of footprints between facilities, official third-party certification,
emissions timing against weather and exposure, and independent verification sources (satellite, meters). These are described in the full
specification under `.kiro/specs/terravow/` and the Java reference model in `src/`.

## Run locally
```bash
export GEMINI_API_KEY=...        # or use Vertex AI, see run_local.sh
./run_local.sh                   # http://localhost:8080
python -m pytest tests           # engine, gate, API and Gemini-wiring tests
```
The header chips show which Gemini mode and which store backend are active, so a local fallback is never mistaken for production.

## Deploy
```bash
PROJECT_ID=your-project ./deploy.sh
```
See `deploy.sh` for the exact steps (APIs, Firestore, IAM, Cloud Run, optional Firebase Hosting).

## Layout
```
app/engine.py         deterministic cost + break-even model
app/gate.py           Decision Gate
app/gemini_client.py  multimodal extraction + function-calling explanation
app/store.py          Firestore + hash-chained audit trail
app/fleet.py          synthetic fleet and remediation options
static/index.html     single-page UI (guided six-step flow)
static/samples/       illustrative sample photos + manifest (see README.txt there)
tests/                33 tests
```

## Known limits (stated honestly)
- The decision record is tamper-evident (hash chain), not tamper-proof: someone with Firestore admin access can delete entries.
- Same-component checking is a Gemini judgement on two photos, so it can be wrong. Low confidence routes to a person.
- Fleet, output rates and cost coefficients are illustrative. Regulatory excerpts are placeholders until sources are added.

> The original TerraVow design specification (the wider vision) is in [SPEC.md](SPEC.md).
