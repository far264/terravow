"""Persistence: Firestore (default database) with an in-memory fallback for local dev.

The active backend is exposed via `backend` and shown in the UI, so a fallback can
never pass as the real thing. Audit entries are hash-chained (each entry stores the
hash of the previous one), which makes after-the-fact edits detectable.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from typing import Dict, List, Optional

AUDIT = "audit"
HEALTH = "health_overrides"


def _hash(prev: str, payload: dict) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256((prev + body).encode("utf-8")).hexdigest()


class Store:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._audit: List[dict] = []
        self._health: Dict[str, dict] = {}
        self._fs = None
        self.backend = "memory"
        self.backend_error: Optional[str] = None
        if os.getenv("USE_FIRESTORE", "false").lower() in ("1", "true", "yes"):
            try:
                from google.cloud import firestore  # imported lazily

                self._fs = firestore.Client(project=os.getenv("GOOGLE_CLOUD_PROJECT") or None)
                self.backend = "firestore"
            except Exception as e:  # surfaced, not hidden
                self.backend_error = f"{type(e).__name__}: {e}"

    # ---- audit -----------------------------------------------------------
    def add_audit(self, entry: dict) -> dict:
        with self._lock:
            last = self._latest_audit()
            prev = last["hash"] if last else "GENESIS"
            record = {
                "id": uuid.uuid4().hex[:12],
                "seq": time.time_ns(),
                "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()) + " UTC",
                **entry,
                "prev_hash": prev,
            }
            record["hash"] = _hash(prev, {k: v for k, v in record.items() if k != "hash"})
            if self._fs is not None:
                self._fs.collection(AUDIT).document(record["id"]).set(record)
            else:
                self._audit.append(record)
            return record

    def list_audit(self, limit: int = 20) -> List[dict]:
        if self._fs is not None:
            from google.cloud import firestore

            docs = (self._fs.collection(AUDIT)
                    .order_by("seq", direction=firestore.Query.DESCENDING)
                    .limit(limit).stream())
            return [d.to_dict() for d in docs]
        return list(reversed(self._audit))[:limit]

    def _latest_audit(self) -> Optional[dict]:
        rows = self.list_audit(1)
        return rows[0] if rows else None

    def verify_chain(self, limit: int = 200) -> dict:
        rows = list(reversed(self.list_audit(limit)))  # oldest first
        ok = True
        for i, r in enumerate(rows):
            expected_prev = rows[i - 1]["hash"] if i > 0 else r["prev_hash"]
            recomputed = _hash(r["prev_hash"], {k: v for k, v in r.items() if k != "hash"})
            if r["prev_hash"] != expected_prev or recomputed != r["hash"]:
                ok = False
                break
        return {"entries_checked": len(rows), "chain_intact": ok}

    # ---- live health overrides (so a Gemini analysis re-ranks the fleet) ---
    def save_health(self, machine_id: str, health: dict) -> None:
        if self._fs is not None:
            self._fs.collection(HEALTH).document(machine_id).set(health)
        else:
            self._health[machine_id] = health

    def load_health(self) -> Dict[str, dict]:
        if self._fs is not None:
            return {d.id: d.to_dict() for d in self._fs.collection(HEALTH).stream()}
        return dict(self._health)

    def clear_health(self) -> None:
        if self._fs is not None:
            for d in self._fs.collection(HEALTH).stream():
                d.reference.delete()
        else:
            self._health.clear()
