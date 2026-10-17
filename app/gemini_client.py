"""All Gemini calls. Two roles only:

 1. analyze_condition  - multimodal: photo / log text -> structured health state.
 2. explain_decision   - function calling: Gemini calls our verified
                         compute_deferral_cost tool, then explains the numbers.

Gemini never produces the cost numbers itself.

Auth modes (pick one via env):
 - Vertex AI : GOOGLE_GENAI_USE_VERTEXAI=true, GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION
               (uses the Cloud Run service account; no key to manage)
 - AI Studio : GEMINI_API_KEY
"""
from __future__ import annotations

import json
import os
import re
from typing import Callable, List, Literal, Optional

from pydantic import BaseModel, Field

from google import genai
from google.genai import types

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
EXPLAIN_MODEL = os.getenv("GEMINI_EXPLAIN_MODEL", MODEL)


class GeminiNotConfigured(RuntimeError):
    pass


def mode() -> str:
    if os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in ("1", "true", "yes"):
        return "vertex-ai"
    if os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
        return "ai-studio"
    return "not-configured"


def _client() -> genai.Client:
    m = mode()
    if m == "not-configured":
        raise GeminiNotConfigured(
            "Gemini is not configured. Set GEMINI_API_KEY, or set "
            "GOOGLE_GENAI_USE_VERTEXAI=true with GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION."
        )
    # genai.Client() reads the env vars above on its own.
    return genai.Client()


# ---------------------------------------------------------------- 1. condition
class HealthState(BaseModel):
    component_category: Literal["burner", "heat_exchanger", "seal", "filter", "other"]
    component_name: str = Field(description="The specific part, e.g. 'burner nozzle'")
    defect: str = Field(description="What is wrong, in plain words")
    severity: int = Field(ge=1, le=5, description="1 = cosmetic, 5 = severe/failing")
    confidence: float = Field(ge=0.0, le=1.0, description="Your confidence in this assessment")
    evidence: List[str] = Field(description="Specific things you saw or read that support this")
    uncertainty: str = Field(description="What you are unsure about, or what is missing")
    suggested_inspection: str = Field(description="One concrete check to confirm")


ANALYZE_SYSTEM = (
    "You are an industrial reliability engineer assessing combustion and thermal "
    "equipment. Read the photo (if any) and the maintenance notes, and return ONLY the "
    "structured assessment. Be calibrated: if the photo is unclear or evidence is thin, "
    "lower your confidence and say what is missing. Never invent measurements. "
    "Severity scale: 1 cosmetic, 2 minor, 3 moderate, 4 serious, 5 severe."
)


def analyze_condition(machine_ctx: str, notes: str, image: Optional[bytes] = None,
                      mime_type: str = "image/jpeg") -> HealthState:
    client = _client()
    parts: list = []
    if image:
        parts.append(types.Part.from_bytes(data=image, mime_type=mime_type))
    parts.append(
        f"Machine: {machine_ctx}\n"
        f"Maintenance notes / log extract:\n{notes.strip() or '(none provided)'}\n\n"
        "Assess the condition of the most concerning component."
    )
    resp = client.models.generate_content(
        model=MODEL,
        contents=parts,
        config=types.GenerateContentConfig(
            system_instruction=ANALYZE_SYSTEM,
            response_mime_type="application/json",
            response_schema=HealthState,
            temperature=0.2,
        ),
    )
    if getattr(resp, "parsed", None) is not None:
        return resp.parsed  # type: ignore[return-value]
    return HealthState.model_validate_json(_strip_fences(resp.text or ""))


# ---------------------------------------------------------------- 1b. after repair
class AfterAssessment(HealthState):
    same_component: bool = Field(
        description="True only if the AFTER photo shows the SAME physical component as the BEFORE photo")
    same_component_confidence: float = Field(ge=0.0, le=1.0, description="Confidence in that judgement")
    same_component_reason: str = Field(description="One sentence: what matches or what differs")


REASSESS_SYSTEM = (
    "You verify maintenance repairs. You are given a BEFORE photo from the original assessment and an "
    "AFTER-repair photo. FIRST decide whether both show the same physical component: same part type, "
    "matching distinctive geometry, vane or bolt layout, and mounting. A different part, even if clean "
    "and in good condition, is NOT the same component: set same_component to false. Be strict. "
    "THEN assess the condition of the component in the AFTER photo, honestly and calibrated, with the "
    "same severity scale (1 cosmetic, 2 minor, 3 moderate, 4 serious, 5 severe). Never assume a repair "
    "worked because the notes say so. Return ONLY the structured result."
)


