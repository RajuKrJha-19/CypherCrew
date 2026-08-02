"""Tolerant parsing of a model's JSON reply.

Models don't always return clean JSON: some wrap it in ``` fences, some add a
sentence of prose, and a "thinking" model can truncate the tail. Parse
defensively rather than trusting exact formatting - but never turn genuinely
empty output into a fake success (the caller still raises on None + no text).
"""
import json


def strip_fences(text):
    """Drop a leading/trailing ``` code fence, if present."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
        if t.endswith("```"):
            t = t.rsplit("```", 1)[0]
    return t.strip()


def extract_json(text):
    """The dict from a model reply, or None. Tolerant of ``` fences and any
    surrounding prose/thinking (falls back to the outermost {...} span)."""
    t = strip_fences(text)
    if not t:
        return None
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        pass
    start, end = t.find("{"), t.rfind("}")
    if 0 <= start < end:
        try:
            obj = json.loads(t[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except (ValueError, TypeError):
            return None
    return None
