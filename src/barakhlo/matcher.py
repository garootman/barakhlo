from __future__ import annotations

import re

from rapidfuzz import fuzz


_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")

# Keywords shorter than this skip fuzzy matching (too many false positives).
MIN_FUZZY_LEN = 5


def normalize(text: str) -> str:
    text = text.lower()
    text = _PUNCT_RE.sub(" ", text)
    text = _SPACE_RE.sub(" ", text).strip()
    return text


def match(text: str, keywords: list[str], threshold: int) -> list[str]:
    """Return keywords that match, with per-keyword scoring.

    Strategy:
      1. substring hit on normalized text  -> always a match
      2. for long enough keywords, fuzzy partial_ratio on full text
    """
    if not text or not keywords:
        return []
    norm = normalize(text)
    if not norm:
        return []

    hits: list[str] = []
    for kw in keywords:
        kw_n = normalize(kw)
        if not kw_n:
            continue
        if kw_n in norm:
            hits.append(kw)
            continue
        if len(kw_n) >= MIN_FUZZY_LEN:
            score = fuzz.partial_ratio(kw_n, norm)
            if score >= threshold:
                hits.append(kw)
    return hits
