"""Tolerant model-JSON parsing + the caption fallback, so a model that wraps,
prefaces, or lightly mangles its JSON still yields a caption - while a truly
empty reply stays a VISIBLE error (never a fake success).
"""
import pytest

from app.ai import parsing
from app.ai.base import CaptionContext
from app.ai.errors import AIPermanent
from app.ai.providers.gemini import GeminiProvider


# -- extract_json (pure) ----------------------------------------------------

def test_extract_json_plain():
    assert parsing.extract_json('{"caption": "hi"}') == {"caption": "hi"}


def test_extract_json_tolerates_fences():
    assert parsing.extract_json('```json\n{"caption": "hi"}\n```') == {"caption": "hi"}


def test_extract_json_tolerates_surrounding_prose():
    raw = 'Sure! Here is your caption:\n{"caption": "hi"}\nHope that helps.'
    assert parsing.extract_json(raw) == {"caption": "hi"}


def test_extract_json_returns_none_on_garbage_or_empty():
    assert parsing.extract_json("not json at all") is None
    assert parsing.extract_json("") is None
    assert parsing.extract_json(None) is None


# -- caption fallback -------------------------------------------------------

def test_caption_uses_raw_text_when_not_json(monkeypatch):
    p = GeminiProvider(model="m", api_key="x")
    monkeypatch.setattr(p, "_generate",
                        lambda *a, **k: "Just a plain caption, no JSON here")
    r = p.generate_caption(CaptionContext(brief="hi", platforms=["twitter"]))
    assert r.caption == "Just a plain caption, no JSON here"


def test_caption_empty_reply_raises_visible_error(monkeypatch):
    p = GeminiProvider(model="m", api_key="x")
    monkeypatch.setattr(p, "_generate", lambda *a, **k: "   ")
    with pytest.raises(AIPermanent):
        p.generate_caption(CaptionContext(brief="hi", platforms=["twitter"]))
