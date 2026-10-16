# Submission checklist (fill as you go)

## Links (all must open logged-out, in an incognito window)
- [ ] GitHub repo (public): 
- [ ] Deployed prototype (Cloud Run or Firebase Hosting URL): 
- [ ] Demo video (3 to 4 minutes, under 3:30 is safer): 
- [ ] Pitch deck: used the official template, exported as PDF

## Before you record or submit
- [ ] `/api/health` on the live URL shows `gemini_mode: vertex-ai` (or ai-studio) and `store_backend: firestore`
- [ ] One full run on the LIVE site: upload photo, Analyze, slider, Explain, Record a BLOCK decision
- [ ] Decision record on the live site shows "Saved in Firestore" and "Chain check: intact"
- [ ] Replaced the TODO source links in `data/regulatory_excerpts.json` with real, verified sources
- [ ] Repo README states only what runs (it does, keep it that way)
- [ ] Cloud Run min-instances=1 while judging is open; set a billing budget alert

## Draft brief description (edit to match what you actually deployed)
TerraVow helps plant teams decide whether to repair a degrading machine now or wait for a planned
shutdown, by pricing what waiting costs in rupees and emissions. An engineer uploads a photo and
maintenance notes; **Gemini** (multimodal, structured JSON output on Vertex AI) returns a calibrated
condition assessment including its own confidence and uncertainty. A verified cost model computes excess
fuel, CO2 and liability and the break-even day. Gemini then uses **function calling** to fetch every figure
from that calculator and explains the result for the engineer and the CFO, citing regulatory excerpts. A
Decision Gate blocks any option that raises total emissions or merely shifts pollution between air and
water. Every assessment and decision is written to **Firestore** with a hash chain for tamper evidence.
The API and UI run on **Cloud Run**[, with **Firebase Hosting** as the front door: delete this clause if you do not deploy it].

## Demo video outline (about 3:20, hard limit 4:00)
1. 0:00 Hook (15s): "A sooty burner is quietly costing this plant money and CO2 every day." Show the fix-first ranking, Boiler B-2 at #4.
2. 0:15 Upload the burner photo and notes, press Analyze. Gemini returns severity, confidence, evidence, and what it is unsure about. Fleet re-ranks, recommendation flips to "Fix now".
3. 0:55 Drag the slider: the rupee figure climbs, the break-even day lands before the planned shutdown.
4. 1:25 Footprint card: CO2 per tonne of steam, healthy versus now. "This replaces an industry average."
5. 1:45 Press Explain: engineer and CFO versions, "verified calculator calls", citations.
6. 2:10 Safety check: wet scrubber BLOCK (moves pollution to water), raised firing rate BLOCK, repair now ALLOW. Record the BLOCK.
7. 2:40 After the repair: upload the after photo, Verify. VERIFIED, tonnes of CO2 avoided, machine drops down the ranking. Mention that notes alone never verify.
8. 3:00 Decision record in Firestore, hash chain intact. Fleet impact card. One architecture slide (Cloud Run, Gemini on Vertex AI, Firestore, Firebase).
9. 3:15 Close: roadmap in one sentence.

Say "illustrative sample image" if your photo is AI-generated or from stock, and "synthetic demo fleet" for the data.