def reassess_after_repair(machine_ctx: str, notes: str, after_image: bytes, after_mime: str,
                          before_thumb: bytes, prior_summary: str) -> AfterAssessment:
    client = _client()
    parts: list = [
        "BEFORE photo (from the original assessment):",
        types.Part.from_bytes(data=before_thumb, mime_type="image/jpeg"),
        "AFTER-repair photo:",
        types.Part.from_bytes(data=after_image, mime_type=after_mime),
        f"Machine: {machine_ctx}\nOriginal finding: {prior_summary}\n"
        f"Notes on what was done:\n{notes.strip() or '(none provided)'}\n\n"
        "Decide if the AFTER photo shows the same component, then assess its condition.",
    ]
    resp = client.models.generate_content(
        model=MODEL, contents=parts,
        config=types.GenerateContentConfig(
            system_instruction=REASSESS_SYSTEM, response_mime_type="application/json",
            response_schema=AfterAssessment, temperature=0.1),
    )
    if getattr(resp, "parsed", None) is not None:
        return resp.parsed  # type: ignore[return-value]
    return AfterAssessment.model_validate_json(_strip_fences(resp.text or ""))


# ---------------------------------------------------------------- 2. explain
EXPLAIN_SYSTEM = (
    "You explain a maintenance decision for an industrial plant. RULES: (1) You must call "
    "the compute_deferral_cost tool to get every rupee or emissions figure you mention; "
    "call it for at least day 0-equivalent context, the planned shutdown day, and the "
    "break-even day if one exists. (2) Never calculate, estimate or invent numbers "
    "yourself. (3) When you rely on a regulatory excerpt, cite its id like [R1]; only cite "
    "ids you were given. (4) Say plainly what is uncertain. (5) Reply with ONLY a JSON "
    'object: {"engineer": "...", "cfo": "...", "citations": ["R1"], "caveat": "..."}. '
    "engineer: 2-3 sentences, technical, about the defect and the emissions effect. "
    "cfo: 2-3 sentences, about rupees, the break-even day, and the recommendation. "
    "caveat: one sentence on what the figures do not capture."
)


def explain_decision(summary: str, corpus: List[dict], tool: Callable[..., dict]) -> dict:
    """`tool` must be a plain function with type hints + docstring (the SDK builds the
    function declaration from it and runs the call loop automatically)."""
    client = _client()
    excerpts = "\n".join(f"[{c['id']}] {c['topic']}: {c['summary']}" for c in corpus)
    prompt = (
        f"Decision context:\n{summary}\n\nRegulatory excerpts you may cite:\n{excerpts}\n\n"
        "Write the explanation."
    )
    resp = client.models.generate_content(
        model=EXPLAIN_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=EXPLAIN_SYSTEM,
            tools=[tool],  # automatic function calling
            temperature=0.3,
        ),
    )
    calls = _count_tool_calls(resp)
    data = _parse_json_loose(resp.text or "")
    valid_ids = {c["id"] for c in corpus}
    data["citations"] = [c for c in data.get("citations", []) if c in valid_ids]
    data["tool_calls"] = calls
    data["model"] = EXPLAIN_MODEL
    return data


# ---------------------------------------------------------------- helpers
def _strip_fences(text: str) -> str:
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.S)


def _parse_json_loose(text: str) -> dict:
    t = _strip_fences(text)
    try:
        d = json.loads(t)
    except Exception:
        m = re.search(r"\{.*\}", t, flags=re.S)
        try:
            d = json.loads(m.group(0)) if m else {}
        except Exception:
            d = {}
    if not isinstance(d, dict) or "engineer" not in d:
        d = {"engineer": t[:600], "cfo": "", "citations": [],
             "caveat": "Model reply was not in the expected format."}
    d.setdefault("citations", [])
    d.setdefault("caveat", "")
    return d


def _count_tool_calls(resp) -> int:
    hist = getattr(resp, "automatic_function_calling_history", None) or []
    n = 0
    for c in hist:
        for p in getattr(c, "parts", None) or []:
            if getattr(p, "function_call", None):
                n += 1
    return n
