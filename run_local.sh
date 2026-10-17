#!/usr/bin/env bash
# Local dev. For Gemini via AI Studio:  export GEMINI_API_KEY=...
# For Vertex AI locally:  gcloud auth application-default login, then
#   export GOOGLE_GENAI_USE_VERTEXAI=true GOOGLE_CLOUD_PROJECT=... GOOGLE_CLOUD_LOCATION=global
pip install -q -r requirements.txt
uvicorn app.main:app --reload --port 8080
