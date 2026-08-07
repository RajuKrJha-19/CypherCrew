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


# -- salvage_caption + truncated-JSON never dumps the raw blob ---------------

def test_salvage_caption_from_truncated_json():
    raw = ('{"caption": "Ready to build a career", '
           '"per_platform": {"facebook": "Ready to bui')       # cut off
    assert parsing.salvage_caption(raw) == "Ready to build a career"


def test_salvage_caption_unescapes_and_is_empty_when_absent():
    raw = '{"caption": "line one\\nline \\"two\\"", "hashtags":['
    assert parsing.salvage_caption(raw) == 'line one\nline "two"'
    assert parsing.salvage_caption('{"per_platform": {}}') == ""
    assert parsing.salvage_caption("") == ""


def test_caption_truncated_json_salvages_clean_caption_not_raw_blob(monkeypatch):
    """The bug: a token-limit truncation dumped the raw JSON into the box.
    Now the caption field is salvaged and the raw blob never appears."""
    p = GeminiProvider(model="m", api_key="x")
    truncated = ('{"caption": "Admissions are open now", '
                 '"per_platform": {"facebook": "Admiss')
    monkeypatch.setattr(p, "_generate", lambda *a, **k: truncated)
    r = p.generate_caption(CaptionContext(brief="hi", platforms=["facebook"]))
    assert r.caption == "Admissions are open now"
    assert not r.caption.lstrip().startswith("{")     # never the raw JSON


def test_caption_broken_json_without_a_caption_raises(monkeypatch):
    p = GeminiProvider(model="m", api_key="x")
    monkeypatch.setattr(p, "_generate",
                        lambda *a, **k: '{"per_platform": {"facebook": "hi')
    with pytest.raises(AIPermanent):
        p.generate_caption(CaptionContext(brief="hi", platforms=["facebook"]))


def test_caption_parses_keywords(monkeypatch):
    p = GeminiProvider(model="m", api_key="x")
    monkeypatch.setattr(p, "_generate", lambda *a, **k: (
        '{"caption":"hi","hashtags":["BBA"],'
        '"keywords":["BBA admissions","study in Bihar"],'
        '"first_comment":"","variations":[]}'))
    r = p.generate_caption(CaptionContext(brief="hi", platforms=["facebook"]))
    assert r.keywords == ["BBA admissions", "study in Bihar"]


def test_caption_prompt_asks_for_keywords():
    from app.ai import prompts
    system, _ = prompts.caption_prompt(
        CaptionContext(brief="x", platforms=["facebook"]))
    assert '"keywords"' in system and "SEO" in system
