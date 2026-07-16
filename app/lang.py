"""Language identification (py3langid — the pip-only langid.py successor).

The frontier pipelines use fastText lid.176 (FineWeb keeps docs at score
>= 0.65), but that needs a 130MB model download; py3langid ships its model
in the wheel and covers 97 languages, which is plenty for tagging a corpus.
"""
from typing import Any, Dict, Optional

from py3langid.langid import LanguageIdentifier, MODEL_FILE

# Plenty of signal for language ID; keeps classification fast on huge pages
MAX_CHARS = 4000

_identifier: Optional[LanguageIdentifier] = None


def detect(text: str) -> Optional[Dict[str, Any]]:
    """Return {"lang": "en", "prob": 0.99} or None for empty/failed input."""
    global _identifier
    if not text or not text.strip():
        return None
    try:
        if _identifier is None:
            _identifier = LanguageIdentifier.from_pickled_model(MODEL_FILE, norm_probs=True)
        lang, prob = _identifier.classify(text[:MAX_CHARS])
        return {"lang": lang, "prob": round(float(prob), 4)}
    except Exception:
        return None
