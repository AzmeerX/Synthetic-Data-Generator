"""J1 — Store Query API (P1 §3 J1): plain read-only functions over the stores.

The FastAPI wrapper lives in :mod:`synth.api.app` (imported on demand, so the
pipeline does not need FastAPI loaded).
"""

from synth.api.query import get_edges, get_entity, get_provenance, get_sentences, get_timeline

__all__ = ["get_sentences", "get_entity", "get_edges", "get_timeline", "get_provenance"]
