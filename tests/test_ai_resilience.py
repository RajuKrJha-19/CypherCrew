"""AI errors stay VISIBLE: the caption/alt-text error mapper surfaces the
specific (key-safe) provider reason to the UI instead of a generic message, so
a failure is diagnosable from the browser without server access. It must never
swallow an error into a silent success.
"""
from app.ai.errors import AIAuth, AIDisabled, AIPermanent, AITransient
from app.routes.social import _ai_error_response


def test_permanent_error_surfaces_specific_reason(app):
    with app.test_request_context():
        resp, code = _ai_error_response(AIPermanent("Gemini request failed (404)"))
    assert code == 502
    assert b"404" in resp.get_data()                 # real reason reaches the UI


def test_transient_error_surfaces_reason_and_503(app):
    with app.test_request_context():
        resp, code = _ai_error_response(AITransient("rate limited"))
    assert code == 503
    assert b"rate limited" in resp.get_data()


def test_auth_error_is_502_and_flagged(app):
    with app.test_request_context():
        resp, code = _ai_error_response(AIAuth("No Gemini API key configured."))
    assert code == 502
    assert b"key" in resp.get_data().lower()


def test_disabled_is_503(app):
    with app.test_request_context():
        _resp, code = _ai_error_response(AIDisabled("off"))
    assert code == 503
