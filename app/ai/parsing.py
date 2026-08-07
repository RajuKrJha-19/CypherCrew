"""Tolerant parsing of a model's JSON reply.

Models don't always return clean JSON: some wrap it in ``` fences, some add a
sentence of prose, and a "thinking" model can truncate the tail. Parse
defensively rather than trusting exact formatting - but never turn genuinely
empty output into a fake success (the caller still raises on None + no text).
"""
import json
import re

#: Pull the value of the FIRST "caption": "..." out of a JSON string, tolerating
#: escaped quotes inside it. Used to salvage a usable caption from a reply whose
#: JSON was cut off mid-object (hit the token limit) so it can't be parsed whole.
_CAPTION_RE = re.compile(r'"caption"\s*:\s*"((?:[^"\\]|\\.)*)"')


def salvage_caption(text):
    """Best-effort caption from a truncated/malformed JSON reply, or "".

    When extract_json() fails (usually a token-limit truncation mid-object), the
    only sane fallback is a CLEAN caption — never the raw JSON blob, which is
    what used to land in the caption box. Pull just the caption field out and
    un-escape it; return "" if it isn't there.
    """
    if not text:
        return ""
    m = _CAPTION_RE.search(text)
    if not m:
        return ""
    frag = m.group(1)
    try:
        return json.loads('"' + frag + '"')      # un-escape \n, \" etc.
    except ValueError:
        return frag


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
